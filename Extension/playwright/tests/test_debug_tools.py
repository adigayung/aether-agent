"""Task 04 — deterministic tests for the debugging tools (no real browser).

A small in-memory Playwright double (page + locators + tracing) drives the
:class:`PlaywrightService` so the console / network / DOM-inspection /
screenshot / trace logic can be exercised deterministically, including the
failure modes (page/session not found, trace not active / already active,
element not found) and page isolation.

The real-browser counterpart lives in ``test_debug_tools_real.py``.
"""

import sys
import tempfile
import unittest
from pathlib import Path

EXT_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = EXT_DIR.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Extension.playwright.services.playwright_service import (  # noqa: E402
    DomElementNotFoundError,
    InvalidLocatorError,
    PageNotFoundError,
    PlaywrightService,
    ScreenshotError,
    SessionNotFoundError,
    TraceAlreadyActiveError,
    TraceNotActiveError,
)
from Extension.playwright.tools.playwright_tools import (  # noqa: E402
    build_playwright_tools,
)


# ===========================================================================
# Helpers
# ===========================================================================
def make_png(width, height):
    """A minimal but valid PNG header so dimension parsing works offline."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = (
        b"\x00\x00\x00\rIHDR"
        + int(width).to_bytes(4, "big")
        + int(height).to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )
    return signature + ihdr


# ===========================================================================
# Fake Playwright model
# ===========================================================================
class FakeElement:
    def __init__(self, tag, *, attrs=None, text="", visible=True, enabled=True, value=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.text = text
        self.visible = visible
        self.enabled = enabled
        self.value = value
        self.box = {"x": 5.0, "y": 12.0, "width": 44.0, "height": 22.0}


class FakeLocator:
    def __init__(self, page, elements):
        self.page = page
        self._elements = list(elements)

    def count(self):
        return len(self._elements)

    @property
    def first(self):
        return FakeLocator(self.page, self._elements[:1])

    def nth(self, index):
        return FakeLocator(self.page, [self._elements[index]])

    def _one(self):
        if len(self._elements) != 1:
            raise RuntimeError(f"expected exactly one element, got {len(self._elements)}")
        return self._elements[0]

    def evaluate(self, script, arg=None):
        text = script if isinstance(script, str) else ""
        if "AETHER_DOM_INSPECT" in text:
            element = self._one()
            attrs = " ".join(f'{k}="{v}"' for k, v in element.attrs.items())
            out = {
                "tag": element.tag,
                "text": element.text,
                "attributes": dict(element.attrs),
                "bounding_box": dict(element.box),
                "html": f"<{element.tag} {attrs}>{element.text}</{element.tag}>".strip(),
            }
            if element.value is not None:
                out["value"] = element.value
            return out
        raise RuntimeError("unknown evaluate script")

    def is_visible(self, **kwargs):
        return self._one().visible

    def is_enabled(self, **kwargs):
        return self._one().enabled

    def inner_html(self, **kwargs):
        element = self._one()
        return f'<{element.tag} id="{element.attrs.get("id", "")}">{element.text}</{element.tag}>'

    def screenshot(self, **kwargs):
        return make_png(17, 9)


class FakeConsoleMessage:
    def __init__(self, ctype, text, url="http://local.test/", line=1, column=2):
        self.type = ctype
        self.text = text
        self.location = {"url": url, "lineNumber": line, "columnNumber": column}


class FakePageError:
    def __init__(self, message="boom", name="TypeError", stack="at line 1"):
        self.message = message
        self.name = name
        self.stack = stack


class FakeRequest:
    def __init__(self, url, method="GET", resource_type="document", timing=None, failure=None):
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.timing = timing or {"startTime": 0, "responseEnd": 12.5}
        self.failure = failure


class FakeResponse:
    def __init__(self, request=None, status=200, ok=True, status_text="OK"):
        self._request = request
        self.status = status
        self.ok = ok
        self.status_text = status_text
        self.url = request.url if request is not None else None

    @property
    def request(self):
        return self._request


class FakePage:
    def __init__(self, context):
        self._context = context
        self.url = "about:blank"
        self._title = "blank"
        self._closed = False
        self.handlers = {}
        self.elements = []

    # -- events ---------------------------------------------------------
    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event, *args):
        for handler in list(self.handlers.get(event, [])):
            handler(*args)

    # -- basics ---------------------------------------------------------
    def title(self):
        return self._title

    def is_closed(self):
        return self._closed

    def close(self):
        self._closed = True
        if self in self._context._pages:
            self._context._pages.remove(self)

    def goto(self, url, **kwargs):
        self.url = url
        self._title = url
        return FakeResponse(status=200, ok=True)

    def reload(self, **kwargs):
        return FakeResponse(status=200, ok=True)

    def evaluate(self, script, arg=None):
        if "AETHER_SCROLL_TO" in (script if isinstance(script, str) else ""):
            return True
        return None

    def screenshot(self, **kwargs):
        return make_png(64, 48)

    def wait_for_load_state(self, *args, **kwargs):
        return None

    # -- locators -------------------------------------------------------
    def locator(self, selector):
        return FakeLocator(self, self._match(selector))

    def _match(self, selector):
        selector = selector.strip()
        if selector in ("body", "*"):
            return list(self.elements)
        if selector.startswith("#"):
            wanted = selector[1:]
            return [el for el in self.elements if el.attrs.get("id") == wanted]
        if selector.startswith("."):
            wanted = selector[1:]
            return [
                el
                for el in self.elements
                if wanted in (el.attrs.get("class") or "").split()
            ]
        return []


class FakeTracing:
    def __init__(self):
        self.started = False
        self.started_options = None
        self.stop_path = None

    def start(self, **kwargs):
        if self.started:
            raise RuntimeError("tracing already started")
        self.started = True
        self.started_options = kwargs

    def stop(self, path=None):
        if not self.started:
            raise RuntimeError("tracing not started")
        self.started = False
        self.stop_path = path
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"PK\x03\x04fake-trace-payload")


class FakeContext:
    def __init__(self, browser):
        self._browser = browser
        self._pages = []
        self.closed = False
        self.tracing = FakeTracing()

    def new_page(self):
        if self.closed:
            raise RuntimeError("context is closed")
        page = FakePage(self)
        self._pages.append(page)
        return page

    def close(self):
        self.closed = True
        self._pages = []


class FakeBrowser:
    def __init__(self):
        self._contexts = []
        self.closed = False

    def new_context(self, **kwargs):
        context = FakeContext(self)
        self._contexts.append(context)
        return context

    def close(self):
        self.closed = True

    def is_connected(self):
        return not self.closed


class FakeBrowserType:
    def __init__(self, engine):
        self.engine = engine

    def launch(self, **kwargs):
        return FakeBrowser()


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeBrowserType("chromium")
        self.firefox = FakeBrowserType("firefox")
        self.webkit = FakeBrowserType("webkit")

    def stop(self):
        pass


# ===========================================================================
# Fixtures
# ===========================================================================
def make_service(tmp_dir):
    service = PlaywrightService(runtime_factory=FakePlaywright, artifact_dir=str(tmp_dir))
    browser_id = service.launch_browser()
    session_id = service.create_session(browser_id)
    return service, session_id


def add_page(service, session_id, elements=None):
    page_id = service.new_page(session_id)
    page = service.page_object(page_id)
    page.elements = list(elements or [])
    return page_id, page


# ===========================================================================
# Tests
# ===========================================================================
class TestDebugToolRegistration(unittest.TestCase):
    def test_task04_tools_registered_and_namespaced(self):
        tools = {t.name: t for t in build_playwright_tools(PlaywrightService())}
        expected = [
            "browser_console",
            "browser_network",
            "browser_dom_inspect",
            "browser_screenshot",
            "browser_trace_start",
            "browser_trace_stop",
        ]
        for suffix in expected:
            self.assertIn("aether.playwright." + suffix, tools)
        for name in tools:
            self.assertTrue(name.startswith("aether.playwright."))

    def test_tool_schemas(self):
        tools = {t.name: t for t in build_playwright_tools(PlaywrightService())}
        for suffix in (
            "browser_console",
            "browser_network",
            "browser_dom_inspect",
            "browser_screenshot",
        ):
            schema = tools["aether.playwright." + suffix].input_schema
            self.assertIn("page_id", schema["required"])
        # trace tools are session-scoped, no page_id required
        self.assertNotIn(
            "page_id", tools["aether.playwright.browser_trace_start"].input_schema.get("required", [])
        )
        self.assertNotIn(
            "page_id", tools["aether.playwright.browser_trace_stop"].input_schema.get("required", [])
        )

    def test_validation_rejects_missing_required(self):
        from agent_ai.tools.base import ToolValidationError

        tools = {t.name: t for t in build_playwright_tools(PlaywrightService())}
        with self.assertRaises(ToolValidationError):
            tools["aether.playwright.browser_console"].validate({})


class TestConsoleCapture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.session_id = make_service(self._tmp.name)
        self.page_id, self.page = add_page(self.service, self.session_id)

    def _emit_all(self):
        self.page.emit("console", FakeConsoleMessage("log", "hello"))
        self.page.emit("console", FakeConsoleMessage("debug", "dbg"))
        self.page.emit("console", FakeConsoleMessage("info", "info msg"))
        self.page.emit("console", FakeConsoleMessage("warning", "careful now"))
        self.page.emit("console", FakeConsoleMessage("warn", "legacy warn"))
        self.page.emit("console", FakeConsoleMessage("error", "boom failure"))

    def test_capture_structured_events(self):
        self._emit_all()
        result = self.service.console(self.page_id)
        self.assertEqual(result["page_id"], self.page_id)
        self.assertEqual(result["session_id"], self.session_id)
        self.assertEqual(result["count"], 6)
        self.assertEqual(result["total"], 6)
        first = result["messages"][0]
        self.assertEqual(first["type"], "log")
        self.assertEqual(first["text"], "hello")
        self.assertIsInstance(first["timestamp"], float)
        self.assertEqual(first["location"]["line"], 1)
        # 'warn' is normalised to 'warning'
        self.assertIn("warning", result["types"])

    def test_filter_by_type_and_level(self):
        self._emit_all()
        errors = self.service.console(self.page_id, type="error")
        self.assertEqual(errors["count"], 1)
        self.assertEqual(errors["messages"][0]["text"], "boom failure")

        warnings = self.service.console(self.page_id, level="warning")
        # 'warning' + normalised 'warn'
        self.assertEqual(warnings["count"], 2)

        several = self.service.console(self.page_id, types=["log", "info"])
        self.assertEqual(several["count"], 2)

    def test_filter_by_search(self):
        self._emit_all()
        result = self.service.console(self.page_id, search="BOOM")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["messages"][0]["type"], "error")

    def test_limit_and_clear(self):
        self._emit_all()
        result = self.service.console(self.page_id, limit=2)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["messages"][-1]["text"], "boom failure")

        cleared = self.service.console(self.page_id, clear=True)
        self.assertEqual(cleared["cleared"], 6)
        self.assertEqual(self.service.console(self.page_id)["count"], 0)

    def test_page_error_is_captured_as_error(self):
        self.page.emit("pageerror", FakePageError("unexpected"))
        result = self.service.console(self.page_id, type="error")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["messages"][0]["source"], "pageerror")
        self.assertEqual(result["messages"][0]["error_name"], "TypeError")

    def test_page_isolation(self):
        other_id, other_page = add_page(self.service, self.session_id)
        self.page.emit("console", FakeConsoleMessage("log", "page A only"))
        other_page.emit("console", FakeConsoleMessage("log", "page B only"))

        a = self.service.console(self.page_id)
        b = self.service.console(other_id)
        self.assertEqual([m["text"] for m in a["messages"]], ["page A only"])
        self.assertEqual([m["text"] for m in b["messages"]], ["page B only"])

    def test_unknown_page_raises(self):
        with self.assertRaises(PageNotFoundError):
            self.service.console("page-does-not-exist")


class TestNetworkCapture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.session_id = make_service(self._tmp.name)
        self.page_id, self.page = add_page(self.service, self.session_id)

    def _exchange(self, url, method="GET", resource_type="document", status=200, ok=True,
                  status_text="OK", failure=None):
        request = FakeRequest(url, method=method, resource_type=resource_type, failure=failure)
        self.page.emit("request", request)
        if failure is None:
            self.page.emit("response", FakeResponse(request, status=status, ok=ok,
                                                    status_text=status_text))
        else:
            self.page.emit("requestfailed", request)
        return request

    def test_capture_request_response(self):
        self._exchange("http://local.test/api/login", method="POST",
                       resource_type="xhr", status=500, ok=False)
        self._exchange("http://local.test/assets/app.js", method="GET",
                       resource_type="script", status=404, ok=False)

        result = self.service.network(self.page_id)
        self.assertEqual(result["count"], 2)
        login = result["requests"][0]
        self.assertEqual(login["method"], "POST")
        self.assertEqual(login["url"], "http://local.test/api/login")
        self.assertEqual(login["status"], 500)
        self.assertEqual(login["resource_type"], "xhr")
        self.assertTrue(login["timing"])
        self.assertGreaterEqual(login["duration_ms"], 0)
        self.assertFalse(login["ok"])

    def test_filters(self):
        self._exchange("http://local.test/api/login", method="POST", status=500, ok=False)
        self._exchange("http://local.test/assets/app.js", method="GET",
                       resource_type="script", status=404, ok=False)
        self._exchange("http://local.test/health", method="GET", status=200)

        by_url = self.service.network(self.page_id, url="/api/login")
        self.assertEqual(by_url["count"], 1)
        by_method = self.service.network(self.page_id, method="POST")
        self.assertEqual(by_method["count"], 1)
        by_status = self.service.network(self.page_id, status=404)
        self.assertEqual(by_status["count"], 1)
        by_class = self.service.network(self.page_id, status="5xx")
        self.assertEqual(by_class["count"], 1)
        by_type = self.service.network(self.page_id, resource_type="script")
        self.assertEqual(by_type["count"], 1)
        failed = self.service.network(self.page_id, failed=True)
        self.assertEqual(failed["count"], 2)

    def test_request_failure(self):
        self._exchange("http://local.test/broken", method="GET",
                       resource_type="fetch", failure="net::ERR_CONNECTION_REFUSED")
        result = self.service.network(self.page_id)
        self.assertEqual(result["count"], 1)
        self.assertIn("ERR_CONNECTION_REFUSED", result["requests"][0]["failure"])
        self.assertEqual(self.service.network(self.page_id, failed=True)["count"], 1)

    def test_limit_and_clear(self):
        for i in range(5):
            self._exchange(f"http://local.test/{i}", status=200)
        limited = self.service.network(self.page_id, limit=2)
        self.assertEqual(limited["count"], 2)
        self.assertEqual(limited["requests"][-1]["url"], "http://local.test/4")
        cleared = self.service.network(self.page_id, clear=True)
        self.assertEqual(cleared["cleared"], 5)
        self.assertEqual(self.service.network(self.page_id)["count"], 0)

    def test_page_isolation(self):
        other_id, other_page = add_page(self.service, self.session_id)
        self.page.emit("request", FakeRequest("http://local.test/a"))
        other_page.emit("request", FakeRequest("http://local.test/b"))
        a = self.service.network(self.page_id)
        b = self.service.network(other_id)
        self.assertEqual(a["requests"][0]["url"], "http://local.test/a")
        self.assertEqual(b["requests"][0]["url"], "http://local.test/b")

    def test_unknown_page_raises(self):
        with self.assertRaises(PageNotFoundError):
            self.service.network("page-does-not-exist")


class TestDomInspect(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.session_id = make_service(self._tmp.name)
        self.button = FakeElement(
            "button", attrs={"id": "go", "class": "btn"}, text="Go", value=None
        )
        self.page_id, self.page = add_page(self.service, self.session_id, [self.button])

    def test_inspect_by_locator(self):
        result = self.service.dom_inspect(self.page_id, locator={"css": "#go"})
        self.assertEqual(result["tag"], "button")
        self.assertEqual(result["text"], "Go")
        self.assertEqual(result["attributes"]["id"], "go")
        self.assertTrue(result["visible"])
        self.assertTrue(result["enabled"])
        self.assertEqual(result["bounding_box"]["width"], 44.0)

    def test_inspect_with_html(self):
        result = self.service.dom_inspect(self.page_id, locator={"css": "#go"}, html=True)
        self.assertIn("<button", result["html"])
        self.assertFalse(result["html_truncated"])

    def test_element_not_found(self):
        with self.assertRaises(DomElementNotFoundError):
            self.service.dom_inspect(self.page_id, locator={"css": "#missing"})

    def test_missing_target(self):
        with self.assertRaises(InvalidLocatorError):
            self.service.dom_inspect(self.page_id)


class TestScreenshot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.session_id = make_service(self._tmp.name)
        self.button = FakeElement("button", attrs={"id": "go"}, text="Go")
        self.page_id, self.page = add_page(self.service, self.session_id, [self.button])

    def test_full_page_screenshot(self):
        result = self.service.screenshot(self.page_id, full_page=True)
        self.assertEqual(result["scope"], "full_page")
        self.assertEqual(result["mime_type"], "image/png")
        self.assertEqual((result["width"], result["height"]), (64, 48))
        self.assertTrue(Path(result["path"]).exists())
        self.assertEqual(result["session_id"], self.session_id)

    def test_viewport_screenshot(self):
        result = self.service.screenshot(self.page_id)
        self.assertEqual(result["scope"], "viewport")
        self.assertTrue(result["filename"].endswith(".png"))

    def test_element_screenshot(self):
        result = self.service.screenshot(self.page_id, locator={"css": "#go"})
        self.assertEqual(result["scope"], "element")
        self.assertEqual((result["width"], result["height"]), (17, 9))

    def test_invalid_type(self):
        with self.assertRaises(ScreenshotError):
            self.service.screenshot(self.page_id, type="gif")

    def test_element_not_found(self):
        with self.assertRaises(DomElementNotFoundError):
            self.service.screenshot(self.page_id, locator={"css": "#missing"})

    def test_custom_filename_and_dir(self):
        out_dir = Path(self._tmp.name) / "shots"
        result = self.service.screenshot(
            self.page_id, filename="shot.png", save_dir=str(out_dir)
        )
        self.assertEqual(result["filename"], "shot.png")
        self.assertTrue((out_dir / "shot.png").exists())


class TestTrace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.session_id = make_service(self._tmp.name)

    def test_start_stop_produces_artifact(self):
        started = self.service.trace_start(self.session_id, screenshots=True,
                                           snapshots=True, sources=True)
        self.assertTrue(started["active"])
        trace = self.service.trace_stop(self.session_id)
        self.assertFalse(trace["active"])
        self.assertEqual(trace["mime_type"], "application/zip")
        self.assertTrue(Path(trace["path"]).exists())
        self.assertGreater(trace["size_bytes"], 0)

    def test_already_active_error(self):
        self.service.trace_start(self.session_id)
        with self.assertRaises(TraceAlreadyActiveError):
            self.service.trace_start(self.session_id)

    def test_not_active_error(self):
        with self.assertRaises(TraceNotActiveError):
            self.service.trace_stop(self.session_id)

    def test_sessions_are_isolated(self):
        # a second session has its own tracing state
        second = self.service.create_session()
        self.service.trace_start(self.session_id)
        with self.assertRaises(TraceNotActiveError):
            self.service.trace_stop(second)
        self.service.trace_stop(self.session_id)

    def test_unknown_session_raises(self):
        with self.assertRaises(SessionNotFoundError):
            self.service.trace_start("session-does-not-exist")


class TestLifecycleCleanup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.session_id = make_service(self._tmp.name)

    def test_closing_page_drops_debug_data(self):
        page_id, page = add_page(self.service, self.session_id)
        page.emit("console", FakeConsoleMessage("log", "x"))
        page.emit("request", FakeRequest("http://local.test/x"))
        self.assertIn(page_id, self.service._console)
        self.service.close_page(page_id)
        self.assertNotIn(page_id, self.service._console)
        self.assertNotIn(page_id, self.service._network)
        self.assertNotIn(page_id, self.service._debug_listeners)

    def test_closing_session_drops_trace_state(self):
        page_id, _page = add_page(self.service, self.session_id)
        self.service.trace_start(self.session_id)
        self.assertIn(self.session_id, self.service._traces)
        self.service.close_session(self.session_id)
        self.assertNotIn(self.session_id, self.service._traces)

    def test_shutdown_clears_debug_state(self):
        page_id, page = add_page(self.service, self.session_id)
        page.emit("console", FakeConsoleMessage("log", "x"))
        self.service.trace_start(self.session_id)
        self.service.shutdown()
        self.assertEqual(self.service._console, {})
        self.assertEqual(self.service._network, {})
        self.assertEqual(self.service._traces, {})


class TestToolLayer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.session_id = make_service(self._tmp.name)
        self.tools = {t.name: t for t in build_playwright_tools(self.service)}
        self.button = FakeElement("button", attrs={"id": "go"}, text="Go")
        self.page_id, self.page = add_page(self.service, self.session_id, [self.button])

    def test_console_tool(self):
        self.page.emit("console", FakeConsoleMessage("log", "from tool"))
        result = self.tools["aether.playwright.browser_console"].execute(
            page_id=self.page_id, type="log"
        )
        self.assertEqual(result["messages"][0]["text"], "from tool")

    def test_network_tool(self):
        request = FakeRequest("http://local.test/api")
        self.page.emit("request", request)
        self.page.emit("response", FakeResponse(request, status=200))
        result = self.tools["aether.playwright.browser_network"].execute(
            page_id=self.page_id, url="/api"
        )
        self.assertEqual(result["count"], 1)

    def test_dom_inspect_tool(self):
        result = self.tools["aether.playwright.browser_dom_inspect"].execute(
            page_id=self.page_id, locator={"css": "#go"}
        )
        self.assertEqual(result["tag"], "button")

    def test_screenshot_tool(self):
        result = self.tools["aether.playwright.browser_screenshot"].execute(
            page_id=self.page_id, full_page=True
        )
        self.assertTrue(Path(result["path"]).exists())
        self.assertEqual(result["mime_type"], "image/png")

    def test_trace_tools(self):
        self.tools["aether.playwright.browser_trace_start"].execute(
            session_id=self.session_id
        )
        result = self.tools["aether.playwright.browser_trace_stop"].execute(
            session_id=self.session_id
        )
        self.assertTrue(Path(result["path"]).exists())


if __name__ == "__main__":
    unittest.main()
