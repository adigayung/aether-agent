"""
Repository discovery for RIG.

Per blueprint sections 2.3 and AD-09:
- Targeted metadata discovery, not unconditional full-tree traversal
- Find build-system markers, manifest/lock files, build metadata,
  test metadata, and relevant configuration files
- MUST NOT treat entire source tree as input semantic graph
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from rig.config import BUILD_SYSTEM_MARKERS, DEFAULT_IGNORE_DIRS
from rig.identity import normalize_path


@dataclass
class DiscoveredFile:
    """A file discovered in the repository, with role classification."""
    path: str  # repo-relative path, POSIX /
    abspath: str  # absolute path for reading
    role: str = "unknown"  # build, manifest, lockfile, source, config, test, generated
    build_system: Optional[str] = None  # cmake, npm, maven, etc.


@dataclass
class DiscoveryResult:
    """Result of repository discovery."""
    repo_root: str
    all_files: List[DiscoveredFile] = field(default_factory=list)
    build_system_markers: Dict[str, List[str]] = field(default_factory=dict)
    manifest_files: List[str] = field(default_factory=list)
    lockfiles: List[str] = field(default_factory=list)
    source_files: List[str] = field(default_factory=list)
    test_files: List[str] = field(default_factory=list)
    config_files: List[str] = field(default_factory=list)
    generated_files: List[str] = field(default_factory=list)

    def build_systems_detected(self) -> List[str]:
        return list(self.build_system_markers.keys())


class RepositoryDiscovery:
    """Discovers repository structure for RIG extraction.

    Uses targeted discovery (section AD-09): look for build system
    markers and relevant metadata files rather than scanning every file.
    """

    def __init__(
        self,
        repo_root: str,
        ignore_dirs: Optional[Set[str]] = None,
        max_depth: Optional[int] = None,
    ):
        self.repo_root = os.path.abspath(repo_root)
        self.ignore_dirs = ignore_dirs or DEFAULT_IGNORE_DIRS.copy()
        self.max_depth = max_depth

    def discover(self) -> DiscoveryResult:
        """Perform targeted discovery of build-relevant files."""
        result = DiscoveryResult(repo_root=self.repo_root)

        # Phase 1: find build system markers (targeted discovery)
        self._find_build_system_markers(result)

        # Phase 2: find relevant manifest and config files near markers
        self._find_relevant_files(result)

        # Phase 3: find lockfiles
        self._find_lockfiles(result)

        return result

    def _find_build_system_markers(self, result: DiscoveryResult) -> None:
        """Find build system marker files at any depth.

        Per section AD-09: targeted, not full traversal.
        """
        for bs_name, markers in BUILD_SYSTEM_MARKERS.items():
            for marker_file in markers["files"]:
                found = self._find_marker(result.repo_root, marker_file)
                for f in found:
                    rel = normalize_path(os.path.relpath(f, result.repo_root))
                    if bs_name not in result.build_system_markers:
                        result.build_system_markers[bs_name] = []
                    result.build_system_markers[bs_name].append(rel)
                    result.all_files.append(DiscoveredFile(
                        path=rel,
                        abspath=f,
                        role="build",
                        build_system=bs_name,
                    ))

    def _find_marker(self, root: str, marker: str, current_depth: int = 0) -> List[str]:
        """Find marker file using targeted search (not full tree walk)."""
        results = []
        if self.max_depth is not None and current_depth > self.max_depth:
            return results

        try:
            entries = sorted(os.listdir(root))
        except PermissionError:
            return results

        for entry in entries:
            if entry.startswith(".") and entry not in (".", ".."):
                # Skip hidden dirs except marker itself might be hidden
                pass

            full_path = os.path.join(root, entry)
            if os.path.isfile(full_path) and entry == marker.split("/")[-1]:
                if marker == entry or full_path.endswith(marker):
                    results.append(full_path)
            elif os.path.isdir(full_path):
                dirname = os.path.basename(full_path)
                if dirname in self.ignore_dirs:
                    continue
                results.extend(
                    self._find_marker(full_path, marker, current_depth + 1)
                )

        return results

    def _find_relevant_files(self, result: DiscoveryResult) -> None:
        """Find relevant configuration and metadata files near build markers."""
        for bs_name, markers in result.build_system_markers.items():
            for marker_path in markers:
                marker_abs = os.path.join(result.repo_root, marker_path)
                marker_dir = os.path.dirname(marker_abs)

                # Look for companion files in the same directory
                if bs_name == "cmake":
                    self._find_cmake_files(result, marker_dir)
                elif bs_name == "npm":
                    self._find_npm_files(result, marker_dir)
                elif bs_name == "maven":
                    self._find_maven_files(result, marker_dir)
                elif bs_name == "cargo":
                    self._find_cargo_files(result, marker_dir)
                elif bs_name == "go":
                    self._find_go_files(result, marker_dir)
                elif bs_name == "meson":
                    self._find_meson_files(result, marker_dir)
                elif bs_name == "python":
                    self._find_python_files(result, marker_dir)

    def _find_cmake_files(self, result: DiscoveryResult, marker_dir: str) -> None:
        """Find CMake-related files."""
        # Look for .cmake files, CMakePresets.json, CTestConfig.cmake
        patterns = [
            ("*.cmake", "config"),
            ("CMakePresets.json", "config"),
            ("CTestConfig.cmake", "test"),
            ("CTestTestfile.cmake", "test"),
        ]
        self._find_by_patterns(result, marker_dir, patterns)

    def _find_npm_files(self, result: DiscoveryResult, marker_dir: str) -> None:
        """Find npm-related files."""
        patterns = [
            ("package.json", "manifest"),
            (".npmrc", "config"),
        ]
        self._find_by_patterns(result, marker_dir, patterns)

    def _find_maven_files(self, result: DiscoveryResult, marker_dir: str) -> None:
        """Find Maven-related files."""
        patterns = [
            ("pom.xml", "manifest"),
            ("mvnw", "config"),
            (".mvn", "config"),
        ]
        self._find_by_patterns(result, marker_dir, patterns)

    def _find_cargo_files(self, result: DiscoveryResult, marker_dir: str) -> None:
        """Find Cargo-related files."""
        patterns = [
            ("Cargo.toml", "manifest"),
            (".cargo", "config"),
            ("build.rs", "build"),
        ]
        self._find_by_patterns(result, marker_dir, patterns)

    def _find_go_files(self, result: DiscoveryResult, marker_dir: str) -> None:
        """Find Go-related files."""
        patterns = [
            ("go.mod", "manifest"),
        ]
        self._find_by_patterns(result, marker_dir, patterns)

    def _find_meson_files(self, result: DiscoveryResult, marker_dir: str) -> None:
        """Find Meson-related files."""
        patterns = [
            ("meson.build", "build"),
            ("meson_options.txt", "config"),
        ]
        self._find_by_patterns(result, marker_dir, patterns)

    def _find_python_files(self, result: DiscoveryResult, marker_dir: str) -> None:
        """Find Python-related files."""
        patterns = [
            ("setup.py", "build"),
            ("setup.cfg", "config"),
            ("pyproject.toml", "manifest"),
            ("Pipfile", "manifest"),
            ("poetry.lock", "lockfile"),
        ]
        self._find_by_patterns(result, marker_dir, patterns)

    def _find_by_patterns(
        self,
        result: DiscoveryResult,
        base_dir: str,
        patterns: List[tuple],
    ) -> None:
        """Find files matching patterns in or near base_dir."""
        for filename, role in patterns:
            full_path = os.path.join(base_dir, filename)
            if os.path.isfile(full_path):
                rel = normalize_path(os.path.relpath(full_path, result.repo_root))
                result.all_files.append(DiscoveredFile(
                    path=rel, abspath=full_path, role=role
                ))
                if role == "manifest":
                    result.manifest_files.append(rel)
                elif role == "config":
                    result.config_files.append(rel)
                elif role == "test":
                    result.test_files.append(rel)

            # Also check parent directory for monorepo roots
            parent_dir = os.path.dirname(base_dir)
            parent_path = os.path.join(parent_dir, filename)
            if parent_dir != base_dir and os.path.isfile(parent_path):
                rel = normalize_path(os.path.relpath(parent_path, result.repo_root))
                result.all_files.append(DiscoveredFile(
                    path=rel, abspath=parent_path, role=role
                ))
                if role == "manifest":
                    result.manifest_files.append(rel)

        # Also scan subdirectories one level deep for build files
        try:
            entries = sorted(os.listdir(base_dir))
        except PermissionError:
            return

        for entry in entries:
            full_entry = os.path.join(base_dir, entry)
            if os.path.isdir(full_entry) and entry not in self.ignore_dirs:
                for filename, role in patterns:
                    nested = os.path.join(full_entry, filename)
                    if os.path.isfile(nested):
                        rel = normalize_path(os.path.relpath(nested, result.repo_root))
                        result.all_files.append(DiscoveredFile(
                            path=rel, abspath=nested, role=role
                        ))

    def _find_lockfiles(self, result: DiscoveryResult) -> None:
        """Find lockfile patterns across repository (targeted)."""
        from rig.config import LOCKFILE_PATTERNS

        for lockfile_pattern in LOCKFILE_PATTERNS:
            found = self._find_marker(result.repo_root, lockfile_pattern)
            for f in found:
                rel = normalize_path(os.path.relpath(f, result.repo_root))
                result.lockfiles.append(rel)
                result.all_files.append(DiscoveredFile(
                    path=rel, abspath=f, role="lockfile"
                ))

    def find_source_files(
        self,
        result: DiscoveryResult,
        source_dirs: Optional[List[str]] = None,
    ) -> List[str]:
        """Find source files in given directories (or common source dirs)."""
        if source_dirs is None:
            source_dirs = ["src", "lib", "source", "include"]

        source_files = []
        for src_dir in source_dirs:
            full_src = os.path.join(result.repo_root, src_dir)
            if os.path.isdir(full_src):
                for root, dirs, files in os.walk(full_src):
                    # Filter ignored dirs
                    dirs[:] = [d for d in dirs if d not in self.ignore_dirs
                               and not d.startswith(".")]
                    for f in sorted(files):
                        rel = normalize_path(os.path.relpath(
                            os.path.join(root, f), result.repo_root
                        ))
                        source_files.append(rel)
                        result.all_files.append(DiscoveredFile(
                            path=rel,
                            abspath=os.path.join(root, f),
                            role="source"
                        ))

        result.source_files = sorted(set(source_files))
        return result.source_files