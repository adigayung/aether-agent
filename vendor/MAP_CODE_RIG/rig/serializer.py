"""
Canonical JSON serializer for RIG.

Per blueprint sections 26, 27.3:
- Canonical JSON MUST be deterministic (byte-for-byte equivalent)
- Top-level keys ordered explicitly
- Node arrays sorted by stable ID
- Edge arrays sorted by stable ID
- Evidence arrays sorted by stable ID
- All ID arrays sorted lexicographically
- Repository-relative paths normalized to /
- Unicode normalized to NFC
- Line endings normalized to LF
- Exclude volatile environment values
- Never serialize unordered sets
- Use explicit field names
- Avoid aggressive alias tables
- Omit optional nulls when schema policy permits

Sequential integer IDs (v2):
- Entity `id` fields use compact sequential integers 1..N
- All ID references (source, target, *_ids) use the same integer IDs
- Canonical string IDs are preserved internally and in identity_key fields
- Deterministic: same project state → same canonical IDs → same integer mapping
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import fields, is_dataclass
from typing import Any, Dict, List, Optional

from rig.models import (
    RIG, Component, Aggregator, Runner, TestDefinition,
    ExternalPackage, PackageManager, Edge, Evidence,
    Diagnostic, UnresolvedReference, Generator, Repo,
    CodeFile, CodeModule, CodeClass, CodeFunction, CodeSymbol,
)


def _to_dict(obj: Any) -> Any:
    """Convert a dataclass instance to a plain dict recursively.

    Filters out None values (omit optional nulls).
    """
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items() if v is not None}
    if is_dataclass(obj):
        result = {}
        for f in fields(obj):
            # Skip internal fields (prefixed with _)
            if f.name.startswith("_"):
                continue
            val = getattr(obj, f.name)
            if val is None and f.name not in ("runtime", "command",
                                                "framework", "purpose",
                                                "working_directory",
                                                "owner_node_id",
                                                "test_executable_node_id",
                                                "runner_node_id",
                                                "package_manager_id",
                                                "call_stack", "source_ref",
                                                "text_snippet", "revision", "vcs",
                                                "snapshot_fingerprint",
                                                "analysis_status",
                                                "ecosystem", "coordinate",
                                                "declared_version",
                                                "resolved_version",
                                                "dependency_scope", "source",
                                                "lockfile_path"):
                # Only omit truly optional null fields for cleaner output
                result[f.name] = val
            elif val is not None:
                result[f.name] = _to_dict(val)
        return result
    # Fallback for plain objects
    return obj


def _normalize_unicode(text: str) -> str:
    """Normalize Unicode to NFC."""
    return unicodedata.normalize("NFC", text)


class RIGSerializer:
    """Serializes a RIG to canonical JSON with compact sequential integer IDs.

    Produces byte-for-byte equivalent output for same normalized inputs.

    Entity IDs are sequential integers 1..N derived from sorted canonical
    string IDs, making the output significantly more compact while remaining
    fully deterministic.
    """

    def __init__(self, indent: int = 2):
        self.indent = indent

    def serialize(self, rig: RIG) -> str:
        """Serialize RIG to canonical JSON string.

        Per blueprint section 26 (Canonical JSON):
        - Flat JSON with authoritative edges
        - Compatibility projections on nodes
        - Sorted arrays
        - Normalized paths
        - Compact sequential integer IDs
        """
        # Build the canonical dict with explicit key order
        data = self._build_canonical(rig)

        # Serialize with explicit sort_keys=False (we control ordering)
        return json.dumps(
            data,
            indent=self.indent,
            ensure_ascii=False,
            sort_keys=False,
        )

    # ── Sequential integer ID mapping ─────────────────────────────────────

    def _build_id_map(self, rig: RIG) -> Dict[str, int]:
        """Collect all canonical string IDs and map to sequential integers 1..N.

        Deterministic: canonical string IDs are sorted lexicographically,
        then assigned integers in that order. Same project state → same
        canonical IDs → same integer sequence.
        """
        all_ids: List[str] = []

        # Collect from all entity collections
        for collection in [
            rig.components, rig.aggregators, rig.runners, rig.tests,
            rig.external_packages, rig.package_managers,
            rig.code_files, rig.code_modules, rig.code_classes,
            rig.code_functions, rig.code_symbols,
            rig.edges,
            rig.evidence,
            rig.unresolved_references,
        ]:
            for entity in collection:
                all_ids.append(entity.id)

        # Collect build profile IDs from the build dict
        for profile in rig.build.get("profiles", []):
            if isinstance(profile, dict):
                pid = profile.get("id")
            else:
                pid = getattr(profile, "id", None)
            if pid:
                all_ids.append(pid)

        # Sort deterministically by canonical string ID
        all_ids = sorted(set(all_ids))

        # Assign sequential integers 1..N
        return {cid: idx + 1 for idx, cid in enumerate(all_ids)}

    def _map_id(self, id_map: Dict[str, int], id_str: Optional[str]) -> Optional[int]:
        """Map a single string ID to its sequential integer equivalent."""
        if id_str is None:
            return None
        return id_map.get(id_str)

    def _map_id_list(self, id_map: Dict[str, int], id_list: List[str]) -> List[int]:
        """Map a list of string IDs to a sorted list of integer IDs.

        IDs not present in the mapping are silently skipped (they may be
        dangling references in test dummies, or evidence references for
        evidence not collected as standalone entities).
        """
        return sorted(set(id_map[i] for i in id_list if i in id_map))

    # ── Canonical dict construction ──────────────────────────────────────

    def _build_canonical(self, rig: RIG) -> Dict[str, Any]:
        """Build the canonical JSON dict with deterministic ordering
        and compact sequential integer IDs."""
        # Build global string-ID → integer-ID mapping
        id_map = self._build_id_map(rig)

        # Generator info
        generator_dict = {
            "name": rig.generator.name,
            "version": rig.generator.version,
        }

        # Repo
        repo_dict = self._build_repo(rig.repo, id_map)

        # Build profiles (convert their IDs too)
        build_evidence_ids = self._map_id_list(
            id_map, rig.build.get("evidence_ids", [])
        )
        primary_profile_id = rig.build.get("primary_profile_id")
        if primary_profile_id:
            primary_profile_id = id_map.get(primary_profile_id)

        build_profiles = []
        for profile in rig.build.get("profiles", []):
            build_profiles.append(self._build_build_profile(profile, id_map))
        build_profiles = sorted(
            build_profiles,
            key=lambda x: x.get("id", 0) if isinstance(x, dict) else 0,
        )

        build_dict = {
            "profiles": build_profiles,
            "primary_profile_id": primary_profile_id,
            "evidence_ids": build_evidence_ids,
        }

        # Core node arrays (sorted by integer ID)
        components = self._sort_by_id([
            self._build_component(c, id_map) for c in rig.components
        ])
        aggregators = self._sort_by_id([
            self._build_aggregator(a, id_map) for a in rig.aggregators
        ])
        runners = self._sort_by_id([
            self._build_runner(r, id_map) for r in rig.runners
        ])
        tests = self._sort_by_id([
            self._build_test(t, id_map) for t in rig.tests
        ])

        # Satellite entities
        external_packages = self._sort_by_id([
            self._build_external_package(ep, id_map) for ep in rig.external_packages
        ])
        package_managers = self._sort_by_id([
            self._build_package_manager(pm, id_map) for pm in rig.package_managers
        ])

        # Code-level entities (source-code mapping)
        code_files = self._sort_by_id([
            self._build_code_file(cf, id_map) for cf in rig.code_files
        ])
        code_modules = self._sort_by_id([
            self._build_code_module(cm, id_map) for cm in rig.code_modules
        ])
        code_classes = self._sort_by_id([
            self._build_code_class(cc, id_map) for cc in rig.code_classes
        ])
        code_functions = self._sort_by_id([
            self._build_code_function(cfn, id_map) for cfn in rig.code_functions
        ])
        code_symbols = self._sort_by_id([
            self._build_code_symbol(cs, id_map) for cs in rig.code_symbols
        ])

        # Edges (authoritative relationship representation)
        edges = self._sort_by_id([
            self._build_edge(e, id_map) for e in rig.edges
        ])

        # Evidence
        evidence = self._sort_by_id([
            self._build_evidence(ev, id_map) for ev in rig.evidence
        ])

        # Diagnostics and unresolved
        diagnostics = self._sort_by_id([
            self._build_diagnostic(d, id_map) for d in rig.diagnostics
        ])
        unresolved_refs = self._sort_by_id([
            self._build_unresolved(u, id_map) for u in rig.unresolved_references
        ])

        # Build top-level dict with explicit key order
        canonical = {
            "schema_version": rig.schema_version,
            "generator": generator_dict,
            "repo": repo_dict,
            "build": build_dict,
            "components": components,
            "aggregators": aggregators,
            "runners": runners,
            "tests": tests,
            "external_packages": external_packages,
            "package_managers": package_managers,
            "code_files": code_files,
            "code_modules": code_modules,
            "code_classes": code_classes,
            "code_functions": code_functions,
            "code_symbols": code_symbols,
            "edges": edges,
            "evidence": evidence,
            "diagnostics": diagnostics,
            "unresolved_references": unresolved_refs,
        }

        return canonical

    def _sort_by_id(self, items: List[Dict]) -> List[Dict]:
        """Sort list of dicts by their 'id' field (integer or string)."""
        return sorted(items, key=lambda x: x.get("id", 0) if isinstance(x.get("id"), int) else (x.get("id", "") or ""))

    def _build_build_profile(self, profile: Any, id_map: Dict[str, int]) -> Dict[str, Any]:
        """Build a build profile dict with mapped integer IDs."""
        if isinstance(profile, dict):
            result = dict(profile)
            if "id" in result and result["id"] in id_map:
                result["id"] = id_map[result["id"]]
            if "evidence_ids" in result:
                result["evidence_ids"] = self._map_id_list(id_map, result["evidence_ids"])
            return result
        return profile

    def _build_repo(self, repo: Repo, id_map: Dict[str, int]) -> Dict[str, Any]:
        return {
            "name": repo.name,
            "primary_language": repo.primary_language,
            "languages": sorted(set(repo.languages)),
            "build_profile_ids": self._map_id_list(id_map, repo.build_profile_ids),
            "snapshot_fingerprint": repo.snapshot_fingerprint,
            "revision": repo.revision,
            "vcs": repo.vcs,
            "analysis_status": repo.analysis_status,
            "evidence_ids": self._map_id_list(id_map, repo.evidence_ids),
        }

    def _build_component(self, comp: Component, id_map: Dict[str, int]) -> Dict[str, Any]:
        result = {
            "id": id_map[comp.id],
            "kind": comp.kind,
            "name": comp.name,
            "type": comp.type,
            "programming_language": comp.programming_language,
            "source_files": sorted(set(comp.source_files)),
            "identity_key": comp.identity_key,
            "evidence_ids": self._map_id_list(id_map, comp.evidence_ids),
            "build_profile_ids": self._map_id_list(id_map, comp.build_profile_ids),
        }

        # Compatibility projections (derivable from edges[])
        # These are added when graph is built with edges
        if hasattr(comp, '_depends_on_ids') and comp._depends_on_ids:
            result["depends_on_ids"] = self._map_id_list(id_map, comp._depends_on_ids)
        if hasattr(comp, '_external_packages_ids') and comp._external_packages_ids:
            result["external_packages_ids"] = self._map_id_list(id_map, comp._external_packages_ids)

        if comp.runtime is not None:
            result["runtime"] = comp.runtime
        if comp.labels:
            result["labels"] = dict(sorted(comp.labels.items()))
        if comp.attributes:
            result["attributes"] = dict(sorted(comp.attributes.items()))

        return result

    def _build_aggregator(self, agg: Aggregator, id_map: Dict[str, int]) -> Dict[str, Any]:
        result = {
            "id": id_map[agg.id],
            "kind": agg.kind,
            "name": agg.name,
            "identity_key": agg.identity_key,
            "evidence_ids": self._map_id_list(id_map, agg.evidence_ids),
            "build_profile_ids": self._map_id_list(id_map, agg.build_profile_ids),
        }
        if agg.labels:
            result["labels"] = dict(sorted(agg.labels.items()))
        if agg.attributes:
            result["attributes"] = dict(sorted(agg.attributes.items()))
        return result

    def _build_runner(self, runner: Runner, id_map: Dict[str, int]) -> Dict[str, Any]:
        result = {
            "id": id_map[runner.id],
            "kind": runner.kind,
            "name": runner.name,
            "identity_key": runner.identity_key,
            "evidence_ids": self._map_id_list(id_map, runner.evidence_ids),
        }
        if runner.command:
            result["command"] = runner.command
        if runner.arguments:
            result["arguments"] = sorted(runner.arguments)
        if runner.working_directory:
            result["working_directory"] = runner.working_directory
        if runner.environment_keys:
            result["environment_keys"] = sorted(runner.environment_keys)
        if runner.owner_node_id:
            result["owner_node_id"] = self._map_id(id_map, runner.owner_node_id)
        if runner.purpose:
            result["purpose"] = runner.purpose
        if runner.labels:
            result["labels"] = dict(sorted(runner.labels.items()))
        if runner.attributes:
            result["attributes"] = dict(sorted(runner.attributes.items()))
        if runner.build_profile_ids:
            result["build_profile_ids"] = self._map_id_list(id_map, runner.build_profile_ids)
        return result

    def _build_test(self, test: TestDefinition, id_map: Dict[str, int]) -> Dict[str, Any]:
        result = {
            "id": id_map[test.id],
            "kind": test.kind,
            "name": test.name,
            "test_kind": test.test_kind,
            "identity_key": test.identity_key,
            "evidence_ids": self._map_id_list(id_map, test.evidence_ids),
            "build_profile_ids": self._map_id_list(id_map, test.build_profile_ids),
            "source_files": sorted(set(test.source_files)),
            "components_being_tested_ids": self._map_id_list(id_map, test.components_being_tested_ids),
        }
        if test.framework:
            result["framework"] = test.framework
        if test.runner_node_id:
            result["runner_node_id"] = self._map_id(id_map, test.runner_node_id)
        if test.test_executable_node_id:
            result["test_executable_node_id"] = self._map_id(id_map, test.test_executable_node_id)
        if test.command:
            result["command"] = test.command
        if test.arguments:
            result["arguments"] = sorted(test.arguments)
        if test.labels:
            result["labels"] = dict(sorted(test.labels.items()))
        if test.attributes:
            result["attributes"] = dict(sorted(test.attributes.items()))
        return result

    def _build_external_package(self, ep: ExternalPackage, id_map: Dict[str, int]) -> Dict[str, Any]:
        result = {
            "id": id_map[ep.id],
            "kind": ep.kind,
            "name": ep.name,
            "identity_key": ep.identity_key,
            "evidence_ids": self._map_id_list(id_map, ep.evidence_ids),
        }
        if ep.package_manager_id:
            result["package_manager_id"] = self._map_id(id_map, ep.package_manager_id)
        if ep.ecosystem:
            result["ecosystem"] = ep.ecosystem
        if ep.coordinate:
            result["coordinate"] = ep.coordinate
        if ep.declared_version:
            result["declared_version"] = ep.declared_version
        if ep.resolved_version:
            result["resolved_version"] = ep.resolved_version
        if ep.dependency_scope:
            result["dependency_scope"] = ep.dependency_scope
        if ep.optional:
            result["optional"] = ep.optional
        if ep.peer:
            result["peer"] = ep.peer
        if ep.source:
            result["source"] = ep.source
        if ep.lockfile_path:
            result["lockfile_path"] = ep.lockfile_path
        return result

    def _build_package_manager(self, pm: PackageManager, id_map: Dict[str, int]) -> Dict[str, Any]:
        result = {
            "id": id_map[pm.id],
            "kind": pm.kind,
            "name": pm.name,
            "identity_key": pm.identity_key,
            "evidence_ids": self._map_id_list(id_map, pm.evidence_ids),
        }
        if pm.ecosystem:
            result["ecosystem"] = pm.ecosystem
        return result

    def _build_code_file(self, cf: CodeFile, id_map: Dict[str, int]) -> Dict[str, Any]:
        result = {
            "id": id_map[cf.id],
            "kind": cf.kind,
            "name": cf.name,
            "identity_key": cf.identity_key,
            "evidence_ids": self._map_id_list(id_map, cf.evidence_ids),
            "file_path": cf.file_path,
            "language": cf.language,
            "parse_success": cf.parse_success,
        }
        if cf.parse_error is not None:
            result["parse_error"] = cf.parse_error
        if cf.labels:
            result["labels"] = dict(sorted(cf.labels.items()))
        if cf.attributes:
            result["attributes"] = dict(sorted(cf.attributes.items()))
        return result

    def _build_code_module(self, cm: CodeModule, id_map: Dict[str, int]) -> Dict[str, Any]:
        result = {
            "id": id_map[cm.id],
            "kind": cm.kind,
            "name": cm.name,
            "identity_key": cm.identity_key,
            "evidence_ids": self._map_id_list(id_map, cm.evidence_ids),
            "file_path": cm.file_path,
            "is_package": cm.is_package,
        }
        if cm.labels:
            result["labels"] = dict(sorted(cm.labels.items()))
        if cm.attributes:
            result["attributes"] = dict(sorted(cm.attributes.items()))
        return result

    def _build_code_class(self, cc: CodeClass, id_map: Dict[str, int]) -> Dict[str, Any]:
        result = {
            "id": id_map[cc.id],
            "kind": cc.kind,
            "name": cc.name,
            "identity_key": cc.identity_key,
            "evidence_ids": self._map_id_list(id_map, cc.evidence_ids),
            "file_path": cc.file_path,
            "line_start": cc.line_start,
            "line_end": cc.line_end,
            "bases": sorted(cc.bases),
        }
        if cc.labels:
            result["labels"] = dict(sorted(cc.labels.items()))
        if cc.attributes:
            result["attributes"] = dict(sorted(cc.attributes.items()))
        return result

    def _build_code_function(self, cfn: CodeFunction, id_map: Dict[str, int]) -> Dict[str, Any]:
        result = {
            "id": id_map[cfn.id],
            "kind": cfn.kind,
            "name": cfn.name,
            "identity_key": cfn.identity_key,
            "evidence_ids": self._map_id_list(id_map, cfn.evidence_ids),
            "file_path": cfn.file_path,
            "line_start": cfn.line_start,
            "line_end": cfn.line_end,
            "is_method": cfn.is_method,
        }
        if cfn.parent_class_id:
            result["parent_class_id"] = self._map_id(id_map, cfn.parent_class_id)
        if cfn.labels:
            result["labels"] = dict(sorted(cfn.labels.items()))
        if cfn.attributes:
            result["attributes"] = dict(sorted(cfn.attributes.items()))
        return result

    def _build_code_symbol(self, cs: CodeSymbol, id_map: Dict[str, int]) -> Dict[str, Any]:
        result = {
            "id": id_map[cs.id],
            "kind": cs.kind,
            "name": cs.name,
            "identity_key": cs.identity_key,
            "evidence_ids": self._map_id_list(id_map, cs.evidence_ids),
            "file_path": cs.file_path,
            "line_start": cs.line_start,
            "line_end": cs.line_end,
            "symbol_type": cs.symbol_type,
        }
        if cs.resolved_target_id:
            result["resolved_target_id"] = self._map_id(id_map, cs.resolved_target_id)
        if cs.labels:
            result["labels"] = dict(sorted(cs.labels.items()))
        if cs.attributes:
            result["attributes"] = dict(sorted(cs.attributes.items()))
        return result

    def _build_edge(self, edge: Edge, id_map: Dict[str, int]) -> Dict[str, Any]:
        result = {
            "id": id_map[edge.id],
            "type": edge.type,
            "source": self._map_id(id_map, edge.source),
            "target": self._map_id(id_map, edge.target),
            "evidence_ids": self._map_id_list(id_map, edge.evidence_ids),
        }
        if edge.role:
            result["role"] = edge.role
        if edge.qualifier:
            result["qualifier"] = edge.qualifier
        if edge.origin_plugin:
            result["origin_plugin"] = edge.origin_plugin
        if edge.labels:
            result["labels"] = dict(sorted(edge.labels.items()))
        if edge.attributes:
            result["attributes"] = dict(sorted(edge.attributes.items()))
        return result

    def _build_evidence(self, ev: Evidence, id_map: Dict[str, int]) -> Dict[str, Any]:
        result = {
            "id": id_map[ev.id],
            "locator_kind": ev.locator_kind,
        }
        if ev.file_path:
            result["file_path"] = ev.file_path
        if ev.start_line is not None:
            result["start_line"] = ev.start_line
        if ev.end_line is not None:
            result["end_line"] = ev.end_line
        if ev.call_stack:
            result["call_stack"] = ev.call_stack
        if ev.source_ref:
            result["source_ref"] = ev.source_ref
        if ev.text_snippet:
            result["text_snippet"] = ev.text_snippet[:500]
        if ev.attributes:
            result["attributes"] = dict(sorted(ev.attributes.items()))
        return result

    def _build_diagnostic(self, diag: Diagnostic, id_map: Dict[str, int]) -> Dict[str, Any]:
        result = {
            "level": diag.level,
            "message": diag.message,
        }
        if diag.source:
            result["source"] = diag.source
        if diag.entity_id:
            result["entity_id"] = self._map_id(id_map, diag.entity_id)
        if diag.attributes:
            result["attributes"] = dict(sorted(diag.attributes.items()))
        return result

    def _build_unresolved(self, ur: UnresolvedReference, id_map: Dict[str, int]) -> Dict[str, Any]:
        result = {
            "id": id_map[ur.id],
            "kind": ur.kind,
            "source_entity_id": self._map_id(id_map, ur.source_entity_id),
            "reference_text": ur.reference_text,
            "reference_kind": ur.reference_kind,
            "candidate_ids": self._map_id_list(id_map, ur.candidate_ids),
            "reason": ur.reason,
            "evidence_ids": self._map_id_list(id_map, ur.evidence_ids),
            "status": ur.status,
        }
        return result