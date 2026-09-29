"""ExtensionContext facade for AETHER Extension System.

Context is the API boundary between Extension and AETHER Core.
It exposes capability placeholders that future tasks will populate
without requiring redesign of base Extension API.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


class _Placeholder:
    """Placeholder for future capability (Task 02-08).

    Accessing not-yet-implemented capability should not crash Extension,
    but should be clearly identifiable. Placeholder is attribute-safe
    and returns None or raises on call with clear message if strict.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ExtensionContext.{self._name} placeholder (not yet implemented)>"

    def __bool__(self) -> bool:
        return False

    def __getattr__(self, item: str) -> Any:  # pragma: no cover
        raise AttributeError(
            f"ExtensionContext.{self._name}.{item} not yet implemented (Task 01 placeholder)"
        )


class ExtensionContext:
    """Facade / API boundary between Extension and AETHER Core.

    Task 03 provides real capability facades for all 10 types.
    Task 04 adds config + storage (real implementations, no placeholders).
    """

    def __init__(
        self,
        *,
        extension_root: Optional[Path] = None,
        manifest: Optional[Any] = None,
        aether_root: Optional[Path] = None,
        extensions_dir: Optional[Path] = None,
        capability_registry: Optional[Any] = None,
        tool_registry: Optional[Any] = None,
        config_store: Optional[Any] = None,
        project_root: Optional[Any] = None,
    ) -> None:
        self.extension_root: Optional[Path] = Path(extension_root) if extension_root is not None else None
        self.manifest = manifest
        self.extension_id: str = getattr(manifest, "id", "") if manifest is not None else ""

        if aether_root is not None:
            self.aether_root = Path(aether_root)
        else:
            from agent_ai.extensions.paths import get_aether_root

            self.aether_root = get_aether_root()

        if extensions_dir is not None:
            self.extensions_dir = Path(extensions_dir)
        else:
            from agent_ai.extensions.paths import get_extensions_dir

            self.extensions_dir = get_extensions_dir(self.aether_root)

        # Capability registry: use provided or global
        if capability_registry is not None:
            self._capability_registry = capability_registry
        else:
            from agent_ai.extensions.capabilities import global_capability_registry

            self._capability_registry = global_capability_registry

        self._tool_registry = tool_registry
        self._config_store = config_store
        self._project_root = Path(project_root) if project_root is not None and str(project_root).strip() else None

        # Build facades — when extension_id is missing (e.g. bare ExtensionContext() in tests), keep placeholders for backward compat
        if self.extension_id:
            from agent_ai.extensions.capabilities import (
                ConfigFacade,
                KnowledgeFacade,
                SkillsFacade,
                ToolsFacade,
                ServicesFacade,
                ProvidersFacade,
                ResourcesFacade,
                HooksFacade,
                CommandsFacade,
                UIFacade,
            )
            from agent_ai.extensions.storage import ExtensionStorage

            src = str(self.extension_root) if self.extension_root is not None else None

            self.tools = ToolsFacade(
                extension_id=self.extension_id,
                capability_registry=self._capability_registry,
                extension_root=self.extension_root,
                source=src,
                tool_registry=self._tool_registry,
            )
            self.skills = SkillsFacade(
                extension_id=self.extension_id,
                capability_registry=self._capability_registry,
                extension_root=self.extension_root,
                source=src,
            )
            self.knowledge = KnowledgeFacade(
                extension_id=self.extension_id,
                capability_registry=self._capability_registry,
                extension_root=self.extension_root,
                source=src,
            )
            # ConfigFacade with real value store (facade -> existing SQLite)
            # Pass config_store if provided, else it will lazily get global store
            try:
                from agent_ai.extensions.config import get_config_store
                _cs = config_store if config_store is not None else get_config_store()
            except Exception:
                _cs = config_store
            self.config = ConfigFacade(
                extension_id=self.extension_id,
                capability_registry=self._capability_registry,
                extension_root=self.extension_root,
                source=src,
                config_store=_cs,
                aether_root=self.aether_root,
            )
            self.services = ServicesFacade(
                extension_id=self.extension_id,
                capability_registry=self._capability_registry,
                extension_root=self.extension_root,
                source=src,
            )
            self.providers = ProvidersFacade(
                extension_id=self.extension_id,
                capability_registry=self._capability_registry,
                extension_root=self.extension_root,
                source=src,
            )
            self.resources = ResourcesFacade(
                extension_id=self.extension_id,
                capability_registry=self._capability_registry,
                extension_root=self.extension_root,
                source=src,
            )
            self.hooks = HooksFacade(
                extension_id=self.extension_id,
                capability_registry=self._capability_registry,
                extension_root=self.extension_root,
                source=src,
            )
            self.commands = CommandsFacade(
                extension_id=self.extension_id,
                capability_registry=self._capability_registry,
                extension_root=self.extension_root,
                source=src,
            )
            self.ui = UIFacade(
                extension_id=self.extension_id,
                capability_registry=self._capability_registry,
                extension_root=self.extension_root,
                source=src,
            )
            # Real storage (isolated per extension_id) — facade over filesystem data/extensions/
            self.storage: Any = ExtensionStorage(
                extension_id=self.extension_id,
                aether_root=self.aether_root,
                project_root=self._project_root,
            )
        else:
            # No extension identity -> keep placeholders (Task 01 backward compat)
            self.tools: Any = _Placeholder("tools")
            self.skills: Any = _Placeholder("skills")
            self.knowledge: Any = _Placeholder("knowledge")
            self.config: Any = _Placeholder("config")
            self.services: Any = _Placeholder("services")
            self.providers: Any = _Placeholder("providers")
            self.resources: Any = _Placeholder("resources")
            self.hooks: Any = _Placeholder("hooks")
            self.commands: Any = _Placeholder("commands")
            self.ui: Any = _Placeholder("ui")
            self.storage: Any = _Placeholder("storage")

        # Keep telemetry/permission as placeholders for future tasks
        self.telemetry: Any = _Placeholder("telemetry")
        self.permissions: Any = _Placeholder("permissions")
        self.artifacts: Any = _Placeholder("artifacts")
        # expose capability registry
        self.capability_registry = self._capability_registry

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ExtensionContext id={self.extension_id!r} root={self.extension_root!r}>"

    def is_placeholder(self, name: str) -> bool:
        val = getattr(self, name, None)
        return isinstance(val, _Placeholder)


__all__ = ["ExtensionContext", "_Placeholder"]
