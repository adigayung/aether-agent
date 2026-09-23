"""Configuration for CODE ATLAS.

Scan boundary policy
--------------------
Daftar directory/file yang di-skip diambil dari SATU sumber bersama:
``src/agent_ai/projects/scan_policy.py`` milik AETHER (lihat
``AETHER_SCAN_POLICY_PATH_ENV`` / ``AETHER_SCAN_POLICY_ENV``). Bila file policy
bersama itu tidak ditemukan (engine dipakai lewat repo lain), engine memakai
daftar bawaan di bawah ini agar tetap berjalan mandiri.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Default output file name used when none is provided.
DEFAULT_OUTPUT_NAME = "atlas.json"

# Environment variable berisi payload JSON policy (di-set oleh AETHER).
_SHARED_POLICY_ENV = "AETHER_SCAN_POLICY"
# Environment variable berisi path file policy bersama (di-set oleh AETHER).
_SHARED_POLICY_PATH_ENV = "AETHER_SCAN_POLICY_PATH"

# Directory yang selalu skipped (daftar bawaan; diperluas oleh policy bersama).
_BASE_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "ENV",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "build",
    "dist",
    ".eggs",
    ".aether",
}

# Extensions that are definitely binary / not source-relevant and
# should be ignored during discovery (daftar bawaan; diperluas policy bersama).
_BASE_BINARY_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".bin",
    ".dat",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".ipynb",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".7z",
    ".rar",
    ".class",
    ".jar",
    ".o",
    ".obj",
    ".a",
    ".lib",
    ".node",
    ".whl",
    ".egg",
    ".lock",
    ".log",
    ".cache",
}

# Suffix directory metadata paket yang di-generate (mis. ``foo.egg-info``).
_GENERATED_DIR_SUFFIXES = (".egg-info", ".dist-info")


def _load_module_from_path(path: Path):
    """Muat modul Python dari path file (defensif; None bila gagal)."""
    try:
        if not path.is_file():
            return None
        import importlib.util

        spec = importlib.util.spec_from_file_location("aether_scan_policy", str(path))
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 - bridge harus tidak pernah crash
        return None


def _load_shared_scan_policy():
    """Muat (excluded_dir_names, excluded_file_suffixes) dari policy bersama."""
    dirs = set()
    suffixes = set()

    payload = os.environ.get(_SHARED_POLICY_ENV)
    if payload:
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            dirs.update(str(x).lower() for x in data.get("excluded_dir_names") or ())
            suffixes.update(
                str(x).lower() for x in data.get("excluded_file_suffixes") or ()
            )

    if not dirs:
        candidates = []
        explicit = os.environ.get(_SHARED_POLICY_PATH_ENV)
        if explicit:
            candidates.append(Path(explicit))
        try:
            repo_root = Path(__file__).resolve().parents[3]
            candidates.append(
                repo_root / "src" / "agent_ai" / "projects" / "scan_policy.py"
            )
        except (IndexError, OSError):
            pass
        for candidate in candidates:
            module = _load_module_from_path(candidate)
            if module is None:
                continue
            dirs.update(
                str(x).lower() for x in getattr(module, "EXCLUDED_DIR_NAMES", ())
            )
            suffixes.update(
                str(x).lower() for x in getattr(module, "EXCLUDED_FILE_SUFFIXES", ())
            )
            if dirs:
                break

    return frozenset(dirs), frozenset(suffixes)


_SHARED_POLICY_DIRS, _SHARED_POLICY_SUFFIXES = _load_shared_scan_policy()

# Directories that are always skipped during discovery.
IGNORED_DIRS = frozenset(_BASE_IGNORED_DIRS) | _SHARED_POLICY_DIRS
IGNORED_DIRS_LOWER = frozenset(name.lower() for name in IGNORED_DIRS)


def is_ignored_dir(name: str) -> bool:
    """True bila directory ini harus di-skip (case-insensitive)."""
    if not name:
        return False
    lowered = name.lower()
    if lowered in IGNORED_DIRS_LOWER:
        return True
    return lowered.endswith(_GENERATED_DIR_SUFFIXES)


def is_reparse_point(path: str) -> bool:
    """True bila path adalah symlink/junction/reparse point (Windows-aware)."""
    import stat

    try:
        st = os.lstat(path)
    except OSError:
        return False
    mode = getattr(st, "st_mode", 0)
    if mode and stat.S_ISLNK(mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and (attrs & flag))


# File extensions considered "interesting" for the compact map.
# Task 1 supports Python as the primary source; other extensions are
# collected for basic information but not parsed.
INTERESTING_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".cfg",
        ".ini",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".md",
        ".txt",
        ".conf",
        ".sh",
        ".bat",
        ".cmd",
    }
)

# Extensions that are definitely binary / not source-relevant and
# should be ignored during discovery.
BINARY_EXTENSIONS = frozenset(_BASE_BINARY_EXTENSIONS) | _SHARED_POLICY_SUFFIXES
