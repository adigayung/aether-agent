"""
Extractor plugin framework for RIG.

Per blueprint sections 2.2, 17, 19:
- Each extractor/plugin discovers entities and relationships from a build system
- Extraction MUST follow "evidence first"
- No relationship is created merely because two entities "look related"
- Each plugin MUST document source priority, fallback order, unsupported semantics
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from rig.evidence import EvidenceCollector
from rig.models import (
    Aggregator, Component, Edge, ExternalPackage, PackageManager,
    Runner, TestDefinition, Diagnostic, UnresolvedReference
)


@dataclass
class ExtractorResult:
    """Output of a single extractor plugin."""
    components: List[Component] = field(default_factory=list)
    aggregators: List[Aggregator] = field(default_factory=list)
    runners: List[Runner] = field(default_factory=list)
    tests: List[TestDefinition] = field(default_factory=list)
    external_packages: List[ExternalPackage] = field(default_factory=list)
    package_managers: List[PackageManager] = field(default_factory=list)
    edges: List[Edge] = field(default_factory=list)
    diagnostics: List[Diagnostic] = field(default_factory=list)
    unresolved_references: List[UnresolvedReference] = field(default_factory=list)
    build_profiles: List[Any] = field(default_factory=list)  # BuildProfile-like dicts

    # Code-level entities (source-code mapping)
    code_files: List['CodeFile'] = field(default_factory=list)
    code_modules: List['CodeModule'] = field(default_factory=list)
    code_classes: List['CodeClass'] = field(default_factory=list)
    code_functions: List['CodeFunction'] = field(default_factory=list)
    code_symbols: List['CodeSymbol'] = field(default_factory=list)


class ExtractorPlugin(ABC):
    """Base class for build-system-specific extractors.

    Each plugin:
    - Detects whether its build system is present
    - Extracts entities, relationships, and evidence
    - Documents its source priority
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Plugin name (e.g., 'cmake', 'npm')."""
        ...

    @abstractmethod
    def detect(self, repo_root: str, discovery_result: Any) -> bool:
        """Detect whether this build system is present."""
        ...

    @abstractmethod
    def extract(
        self,
        repo_root: str,
        discovery_result: Any,
        evidence_collector: EvidenceCollector,
        config: Any,
    ) -> ExtractorResult:
        """Extract RIG entities and relationships.

        Per blueprint section 19 (Relationship Extraction):
        1. identify source artifact
        2. identify target artifact
        3. identify semantic relation type
        4. construct deterministic identity key
        5. attach evidence
        6. normalize endpoints
        7. deduplicate
        8. validate endpoint type
        9. emit only when semantics are sufficiently supported
        """
        ...

    def get_source_priority(self) -> str:
        """Document source priority per blueprint section 17.

        Returns a description of priority order.
        """
        return "machine-readable metadata > manifest/lockfile > source fallback > UNKNOWN"


class ExtractorPipeline:
    """Orchestrates extraction across all registered plugins."""

    def __init__(self, config: Any):
        self.config = config
        self.plugins: List[ExtractorPlugin] = []
        self.diagnostics: List[Diagnostic] = []

    def register(self, plugin: ExtractorPlugin):
        """Register an extractor plugin."""
        self.plugins.append(plugin)

    def run_all(
        self,
        repo_root: str,
        discovery_result: Any,
        evidence_collector: EvidenceCollector,
    ) -> ExtractorResult:
        """Run all registered extractors and merge results."""
        merged = ExtractorResult()

        for plugin in self.plugins:
            try:
                if not plugin.detect(repo_root, discovery_result):
                    self.diagnostics.append(Diagnostic(
                        level="info",
                        message=f"Plugin {plugin.name}: build system not detected",
                        source=plugin.name,
                    ))
                    continue

                self.diagnostics.append(Diagnostic(
                    level="info",
                    message=f"Plugin {plugin.name}: extracting",
                    source=plugin.name,
                ))

                result = plugin.extract(
                    repo_root, discovery_result, evidence_collector, self.config
                )

                # Merge results
                merged.components.extend(result.components)
                merged.aggregators.extend(result.aggregators)
                merged.runners.extend(result.runners)
                merged.tests.extend(result.tests)
                merged.external_packages.extend(result.external_packages)
                merged.package_managers.extend(result.package_managers)
                merged.edges.extend(result.edges)
                merged.diagnostics.extend(result.diagnostics)
                merged.unresolved_references.extend(result.unresolved_references)
                merged.build_profiles.extend(result.build_profiles)

                # Code-level entities
                merged.code_files.extend(result.code_files)
                merged.code_modules.extend(result.code_modules)
                merged.code_classes.extend(result.code_classes)
                merged.code_functions.extend(result.code_functions)
                merged.code_symbols.extend(result.code_symbols)

                self.diagnostics.append(Diagnostic(
                    level="info",
                    message=f"Plugin {plugin.name}: extracted "
                            f"{len(result.components)} components, "
                            f"{len(result.edges)} edges",
                    source=plugin.name,
                ))

            except Exception as e:
                # Plugin-local failure SHOULD not fail unrelated extractors
                # (blueprint section 21, 29)
                self.diagnostics.append(Diagnostic(
                    level="warning",
                    message=f"Plugin {plugin.name}: extraction failed: {e}",
                    source=plugin.name,
                    attributes={"error": str(e)},
                ))

        merged.diagnostics.extend(self.diagnostics)
        return merged