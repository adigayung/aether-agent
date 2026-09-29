"""Task 05 — real-browser integration tests for the debug UI + persistence.

Drives the *real* Playwright engine against a local, fully routed fixture (no
internet: every request is fulfilled by ``page.route``) and proves the full
Task 05 flow::

    launch -> session -> page -> debug panel data -> screenshot -> trace
           -> save state -> restart -> restore state

The whole class is skipped when no Playwright browser can be launched.
"""

import sys
import tempfile
import unittest
from pathlib import Path

EXT_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = EXT_DIR.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Pre-import the real Playwright library before the extension directory may
# shadow the ``playwright`` package name.
try:  # pragma: no cover - environment dependent
    from playwright.sync_api import sync_playwright as _SYNC_PLAYWRIGHT
except Exception:  # pragma: no cover
    _SYNC_PLAYWRIGHT = None

from Extension.playwright.services.playwright_service import (  # noqa: E402
    PlaywrightService,
)
from Extension.playwright.tools.playwright_tools import (  # noqa: E402
    build_playwright_tools,
)


FIXTURE_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Debug UI Demo</title></head>
<body>
  <h1 id="title">Debug UI Demo</h1>
  <button id="go" type="button">Go</button>
  <script>
    console.log('hello from page');
    console.warn('careful');
    console.error('boom');
    fetch('/api/login', { method: 'POST' }).then(function (r) {
      console.log('login status ' + r.status);
    });
    fetch('/assets/app.js').then(function (r) {
      console.log('asset status ' + r.status);
    });
  </script>
</body>
</html>
"""

BASE_URL = "http://local.test/index.html"
ORIGIN = "http://local.test"

_AVAILABLE = None


def _real_browser_available() -> bool:
    global _AVAILABLE
    if _AVAILABLE is None:
        _AVAILABLE = False
        if _SYNC_PLAYWRIGHT is not None:
            runtime = None
            try:
                runtime = _SYNC_PLAYWRIGHT().start()
                browser = runtime.chromium.launch(headless=True)
                browser.close()
                _AVAILABLE = True
            except Exception:
                _AVAILABLE = False
            finally:
                if runtime is not None:
                    try:
                        runtime.stop()
                    except Exception:
                        pass
    return _AVAILABLE


class TestDebugUIReal(unittest.TestCase):
    """End-to-end Task 05 behaviour on a real Chromium browser."""

    @classmethod
    def setUpClass(cls):
        if not _real_browser_available():
            raise unittest.SkipTest("Playwright browser is not available")
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        (root / "artifacts").mkdir(exist_ok=True)
        cls.state_dir = root / "state"
        cls.state_dir.mkdir(exist_ok=True)
        cls.artifact_dir = root / "artifacts"
        cls.service = PlaywrightService(
            headless=True,
            timeout=20,
            artifact_dir=str(cls.artifact_dir),
            state_dir=str(cls.state_dir),
            runtime_factory=lambda: _SYNC_PLAYWRIGHT().start(),
        )
        cls.browser_id = cls.service.launch_browser()
        cls.session_id = cls.service.create_session(cls.browser_id)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.service.shutdown()
        finally:
            cls._tmp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _route(route):
        url = route.request.url
        if url.endswith("/api/login"):
            route.fulfill(status=500, content_type="application/json", body='{"error": "invalid"}')
        elif url.endswith("/assets/app.js"):
            route.fulfill(status=404, content_type="text/javascript", body="not found")
        else:
            route.fulfill(status=200, content_type="text/html", body=FIXTURE_HTML)

    def _open_page(self):
        page_id = self.service.new_page(self.session_id)
        page = self.service.page_object(page_id)
        page.route("**/*", self._route)
        self.service.navigate(page_id, BASE_URL)
        self.service.wait(page_id, condition="load", load_state="networkidle")
        return page_id, page

    # ------------------------------------------------------------------
    # Debug panel data
    # ------------------------------------------------------------------
    def test_debug_panel_data(self):
        page_id, _page = self._open_page()
        try:
            panel = self.service.debug_panel(session_id=self.session_id, page_id=page_id)
            data = panel["data"]
            self.assertEqual(panel["type"], "panel")
            self.assertEqual(data["session_id"], self.session_id)
            self.assertEqual(data["page_id"], page_id)

            session_ids = [row["session_id"] for row in data["sessions"]["data"]["rows"]]
            self.assertIn(self.session_id, session_ids)

            page_rows = data["pages"]["data"]["rows"]
            self.assertTrue(any(row["page_id"] == page_id and row["active"] for row in page_rows))
            self.assertTrue(any((row["title"] or "").startswith("Debug UI Demo") for row in page_rows))

            console_texts = [entry["text"] for entry in data["console"]["data"]["payload"]["entries"]]
            self.assertIn("hello from page", console_texts)
            self.assertIn("boom", console_texts)

            network_rows = data["network"]["data"]["rows"]
            statuses = sorted(row["status"] for row in network_rows if row["status"] is not None)
            self.assertIn(404, statuses)
            self.assertIn(500, statuses)
            login = next(row for row in network_rows if row["url"].endswith("/api/login"))
            self.assertEqual(login["method"], "POST")
            self.assertTrue(login["failed"])
        finally:
            self.service.close_page(page_id)

    def test_dom_viewer(self):
        page_id, _page = self._open_page()
        try:
            view = self.service.dom_view(page_id, locator={"css": "#go"}, html=True)
            payload = view["data"]["payload"]
            self.assertEqual(payload["tag"], "button")
            self.assertEqual(payload["text"], "Go")
            self.assertTrue(payload["visible"])
            self.assertTrue(payload["enabled"])
            self.assertIn("<button", payload["html"])
        finally:
            self.service.close_page(page_id)

    # ------------------------------------------------------------------
    # Screenshot + trace artifacts
    # ------------------------------------------------------------------
    def test_screenshot_artifact(self):
        page_id, _page = self._open_page()
        try:
            view = self.service.screenshot_view(page_id, full_page=True)
            payload = view["data"]["payload"]
            self.assertEqual(view["type"], "image")
            self.assertGreater(payload["width"], 0)
            self.assertGreater(payload["height"], 0)
            self.assertTrue(Path(payload["path"]).exists())
            self.assertEqual(view["artifact"]["mime_type"], "image/png")
        finally:
            self.service.close_page(page_id)

    def test_trace_artifact(self):
        page_id, _page = self._open_page()
        try:
            self.service.trace_start(self.session_id, screenshots=True, snapshots=True, sources=True)
            self.service.navigate(page_id, BASE_URL)
            view = self.service.trace_view(self.session_id)
            payload = view["data"]["payload"]
            self.assertEqual(view["type"], "file")
            self.assertEqual(payload["mime_type"], "application/zip")
            self.assertEqual(payload["open_action"], "aether.playwright.browser_trace_open")
            path = Path(payload["path"])
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes()[:2], b"PK")
            self.assertGreater(view["artifact"]["size"], 0)
        finally:
            self.service.close_page(page_id)

    # ------------------------------------------------------------------
    # Persistence: save state -> restart -> restore state
    # ------------------------------------------------------------------
    def test_save_state_restart_restore(self):
        page_id, page = self._open_page()
        try:
            # seed a cookie + localStorage on the http origin
            self.service.context_object(self.session_id).add_cookies(
                [
                    {
                        "name": "sid",
                        "value": "abc123",
                        "domain": "local.test",
                        "path": "/",
                    }
                ]
            )
            page.evaluate("() => window.localStorage.setItem('token', 'xyz')")

            saved = self.service.save_state(self.session_id, name="t05-real")
            self.assertTrue(saved["saved"])
            self.assertEqual(saved["scope"], "extension")
            self.assertGreaterEqual(saved["cookie_count"], 1)
            self.assertGreaterEqual(saved["origin_count"], 1)
            # the persisted state really contains the localStorage entry
            state = self.service.load_state("t05-real")
            self.assertTrue(
                any(
                    entry.get("name") == "token"
                    for origin in state["origins"]
                    for entry in origin.get("localStorage", [])
                )
            )
        finally:
            self.service.close_page(page_id)

        # "restart AETHER": shut the service down and start a fresh one
        # over the same state directory.
        self.service.shutdown()
        restarted = PlaywrightService(
            headless=True,
            timeout=20,
            artifact_dir=str(self.artifact_dir),
            state_dir=str(self.state_dir),
            runtime_factory=lambda: _SYNC_PLAYWRIGHT().start(),
        )
        try:
            self.assertIn("t05-real", restarted.list_saved_states())
            restarted.launch_browser()
            restored = restarted.restore_state(name="t05-real")
            self.assertTrue(restored["restored"])
            self.assertTrue(restored["created_session"])
            self.assertEqual(restored["method"], "new_context")

            new_session = restored["session_id"]
            cookies = restarted.context_object(new_session).cookies()
            self.assertTrue(any(cookie["name"] == "sid" for cookie in cookies))

            # localStorage is re-applied when a page of that origin loads
            new_page = restarted.new_page(new_session)
            page_obj = restarted.page_object(new_page)
            page_obj.route("**/*", self._route)
            restarted.navigate(new_page, BASE_URL)
            token = page_obj.evaluate("() => window.localStorage.getItem('token')")
            self.assertEqual(token, "xyz")
        finally:
            restarted.shutdown()

        # restore the class-level browser/session for any later test
        TestDebugUIReal.service = PlaywrightService(
            headless=True,
            timeout=20,
            artifact_dir=str(self.artifact_dir),
            state_dir=str(self.state_dir),
            runtime_factory=lambda: _SYNC_PLAYWRIGHT().start(),
        )
        TestDebugUIReal.browser_id = TestDebugUIReal.service.launch_browser()
        TestDebugUIReal.session_id = TestDebugUIReal.service.create_session(
            TestDebugUIReal.browser_id
        )

    # ------------------------------------------------------------------
    # Tool layer — the documented flow
    # ------------------------------------------------------------------
    def test_workflow_via_tool_layer(self):
        page_id, _page = self._open_page()
        try:
            tools = {tool.name: tool for tool in build_playwright_tools(self.service)}
            panel = tools["aether.playwright.browser_debug_panel"].execute(
                session_id=self.session_id, page_id=page_id
            )
            self.assertEqual(panel["type"], "panel")
            self.assertEqual(panel["data"]["page_id"], page_id)

            shot = tools["aether.playwright.browser_screenshot"].execute(
                page_id=page_id, full_page=True
            )
            self.assertTrue(Path(shot["path"]).exists())

            tools["aether.playwright.browser_trace_start"].execute(session_id=self.session_id)
            trace = tools["aether.playwright.browser_trace_stop"].execute(session_id=self.session_id)
            self.assertTrue(Path(trace["path"]).exists())

            saved = tools["aether.playwright.session_save_state"].execute(
                session_id=self.session_id, name="t05-tool"
            )
            self.assertTrue(saved["saved"])
            listed = tools["aether.playwright.session_state_list"].execute()
            self.assertIn("t05-tool", listed["states"])
            self.service.delete_saved_state("t05-tool")
        finally:
            self.service.close_page(page_id)


class TestDebugUIFullFlowReal(unittest.TestCase):
    """A single real-Chromium test covering the whole Task 05 flow:

        launch -> page -> debug panel data -> screenshot -> trace
               -> save state -> restart -> restore state
    """

    @classmethod
    def setUpClass(cls):
        if not _real_browser_available():
            raise unittest.SkipTest("Playwright browser is not available")
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        (root / "state").mkdir(exist_ok=True)
        (root / "artifacts").mkdir(exist_ok=True)
        cls.state_dir = root / "state"
        cls.artifact_dir = root / "artifacts"
        cls.service = cls._new_service()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.service.shutdown()
        finally:
            cls._tmp.cleanup()

    @classmethod
    def _new_service(cls):
        return PlaywrightService(
            headless=True,
            timeout=20,
            artifact_dir=str(cls.artifact_dir),
            state_dir=str(cls.state_dir),
            runtime_factory=lambda: _SYNC_PLAYWRIGHT().start(),
        )

    @staticmethod
    def _route(route):
        url = route.request.url
        if url.endswith("/api/login"):
            route.fulfill(status=500, content_type="application/json", body='{"error": "invalid"}')
        elif url.endswith("/assets/app.js"):
            route.fulfill(status=404, content_type="text/javascript", body="not found")
        else:
            route.fulfill(status=200, content_type="text/html", body=FIXTURE_HTML)

    def test_full_flow(self):
        service = self.service
        service.launch_browser()
        session_id = service.create_session()
        page_id = service.new_page(session_id)
        page = service.page_object(page_id)
        page.route("**/*", self._route)
        service.navigate(page_id, BASE_URL)
        service.wait(page_id, condition="load", load_state="networkidle")

        # 1. debug panel data
        panel = service.debug_panel(session_id=session_id, page_id=page_id)
        self.assertEqual(panel["type"], "panel")
        self.assertTrue(
            any(row["page_id"] == page_id for row in panel["data"]["pages"]["data"]["rows"])
        )
        self.assertTrue(panel["data"]["console"]["data"]["payload"]["entries"])

        # 2. screenshot artifact
        shot = service.screenshot_view(page_id, full_page=True)
        self.assertTrue(Path(shot["data"]["payload"]["path"]).exists())

        # 3. trace artifact
        service.trace_start(session_id)
        service.navigate(page_id, BASE_URL)
        trace = service.trace_view(session_id)
        self.assertEqual(Path(trace["data"]["payload"]["path"]).read_bytes()[:2], b"PK")

        # 4. save state (cookies + localStorage)
        service.context_object(session_id).add_cookies(
            [{"name": "sid", "value": "flow", "domain": "local.test", "path": "/"}]
        )
        page.evaluate("() => window.localStorage.setItem('token', 'flow')")
        saved = service.save_state(session_id, name="t05-flow")
        self.assertGreaterEqual(saved["cookie_count"], 1)
        self.assertGreaterEqual(saved["origin_count"], 1)

        # 5. restart AETHER -> restore state
        service.shutdown()
        restarted = self._new_service()
        try:
            restarted.launch_browser()
            self.assertIn("t05-flow", restarted.list_saved_states())
            restored = restarted.restore_state(name="t05-flow")
            cookies = restarted.context_object(restored["session_id"]).cookies()
            self.assertTrue(any(cookie["name"] == "sid" for cookie in cookies))
        finally:
            restarted.shutdown()

        # leave the class service usable for tearDownClass
        TestDebugUIFullFlowReal.service = self._new_service()


if __name__ == "__main__":
    unittest.main()
