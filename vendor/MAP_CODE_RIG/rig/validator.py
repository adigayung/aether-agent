"""
Validation for RIG graphs.

Per blueprint sections 27, 30:
- Structural validation: unique IDs, endpoint existence, kind/type validity,
  evidence presence, acyclic component build-dependency graph, safe paths.
- Semantic diagnostics: missing source file, broken dependency, orphan node,
  circular dependency, etc.
- ERROR = cannot be trusted structurally; WARNING = partial facts lost;
  INFO = normal diagnostic.

Validation happens before JSON commit.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from rig.models import (
    Component, Edge, Diagnostic, RIG, EdgeType,
    DiagnosticLevel,
)


class ValidationResult:
    """Result of RIG validation.

    Errors prevent output commit.
    Warnings and info allow commit with diagnostics.
    """

    def __init__(self):
        self.errors: List[Diagnostic] = []
        self.warnings: List[Diagnostic] = []
        self.infos: List[Diagnostic] = []

    @property
    def is_valid(self) -> bool:
        """Graph passes structural validation (no errors)."""
        return len(self.errors) == 0

    @property
    def all_diagnostics(self) -> List[Diagnostic]:
        return self.errors + self.warnings + self.infos


class RIGValidator:
    """Validator for RIG graphs.

    Per blueprint section 27.1 structural validation checks.
    """

    def validate(self, rig: RIG) -> ValidationResult:
        """Run all validation checks on the RIG."""
        result = ValidationResult()
        rig.index_nodes()

        # 1. Unique node IDs
        self._check_unique_ids(rig, result)

        # 2. Unique edge IDs
        self._check_unique_edge_ids(rig, result)

        # 3. Edge endpoints exist
        self._check_edge_endpoints(rig, result)

        # 4. Node kinds valid
        self._check_node_kinds(rig, result)

        # 5. Edge types valid
        self._check_edge_types(rig, result)

        # 6. Every node has evidence
        self._check_node_evidence(rig, result)

        # 7. Every edge has evidence
        self._check_edge_evidence(rig, result)

        # 8. Source paths safe and normalized
        self._check_source_paths(rig, result)

        # 9. Duplicate semantic edges absent
        self._check_duplicate_edges(rig, result)

        # 10. Component build-dependency subgraph acyclic
        self._check_acyclic(rig, result)

        # 11. Evidence references valid
        self._check_evidence_refs(rig, result)

        return result

    def _check_unique_ids(self, rig: RIG, result: ValidationResult):
        """Check that all node IDs are unique."""
        seen: Dict[str, str] = {}
        for node_list, kind_name in [
            (rig.components, "component"),
            (rig.aggregators, "aggregator"),
            (rig.runners, "runner"),
            (rig.tests, "test"),
            (rig.external_packages, "external_package"),
            (rig.package_managers, "package_manager"),
            (rig.code_files, "code_file"),
            (rig.code_modules, "code_module"),
            (rig.code_classes, "code_class"),
            (rig.code_functions, "code_function"),
            (rig.code_symbols, "code_symbol"),
        ]:
            for node in node_list:
                if node.id in seen:
                    result.errors.append(Diagnostic(
                        level=DiagnosticLevel.ERROR,
                        message=f"Duplicate node ID: {node.id} "
                                f"(was {seen[node.id]}, now {kind_name})",
                        entity_id=node.id,
                        source="validator",
                    ))
                seen[node.id] = kind_name

    def _check_unique_edge_ids(self, rig: RIG, result: ValidationResult):
        """Check that all edge IDs are unique."""
        seen: Set[str] = set()
        for edge in rig.edges:
            if edge.id in seen:
                result.errors.append(Diagnostic(
                    level=DiagnosticLevel.ERROR,
                    message=f"Duplicate edge ID: {edge.id}",
                    entity_id=edge.id,
                    source="validator",
                ))
            seen.add(edge.id)

    def _check_edge_endpoints(self, rig: RIG, result: ValidationResult):
        """Check that edge source/target IDs exist as nodes."""
        for edge in rig.edges:
            if edge.id not in rig._node_map and edge.source not in rig._node_map:
                result.errors.append(Diagnostic(
                    level=DiagnosticLevel.ERROR,
                    message=f"Edge {edge.id}: source {edge.source} not found",
                    entity_id=edge.id,
                    source="validator",
                ))
            if edge.target not in rig._node_map:
                result.errors.append(Diagnostic(
                    level=DiagnosticLevel.ERROR,
                    message=f"Edge {edge.id}: target {edge.target} not found",
                    entity_id=edge.id,
                    source="validator",
                ))

    def _check_node_kinds(self, rig: RIG, result: ValidationResult):
        """Check that node kinds are valid."""
        for node_list, expected_kind in [
            (rig.components, "component"),
            (rig.aggregators, "aggregator"),
            (rig.runners, "runner"),
            (rig.tests, "test"),
            (rig.external_packages, "external_package"),
            (rig.package_managers, "package_manager"),
        ]:
            for node in node_list:
                if node.kind != expected_kind:
                    result.warnings.append(Diagnostic(
                        level=DiagnosticLevel.WARNING,
                        message=f"Node {node.id}: expected kind '{expected_kind}', "
                                f"got '{node.kind}'",
                        entity_id=node.id,
                        source="validator",
                    ))

    def _check_edge_types(self, rig: RIG, result: ValidationResult):
        """Check that edge types are valid."""
        for edge in rig.edges:
            if edge.type not in EdgeType.ALL:
                result.errors.append(Diagnostic(
                    level=DiagnosticLevel.ERROR,
                    message=f"Edge {edge.id}: invalid type '{edge.type}'",
                    entity_id=edge.id,
                    source="validator",
                ))
            # Check MVP status
            if edge.type not in EdgeType.MVP:
                result.warnings.append(Diagnostic(
                    level=DiagnosticLevel.WARNING,
                    message=f"Edge {edge.id}: type '{edge.type}' is reserved/supported "
                            f"but not MVP active",
                    entity_id=edge.id,
                    source="validator",
                ))

    def _check_node_evidence(self, rig: RIG, result: ValidationResult):
        """Check that every node has ≥1 evidence reference."""
        for node_list, kind_name in [
            (rig.components, "component"),
            (rig.aggregators, "aggregator"),
            (rig.runners, "runner"),
            (rig.tests, "test"),
            (rig.external_packages, "external_package"),
            (rig.package_managers, "package_manager"),
            (rig.code_files, "code_file"),
            (rig.code_modules, "code_module"),
            (rig.code_classes, "code_class"),
            (rig.code_functions, "code_function"),
            (rig.code_symbols, "code_symbol"),
        ]:
            for node in node_list:
                if not node.evidence_ids:
                    result.errors.append(Diagnostic(
                        level=DiagnosticLevel.ERROR,
                        message=f"{kind_name} {node.id} has no evidence",
                        entity_id=node.id,
                        source="validator",
                    ))
                else:
                    # Check evidence references exist
                    for eid in node.evidence_ids:
                        if eid not in rig._evidence_map:
                            result.warnings.append(Diagnostic(
                                level=DiagnosticLevel.WARNING,
                                message=f"{kind_name} {node.id}: evidence {eid} not found",
                                entity_id=node.id,
                                source="validator",
                            ))

    def _check_edge_evidence(self, rig: RIG, result: ValidationResult):
        """Check that every edge has ≥1 evidence reference."""
        for edge in rig.edges:
            if not edge.evidence_ids:
                result.errors.append(Diagnostic(
                    level=DiagnosticLevel.ERROR,
                    message=f"Edge {edge.id} has no evidence",
                    entity_id=edge.id,
                    source="validator",
                ))

    def _check_source_paths(self, rig: RIG, result: ValidationResult):
        """Check that source paths are safe and normalized."""
        for comp in rig.components:
            for path in comp.source_files:
                if ".." in path.split("/"):
                    result.warnings.append(Diagnostic(
                        level=DiagnosticLevel.WARNING,
                        message=f"Component {comp.id}: source path contains '..': {path}",
                        entity_id=comp.id,
                        source="validator",
                    ))
                if path.startswith("/"):
                    result.warnings.append(Diagnostic(
                        level=DiagnosticLevel.WARNING,
                        message=f"Component {comp.id}: source path is absolute: {path}",
                        entity_id=comp.id,
                        source="validator",
                    ))

    def _check_duplicate_edges(self, rig: RIG, result: ValidationResult):
        """Check for duplicate semantic edges (same type/source/target/role)."""
        seen: Set[Tuple[str, str, str, Optional[str]]] = set()
        for edge in rig.edges:
            key = (edge.type, edge.source, edge.target, edge.role)
            if key in seen:
                result.warnings.append(Diagnostic(
                    level=DiagnosticLevel.WARNING,
                    message=f"Duplicate edge semantics: {edge.id} "
                            f"(type={edge.type}, source={edge.source}, "
                            f"target={edge.target}, role={edge.role})",
                    entity_id=edge.id,
                    source="validator",
                ))
            seen.add(key)

    def _check_acyclic(self, rig: RIG, result: ValidationResult):
        """Check that component build-dependency subgraph is acyclic.

        Per blueprint section 27.1, the component build-dependency
        subgraph MUST be acyclic.
        """
        # Build adjacency list for build dependencies
        comp_ids = {c.id for c in rig.components}
        adj: Dict[str, List[str]] = {cid: [] for cid in comp_ids}

        for edge in rig.edges:
            if (edge.type == EdgeType.DEPENDS_ON
                    and edge.role == "build"
                    and edge.source in comp_ids
                    and edge.target in comp_ids):
                adj.setdefault(edge.source, []).append(edge.target)

        # DFS for cycles
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {cid: WHITE for cid in comp_ids}

        def dfs(node_id: str, path: List[str]) -> Optional[List[str]]:
            color[node_id] = GRAY
            path.append(node_id)
            for neighbor in adj.get(node_id, []):
                if color.get(neighbor, WHITE) == GRAY:
                    # Found cycle
                    cycle_start = path.index(neighbor) if neighbor in path else 0
                    cycle = path[cycle_start:] + [neighbor]
                    return cycle
                if color.get(neighbor, WHITE) == WHITE:
                    cycle = dfs(neighbor, path)
                    if cycle:
                        return cycle
            path.pop()
            color[node_id] = BLACK
            return None

        for cid in comp_ids:
            if color[cid] == WHITE:
                cycle = dfs(cid, [])
                if cycle:
                    result.errors.append(Diagnostic(
                        level=DiagnosticLevel.ERROR,
                        message=f"Circular build dependency: {' -> '.join(cycle)}",
                        entity_id=cid,
                        source="validator",
                    ))

    def _check_evidence_refs(self, rig: RIG, result: ValidationResult):
        """Check evidence references in diagnostics and unresolved."""
        # Check that evidence file_paths are repo-relative
        for ev in rig.evidence:
            if ev.file_path and ev.file_path.startswith("/"):
                result.warnings.append(Diagnostic(
                    level=DiagnosticLevel.WARNING,
                    message=f"Evidence {ev.id}: path '{ev.file_path}' is absolute",
                    entity_id=ev.id,
                    source="validator",
                ))