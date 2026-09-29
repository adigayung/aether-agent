"""Extension Startup Loader — discovers, validates, imports, registers all Extensions (Task 02+03).

Flow:
    discover (filesystem + entry-points, via Task 01 contract)
        -> validate manifest (via Task 01 validator)
        -> import/load (load_extension_from_dir / entry-points)
        -> create Extension instance
        -> register Extension (extension.register(context)) with capability facades
        -> store in ExtensionRegistry
        -> read enabled state (lifecycle) -> activate enabled extensions (on_enable)

All Extensions in <AETHER_ROOT>/Extension/ are processed at startup.
AETHER must not crash if one Extension fails; failures are recorded
and other Extensions remain available.
Disabled extensions remain discoverable/loaded/registered but capabilities not active.

Task 06 integration:
  - reads enabled state from lifecycle store (defaults enabled for backward compat)
  - after register, calls on_enable for enabled extensions (lightweight)
  - deactivates capabilities for disabled extensions (isolation)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_ai.extensions.context import ExtensionContext
from agent_ai.extensions.discovery import (
    ExtensionEntry,
    ExtensionLoadError,
    discover_extensions,
    discover_extensions_from_dir,
    load_extension_from_dir,
)
from agent_ai.extensions.manifest import (
    DuplicateExtensionError,
    ManifestError,
    ManifestValidationError,
)
from agent_ai.extensions.paths import get_aether_root, get_extensions_dir
from agent_ai.extensions.registry import ExtensionRecord, ExtensionRegistry


@dataclass
class ExtensionLoadResult:
    """Result of loading one extension (success or failure)."""

    id: Optional[str]
    source: str
    status: str  # "loaded" or "failed"
    error: Optional[str] = None
    record: Optional[ExtensionRecord] = None


@dataclass
class StartupLoadResult:
    """Aggregate result of startup loader."""

    loaded: List[ExtensionRecord] = field(default_factory=list)
    failed: List[ExtensionLoadResult] = field(default_factory=list)

    @property
    def count_loaded(self) -> int:
        return len(self.loaded)

    @property
    def count_failed(self) -> int:
        return len(self.failed)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "loaded": [{"id": r.id, "name": r.name, "version": r.version, "source": r.source} for r in self.loaded],
            "failed": [{"id": f.id, "source": f.source, "error": f.error} for f in self.failed],
            "count_loaded": self.count_loaded,
            "count_failed": self.count_failed,
        }


class ExtensionLoader:
    """Startup loader that populates an ExtensionRegistry.

    Args:
        registry: target registry (created if None)
        aether_root: override AETHER root (uses resolver otherwise)
        extensions_dir: override extensions dir (uses resolver otherwise)
        entry_point_group: entry-point group for installed packages
        enable_entry_points: whether to load via entry-points (default True)
        capability_registry: shared capability registry (created if None)
        tool_registry: ToolRegistry to integrate tool capabilities (optional)
    """

    def __init__(
        self,
        registry: Optional[ExtensionRegistry] = None,
        aether_root: Optional[Path] = None,
        extensions_dir: Optional[Path] = None,
        entry_point_group: str = "aether.extensions",
        enable_entry_points: bool = True,
        capability_registry: Optional[Any] = None,
        tool_registry: Optional[Any] = None,
        config_store: Optional[Any] = None,
        project_root: Optional[Any] = None,
        lifecycle_store: Optional[Any] = None,
    ) -> None:
        self.registry = registry if registry is not None else ExtensionRegistry()
        self._aether_root = aether_root
        self._extensions_dir = extensions_dir
        self.entry_point_group = entry_point_group
        self.enable_entry_points = enable_entry_points
        # Capability registry — shared across all extensions in this loader
        if capability_registry is not None:
            self.capability_registry = capability_registry
        else:
            from agent_ai.extensions.capabilities import CapabilityRegistry as _CR

            self.capability_registry = _CR()
        self.tool_registry = tool_registry
        self.config_store = config_store
        self.project_root = Path(project_root) if project_root is not None and str(project_root).strip() else None
        self._lifecycle_store = lifecycle_store  # lazy get if None

    def _resolve_extensions_dir(self) -> Path:
        if self._extensions_dir is not None:
            return Path(self._extensions_dir)
        root = self._aether_root if self._aether_root is not None else get_aether_root()
        return get_extensions_dir(root)

    def _make_context(self, manifest: Any, extension_root: Optional[Path], extensions_dir: Path) -> ExtensionContext:
        return ExtensionContext(
            extension_root=extension_root,
            manifest=manifest,
            aether_root=get_aether_root(self._aether_root) if self._aether_root is not None else get_aether_root(),
            extensions_dir=extensions_dir,
            capability_registry=self.capability_registry,
            tool_registry=self.tool_registry,
            config_store=self.config_store,
            project_root=self.project_root,
        )

    def _rollback_capabilities(self, extension_id: str) -> None:
        """Remove any capabilities already registered for extension_id after failed register."""
        try:
            # remove all capabilities for this extension
            to_remove = self.capability_registry.list_by_extension(extension_id)
            for rec in list(to_remove):
                self.capability_registry.remove(rec.type, rec.id)
            # Also rollback from tool_registry if available (if tool name equals capability id)
            if self.tool_registry is not None:
                for rec in to_remove:
                    if rec.type == "tool":
                        try:
                            # ToolRegistry has no remove; check if we can delete from internal dict
                            # Try unregister/remove methods if exist, otherwise direct dict
                            if hasattr(self.tool_registry, "unregister"):
                                self.tool_registry.unregister(rec.id)  # type: ignore[attr-defined]
                            elif hasattr(self.tool_registry, "remove"):
                                self.tool_registry.remove(rec.id)  # type: ignore[attr-defined]
                            elif hasattr(self.tool_registry, "_tools"):
                                self.tool_registry._tools.pop(rec.id.lower(), None)  # type: ignore[attr-defined]
                        except Exception:
                            pass
        except Exception:
            pass

    def _get_lifecycle_store(self):  # type: ignore[no-untyped-def]
        if self._lifecycle_store is not None:
            return self._lifecycle_store
        try:
            from agent_ai.extensions.lifecycle import get_lifecycle_store as _gls

            return _gls()
        except Exception:
            return None

    def _activate_enabled_or_disable(self, result: StartupLoadResult) -> None:
        """Post-load: read enabled state, call on_enable for enabled, deactivate disabled."""
        store = self._get_lifecycle_store()
        if store is None:
            return
        extensions_dir = self._resolve_extensions_dir()
        for rec in list(result.loaded):
            ext_id = rec.id
            # ensure lifecycle record exists (backward compat: default enabled)
            try:
                if store.get_status_row(ext_id) is None:
                    store.ensure_installed(ext_id, version=getattr(rec.manifest, "version", ""))
                    # if record already existed via previous install, ensure remains enabled true default
                enabled = store.is_enabled(ext_id)
            except Exception:
                enabled = True
            try:
                if not enabled:
                    # Deactivate capabilities for disabled extension
                    try:
                        for cap in self.capability_registry.list_by_extension(ext_id):
                            self.capability_registry.set_enabled(cap.type, cap.id, False)
                    except Exception:
                        pass
                    # Also disable tools from ToolRegistry
                    if self.tool_registry is not None:
                        try:
                            for cap in self.capability_registry.list_by_extension(ext_id):
                                if cap.type == "tool" and hasattr(self.tool_registry, "_tools"):
                                    try:
                                        self.tool_registry._tools.pop(cap.id.lower(), None)  # type: ignore[attr-defined]
                                        self.tool_registry._tools.pop(cap.id, None)  # type: ignore[attr-defined]
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                    rec.status = "disabled"
                    continue
                # Enabled -> call on_enable (optional hook) safely
                try:
                    ctx = self._make_context(rec.manifest, rec.root or (Path(rec.source) if Path(rec.source).exists() else None), extensions_dir)
                    hook = getattr(rec.extension, "on_enable", None)
                    if hook is None:
                        hook = getattr(rec.extension, "enable", None)
                    if callable(hook):
                        hook(ctx)
                    # keep registry status as "loaded" for backward compat (Task 02 expects "loaded")
                    # lifecycle store is authority for enabled/disabled
                    if rec.status not in ("disabled", "failed"):
                        rec.status = "loaded"
                    try:
                        store.set_status(ext_id, "enabled", enabled=True, version=getattr(rec.manifest, "version", ""))
                    except Exception:
                        pass
                except Exception as exc:
                    # on_enable failed -> mark failed/disabled, disable capabilities
                    try:
                        for cap in self.capability_registry.list_by_extension(ext_id):
                            self.capability_registry.set_enabled(cap.type, cap.id, False)
                    except Exception:
                        pass
                    if self.tool_registry is not None:
                        try:
                            for cap in self.capability_registry.list_by_extension(ext_id):
                                if cap.type == "tool" and hasattr(self.tool_registry, "_tools"):
                                    try:
                                        self.tool_registry._tools.pop(cap.id.lower(), None)  # type: ignore[attr-defined]
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                    try:
                        store.set_status(ext_id, "failed", enabled=False, error=str(exc))
                        store.log_activity(ext_id, "extension_failed", str(exc))
                    except Exception:
                        pass
                    rec.status = "failed"
                    rec.error = str(exc)
            except Exception:
                pass

    # -- main entry -----------------------------------------------------

    def load_all(self) -> StartupLoadResult:
        result = StartupLoadResult()
        extensions_dir = self._resolve_extensions_dir()
        candidates: List[Path] = []
        if extensions_dir.is_dir():
            for child in sorted(extensions_dir.iterdir(), key=lambda p: p.name.lower()):
                if child.is_dir() and (child / "manifest.json").is_file():
                    candidates.append(child)

        for folder in candidates:
            manifest_id: Optional[str] = None
            try:
                from agent_ai.extensions.manifest import load_manifest as _load_manifest

                try:
                    pm = _load_manifest(str(folder / "manifest.json"))
                    manifest_id = pm.id
                except Exception:
                    manifest_id = None
            except Exception:
                manifest_id = None

            try:
                entry: ExtensionEntry = load_extension_from_dir(folder)
            except (ManifestValidationError, ManifestError, ExtensionLoadError) as exc:
                msg = f'Failed to load extension "{manifest_id or folder.name}": {exc}'
                result.failed.append(ExtensionLoadResult(id=manifest_id, source=str(folder), status="failed", error=msg))
                self.registry.add_failure(str(folder), msg, manifest_id)
                continue
            except Exception as exc:  # noqa: BLE001
                msg = f'Failed to load extension "{manifest_id or folder.name}": {exc}'
                result.failed.append(ExtensionLoadResult(id=manifest_id, source=str(folder), status="failed", error=msg))
                self.registry.add_failure(str(folder), msg, manifest_id)
                continue

            # Startup API compatibility validation: an Extension that targets an
            # unsupported Extension API must not be activated. Failure is
            # isolated to that Extension (others keep loading).
            try:
                from agent_ai.extensions.api_version import SUPPORTED_API_VERSIONS

                if entry.manifest.api_version not in SUPPORTED_API_VERSIONS:
                    raise ManifestValidationError(
                        f'Extension "{entry.manifest.id}" requires API version '
                        f'{entry.manifest.api_version}, AETHER supports '
                        f'{", ".join(SUPPORTED_API_VERSIONS)}'
                    )
            except ManifestValidationError as exc:
                msg = f'Failed to load extension "{entry.manifest.id}": {exc}'
                result.failed.append(ExtensionLoadResult(id=entry.manifest.id, source=entry.source, status="failed", error=msg))
                self.registry.add_failure(entry.source, msg, entry.manifest.id)
                continue

            if self.registry.exists(entry.manifest.id):
                existing = self.registry.get(entry.manifest.id)
                msg = f'Duplicate Extension id "{entry.manifest.id}" detected (sources: "{existing.source}" and "{entry.source}")'
                result.failed.append(ExtensionLoadResult(id=entry.manifest.id, source=entry.source, status="failed", error=msg))
                self.registry.add_failure(entry.source, msg, entry.manifest.id)
                continue

            try:
                ctx = self._make_context(entry.manifest, Path(entry.source) if Path(entry.source).exists() else None, extensions_dir)
                if hasattr(entry.extension, "register"):
                    entry.extension.register(ctx)
            except Exception as exc:  # noqa: BLE001
                # error isolation: log and continue, rollback partial capabilities
                msg = f'Failed to register extension "{entry.manifest.id}": {exc}'
                result.failed.append(ExtensionLoadResult(id=entry.manifest.id, source=entry.source, status="failed", error=msg))
                self.registry.add_failure(entry.source, msg, entry.manifest.id)
                # rollback any capabilities that may have been partially registered
                try:
                    self._rollback_capabilities(entry.manifest.id)
                except Exception:
                    pass
                continue

            try:
                record = self.registry.register(entry)
                result.loaded.append(record)
            except DuplicateExtensionError as exc:
                msg = f'Duplicate Extension id "{entry.manifest.id}" detected: {exc}'
                result.failed.append(ExtensionLoadResult(id=entry.manifest.id, source=entry.source, status="failed", error=msg))
                self.registry.add_failure(entry.source, msg, entry.manifest.id)
                try:
                    self._rollback_capabilities(entry.manifest.id)
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001
                msg = f'Failed to register extension "{entry.manifest.id}": {exc}'
                result.failed.append(ExtensionLoadResult(id=entry.manifest.id, source=entry.source, status="failed", error=msg))
                self.registry.add_failure(entry.source, msg, entry.manifest.id)
                try:
                    self._rollback_capabilities(entry.manifest.id)
                except Exception:
                    pass

        if self.enable_entry_points:
            try:
                ep_entries: List[ExtensionEntry] = discover_extensions(entry_point_group=self.entry_point_group)
            except Exception:
                ep_entries = []
            for entry in ep_entries:
                if self.registry.exists(entry.manifest.id):
                    existing = self.registry.get(entry.manifest.id)
                    msg = f'Duplicate Extension id "{entry.manifest.id}" detected (sources: "{existing.source}" and "{entry.source}")'
                    result.failed.append(ExtensionLoadResult(id=entry.manifest.id, source=entry.source, status="failed", error=msg))
                    self.registry.add_failure(entry.source, msg, entry.manifest.id)
                    continue
                try:
                    ctx = self._make_context(entry.manifest, None, extensions_dir)
                    if hasattr(entry.extension, "register"):
                        entry.extension.register(ctx)
                except Exception as exc:  # noqa: BLE001
                    msg = f'Failed to register extension "{entry.manifest.id}": {exc}'
                    result.failed.append(ExtensionLoadResult(id=entry.manifest.id, source=entry.source, status="failed", error=msg))
                    self.registry.add_failure(entry.source, msg, entry.manifest.id)
                    try:
                        self._rollback_capabilities(entry.manifest.id)
                    except Exception:
                        pass
                    continue
                try:
                    record = self.registry.register(entry)
                    result.loaded.append(record)
                except DuplicateExtensionError as exc:
                    msg = f'Duplicate Extension id "{entry.manifest.id}" detected: {exc}'
                    result.failed.append(ExtensionLoadResult(id=entry.manifest.id, source=entry.source, status="failed", error=msg))
                    self.registry.add_failure(entry.source, msg, entry.manifest.id)
                    try:
                        self._rollback_capabilities(entry.manifest.id)
                    except Exception:
                        pass
                except Exception as exc:  # noqa: BLE001
                    msg = f'Failed to register extension "{entry.manifest.id}": {exc}'
                    result.failed.append(ExtensionLoadResult(id=entry.manifest.id, source=entry.source, status="failed", error=msg))
                    self.registry.add_failure(entry.source, msg, entry.manifest.id)
                    try:
                        self._rollback_capabilities(entry.manifest.id)
                    except Exception:
                        pass

        # Task 06: apply enabled/disabled state (startup lifecycle)
        try:
            self._activate_enabled_or_disable(result)
        except Exception:
            pass
        return result

    def startup(self) -> StartupLoadResult:
        return self.load_all()


def load_all_extensions(
    registry: Optional[ExtensionRegistry] = None,
    aether_root: Optional[Path] = None,
    extensions_dir: Optional[Path] = None,
    entry_point_group: str = "aether.extensions",
    enable_entry_points: bool = True,
    capability_registry: Optional[Any] = None,
    tool_registry: Optional[Any] = None,
    config_store: Optional[Any] = None,
    project_root: Optional[Any] = None,
    lifecycle_store: Optional[Any] = None,
) -> tuple[ExtensionRegistry, StartupLoadResult]:
    reg = registry or ExtensionRegistry()
    loader = ExtensionLoader(
        registry=reg,
        aether_root=aether_root,
        extensions_dir=extensions_dir,
        entry_point_group=entry_point_group,
        enable_entry_points=enable_entry_points,
        capability_registry=capability_registry,
        tool_registry=tool_registry,
        config_store=config_store,
        project_root=project_root,
        lifecycle_store=lifecycle_store,
    )
    res = loader.load_all()
    return reg, res


__all__ = ["ExtensionLoader", "ExtensionLoadResult", "StartupLoadResult", "load_all_extensions"]
