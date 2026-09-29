"""Path resolver for Extension System (no hardcode J:\\Agent_Ai).

Resolves AETHER root and Extension runtime directory in a portable way.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

ProjectPath = Union[str, "os.PathLike[str]"]

# src/agent_ai/extensions/paths.py -> parents[3] = repo root (AETHER_ROOT)
_AETHER_ROOT: Path = Path(__file__).resolve().parents[3]

#: Environment variable to override extensions dir or aether root.
ENV_AETHER_ROOT = "AETHER_ROOT"
ENV_EXTENSIONS_DIR = "AETHER_EXTENSIONS_DIR"


def get_aether_root(explicit: ProjectPath | None = None) -> Path:
    """Return AETHER root path.

    Priority:
        1. explicit argument
        2. env AETHER_ROOT
        3. repo root computed from this file location
    """
    if explicit is not None and str(explicit).strip():
        return Path(str(explicit).strip())
    env = os.environ.get(ENV_AETHER_ROOT, "")
    if isinstance(env, str) and env.strip():
        return Path(env.strip())
    return _AETHER_ROOT


def get_extensions_dir(
    aether_root: ProjectPath | None = None, explicit: ProjectPath | None = None
) -> Path:
    """Return extensions runtime directory.

    Priority:
        1. explicit
        2. env AETHER_EXTENSIONS_DIR
        3. <AETHER_ROOT>/Extension
    """
    if explicit is not None and str(explicit).strip():
        return Path(str(explicit).strip())
    env = os.environ.get(ENV_EXTENSIONS_DIR, "")
    if isinstance(env, str) and env.strip():
        return Path(env.strip())
    root = get_aether_root(aether_root)
    return root / "Extension"


def get_aether_data_dir(aether_root: ProjectPath | None = None) -> Path:
    """Return AETHER data directory: <AETHER_ROOT>/data (existing)."""
    return get_aether_root(aether_root) / "data"


def get_extensions_data_dir(aether_root: ProjectPath | None = None) -> Path:
    """Return runtime extension data directory: <AETHER_ROOT>/data/extensions."""
    return get_aether_data_dir(aether_root) / "extensions"


__all__ = [
    "get_aether_root",
    "get_extensions_dir",
    "get_aether_data_dir",
    "get_extensions_data_dir",
    "ENV_AETHER_ROOT",
    "ENV_EXTENSIONS_DIR",
    "_AETHER_ROOT",
]
