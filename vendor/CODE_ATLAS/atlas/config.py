"""Configuration for CODE ATLAS."""

from __future__ import annotations

# Default output file name used when none is provided.
DEFAULT_OUTPUT_NAME = "atlas.json"

# Directories that are always skipped during discovery.
IGNORED_DIRS = frozenset(
    {
        ".git",
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
    }
)

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
BINARY_EXTENSIONS = frozenset(
    {
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
)
