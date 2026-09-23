"""
Pipeline orchestrator for MAP_CODE_RIG.

Per blueprint sections 38 (Final Architecture):
  repository root → Discovery → Profile Detect → Extractor Plugins →
  Raw Facts + Evidence → Normalizer → Identity Materializer →
  Graph Assembly → Validation → Canonical JSON

This is the main pipeline that coordinates all RIG construction steps.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from rig.config import Config
from rig.discovery import RepositoryDiscovery
from rig.evidence import EvidenceCollector
from rig.extractor import ExtractorPipeline
from rig.extractors.cmake_extractor import CMakeExtractor
from rig.extractors.npm_extractor import NpmExtractor
from rig.code_extractor import PythonCodeExtractor
from rig.graph import GraphBuilder
from rig.identity import build_profile_id
from rig.models import (
    RIG, Diagnostic, DiagnosticLevel, BuildProfile,
)
from rig.serializer import RIGSerializer
from rig.validator import RIGValidator


class RIGPipeline:
    """Orchestrates the full RIG construction pipeline.

    Per blueprint section 38 architecture:
    1. Repository discovery
    2. Build/test profile detection
    3. Extractor plugins (deterministic)
    4. Evidence collection
    5. Graph assembly
    6. Validation
    7. Canonical serialization
    """

    def __init__(self, config: Config):
        self.config = config
        self.diagnostics: List[Diagnostic] = []

    def run(self) -> Tuple[Optional[RIG], List[Diagnostic]]:
        """Run the full RIG pipeline.

        Returns (rig, diagnostics). rig is None if fatal errors prevent output.
        """
        repo_root = os.path.abspath(self.config.project_path)

        # ── Step 1: Repository Discovery ──
        self._log("info", "Starting repository discovery", "pipeline")
        discovery = RepositoryDiscovery(
            repo_root=repo_root,
            ignore_dirs=self.config.ignore_dirs,
            max_depth=self.config.max_discovery_depth,
        )
        discovery_result = discovery.discover()
        detected = discovery_result.build_systems_detected()

        if not detected:
            self._log("warning",
                      "No build systems detected. Output will be minimal.",
                      "pipeline")
        else:
            self._log("info",
                      f"Detected build systems: {', '.join(detected)}",
                      "pipeline")

        # ── Step 2: Evidence Collector ──
        evidence_collector = EvidenceCollector(repo_root)

        # ── Step 3: Extractor Pipeline ──
        self._log("info", "Running extractor plugins", "pipeline")
        extractor_pipeline = ExtractorPipeline(self.config)

        # Register plugins (extend this list as more extractors are added)
        extractor_pipeline.register(CMakeExtractor())
        extractor_pipeline.register(NpmExtractor())
        extractor_pipeline.register(PythonCodeExtractor())

        extractor_result = extractor_pipeline.run_all(
            repo_root, discovery_result, evidence_collector
        )

        # Collect all evidence
        all_evidence = evidence_collector.get_all()

        # ── Step 4: Graph Assembly ──
        self._log("info", "Assembling RIG graph", "pipeline")
        builder = GraphBuilder()
        rig = builder.build_from_extractor_result(
            extractor_result, all_evidence, repo_root
        )

        # Rebuild repository info
        rig.repo.name = os.path.basename(repo_root)
        if detected:
            rig.repo.primary_language = self._detect_primary_language(extractor_result)
            rig.repo.languages = self._collect_languages(extractor_result)
        rig.repo.build_profile_ids = list(set(
            c.build_profile_ids[0] for c in rig.components if c.build_profile_ids
        ))

        # Add pipeline diagnostics
        for diag in self.diagnostics:
            rig.diagnostics.append(diag)

        # ── Step 5: Validation ──
        self._log("info", "Validating RIG graph", "pipeline")
        validator = RIGValidator()
        validation_result = validator.validate(rig)

        # Add validation diagnostics to rig
        for diag in validation_result.all_diagnostics:
            rig.diagnostics.append(diag)

        # Log validation result
        if validation_result.errors:
            self._log("error",
                      f"Validation failed with {len(validation_result.errors)} error(s)",
                      "pipeline")
            for err in validation_result.errors:
                self._log("error", f"  {err.message}", err.source or "validator")
        else:
            self._log("info", "Validation passed", "pipeline")

        if validation_result.warnings:
            self._log("warning",
                      f"{len(validation_result.warnings)} warning(s)",
                      "pipeline")

        # ── Step 6: Final diagnostics ──
        all_diagnostics = rig.diagnostics
        self._log("info",
                  f"RIG built: {len(rig.components)} components, "
                  f"{len(rig.edges)} edges, "
                  f"{len(rig.evidence)} evidence items",
                  "pipeline")

        return rig, all_diagnostics

    def _detect_primary_language(self, extractor_result) -> str:
        """Detect primary language from extracted components."""
        languages = self._collect_languages(extractor_result)
        if not languages:
            return "unknown"
        # Return most common language
        from collections import Counter
        counter = Counter(languages)
        return counter.most_common(1)[0][0]

    def _collect_languages(self, extractor_result) -> List[str]:
        """Collect all programming languages from components."""
        languages = []
        for comp in extractor_result.components:
            if comp.programming_language and comp.programming_language != "unknown":
                languages.append(comp.programming_language)
        return languages

    def _log(self, level: str, message: str, source: str = "pipeline"):
        """Add a diagnostic message."""
        self.diagnostics.append(Diagnostic(
            level=level,
            message=message,
            source=source,
        ))