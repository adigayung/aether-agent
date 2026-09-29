"""Task 04 — real-browser integration tests for the debugging tools.

These tests drive the *real* Playwright engine against a local, fully routed
fixture (no internet: every request is fulfilled by ``page.route``) and prove
the full debugging workflow::

    launch -> session -> page -> navigate -> console -> network
           -> snapshot -> dom_inspect -> screenshot -> trace_start -> trace_stop

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
    TraceAlreadyActiveError,
    TraceNotActiveError,
)
from Extension.playwright.tools.playwright_tools import (  # noqa: E402
    build_playwright_tools,
)


FIXTURE_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Debug Demo</title></head>
<body>
  <h1 id="title">Debug Demo</h1>
  <button id="go" type="button">Go</button>
  <a id="asset-link" href="/assets/app.js">Asset</a>
  <script>
    console.log('hello from page');
    console.debug('debug detail');
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


class TestDebugToolsReal(unittest.TestCase):
    """End-to-end Task 04 behaviour on a real Chromium browser."""

    BASE_URL = "http://local.test/index.html"

    @classmethod
    def setUpClass(cls):
        if not _real_browser_available():
            raise unittest.SkipTest("Playwright browser is not available")
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        (root / "artifacts").mkdir(exist_ok=True)
        cls.artifact_dir = root / "artifacts"
        cls.service = PlaywrightService(
            headless=True,
            timeout=20,
            artifact_dir=str(cls.artifact_dir),
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
            route.fulfill(
                status=500,
                content_type="application/json",
                body='{"error": "invalid"}',
            )
        elif url.endswith("/assets/app.js"):
            route.fulfill(
                status=404, content_type="text/javascript", body="not found"
            )
        else:
            route.fulfill(status=200, content_type="text/html", body=FIXTURE_HTML)

    def _open_page(self, navigate=True):
        page_id = self.service.new_page(self.session_id)
        page = self.service.page_object(page_id)
        page.route("**/*", self._route)
        if navigate:
            self.service.navigate(page_id, self.BASE_URL)
            self.service.wait(page_id, condition="load", load_state="networkidle")
        return page_id, page

    def setUp(self):
        self.page_id, self.page = self._open_page()

    def tearDown(self):
        try:
            self.service.close_page(self.page_id)
        except Exception:
            pass

    def _console_texts(self, result):
        return [message["text"] for message in result["messages"]]

    # ------------------------------------------------------------------
    # Console
    # ------------------------------------------------------------------
    def test_console_capture_and_filter(self):
        result = self.service.console(self.page_id)
        texts = self._console_texts(result)
        self.assertIn("hello from page", texts)
        self.assertIn("careful", texts)
        self.assertIn("boom", texts)
        self.assertIn("login status 500", texts)
        self.assertIn("asset status 404", texts)
        for message in result["messages"]:
            self.assertIsInstance(message["timestamp"], float)
            self.assertIn("type", message)
            self.assertIn("text", message)
        self.assertIn("warning", result["types"])

        errors = self.service.console(self.page_id, type="error")
        error_texts = self._console_texts(errors)
        # our explicit error plus Chromium's own resource-load errors (500/404)
        self.assertIn("boom", error_texts)
        self.assertTrue(any("500" in text for text in error_texts))

        logs = self.service.console(self.page_id, type="log", search="login")
        self.assertEqual(len(logs["messages"]), 1)
        self.assertIn("500", logs["messages"][0]["text"])

        limited = self.service.console(self.page_id, limit=1)
        self.assertEqual(limited["count"], 1)

    def test_console_location_available(self):
        result = self.service.console(self.page_id, type="log", search="hello")
        location = result["messages"][0].get("location")
        self.assertIsNotNone(location)
        self.assertTrue(location.get("url", "").endswith("index.html"))

    def test_console_clear(self):
        before = self.service.console(self.page_id)
        self.assertGreater(before["count"], 0)
        self.service.console(self.page_id, clear=True)
        self.assertEqual(self.service.console(self.page_id)["count"], 0)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------
    def test_network_capture_and_filter(self):
        result = self.service.network(self.page_id)
        self.assertGreaterEqual(result["count"], 3)

        login = self.service.network(self.page_id, url="/api/login", method="POST")
        self.assertEqual(login["count"], 1)
        record = login["requests"][0]
        self.assertEqual(record["status"], 500)
        self.assertEqual(record["method"], "POST")
        self.assertFalse(record["ok"])

        asset = self.service.network(self.page_id, url="app.js")
        self.assertEqual(asset["count"], 1)
        self.assertEqual(asset["requests"][0]["status"], 404)

        failed = self.service.network(self.page_id, failed=True)
        statuses = sorted(r["status"] for r in failed["requests"])
        self.assertEqual(statuses, [404, 500])

        by_class = self.service.network(self.page_id, status="5xx")
        self.assertEqual(by_class["count"], 1)

        document = self.service.network(self.page_id, resource_type="document")
        self.assertEqual(document["count"], 1)
        self.assertEqual(document["requests"][0]["status"], 200)

    def test_network_record_is_serialisable(self):
        import json

        result = self.service.network(self.page_id)
        for record in result["requests"]:
            for key in ("url", "method", "status", "resource_type"):
                self.assertIn(key, record)
        json.dumps(result)

    # ------------------------------------------------------------------
    # DOM inspection
    # ------------------------------------------------------------------
    def test_dom_inspect_by_locator(self):
        result = self.service.dom_inspect(self.page_id, locator={"css": "#go"})
        self.assertEqual(result["tag"], "button")
        self.assertEqual(result["text"], "Go")
        self.assertEqual(result["attributes"].get("id"), "go")
        self.assertTrue(result["visible"])
        self.assertTrue(result["enabled"])
        self.assertGreater(result["bounding_box"]["width"], 0)

    def test_dom_inspect_by_ref(self):
        snapshot = self.service.snapshot(self.page_id)
        go_ref = next(
            ref for ref, info in snapshot["refs"].items() if info["name"] == "Go"
        )
        result = self.service.dom_inspect(self.page_id, ref=go_ref, html=True)
        self.assertEqual(result["tag"], "button")
        self.assertEqual(result["role"], "button")
        self.assertIn("<button", result["html"])

    def test_dom_inspect_element_not_found(self):
        with self.assertRaises(Exception):
            self.service.dom_inspect(self.page_id, locator={"css": "#missing"})

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------
    def test_screenshot_full_page_and_element(self):
        full = self.service.screenshot(self.page_id, full_page=True)
        self.assertEqual(full["scope"], "full_page")
        self.assertEqual(full["mime_type"], "image/png")
        self.assertGreater(full["width"], 0)
        self.assertGreater(full["height"], 0)
        self.assertTrue(Path(full["path"]).exists())

        element = self.service.screenshot(self.page_id, locator={"css": "#go"})
        self.assertEqual(element["scope"], "element")
        self.assertGreater(element["width"], 0)

        viewport = self.service.screenshot(self.page_id)
        self.assertEqual(viewport["scope"], "viewport")

    # ------------------------------------------------------------------
    # Trace
    # ------------------------------------------------------------------
    def test_trace_start_stop_produces_reopenable_artifact(self):
        started = self.service.trace_start(
            self.session_id, screenshots=True, snapshots=True, sources=True
        )
        self.assertTrue(started["active"])
        with self.assertRaises(TraceAlreadyActiveError):
            self.service.trace_start(self.session_id)

        self.service.navigate(self.page_id, self.BASE_URL)
        trace = self.service.trace_stop(self.session_id)

        self.assertFalse(trace["active"])
        self.assertEqual(trace["mime_type"], "application/zip")
        path = Path(trace["path"])
        self.assertTrue(path.exists())
        self.assertGreater(trace["size_bytes"], 0)
        # a Playwright trace is a zip archive
        self.assertEqual(path.read_bytes()[:2], b"PK")

        with self.assertRaises(TraceNotActiveError):
            self.service.trace_stop(self.session_id)

    # ------------------------------------------------------------------
    # Page isolation
    # ------------------------------------------------------------------
    def test_page_isolation(self):
        other_id, other_page = self._open_page()
        try:
            self.page.evaluate("() => console.log('marker-A')")
            other_page.evaluate("() => console.log('marker-B')")

            a_texts = self._console_texts(self.service.console(self.page_id))
            b_texts = self._console_texts(self.service.console(other_id))
            self.assertIn("marker-A", a_texts)
            self.assertNotIn("marker-B", a_texts)
            self.assertIn("marker-B", b_texts)
            self.assertNotIn("marker-A", b_texts)

            a_urls = [r["url"] for r in self.service.network(self.page_id)["requests"]]
            b_urls = [r["url"] for r in self.service.network(other_id)["requests"]]
            self.assertNotEqual(a_urls, [])  # each page keeps its own evidence
            self.assertNotEqual(b_urls, [])
        finally:
            self.service.close_page(other_id)

    # ------------------------------------------------------------------
    # Tool layer — the documented workflow
    # ------------------------------------------------------------------
    def test_workflow_via_tool_layer(self):
        tools = {tool.name: tool for tool in build_playwright_tools(self.service)}

        console = tools["aether.playwright.browser_console"].execute(
            page_id=self.page_id, type="error"
        )
        self.assertTrue(any("boom" in m["text"] for m in console["messages"]))

        network = tools["aether.playwright.browser_network"].execute(
            page_id=self.page_id, status=500
        )
        self.assertEqual(network["count"], 1)

        snapshot = tools["aether.playwright.browser_snapshot"].execute(
            page_id=self.page_id
        )
        go_ref = next(
            ref for ref, info in snapshot["refs"].items() if info["name"] == "Go"
        )
        dom = tools["aether.playwright.browser_dom_inspect"].execute(
            page_id=self.page_id, ref=go_ref
        )
        self.assertEqual(dom["tag"], "button")

        shot = tools["aether.playwright.browser_screenshot"].execute(
            page_id=self.page_id, full_page=True
        )
        self.assertTrue(Path(shot["path"]).exists())

        tools["aether.playwright.browser_trace_start"].execute(session_id=self.session_id)
        trace = tools["aether.playwright.browser_trace_stop"].execute(
            session_id=self.session_id
        )
        self.assertTrue(Path(trace["path"]).exists())


if __name__ == "__main__":
    unittest.main()
