"""
CMake extractor plugin for RIG.

Per blueprint sections 17.2 and 2.2:
- CMake is the reference implementation with strongest study evidence
- Primary source: CMake File API for target/artifact/build graph
- CTest JSON for tests
- CMakeLists.txt inspection for properties not in File API
- Deterministic fallback JSON parsing for custom command/target cases

Priority: CMake File API > CTest JSON > CMakeLists.txt fallback > UNKNOWN
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from rig.evidence import EvidenceCollector
from rig.extractor import ExtractorPlugin, ExtractorResult
from rig.identity import (
    build_profile_id, component_id,
    edge_id, evidence_id, normalize_path, test_id,
)
from rig.models import (
    Component, ComponentType, Edge,
    EdgeType, TestDefinition, TestKind, DependsOnRole,
)


class CMakeExtractor(ExtractorPlugin):
    """CMake build system extractor.

    Detects CMake projects by CMakeLists.txt presence.
    Extracts targets from CMake File API replies if available,
    otherwise falls back to CMakeLists.txt analysis.
    """

    @property
    def name(self) -> str:
        return "cmake"

    def detect(self, repo_root: str, discovery_result: Any) -> bool:
        return "cmake" in discovery_result.build_system_markers

    def get_source_priority(self) -> str:
        return (
            "CMake File API (.cmake/api) > CTest JSON > "
            "CMakeLists.txt inspection > UNKNOWN"
        )

    def extract(
        self,
        repo_root: str,
        discovery_result: Any,
        evidence_collector: EvidenceCollector,
        config: Any,
    ) -> ExtractorResult:
        result = ExtractorResult()
        build_profile_id_val = build_profile_id("cmake", "root")

        # Try to find File API replies
        api_replies = self._find_file_api_replies(repo_root)

        if api_replies:
            # Use File API as primary source
            result = self._extract_from_file_api(
                repo_root, api_replies, evidence_collector,
                build_profile_id_val, result, discovery_result
            )
        else:
            # Fallback: parse CMakeLists.txt files
            result = self._extract_from_cmakelists(
                repo_root, discovery_result, evidence_collector,
                build_profile_id_val, result
            )

        return result

    def _find_file_api_replies(self, repo_root: str) -> Dict[str, Any]:
        """Find CMake File API reply objects.

        Path: <build-dir>/.cmake/api/v1/reply/*
        """
        # Common build directories
        build_dirs = ["build", "_build", "out", "cmake-build-*"]
        api_replies = {}

        for bd_pattern in build_dirs:
            if "*" in bd_pattern:
                # Try to match prefix
                for entry in os.listdir(repo_root):
                    if entry.startswith(bd_pattern.replace("*", "")):
                        self._scan_api_dir(os.path.join(repo_root, entry), api_replies)
            else:
                full_bd = os.path.join(repo_root, bd_pattern)
                if os.path.isdir(full_bd):
                    self._scan_api_dir(full_bd, api_replies)

        return api_replies

    def _scan_api_dir(self, build_dir: str, api_replies: Dict[str, Any]):
        """Scan a build directory for File API replies."""
        api_dir = os.path.join(build_dir, ".cmake", "api", "v1", "reply")
        if not os.path.isdir(api_dir):
            return

        try:
            for fname in os.listdir(api_dir):
                if fname.endswith(".json"):
                    fpath = os.path.join(api_dir, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        api_replies[fname] = data
                    except (json.JSONDecodeError, IOError):
                        pass
        except PermissionError:
            pass

    def _extract_from_file_api(
        self, repo_root: str, api_replies: Dict[str, Any],
        evidence_collector: EvidenceCollector,
        build_profile_id_val: str, result: ExtractorResult,
        discovery_result: Any,
    ) -> ExtractorResult:
        """Extract using CMake File API replies."""
        # Process codemodel replies (index)
        codemodel_v2 = None
        target_replies = {}

        for fname, data in api_replies.items():
            if fname == "index.json" or fname.startswith("index-"):
                # Index file - find codemodel
                if isinstance(data, dict):
                    for obj in data.get("objects", data.get("children", [])):
                        if isinstance(obj, dict) and obj.get("kind") == "codemodel":
                            codemodel_v2 = obj
            elif "codemodel" in fname:
                codemodel_v2 = {"kind": "codemodel", "jsonFile": fname}
            elif data.get("kind") == "target":
                target_replies[fname] = data

        # If we found codemodel reference, load it
        if codemodel_v2 and isinstance(codemodel_v2, dict):
            json_file = codemodel_v2.get("jsonFile", "")
            for fname, data in api_replies.items():
                if json_file and json_file in fname:
                    codemodel_v2 = data
                    break

        # Process codemodel for targets
        if isinstance(codemodel_v2, dict):
            components_made: Dict[str, Component] = {}

            for target_entry in codemodel_v2.get("configurations", []):
                if isinstance(target_entry, dict):
                    for tg in target_entry.get("targets", []):
                        target_name = tg.get("name", "unknown")
                        target_json_file = tg.get("jsonFile", "")

                        # Find full target data
                        target_data = None
                        for fname, data in target_replies.items():
                            if target_json_file and target_json_file in fname:
                                target_data = data
                                break
                            if isinstance(data, dict) and data.get("name") == target_name:
                                target_data = data
                                break

                        comp = self._process_target_from_api(
                            repo_root, target_name, target_data,
                            evidence_collector, build_profile_id_val
                        )
                        if comp:
                            components_made[comp.id] = comp
                            result.components.append(comp)

            # Extract dependencies from target data
            self._extract_dependencies_from_api(
                repo_root, components_made, target_replies,
                evidence_collector, result
            )

        return result

    def _process_target_from_api(
        self, repo_root: str, target_name: str,
        target_data: Optional[Dict], evidence_collector: EvidenceCollector,
        build_profile_id_val: str,
    ) -> Optional[Component]:
        """Process a CMake target from File API data into a Component."""
        if not target_data:
            # Minimal component with just name
            identity_key = f"component|cmake|profile={build_profile_id_val}|target={target_name}"
            from rig.identity import component_id as comp_id
            cid = comp_id(build_profile_id_val, target_name)

            ev = evidence_collector.add_file_evidence(
                os.path.join(repo_root, "CMakeLists.txt")
            )

            return Component(
                id=cid,
                name=target_name,
                type=self._determine_type(target_data),
                programming_language="unknown",
                identity_key=identity_key,
                evidence_ids=[ev.id],
                build_profile_ids=[build_profile_id_val],
            )

        # Determine type
        target_type = target_data.get("type", "EXECUTABLE")
        comp_type = self._map_target_type(target_type)

        # Determine language
        language = "unknown"
        sources = []
        for src_group in target_data.get("sources", []):
            if isinstance(src_group, dict):
                src_file = src_group.get("path", "")
                if src_file:
                    sources.append(normalize_path(src_file))
                    detected_lang = self._detect_language(src_file)
                    if detected_lang != "unknown" and language == "unknown":
                        language = detected_lang

        # Build identity
        identity_key = (f"component|cmake|profile={build_profile_id_val}|"
                        f"target={target_name}")
        from rig.identity import component_id as comp_id
        cid = comp_id(build_profile_id_val, target_name)

        # Evidence from File API reply
        # Find the actual reply file
        api_ev = evidence_collector.add_build_artifact_evidence(
            os.path.join(repo_root, "build", ".cmake", "api", "v1", "reply",
                         f"target-{target_name}.json"),
            text_snippet=json.dumps({
                "name": target_name,
                "type": target_type,
            }, ensure_ascii=False)[:500],
        )

        return Component(
            id=cid,
            name=target_name,
            type=comp_type,
            programming_language=language,
            source_files=sorted(set(sources)),
            identity_key=identity_key,
            evidence_ids=[api_ev.id],
            build_profile_ids=[build_profile_id_val],
        )

    def _extract_dependencies_from_api(
        self, repo_root: str, components: Dict[str, Component],
        target_replies: Dict[str, Any], evidence_collector: EvidenceCollector,
        result: ExtractorResult,
    ):
        """Extract dependency edges from File API target data."""
        for comp in components.values():
            # Look up the target data for this component
            target_name = comp.name
            for fname, data in target_replies.items():
                if isinstance(data, dict) and data.get("name") == target_name:
                    # Process dependencies
                    for dep_entry in data.get("dependencies", []):
                        dep_name = dep_entry if isinstance(dep_entry, str) else dep_entry.get("name", "")
                        if dep_name in components:
                            target_comp = components[dep_name]
                            from rig.identity import edge_id as eid
                            dedge_id = eid(
                                "depends_on", comp.id, target_comp.id,
                                role="build"
                            )
                            ev = evidence_collector.add_build_artifact_evidence(
                                os.path.join(repo_root, "build", ".cmake", "api", "v1", "reply", fname),
                            )
                            edge = Edge(
                                id=dedge_id,
                                type=EdgeType.DEPENDS_ON,
                                source=comp.id,
                                target=target_comp.id,
                                role=DependsOnRole.BUILD,
                                evidence_ids=[ev.id],
                                origin_plugin="cmake",
                            )
                            result.edges.append(edge)
                    break

    def _extract_from_cmakelists(
        self, repo_root: str, discovery_result: Any,
        evidence_collector: EvidenceCollector,
        build_profile_id_val: str, result: ExtractorResult,
    ) -> ExtractorResult:
        """Fallback: parse CMakeLists.txt files for targets."""
        cmake_files = discovery_result.build_system_markers.get("cmake", [])

        # Regex patterns for target definitions
        target_patterns = [
            (r"add_executable\s*\(\s*(\S+)", ComponentType.EXECUTABLE),
            (r"add_library\s*\(\s*(\S+)", ComponentType.SHARED_LIBRARY),
            (r"add_test\s*\(\s*(?:\w+\s+)?(\S+)", TestKind.COMPILER),
            (r"enable_testing\s*\(", None),
        ]

        for cmake_rel in cmake_files:
            cmake_abs = os.path.join(repo_root, cmake_rel)
            if not os.path.isfile(cmake_abs):
                continue

            try:
                with open(cmake_abs, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except IOError:
                continue

            lines = content.split("\n")

            for i, line in enumerate(lines):
                stripped = line.strip()
                # Skip comments
                if stripped.startswith("#"):
                    continue

                for pattern, kind in target_patterns:
                    match = re.search(pattern, stripped, re.IGNORECASE)
                    # Patterns without a capture group (e.g. `enable_testing(`)
                    # are presence markers only (kind=None) and define no target,
                    # so there is no target name to read from group(1).
                    if match and match.groups():
                        target_name = match.group(1)
                        self._add_target_from_cmakelists(
                            target_name, kind, cmake_abs, cmake_rel,
                            i + 1, evidence_collector,
                            build_profile_id_val, result, content
                        )

        return result

    def _add_target_from_cmakelists(
        self, target_name: str, target_kind: Optional[str],
        cmake_abs, cmake_rel: str, line_num: int,
        evidence_collector: EvidenceCollector,
        build_profile_id_val: str, result: ExtractorResult,
        content: str,
    ):
        """Add a component from a CMakeLists.txt target definition."""
        ev = evidence_collector.add_file_evidence(
            cmake_abs, start_line=line_num, end_line=line_num,
        )

        from rig.identity import component_id as comp_id
        cid = comp_id(build_profile_id_val, target_name)
        identity_key = (f"component|cmake|profile={build_profile_id_val}|"
                        f"target={target_name}")

        if target_kind == TestKind.COMPILER:
            # This is a test definition
            tid = test_id("cmake", build_profile_id_val, target_name)
            test = TestDefinition(
                id=tid,
                name=target_name,
                framework="CTest",
                test_kind=TestKind.COMPILER,
                identity_key=f"test|cmake|profile={build_profile_id_val}|name={target_name}",
                evidence_ids=[ev.id],
                build_profile_ids=[build_profile_id_val],
            )
            result.tests.append(test)
        else:
            comp_type = target_kind or ComponentType.UNKNOWN
            comp = Component(
                id=cid,
                name=target_name,
                type=comp_type,
                identity_key=identity_key,
                evidence_ids=[ev.id],
                build_profile_ids=[build_profile_id_val],
            )
            result.components.append(comp)

    def _map_target_type(self, target_type: str) -> str:
        """Map CMake target type to ComponentType."""
        mapping = {
            "EXECUTABLE": ComponentType.EXECUTABLE,
            "STATIC_LIBRARY": ComponentType.STATIC_LIBRARY,
            "SHARED_LIBRARY": ComponentType.SHARED_LIBRARY,
            "MODULE_LIBRARY": ComponentType.PACKAGE_LIBRARY,
            "OBJECT_LIBRARY": ComponentType.SHARED_LIBRARY,
            "INTERFACE_LIBRARY": ComponentType.PACKAGE_LIBRARY,
            "UTILITY": ComponentType.UNKNOWN,
        }
        return mapping.get(target_type, ComponentType.UNKNOWN)

    def _determine_type(self, target_data: Optional[Dict]) -> str:
        if not target_data:
            return ComponentType.UNKNOWN
        return self._map_target_type(target_data.get("type", ""))

    def _detect_language(self, filename: str) -> str:
        ext = os.path.splitext(filename)[1].lower()
        lang_map = {
            ".c": "c", ".h": "c",
            ".cpp": "cxx", ".cc": "cxx", ".cxx": "cxx", ".hpp": "cxx",
            ".f": "fortran", ".f90": "fortran",
            ".cu": "cuda",
            ".java": "java",
            ".py": "python",
        }
        return lang_map.get(ext, "unknown")