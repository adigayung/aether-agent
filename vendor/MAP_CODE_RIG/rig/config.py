"""
Configuration and constants for MAP_CODE_RIG.

Per blueprint section 5, 29 (CLI), and 33 (Security/Workspace Boundary).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional, Set


# ── Version ───────────────────────────────────────────────────────────────

VERSION = "1.0.0"
SCHEMA_VERSION = "rig-json/v1"


# ── Default ignore patterns ──────────────────────────────────────────────

# Directories and files that SHOULD be excluded from discovery.
# Per blueprint section 33: .env, credential files, private keys,
# and similar secret material must not be copied into evidence.
# node_modules, .git, .venv etc. are excluded because they are not
# part of the repository's own build/test architecture.
DEFAULT_IGNORE_DIRS: Set[str] = {
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


