"""Deterministic source discovery for CODE ATLAS.

Task 1 scope: walk a project directory, ignore common build/dependency
directories, and collect a sorted list of relative file paths with
normalized '/' separators.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable

from atlas.config import (
    BINARY_EXTENSIONS,
    INTERESTING_EXTENSIONS,
    is_ignored_dir,
    is_reparse_point,
)


@dataclass
class DiscoveryResult:
    """Result of a project discovery scan.

    Attributes:
        root: Absolute, normalized path of the project root.
        name: Project name derived from the root directory's basename.
        files: Sorted list of relative file paths (normalized, '/' separated).
    """

    root: str
    name: str
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Return a plain, deterministic dict suitable for JSON output."""
        return {
            "version": 1,
            "project": {
                "name": self.name,
                "language": "python",
            },
            "files": list(self.files),
        }

    def to_json(self, pretty: bool = False) -> str:
        """Serialize this result to a JSON string."""
        import json

        indent = 4 if pretty else None
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False)


def _norm_rel(path: str) -> str:
    """Convert an OS-relative path to canonical form with '/' separators."""
    return path.replace(os.sep, "/")


def _is_skippable_dir(entry_name: str) -> bool:
    return is_ignored_dir(entry_name)


def _prune_dirnames(dirpath: str, dirnames: list[str]) -> list[str]:
    """Pangkas ``dirnames`` IN-PLACE sebelum rekursi (case-insensitive + link-safe).

    Directory yang di-exclude (environment/dependency/cache/build/metadata)
    atau berupa symlink/junction dipangkas SEBELUM direkursi, sehingga isinya
    tidak pernah dipindai.
    """
    kept = []
    for name in dirnames:
        if _is_skippable_dir(name):
            continue
        if is_reparse_point(os.path.join(dirpath, name)):
            continue
        kept.append(name)
    dirnames[:] = kept
    return dirnames


def _is_relevant_file(entry_name: str) -> bool:
    """Return True if a filename is worth including in the map."""
    _, ext = os.path.splitext(entry_name)

    # Skip explicit binary / non-source extensions.
    if ext.lower() in BINARY_EXTENSIONS:
        return False

    # Accept anything with an interesting extension.
    if ext.lower() in INTERESTING_EXTENSIONS:
        return True

    # Plain files without an extension (e.g. Dockerfile, Makefile, LICENSE)
    # are considered relevant source/config.
    return entry_name.endswith(("LICENSE", "README", "Makefile", "Dockerfile",
                                "Procfile", ".gitignore", ".gitattributes",
                                ".env.example", "pyproject", "setup.cfg",
                                "MANIFEST", "requirements"))


def discover_project(project_root: str) -> DiscoveryResult:
    """Discover source files inside ``project_root``.

    Args:
        project_root: Path to the project directory.

    Returns:
        A :class:`DiscoveryResult` with sorted, relative, '/' separated
        file paths.

    Raises:
        FileNotFoundError: If ``project_root`` does not exist.
        NotADirectoryError: If ``project_root`` is not a directory.
    """
    if not os.path.exists(project_root):
        raise FileNotFoundError(f"project path does not exist: {project_root}")

    if not os.path.isdir(project_root):
        raise NotADirectoryError(
            f"project path is not a directory: {project_root}"
        )

    root = os.path.normpath(os.path.abspath(project_root))
    name = os.path.basename(root.rstrip(os.sep)) or root

    files: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored directories (and symlink/junction) in-place so os.walk
        # never descends into them.
        _prune_dirnames(dirpath, dirnames)
        dirnames.sort()

        for filename in sorted(filenames):
            if not _is_relevant_file(filename):
                continue
            full = os.path.join(dirpath, filename)
            rel = _norm_rel(os.path.relpath(full, root))
            files.append(rel)

    files.sort()
    return DiscoveryResult(root=root, name=name, files=files)
