"""
npm extractor plugin for RIG.

Per blueprint sections 4.6 and 17.4:
- Declaration/lockfile evidence is the default; npm list is NOT authoritative
- package.json and lockfiles are primary sources
- node_modules MUST NOT become repository Components by default
- Installed-state command is only opt-in input

Priority: package.json > lockfile > workspace metadata > npm list (opt-in) > UNKNOWN
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional

from rig.evidence import EvidenceCollector
from rig.extractor import ExtractorPlugin, ExtractorResult
from rig.identity import (
    build_profile_id, component_id, edge_id,
    external_package_id, normalize_path,
    package_manager_id, test_id,
)
from rig.models import (
    Component, ComponentType, Edge,
    EdgeType, ExternalPackage, PackageManager, Runner,
    TestDefinition, TestKind, DependsOnRole,
)


class NpmExtractor(ExtractorPlugin):
    """npm/JavaScript build system extractor."""

    @property
    def name(self) -> str:
        return "npm"

    def detect(self, repo_root: str, discovery_result: Any) -> bool:
        return "npm" in discovery_result.build_system_markers

    def get_source_priority(self) -> str:
        return "package.json > lockfile > workspace metadata > npm list (opt-in) > UNKNOWN"

    def extract(
        self, repo_root: str, discovery_result: Any,
        evidence_collector: EvidenceCollector, config: Any,
    ) -> ExtractorResult:
        result = ExtractorResult()
        bs_id = build_profile_id("npm", "root")

        # Find all package.json files (not in node_modules)
        package_jsons = self._find_package_jsons(repo_root, discovery_result)

        # Create package manager entity
        pm_id = package_manager_id("npm", "npm")
        pm_ev = evidence_collector.add_file_evidence(
            os.path.join(repo_root, "package.json")
        )
        pm = PackageManager(
            id=pm_id,
            name="npm",
            ecosystem="npm",
            identity_key="package_manager|ecosystem=npm|name=npm",
            evidence_ids=[pm_ev.id],
        )
        result.package_managers.append(pm)

        # Process each package.json
        for pkg_rel, pkg_data in package_jsons.items():
            if pkg_data is None:
                continue
            self._process_package_json(
                repo_root, pkg_rel, pkg_data,
                evidence_collector, bs_id, pm_id, result
            )

        # Look for lockfiles and add package details
        for lockfile in discovery_result.lockfiles:
            if "package-lock" in lockfile:
                self._process_package_lock(
                    repo_root, lockfile, evidence_collector,
                    bs_id, pm_id, result
                )
            elif "yarn.lock" in lockfile:
                self._process_yarn_lock(
                    repo_root, lockfile, evidence_collector,
                    bs_id, pm_id, result
                )

        return result

    def _find_package_jsons(
        self, repo_root: str, discovery_result: Any,
    ) -> Dict[str, Optional[Dict]]:
        """Find package.json files, excluding node_modules."""
        results = {}

        # First from discovery
        for f in discovery_result.all_files:
            if f.path.endswith("package.json") and "node_modules" not in f.path:
                try:
                    with open(f.abspath, "r", encoding="utf-8") as fh:
                        results[f.path] = json.load(fh)
                except (json.JSONDecodeError, IOError):
                    results[f.path] = None

        # Also find other package.json files via targeted search
        cmake_files = discovery_result.build_system_markers.get("npm", [])
        for marker in cmake_files:
            marker_dir = os.path.dirname(os.path.join(repo_root, marker))
            # Walk up to find workspace roots, down to find workspace packages
            self._find_nearby_package_jsons(marker_dir, repo_root, results)

        return results

    def _find_nearby_package_jsons(
        self, start_dir: str, repo_root: str,
        results: Dict[str, Optional[Dict]],
    ):
        """Find package.json files in or near the start directory."""
        # Check start_dir
        pkg_path = os.path.join(start_dir, "package.json")
        if os.path.isfile(pkg_path):
            rel = normalize_path(os.path.relpath(pkg_path, repo_root))
            if rel not in results:
                try:
                    with open(pkg_path, "r", encoding="utf-8") as f:
                        results[rel] = json.load(f)
                except (json.JSONDecodeError, IOError):
                    results[rel] = None

        # Check immediate subdirectories (one level) for workspace packages
        try:
            for entry in os.listdir(start_dir):
                sub_dir = os.path.join(start_dir, entry)
                if os.path.isdir(sub_dir) and not entry.startswith("."):
                    sub_pkg = os.path.join(sub_dir, "package.json")
                    if os.path.isfile(sub_pkg):
                        rel = normalize_path(os.path.relpath(sub_pkg, repo_root))
                        if rel not in results:
                            try:
                                with open(sub_pkg, "r", encoding="utf-8") as f:
                                    results[rel] = json.load(f)
                            except (json.JSONDecodeError, IOError):
                                results[rel] = None
        except PermissionError:
            pass

    def _process_package_json(
        self, repo_root: str, pkg_rel: str,
        pkg_data: Dict, evidence_collector: EvidenceCollector,
        bs_id: str, pm_id: str, result: ExtractorResult,
    ):
        """Extract entities from a package.json."""
        pkg_abs = os.path.join(repo_root, pkg_rel)
        name = pkg_data.get("name", os.path.basename(os.path.dirname(pkg_rel)) or "unknown")

        # Create component for this package
        identity_key = f"component|npm|profile={bs_id}|target={name}"
        cid = component_id(bs_id, name)

        ev = evidence_collector.add_manifest_evidence(
            pkg_abs, text_snippet=json.dumps({
                "name": name,
                "version": pkg_data.get("version"),
            }, ensure_ascii=False)
        )

        # Determine language from files
        language = "javascript"
        if pkg_data.get("type") == "module":
            language = "javascript"

        comp = Component(
            id=cid,
            name=name,
            type=ComponentType.PACKAGE_LIBRARY,
            programming_language=language,
            source_files=[normalize_path(pkg_rel)],
            identity_key=identity_key,
            evidence_ids=[ev.id],
            build_profile_ids=[bs_id],
        )
        result.components.append(comp)

        # Scripts become runners
        scripts = pkg_data.get("scripts", {})
        for script_name, script_cmd in scripts.items():
            if isinstance(script_cmd, str):
                runner_identity_key = (
                    f"runner|npm|profile={bs_id}|command={script_name}|owner={cid}"
                )
                rid = self._runner_id(f"npm:{bs_id}:{script_name}:{cid}")
                rev = evidence_collector.add_manifest_evidence(
                    pkg_abs,
                    text_snippet=f"scripts.{script_name}: {script_cmd[:200]}",
                )
                runner = Runner(
                    id=rid,
                    name=f"{name}:{script_name}",
                    identity_key=runner_identity_key,
                    evidence_ids=[rev.id],
                    build_profile_ids=[bs_id],
                    command=script_name,
                    arguments=[script_cmd],
                    owner_node_id=cid,
                )
                result.runners.append(runner)

                # Edge: runner input
                dedge_id = edge_id("depends_on", rid, cid, role=DependsOnRole.RUNNER_INPUT)
                edge = Edge(
                    id=dedge_id,
                    type=EdgeType.DEPENDS_ON,
                    source=rid,
                    target=cid,
                    role=DependsOnRole.RUNNER_INPUT,
                    evidence_ids=[rev.id],
                    origin_plugin="npm",
                )
                result.edges.append(edge)

                # Test scripts
                if script_name in ("test", "test:*"):
                    tid = test_id("npm", bs_id, f"{name}:{script_name}",
                                  command=script_cmd)
                    test = TestDefinition(
                        id=tid,
                        name=f"{name}:{script_name}",
                        framework=script_name if script_name == "test" else "custom",
                        test_kind=TestKind.UNIT if script_name == "test" else TestKind.UNKNOWN,
                        identity_key=f"test|npm|profile={bs_id}|name={name}:{script_name}",
                        evidence_ids=[rev.id],
                        build_profile_ids=[bs_id],
                        runner_node_id=rid,
                        command=script_cmd,
                    )
                    result.tests.append(test)

        # Extract dependencies
        for dep_type, dep_key in [("dependencies", "runtime"),
                                   ("devDependencies", "dev"),
                                   ("peerDependencies", "peer"),
                                   ("optionalDependencies", "optional")]:
            deps = pkg_data.get(dep_type, {})
            for dep_name, dep_version in deps.items():
                ep_identity_key = (
                    f"external_package|ecosystem=npm|coordinate={dep_name}|scope={dep_key}"
                )
                epid = external_package_id("npm", dep_name, dep_key)
                ep_ev = evidence_collector.add_manifest_evidence(
                    pkg_abs,
                    text_snippet=f"{dep_type}.{dep_name}: {dep_version}",
                )

                ep = ExternalPackage(
                    id=epid,
                    name=dep_name,
                    ecosystem="npm",
                    coordinate=dep_name,
                    declared_version=str(dep_version) if dep_version else None,
                    dependency_scope=dep_key,
                    optional=(dep_type == "optionalDependencies"),
                    peer=(dep_type == "peerDependencies"),
                    identity_key=ep_identity_key,
                    evidence_ids=[ep_ev.id],
                    package_manager_id=pm_id,
                )
                result.external_packages.append(ep)

                # Edge: component depends on external package
                dedge_id = edge_id("external", cid, epid, role=dep_key)
                edge = Edge(
                    id=dedge_id,
                    type=EdgeType.EXTERNAL,
                    source=cid,
                    target=epid,
                    role=dep_key,
                    evidence_ids=[ep_ev.id],
                    origin_plugin="npm",
                )
                result.edges.append(edge)

    def _process_package_lock(
        self, repo_root: str, lockfile_rel: str,
        evidence_collector: EvidenceCollector,
        bs_id: str, pm_id: str, result: ExtractorResult,
    ):
        """Extract resolved versions from package-lock.json."""
        lockfile_abs = os.path.join(repo_root, lockfile_rel)
        try:
            with open(lockfile_abs, "r", encoding="utf-8") as f:
                lock_data = json.load(f)
        except (json.JSONDecodeError, IOError):
            return

        packages = lock_data.get("packages", lock_data.get("dependencies", {}))
        for pkg_key, pkg_info in packages.items():
            if isinstance(pkg_info, dict):
                resolved = pkg_info.get("version", "")
                # Match to existing external packages
                pkg_name = pkg_key.split("/")[-1] if pkg_key else ""
                if pkg_name:
                    for ep in result.external_packages:
                        if ep.name == pkg_name and ep.resolved_version is None:
                            ep.resolved_version = resolved
                            ep.lockfile_path = normalize_path(lockfile_rel)

    def _process_yarn_lock(
        self, repo_root: str, lockfile_rel: str,
        evidence_collector: EvidenceCollector,
        bs_id: str, pm_id: str, result: ExtractorResult,
    ):
        """Basic yarn.lock processing (placeholder - extracts dependency entries)."""
        # Full yarn.lock parsing requires a proper parser;
        # this is a placeholder for future implementation.
        pass

    def _runner_id(self, key: str) -> str:
        """Deterministic runner ID from key."""
        return f"runner:{hashlib.sha256(key.encode()).hexdigest()}"