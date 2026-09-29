"""Task 05 — deterministic tests for the Playwright debug UI (no real browser).

The debug panel / viewers are pure view models over the Task 02-04 service
surface, so they are exercised against the in-memory Playwright double in
``_fake_playwright.py``. Also verifies the generic Extension UI contributions
(the panel + viewers) registered by the extension.
"""

import sys
import tempfile
import unittest
from pathlib import Path

EXT_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = EXT_DIR.parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _path in (str(PROJECT_ROOT), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from _fake_playwright import (  # noqa: E402
    FakeConsoleMessage,
    FakeElement,
    FakePageError,
    FakeRequest,
    FakeResponse,
    add_page,
    make_service,
)
from Extension.playwright.services.playwright_service import (  # noqa: E402
    DEBUG_PANEL_ACTIONS,
    PlaywrightService,
)
from Extension.playwright.tools.playwright_tools import (  # noqa: E402
    build_playwright_tools,
)


def build_context():
    """Build a fresh ExtensionContext + register the extension's UI."""
    from agent_ai.extensions.capabilities import CapabilityRegistry
    from agent_ai.extensions.context import ExtensionContext
    from agent_ai.extensions.manifest import load_manifest
    import importlib

    manifest = load_manifest(EXT_DIR / "manifest.json")
    registry = CapabilityRegistry()
    context = ExtensionContext(
        extension_root=EXT_DIR, manifest=manifest, capability_registry=registry
    )
    extension = importlib.import_module("Extension.playwright.extension").extension
    extension.register(context)
    return context, registry


# ===========================================================================
# UI contribution registration
# ===========================================================================
class TestDebugPanelRegistration(unittest.TestCase):
    def test_panel_and_viewers_registered(self):
        _context, registry = build_context()
        ui_ids = {record.id for record in registry.list("ui")}
        for suffix in (
            "debug_panel",
            "console_viewer",
            "network_viewer",
            "dom_viewer",
            "screenshot_viewer",
            "trace_viewer",
        ):
            self.assertIn("aether.playwright." + suffix, ui_ids)

    def test_every_ui_type_is_valid(self):
        from agent_ai.extensions.capabilities import UI_TYPES

        _context, registry = build_context()
        for record in registry.list("ui"):
            self.assertIn(record.metadata.get("type"), UI_TYPES)
            self.assertTrue(record.id.startswith("aether.playwright."))

    def test_panel_exposes_views_and_actions(self):
        _context, registry = build_context()
        panel = registry.get("ui", "aether.playwright.debug_panel")
        self.assertIsNotNone(panel)
        self.assertEqual(panel.metadata["type"], "panel")
        props = panel.metadata["props"]
        for view in ("sessions", "pages", "console", "network", "dom", "screenshot", "trace"):
            self.assertIn(view, props["views"])
        action_ids = {action["id"] for action in panel.metadata["actions"]}
        for required in (
            "aether.playwright.browser_debug_panel",  # Refresh
            "aether.playwright.page_close",  # Close Page
            "aether.playwright.browser_console",  # Clear Console
            "aether.playwright.browser_network",  # Clear Network
            "aether.playwright.browser_screenshot",  # Screenshot
            "aether.playwright.browser_trace_start",  # Start Trace
            "aether.playwright.browser_trace_stop",  # Stop Trace
        ):
            self.assertIn(required, action_ids)
        self.assertEqual(action_ids, {a["id"] for a in DEBUG_PANEL_ACTIONS})

    def test_registration_is_idempotent(self):
        _context, registry = build_context()
        # Registering again must not raise or duplicate.
        import importlib

        generic_registry = registry
        extension = importlib.import_module("Extension.playwright.extension").extension
        count_before = len(generic_registry.list("ui"))
        extension.register(_context)
        self.assertEqual(len(generic_registry.list("ui")), count_before)


# ===========================================================================
# Panel / view data
# ===========================================================================
class TestDebugPanelData(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.session_id = make_service(self._tmp.name)
        self.page_id, self.page = add_page(self.service, self.session_id)

    def _seed_evidence(self):
        self.page.emit("console", FakeConsoleMessage("log", "hello"))
        self.page.emit("console", FakeConsoleMessage("error", "boom"))
        request = FakeRequest("http://local.test/api/login", method="POST", resource_type="fetch")
        self.page.emit("request", request)
        self.page.emit("response", FakeResponse(request=request, status=500, ok=False))

    def test_panel_structure(self):
        self._seed_evidence()
        panel = self.service.debug_panel(session_id=self.session_id, page_id=self.page_id)
        self.assertEqual(panel["renderer"], "panel")
        self.assertEqual(panel["type"], "panel")
        data = panel["data"]
        self.assertEqual(data["session_id"], self.session_id)
        self.assertEqual(data["page_id"], self.page_id)
        self.assertEqual(data["selected_page_id"], self.page_id)
        for view in self.service.DEBUG_VIEWS:
            self.assertIn(view, data["views"])
        # sessions table
        self.assertEqual(data["sessions"]["renderer"], "table")
        self.assertEqual(data["sessions"]["data"]["rows"][0]["session_id"], self.session_id)
        # pages table marks the active page
        self.assertEqual(data["pages"]["renderer"], "table")
        self.assertTrue(data["pages"]["data"]["rows"][0]["active"])
        # console + network views are embedded
        self.assertEqual(data["console"]["type"], "log")
        self.assertEqual(data["network"]["renderer"], "table")

    def test_panel_without_session_has_no_side_effects(self):
        fresh = PlaywrightService(state_dir=str(self._tmp.name))
        panel = fresh.debug_panel()
        data = panel["data"]
        self.assertIsNone(data["session_id"])
        self.assertIsNone(data["page_id"])
        self.assertEqual(data["sessions"]["data"]["rows"], [])
        # no browser/session should have been launched
        self.assertEqual(fresh.status()["browser_count"], 0)

    def test_panel_screenshot_and_trace_are_opt_in(self):
        # A plain refresh must not capture anything.
        panel = self.service.debug_panel(session_id=self.session_id, page_id=self.page_id)
        self.assertNotIn("screenshot", panel["data"])
        # but the running trace state is always reported
        self.assertIn("trace_state", panel["data"])
        panel = self.service.debug_panel(
            session_id=self.session_id, page_id=self.page_id, screenshot={"full_page": True}
        )
        self.assertIn("screenshot", panel["data"])
        self.assertEqual(panel["data"]["screenshot"]["data"]["payload"]["scope"], "full_page")

    def test_sessions_and_pages_views(self):
        sessions = self.service.sessions_view()["data"]
        self.assertEqual(sessions["rows"][0]["session_id"], self.session_id)
        self.assertEqual(sessions["rows"][0]["page_count"], 1)
        pages = self.service.pages_view(self.session_id, selected_page_id=self.page_id)["data"]
        self.assertEqual(pages["selected"], self.page_id)
        self.assertEqual(pages["rows"][0]["page_id"], self.page_id)
        self.assertTrue(pages["rows"][0]["active"])
        self.assertFalse(pages["rows"][0]["closed"])


# ===========================================================================
# Console viewer
# ===========================================================================
class TestConsoleViewer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.session_id = make_service(self._tmp.name)
        self.page_id, self.page = add_page(self.service, self.session_id)

    def test_console_viewer_fields(self):
        self.page.emit("console", FakeConsoleMessage("log", "hello", line=3))
        self.page.emit("console", FakeConsoleMessage("warning", "careful"))
        self.page.emit("console", FakeConsoleMessage("error", "boom"))
        self.page.emit("pageerror", FakePageError(message="uncaught"))
        view = self.service.console_view(self.page_id)
        self.assertEqual(view["renderer"], "viewer")
        self.assertEqual(view["type"], "log")
        payload = view["data"]["payload"]
        self.assertEqual(payload["count"], 4)
        entry = payload["entries"][0]
        for key in ("timestamp", "type", "text", "location"):
            self.assertIn(key, entry)
        self.assertIn("error", payload["types"])

    def test_console_viewer_level_filter_and_clear(self):
        self.page.emit("console", FakeConsoleMessage("log", "hello"))
        self.page.emit("console", FakeConsoleMessage("error", "boom"))
        errors = self.service.console_view(self.page_id, type="error")["data"]["payload"]
        self.assertEqual(errors["count"], 1)
        cleared = self.service.console_view(self.page_id, clear=True)["data"]["payload"]
        self.assertGreaterEqual(cleared["cleared"], 1)
        self.assertEqual(self.service.console_view(self.page_id)["data"]["payload"]["count"], 0)


# ===========================================================================
# Network viewer
# ===========================================================================
class TestNetworkViewer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.session_id = make_service(self._tmp.name)
        self.page_id, self.page = add_page(self.service, self.session_id)

    def _emit(self, url, method, status, resource_type):
        request = FakeRequest(url, method=method, resource_type=resource_type)
        self.page.emit("request", request)
        self.page.emit("response", FakeResponse(request=request, status=status, ok=status < 400))

    def test_network_viewer_table(self):
        self._emit("http://local.test/api/login", "POST", 500, "fetch")
        self._emit("http://local.test/assets/app.js", "GET", 404, "script")
        self._emit("http://local.test/", "GET", 200, "document")
        view = self.service.network_view(self.page_id)
        self.assertEqual(view["renderer"], "table")
        data = view["data"]
        column_keys = [column["key"] for column in data["columns"]]
        for key in ("method", "url", "status", "resource_type", "failed"):
            self.assertIn(key, column_keys)
        rows = data["rows"]
        self.assertEqual(len(rows), 3)
        login = next(row for row in rows if row["url"].endswith("/api/login"))
        self.assertEqual(login["method"], "POST")
        self.assertEqual(login["status"], 500)
        self.assertTrue(login["failed"])
        ok = next(row for row in rows if row["status"] == 200)
        self.assertFalse(ok["failed"])

    def test_network_viewer_filters(self):
        self._emit("http://local.test/api/login", "POST", 500, "fetch")
        self._emit("http://local.test/", "GET", 200, "document")
        only_failed = self.service.network_view(self.page_id, failed=True)["data"]["rows"]
        self.assertEqual(len(only_failed), 1)
        self.assertEqual(only_failed[0]["status"], 500)
        by_method = self.service.network_view(self.page_id, method="POST")["data"]["rows"]
        self.assertEqual(len(by_method), 1)
        by_url = self.service.network_view(self.page_id, url="app.js")["data"]["rows"]
        self.assertEqual(by_url, [])


# ===========================================================================
# DOM viewer
# ===========================================================================
class TestDomViewer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.session_id = make_service(self._tmp.name)
        elements = [
            FakeElement("button", attrs={"id": "go"}, text="Go"),
        ]
        self.page_id, self.page = add_page(self.service, self.session_id, elements=elements)

    def test_dom_viewer_fields(self):
        view = self.service.dom_view(self.page_id, locator={"css": "#go"}, html=True)
        self.assertEqual(view["renderer"], "viewer")
        self.assertEqual(view["type"], "json")
        payload = view["data"]["payload"]
        self.assertEqual(payload["tag"], "button")
        self.assertEqual(payload["text"], "Go")
        self.assertEqual(payload["attributes"]["id"], "go")
        self.assertTrue(payload["visible"])
        self.assertTrue(payload["enabled"])
        self.assertIn("bounding_box", payload)
        self.assertIn("<button", payload["html"])


# ===========================================================================
# Screenshot + Trace artifacts
# ===========================================================================
class TestArtifactViewers(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.session_id = make_service(self._tmp.name)
        self.page_id, self.page = add_page(self.service, self.session_id)

    def test_screenshot_artifact(self):
        view = self.service.screenshot_view(self.page_id, full_page=True)
        self.assertEqual(view["renderer"], "viewer")
        self.assertEqual(view["type"], "image")
        payload = view["data"]["payload"]
        self.assertEqual(payload["scope"], "full_page")
        self.assertGreater(payload["width"], 0)
        self.assertGreater(payload["height"], 0)
        self.assertTrue(Path(payload["path"]).exists())
        artifact = view["artifact"]
        self.assertEqual(artifact["mime_type"], "image/png")
        self.assertEqual(artifact["name"], payload["filename"])
        self.assertGreater(artifact["size"], 0)

    def test_trace_artifact(self):
        self.service.trace_start(self.session_id)
        view = self.service.trace_view(self.session_id)
        self.assertEqual(view["renderer"], "viewer")
        self.assertEqual(view["type"], "file")
        payload = view["data"]["payload"]
        self.assertTrue(Path(payload["path"]).exists())
        self.assertEqual(payload["mime_type"], "application/zip")
        self.assertEqual(payload["open_action"], "aether.playwright.browser_trace_open")
        artifact = view["artifact"]
        self.assertEqual(artifact["mime_type"], "application/zip")
        self.assertGreater(artifact["size"], 0)
        # the artifact is a real (fake) zip payload
        self.assertEqual(Path(payload["path"]).read_bytes()[:2], b"PK")
        # the trace can be opened through a real action capability
        descriptor = self.service.trace_open(session_id=self.session_id)
        self.assertTrue(descriptor["exists"])
        self.assertEqual(descriptor["path"], payload["path"])
        self.assertEqual(descriptor["viewer"], "playwright-trace")
        self.assertIn("open_command", descriptor)

    def test_trace_status(self):
        self.assertEqual(self.service.trace_status(self.session_id)["active"], False)
        self.service.trace_start(self.session_id)
        status = self.service.trace_status(self.session_id)
        self.assertTrue(status["active"])
        self.service.trace_stop(self.session_id)
        self.assertFalse(self.service.trace_status(self.session_id)["active"])

    def test_trace_open_without_trace_raises(self):
        from Extension.playwright.services.playwright_service import TraceError

        with self.assertRaises(TraceError):
            self.service.trace_open(session_id=self.session_id)


# ===========================================================================
# Tool layer
# ===========================================================================
class TestDebugUIToolLayer(unittest.TestCase):
    def test_task05_tools_registered_and_namespaced(self):
        tools = {tool.name: tool for tool in build_playwright_tools(PlaywrightService())}
        for suffix in (
            "browser_debug_panel",
            "session_save_state",
            "session_restore_state",
            "session_state_list",
            "session_state_delete",
        ):
            self.assertIn("aether.playwright." + suffix, tools)
        for name in tools:
            self.assertTrue(name.startswith("aether.playwright."))

    def test_panel_tool_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, session_id = make_service(tmp)
            page_id, page = add_page(service, session_id)
            page.emit("console", FakeConsoleMessage("log", "hi"))
            tools = {tool.name: tool for tool in build_playwright_tools(service)}
            result = tools["aether.playwright.browser_debug_panel"].execute(
                session_id=session_id, page_id=page_id
            )
            self.assertEqual(result["type"], "panel")
            self.assertEqual(result["data"]["page_id"], page_id)

    def test_restore_tool_requires_name(self):
        from agent_ai.tools.base import ToolValidationError

        tools = {tool.name: tool for tool in build_playwright_tools(PlaywrightService())}
        with self.assertRaises(ToolValidationError):
            tools["aether.playwright.session_restore_state"].validate({})

    def test_trace_open_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, session_id = make_service(tmp)
            add_page(service, session_id)
            service.trace_start(session_id)
            service.trace_stop(session_id)
            tools = {tool.name: tool for tool in build_playwright_tools(service)}
            descriptor = tools["aether.playwright.browser_trace_open"].execute(
                session_id=session_id
            )
            self.assertTrue(descriptor["exists"])
            self.assertEqual(descriptor["mime_type"], "application/zip")


if __name__ == "__main__":
    unittest.main()
