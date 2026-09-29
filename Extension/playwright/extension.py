"""
Playwright Extension — browser lifecycle wiring (Task 02).

The extension is intentionally thin:

* ``register`` declares the config keys and the service capability id.
* ``enable`` lazily builds the :class:`PlaywrightService` (no browser process
  is started here) and registers the Playwright tool layer through the
  Extension API.
* ``disable`` shuts the service down and releases every browser/session/page.

Importing this module has **no side effects**: neither the Playwright engine
nor the service is instantiated at import time.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_ai.extensions import Extension

#: Directory containing this extension (robust for both package import and the
#: loader's standalone ``spec_from_file_location`` execution).
_HERE = Path(__file__).resolve().parent


def _load_local(rel_path: str, dotted_suffix: str) -> Any:
    """Import a sibling module of this extension.

    Works both when the extension is imported as a package and when the
    AETHER loader executes ``extension.py`` as a standalone module (where
    relative imports are unavailable). Falls back to file-path loading.
    """
    package = __package__ or ""
    if package:
        try:
            return importlib.import_module("." + dotted_suffix, package)
        except Exception:
            pass
    mod_name = "_aether_playwright_" + dotted_suffix.replace(".", "_")
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    target = _HERE / rel_path
    spec = importlib.util.spec_from_file_location(mod_name, str(target))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load extension module '{rel_path}'")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[attr-defined]
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    return module


class PlaywrightExtension(Extension):
    id: str = "aether.playwright"

    #: Namespaced capability id for the lifecycle service.
    SERVICE_CAPABILITY_ID = "aether.playwright.playwright_service"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # NOTE: kept ``None`` until ``enable``/``get_service`` so that merely
        # registering the extension instantiates nothing.
        self._service = None
        self._context = None
        self._tools: List[Any] = []
        self._tools_registered = False

    # -----------------------------------------------------------------
    # Lazy service ownership
    # -----------------------------------------------------------------
    def _read_config(self, context) -> Dict[str, Any]:
        """Build the effective service config from the Extension config store."""
        config: Dict[str, Any] = {"browser": "chromium", "headless": True, "timeout": 30}
        getter = getattr(getattr(context, "config", None), "get", None)
        if callable(getter):
            for key, default in list(config.items()):
                try:
                    value = getter(key)
                except Exception:
                    value = None
                if value is not None:
                    config[key] = value
        return config

    def get_service(self, context=None):
        """Return the PlaywrightService, creating it lazily on first use."""
        if self._service is None:
            ctx = context or self._context
            module = _load_local(
                "services/playwright_service.py", "services.playwright_service"
            )
            config = self._read_config(ctx) if ctx is not None else {}
            # Task 05: hand the service the Extension storage facade so browser
            # state persists through the official Extension storage API.
            storage = getattr(ctx, "storage", None) if ctx is not None else None
            try:
                self._service = module.PlaywrightService(
                    storage=storage, extension_id=self.extension_id, **config
                )
            except TypeError:
                # Older service signature without persistence params.
                self._service = module.PlaywrightService(**config)
        return self._service

    @property
    def service(self):
        """The cached PlaywrightService instance (or ``None`` before enable)."""
        return self._service

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------
    def register(self, context):
        self._context = context
        # Register basic config entries for browser selection, headless mode, timeout
        # Using context.config facade (Task 04 capability)
        try:
            context.config.register(
                key="browser",
                type="enum",
                choices=["chromium", "firefox", "webkit"],
                default="chromium",
                description="Browser engine for Playwright",
            )
        except Exception:
            pass
        try:
            context.config.register(
                key="headless",
                type="boolean",
                default=True,
                description="Run browsers in headless mode",
            )
        except Exception:
            pass
        try:
            context.config.register(
                key="timeout",
                type="integer",
                default=30,
                description="Default timeout for Playwright actions (seconds)",
            )
        except Exception:
            pass
        # Service capability (Task 01 foundation). The live service instance is
        # attached in ``enable`` (lazy: nothing is instantiated here).
        try:
            context.services.register(
                self.SERVICE_CAPABILITY_ID,
                type="service",
                description="Service managing Playwright browser sessions",
            )
        except Exception:
            pass
        # Task 05: generic debug UI contributions (panel + viewers).
        self._register_ui(context)

    # -----------------------------------------------------------------
    # UI contributions (Task 05)
    # -----------------------------------------------------------------
    #: Debug panel actions — each references an existing capability/tool.
    PANEL_ACTIONS = [
        {"id": "aether.playwright.browser_debug_panel", "title": "Refresh"},
        {"id": "aether.playwright.page_close", "title": "Close Page"},
        {"id": "aether.playwright.browser_console", "title": "Clear Console", "arguments": {"clear": True}},
        {"id": "aether.playwright.browser_network", "title": "Clear Network", "arguments": {"clear": True}},
        {"id": "aether.playwright.browser_screenshot", "title": "Screenshot", "arguments": {"full_page": True}},
        {"id": "aether.playwright.browser_trace_start", "title": "Start Trace"},
        {"id": "aether.playwright.browser_trace_stop", "title": "Stop Trace"},
        {"id": "aether.playwright.session_save_state", "title": "Save State"},
        {"id": "aether.playwright.session_restore_state", "title": "Restore State"},
    ]

    def _register_ui(self, context) -> None:
        """Register the generic Playwright debug UI contributions (idempotent)."""
        ui = getattr(context, "ui", None)
        register = getattr(ui, "register", None)
        if not callable(register):
            return
        ext = self.extension_id
        prefix = ext + "."
        contributions = [
            {
                "id": prefix + "debug_panel",
                "type": "panel",
                "title": "Playwright Debug Panel",
                "description": (
                    "Generic browser debug surface: sessions, pages/tabs, console, "
                    "network and DOM inspection for a chosen session_id / page_id."
                ),
                "props": {
                    "placement": "right",
                    "views": ["sessions", "pages", "console", "network", "dom", "screenshot", "trace"],
                    "requires": ["session_id", "page_id"],
                    "data_tool": "aether.playwright.browser_debug_panel",
                    "session_scoped": True,
                    "page_scoped": True,
                },
                "actions": list(self.PANEL_ACTIONS),
            },
            {
                "id": prefix + "console_viewer",
                "type": "viewer",
                "title": "Console Viewer",
                "description": "Structured console/page-error events with level filter and clear.",
                "props": {
                    "viewer_type": "log",
                    "data_tool": "aether.playwright.browser_console",
                    "level_types": ["log", "debug", "info", "warning", "error"],
                    "filterable": True,
                    "clearable": True,
                },
                "actions": [
                    {"id": "aether.playwright.browser_console", "title": "Refresh"},
                    {"id": "aether.playwright.browser_console", "title": "Clear", "arguments": {"clear": True}},
                ],
            },
            {
                "id": prefix + "network_viewer",
                "type": "table",
                "title": "Network Viewer",
                "description": "Request/response records (method, url, status, resource type, failed).",
                "props": {
                    "data_tool": "aether.playwright.browser_network",
                    "filters": ["url", "method", "status"],
                    "columns": [
                        {"key": "method", "title": "Method"},
                        {"key": "url", "title": "URL"},
                        {"key": "status", "title": "Status"},
                        {"key": "resource_type", "title": "Type"},
                        {"key": "failed", "title": "Failed"},
                    ],
                },
                "actions": [
                    {"id": "aether.playwright.browser_network", "title": "Refresh"},
                    {"id": "aether.playwright.browser_network", "title": "Clear", "arguments": {"clear": True}},
                ],
            },
            {
                "id": prefix + "dom_viewer",
                "type": "viewer",
                "title": "DOM Inspector",
                "description": "Inspect one element by ref/locator (tag, text, attributes, box, HTML).",
                "props": {
                    "viewer_type": "json",
                    "data_tool": "aether.playwright.browser_dom_inspect",
                },
                "actions": [
                    {"id": "aether.playwright.browser_snapshot", "title": "Snapshot"},
                ],
            },
            {
                "id": prefix + "screenshot_viewer",
                "type": "viewer",
                "title": "Screenshot Viewer",
                "description": "Preview screenshots captured by browser_screenshot.",
                "props": {
                    "viewer_type": "image",
                    "mime_type": "image/png",
                    "data_tool": "aether.playwright.browser_screenshot",
                },
                "actions": [
                    {"id": "aether.playwright.browser_screenshot", "title": "Capture", "arguments": {"full_page": True}},
                ],
            },
            {
                "id": prefix + "trace_viewer",
                "type": "viewer",
                "title": "Trace Viewer",
                "description": "Open the .zip artifact produced by browser_trace_stop.",
                "props": {
                    "viewer_type": "file",
                    "mime_type": "application/zip",
                    "data_tool": "aether.playwright.browser_trace_stop",
                    "open_action": "aether.playwright.browser_trace_open",
                },
                "actions": [
                    {"id": "aether.playwright.browser_trace_start", "title": "Start Trace"},
                    {"id": "aether.playwright.browser_trace_stop", "title": "Stop Trace"},
                    {"id": "aether.playwright.browser_trace_open", "title": "Open Trace"},
                ],
            },
        ]
        exists = getattr(ui, "exists", None)
        for contribution in contributions:
            try:
                if callable(exists) and exists(contribution["id"]):
                    continue
                register(**contribution)
            except Exception:
                pass

    def _register_tools(self, context) -> None:
        """Register the Playwright tool layer via the Extension API (once)."""
        if self._tools_registered:
            return
        try:
            module = _load_local("tools/playwright_tools.py", "tools.playwright_tools")
            tools = module.build_playwright_tools(self.get_service(context))
        except Exception:
            return
        registered: List[Any] = []
        for tool in tools:
            try:
                context.tools.register(tool)
                registered.append(tool)
            except Exception:
                pass
        self._tools = registered
        self._tools_registered = True

    def enable(self, context):
        self._context = context
        # Build the service (still no browser launched) and expose the live
        # instance through the service capability metadata.
        try:
            service = self.get_service(context)
            rec = context.services.get(self.SERVICE_CAPABILITY_ID)
            if rec is not None:
                rec.metadata["service_instance"] = service
        except Exception:
            pass
        # Wire the LLM-facing tool layer (no browser launched here either).
        self._register_tools(context)

    def disable(self, context):
        # Shut down every browser/session/page and stop the runtime.
        if self._service is not None:
            try:
                self._service.shutdown()
            except Exception:
                pass
        self._tools_registered = False


extension = PlaywrightExtension()
