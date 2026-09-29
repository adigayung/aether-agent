"""Extension base contract for AETHER Extension System.

Provides minimal lifecycle contract: register/enable/disable with no-op defaults.
Extension identity and manifest are available via context / properties.
"""

from __future__ import annotations

from typing import Any, Optional

from agent_ai.extensions.context import ExtensionContext
from agent_ai.extensions.manifest import Manifest


class Extension:
    """Base class for AETHER Extensions.

    Minimal contract for Task 01:
        - identity (via manifest.id)
        - manifest
        - lifecycle: register / enable / disable (with no-op defaults)
        - load is implicit (import); future discovery handles it

    Subclasses should override register() to hook into Context capabilities
    (when implemented in Task 03+). Lifecycle methods may be no-ops.

    Attributes:
        manifest: optional Manifest; filled by loader/discovery (Task 02) or
                  manually for testing. When absent, id defaults to "".
    """

    #: Override in subclass if you want to declare identity inline (fallback).
    #: Preferred identity is manifest.json -> id.
    id: str = ""

    def __init__(self, manifest: Optional[Manifest] = None) -> None:
        self._manifest: Optional[Manifest] = manifest

    @property
    def manifest(self) -> Optional[Manifest]:
        return self._manifest

    @manifest.setter
    def manifest(self, value: Optional[Manifest]) -> None:
        self._manifest = value

    @property
    def extension_id(self) -> str:
        """Identity of extension. Prefers manifest.id over class id."""
        if self._manifest is not None and getattr(self._manifest, "id", None):
            return self._manifest.id
        return getattr(self, "id", "") or self.__class__.__name__

    # -- Lifecycle (all no-op defaults; do not require override) --

    def register(self, context: ExtensionContext) -> None:
        """Called during register phase. Override to register capabilities."""
        return None

    def enable(self, context: ExtensionContext) -> None:
        """Called when extension is enabled."""
        return None

    def disable(self, context: ExtensionContext) -> None:
        """Called when extension is disabled."""
        return None

    # Task 06 aliases (on_enable / on_disable as optional hooks)
    def on_enable(self, context: ExtensionContext) -> None:
        """Called when extension is enabled (alias for enable, optional hook)."""
        # Delegate to enable() if overridden
        if type(self).enable is not Extension.enable:  # type: ignore[comparison-overlap]
            return self.enable(context)
        return None

    def on_disable(self, context: ExtensionContext) -> None:
        """Called when extension is disabled (alias for disable, optional hook)."""
        if type(self).disable is not Extension.disable:  # type: ignore[comparison-overlap]
            return self.disable(context)
        return None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.__class__.__name__} id={self.extension_id!r}>"


__all__ = ["Extension"]
