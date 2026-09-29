"""Git Installer wrapper for Task 06.

Thin wrapper that delegates to ExtensionManager for branded API
`install(repository_url, ref=None)` etc. Also exposes standalone helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from agent_ai.extensions.manager import ExtensionManager
from agent_ai.extensions.capabilities import CapabilityRegistry
from agent_ai.extensions.lifecycle import get_lifecycle_store
from agent_ai.extensions.registry import ExtensionRegistry

# Re-export error types for convenience

def create_installer(
    registry: Optional[ExtensionRegistry] = None,
    capability_registry: Optional[CapabilityRegistry] = None,
    aether_root: Optional[Path] = None,
    extensions_dir: Optional[Path] = None,
    tool_registry: Optional[Any] = None,
    config_store: Optional[Any] = None,
    **kwargs: Any,
) -> ExtensionManager:
    return ExtensionManager(
        registry=registry,
        capability_registry=capability_registry,
        aether_root=aether_root,
        extensions_dir=extensions_dir,
        tool_registry=tool_registry,
        config_store=config_store,
        lifecycle_store=get_lifecycle_store(db_path=kwargs.get("db_path")),
        **{k: v for k, v in kwargs.items() if k not in ("db_path",)},
    )


__all__ = ["create_installer"]
