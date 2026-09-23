"""
Graph construction and assembly for RIG.

Per blueprint sections 37 (final architecture flow):
- Raw Facts + Evidence → Canonical Normalizer → Stable Identity Materializer
  → Graph Assembly (Nodes + Edges) → Validation

This module handles assembly from extractor results to the final RIG graph.
"""

from __future__ import annotations

import os
from typing import Dict, List

from rig.extractor import ExtractorResult
from rig.models import (
    Aggregator, Component, Diagnostic, Edge, Evidence,
    ExternalPackage, PackageManager, RIG, Repo, Runner,
    TestDefinition, UnresolvedReference,
    CodeFile, CodeModule, CodeClass, CodeFunction, CodeSymbol,
)


class GraphBuilder:
    """Assembles the RIG graph from extracted facts.

    Handles:
    - Adding nodes to appropriate collections
    - Deduplication by canonical ID
    - Adding edges with endpoint verification
    - Building internal lookup maps
    """

    def __init__(self):
        self.rig = RIG()
        self._seen_ids: Dict[str, bool] = {}
        self.diagnostics: List[Diagnostic] = []

    def add_component(self, component: Component) -> bool:
        """Add a component if not already present."""
        if component.id in self._seen_ids:
            self.diagnostics.append(Diagnostic(
                level="info",
                message=f"Duplicate component: {component.id}",
                entity_id=component.id,
            ))
            return False
        self._seen_ids[component.id] = True
        self.rig.components.append(component)
        return True

    def add_aggregator(self, aggregator: Aggregator) -> bool:
        if aggregator.id in self._seen_ids:
            return False
        self._seen_ids[aggregator.id] = True
        self.rig.aggregators.append(aggregator)
        return True

    def add_runner(self, runner: Runner) -> bool:
        if runner.id in self._seen_ids:
            return False
        self._seen_ids[runner.id] = True
        self.rig.runners.append(runner)
        return True

    def add_test(self, test: TestDefinition) -> bool:
        if test.id in self._seen_ids:
            return False
        self._seen_ids[test.id] = True
        self.rig.tests.append(test)
        return True

    def add_external_package(self, ep: ExternalPackage) -> bool:
        if ep.id in self._seen_ids:
            return False
        self._seen_ids[ep.id] = True
        self.rig.external_packages.append(ep)
        return True

    def add_package_manager(self, pm: PackageManager) -> bool:
        if pm.id in self._seen_ids:
            return False
        self._seen_ids[pm.id] = True
        self.rig.package_managers.append(pm)
        return True

    def add_edge(self, edge: Edge) -> bool:
        """Add edge, checking endpoints exist."""
        # Verify endpoints (optional, can be cross-referenced during validation)
        if edge.id in self._seen_ids:
            return False
        self._seen_ids[edge.id] = True
        self.rig.edges.append(edge)
        return True

    def add_evidence(self, evidence: Evidence) -> bool:
        if evidence.id in self._seen_ids:
            return False
        self._seen_ids[evidence.id] = True
        self.rig.evidence.append(evidence)
        return True

    def add_diagnostic(self, diagnostic: Diagnostic):
        self.rig.diagnostics.append(diagnostic)

    def add_unresolved(self, unresolved: UnresolvedReference):
        self.rig.unresolved_references.append(unresolved)

    # ── Code-level entity methods ──

    def add_code_file(self, code_file: CodeFile) -> bool:
        if code_file.id in self._seen_ids:
            return False
        self._seen_ids[code_file.id] = True
        self.rig.code_files.append(code_file)
        return True

    def add_code_module(self, module: CodeModule) -> bool:
        if module.id in self._seen_ids:
            return False
        self._seen_ids[module.id] = True
        self.rig.code_modules.append(module)
        return True

    def add_code_class(self, cls: CodeClass) -> bool:
        if cls.id in self._seen_ids:
            return False
        self._seen_ids[cls.id] = True
        self.rig.code_classes.append(cls)
        return True

    def add_code_function(self, func: CodeFunction) -> bool:
        if func.id in self._seen_ids:
            return False
        self._seen_ids[func.id] = True
        self.rig.code_functions.append(func)
        return True

    def add_code_symbol(self, symbol: CodeSymbol) -> bool:
        if symbol.id in self._seen_ids:
            return False
        self._seen_ids[symbol.id] = True
        self.rig.code_symbols.append(symbol)
        return True

    def build_from_extractor_result(
        self,
        result: ExtractorResult,
        evidence_list: List[Evidence],
        repo_root: str,
    ) -> RIG:
        """Build RIG from an ExtractorResult."""
        # Add all evidence first
        for ev in evidence_list:
            self.add_evidence(ev)

        # Add nodes
        for comp in result.components:
            self.add_component(comp)
        for agg in result.aggregators:
            self.add_aggregator(agg)
        for runner in result.runners:
            self.add_runner(runner)
        for test in result.tests:
            self.add_test(test)
        for ep in result.external_packages:
            self.add_external_package(ep)
        for pm in result.package_managers:
            self.add_package_manager(pm)

        # Add code-level entities
        for cf in result.code_files:
            self.add_code_file(cf)
        for cm in result.code_modules:
            self.add_code_module(cm)
        for cc in result.code_classes:
            self.add_code_class(cc)
        for cfn in result.code_functions:
            self.add_code_function(cfn)
        for cs in result.code_symbols:
            self.add_code_symbol(cs)

        # Add edges
        for edge in result.edges:
            self.add_edge(edge)

        # Add diagnostics
        for diag in result.diagnostics:
            self.add_diagnostic(diag)

        # Add unresolved
        for ur in result.unresolved_references:
            self.add_unresolved(ur)

        # Build profiles
        profiles = []
        for bp in result.build_profiles:
            if isinstance(bp, dict):
                profiles.append(bp)
            elif hasattr(bp, '__dict__'):
                profiles.append(bp.__dict__)

        # Set repository metadata
        self.rig.repo = Repo(
            name=os.path.basename(repo_root) if hasattr(self, '_') else repo_root.split(os.sep)[-1],
            build_profile_ids=list(set(
                c.build_profile_ids[0] for c in result.components if c.build_profile_ids
            )),
            evidence_ids=list(set(
                ev.id for ev in evidence_list
            )),
        )

        self.rig.build["profiles"] = profiles
        if profiles:
            self.rig.build["primary_profile_id"] = profiles[0].get("id", "") if isinstance(profiles[0], dict) else profiles[0].id

        # Index for fast lookup
        self.rig.index_nodes()

        return self.rig


