"""
Configuration and constants for MAP_CODE_RIG.

Per blueprint section 5, 29 (CLI), and 33 (Security/Workspace Boundary).

Scan boundary policy
--------------------
Daftar directory/file yang di-ignore diambil dari SATU sumber bersama:
``src/agent_ai/projects/scan_policy.py`` milik AETHER (lihat
``AETHER_SCAN_POLICY_PATH_ENV`` / ``AETHER_SCAN_POLICY_ENV``). Bila file policy
bersama itu tidak ditemukan, engine memakai daftar bawaan di bawah ini agar
tetap berjalan mandiri.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from typing import List, Optional, Set


# Environment variable berisi payload JSON policy (di-set oleh AETHER).
_SHARED_POLICY_ENV = "AETHER_SCAN_POLICY"
# Environment variable berisi path file policy bersama (di-set oleh AETHER).
_SHARED_POLICY_PATH_ENV = "AETHER_SCAN_POLICY_PATH"

# Suffix directory metadata paket yang di-generate (mis. ``foo.egg-info``).
_GENERATED_DIR_SUFFIXES = (".egg-info", ".dist-info")


# ── Version ───────────────────────────────────────────────────────────────

VERSION = "1.0.0"
SCHEMA_VERSION = "rig-json/v1"


# ── Default ignore patterns ──────────────────────────────────────────────

# Directories and files that SHOULD be excluded from discovery.
# Per blueprint section 33: .env, credential files, private keys,
# and similar secret material must not be copied into evidence.
# node_modules, .git, .venv etc. are excluded because they are not
# part of the repository's own build/test architecture.
_BASE_IGNORE_DIRS: Set[str] = {
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "node_modules",
    ".npm",
    ".cache",
    ".aether",
    "build",
    "dist",
    "target",
    "out",
    "_build",
    "CMakeFiles",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".coverage",
    "htmlcov",
}


def _load_module_from_path(path):
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
    from pathlib import Path

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

# Effective ignore set (bawaan engine + policy bersama).
DEFAULT_IGNORE_DIRS: Set[str] = set(_BASE_IGNORE_DIRS) | set(_SHARED_POLICY_DIRS)
_IGNORE_DIRS_LOWER = frozenset(name.lower() for name in DEFAULT_IGNORE_DIRS)

# Binary/compiler/cache file suffixes (bawaan + policy bersama).
IGNORED_FILE_SUFFIXES: Set[str] = set(_SHARED_POLICY_SUFFIXES)


def is_ignored_dir(name: str) -> bool:
    """True bila directory ini harus di-skip (case-insensitive)."""
    if not name:
        return False
    lowered = name.lower()
    if lowered in _IGNORE_DIRS_LOWER:
        return True
    return lowered.endswith(_GENERATED_DIR_SUFFIXES)


def is_reparse_point(path: str) -> bool:
    """True bila path adalah symlink/junction/reparse point (Windows-aware)."""
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


DEFAULT_IGNORE_FILES: Set[str] = {
    ".env",
    ".env.local",
    ".env.*",
    "*.secret*",
    "*.key",
    "*.pem",
    "*.crt",
    "*.cert",
    "credentials",
    "secrets.yml",
    "secrets.yaml",
    "secrets.json",
    "*.log",
    "*.pid",
    "*.lock",  # except lockfiles (handled separately)
}

# Lockfiles that ARE relevant (not ignored)
LOCKFILE_PATTERNS: Set[str] = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "Gemfile.lock",
    "go.sum",
    "composer.lock",
    "poetry.lock",
}


# ── Build system markers ─────────────────────────────────────────────────

BUILD_SYSTEM_MARKERS = {
    "cmake": {
        "files": ["CMakeLists.txt", "CMakePresets.json"],
        "dirs": [],
    },
    "maven": {
        "files": ["pom.xml"],
        "dirs": [],
    },
    "npm": {
        "files": ["package.json"],
        "dirs": [],
    },
    "cargo": {
        "files": ["Cargo.toml"],
        "dirs": [],
    },
    "go": {
        "files": ["go.mod"],
        "dirs": [],
    },
    "meson": {
        "files": ["meson.build", "meson_options.txt"],
        "dirs": [],
    },
    "python": {
        "files": ["setup.py", "setup.cfg", "pyproject.toml"],
        "dirs": [],
    },
}


# ── CLI defaults ──────────────────────────────────────────────────────────

@dataclass
class Config:
    """Runtime configuration."""
    project_path: str = ""
    output_path: str = ""
    verbose: bool = False
    overwrite: bool = False

    # Discovery
    ignore_dirs: Set[str] = field(default_factory=lambda: DEFAULT_IGNORE_DIRS.copy())
    ignore_files: Set[str] = field(default_factory=lambda: DEFAULT_IGNORE_FILES.copy())
    max_discovery_depth: Optional[int] = None  # None = unlimited

    # Extractor options
    enable_cmake: bool = True
    enable_npm: bool = True
    enable_maven: bool = True
    enable_cargo: bool = True
    enable_go: bool = True
    enable_meson: bool = True
    enable_python: bool = True

    # Read-only by default (blueprint section 24)
    read_only: bool = True

    def validate(self):
        """Validate config - ensure paths are sensible."""
        if not self.project_path:
            raise ValueError("project_path is required")
        p = os.path.abspath(self.project_path)
        if not os.path.isdir(p):
            raise ValueError(f"Project path is not a directory: {p}")
