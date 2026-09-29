"""Task 02 tests — Playwright browser / session / page lifecycle.

These tests verify the :class:`PlaywrightService` lifecycle without requiring
a real browser, by injecting a fake Playwright runtime. A single guarded
integration test exercises the *real* Playwright engine and is skipped when a
browser cannot be launched in the current environment.
"""

import sys
import unittest
from pathlib import Path

# Make the project root importable (mirrors tests/test_foundation.py).
EXT_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = EXT_DIR.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Extension.playwright.services.playwright_service import (  # noqa: E402
    BrowserLaunchError,
    BrowserNotFoundError,
    PageNotFoundError,
    PlaywrightService,
    SessionNotFoundError,
)

# Pre-import the *real* Playwright library while the ``Extension`` directory is
# not (yet) on ``sys.path``. The extension folder is itself named
# ``playwright``, so a later import could otherwise resolve to the extension
# package instead of the installed library. Caching it here keeps the optional
# integration test reliable.
try:  # pragma: no cover - environment dependent
    from playwright.sync_api import sync_playwright as _REAL_SYNC_PLAYWRIGHT
except Exception:  # pragma: no cover
    _REAL_SYNC_PLAYWRIGHT = None


# ---------------------------------------------------------------------------
# Fake Playwright runtime (no real browser, deterministic)
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=200, ok=True):
        self.status = status
        self.ok = ok


class FakePage:
    def __init__(self, context):
        self._context = context
        self._closed = False
        self._history = []
        self._index = -1
        self.url = "about:blank"
        self._title = "blank"

    def goto(self, url, **kwargs):
        if self._closed:
            raise RuntimeError("page is closed")
        self._history = self._history[: self._index + 1]
        self._history.append(url)
        self._index = len(self._history) - 1
        self.url = url
        self._title = url
        return FakeResponse()

    def reload(self, **kwargs):
        if self._closed:
            raise RuntimeError("page is closed")
        return FakeResponse()

    def go_back(self, **kwargs):
        if self._index > 0:
            self._index -= 1
            self.url = self._history[self._index]
            return FakeResponse()
        return None

    def go_forward(self, **kwargs):
        if self._index < len(self._history) - 1:
            self._index += 1
            self.url = self._history[self._index]
            return FakeResponse()
        return None

    def title(self):
        return self._title

    def is_closed(self):
        return self._closed

    def close(self):
        self._closed = True
        if self in self._context._pages:
            self._context._pages.remove(self)


class FakeContext:
    def __init__(self, browser):
        self._browser = browser
        self._pages = []
        self.closed = False

    def new_page(self):
        if self.closed:
            raise RuntimeError("context is closed")
        page = FakePage(self)
        self._pages.append(page)
        return page

    def close(self):
        self.closed = True
        for page in list(self._pages):
            page._closed = True
        self._pages = []


class FakeBrowser:
    def __init__(self):
        self._contexts = []
        self.closed = False

    def new_context(self, **kwargs):
        if self.closed:
            raise RuntimeError("browser is closed")
        ctx = FakeContext(self)
        self._contexts.append(ctx)
        return ctx

    def close(self):
        self.closed = True
        for ctx in list(self._contexts):
            ctx.close()

    def is_connected(self):
        return not self.closed


class FakeBrowserType:
    def __init__(self, engine):
        self.engine = engine
        self.launch_calls = []

    def launch(self, **kwargs):
        if kwargs.get("fail"):
            raise RuntimeError("boom")
        browser = FakeBrowser()
        self.launch_calls.append(kwargs)
        return browser


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeBrowserType("chromium")
        self.firefox = FakeBrowserType("firefox")
        self.webkit = FakeBrowserType("webkit")
        self.stopped = False

    def stop(self):
        self.stopped = True


def make_service(**kwargs):
    return PlaywrightService(runtime_factory=FakePlaywright, **kwargs)


# ---------------------------------------------------------------------------
# Runtime laziness
# ---------------------------------------------------------------------------
class TestRuntimeLaziness(unittest.TestCase):
    def test_runtime_not_started_until_browser_used(self):
        service = make_service()
        self.assertFalse(service.is_runtime_started)
        self.assertIsNone(service.runtime)
        service.launch_browser()
        self.assertTrue(service.is_runtime_started)
        service.shutdown()

    def test_construct_has_no_playwright_side_effects(self):
        # Constructing the service must not start the engine.
        service = PlaywrightService()
        self.assertIsNone(service._browser)
        self.assertEqual(service._contexts, {})
        self.assertEqual(service._pages, {})
        self.assertFalse(service.is_runtime_started)


# ---------------------------------------------------------------------------
# Browsers
# ---------------------------------------------------------------------------
class TestBrowserLifecycle(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.addCleanup(self.service.shutdown)

    def test_launch_returns_id_and_records_engine(self):
        browser_id = self.service.launch_browser()
        self.assertTrue(browser_id.startswith("browser-"))
        browsers = self.service.list_browsers()
        self.assertEqual(len(browsers), 1)
        self.assertEqual(browsers[0]["browser_id"], browser_id)
        self.assertEqual(browsers[0]["engine"], "chromium")
        self.assertTrue(browsers[0]["connected"])

    def test_launch_uses_config_defaults_and_overrides(self):
        service = make_service(browser="firefox", headless=False)
        service.launch_browser()
        runtime = service.runtime
        self.assertEqual(len(runtime.firefox.launch_calls), 1)
        self.assertEqual(runtime.firefox.launch_calls[0]["headless"], False)
        # explicit override wins
        service.launch_browser("webkit", True)
        self.assertEqual(runtime.webkit.launch_calls[0]["headless"], True)
        service.shutdown()

    def test_close_browser(self):
        browser_id = self.service.launch_browser()
        result = self.service.close_browser(browser_id)
        self.assertTrue(result["closed"])
        self.assertEqual(self.service.list_browsers(), [])

    def test_close_browser_unknown_raises(self):
        with self.assertRaises(BrowserNotFoundError):
            self.service.close_browser("browser-missing")

    def test_close_all_browsers(self):
        self.service.launch_browser()
        self.service.launch_browser()
        result = self.service.close_browser()
        self.assertEqual(len(result["closed_browsers"]), 2)
        self.assertEqual(self.service.list_browsers(), [])


# ---------------------------------------------------------------------------
# Sessions + pages
# ---------------------------------------------------------------------------
class TestSessionAndPageLifecycle(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.addCleanup(self.service.shutdown)

    def test_create_session(self):
        session_id = self.service.create_session()
        self.assertTrue(session_id.startswith("session-"))
        sessions = self.service.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], session_id)
        self.assertEqual(sessions[0]["page_count"], 0)

    def test_session_auto_launches_browser(self):
        # No browser yet -> create_session launches one on demand.
        self.service.create_session()
        self.assertEqual(len(self.service.list_browsers()), 1)

    def test_new_page_and_list(self):
        session_id = self.service.create_session()
        page_id = self.service.new_page(session_id)
        self.assertTrue(page_id.startswith("page-"))
        pages = self.service.list_pages(session_id)
        self.assertEqual([p["page_id"] for p in pages], [page_id])
        self.assertEqual(self.service.session_info(session_id)["page_count"], 1)

    def test_multiple_pages_per_session(self):
        session_id = self.service.create_session()
        p1 = self.service.new_page(session_id)
        p2 = self.service.new_page(session_id)
        self.assertEqual(len(self.service.list_pages(session_id)), 2)
        self.assertNotEqual(p1, p2)

    def test_list_pages_across_all_sessions(self):
        s1 = self.service.create_session()
        s2 = self.service.create_session()
        self.service.new_page(s1)
        self.service.new_page(s2)
        self.assertEqual(len(self.service.list_pages()), 2)

    def test_close_page(self):
        session_id = self.service.create_session()
        page_id = self.service.new_page(session_id)
        result = self.service.close_page(page_id)
        self.assertTrue(result["closed"])
        self.assertEqual(self.service.list_pages(session_id), [])
        with self.assertRaises(PageNotFoundError):
            self.service.page_info(page_id)

    def test_close_session_closes_its_pages(self):
        session_id = self.service.create_session()
        self.service.new_page(session_id)
        self.service.new_page(session_id)
        result = self.service.close_session(session_id)
        self.assertEqual(len(result["pages_closed"]), 2)
        self.assertEqual(self.service.list_sessions(), [])
        self.assertEqual(self.service.list_pages(), [])

    def test_unknown_ids_raise(self):
        with self.assertRaises(SessionNotFoundError):
            self.service.new_page("session-missing")
        with self.assertRaises(PageNotFoundError):
            self.service.navigate("page-missing", "about:blank")
        with self.assertRaises(SessionNotFoundError):
            self.service.close_session("session-missing")


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------
class TestNavigation(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.addCleanup(self.service.shutdown)

    def test_navigate_returns_state(self):
        page_id = self.service.new_page()
        state = self.service.navigate(page_id, "https://example.com")
        self.assertEqual(state["url"], "https://example.com")
        self.assertEqual(state["page_id"], page_id)
        self.assertEqual(state["status"], 200)

    def test_reload(self):
        page_id = self.service.new_page()
        self.service.navigate(page_id, "https://a.example")
        state = self.service.reload(page_id)
        self.assertEqual(state["url"], "https://a.example")

    def test_back_and_forward(self):
        page_id = self.service.new_page()
        self.service.navigate(page_id, "https://one.example")
        self.service.navigate(page_id, "https://two.example")
        back = self.service.go_back(page_id)
        self.assertEqual(back["url"], "https://one.example")
        self.assertTrue(back["history_moved"])
        forward = self.service.go_forward(page_id)
        self.assertEqual(forward["url"], "https://two.example")
        self.assertTrue(forward["history_moved"])

    def test_back_without_history_does_not_move(self):
        page_id = self.service.new_page()
        state = self.service.go_back(page_id)
        self.assertFalse(state["history_moved"])

    def test_new_page_with_url_navigates(self):
        page_id = self.service.new_page(url="https://start.example")
        self.assertEqual(self.service.page_info(page_id)["url"], "https://start.example")


# ---------------------------------------------------------------------------
# Service status / shutdown
# ---------------------------------------------------------------------------
class TestServiceStatus(unittest.TestCase):
    def test_status_counts(self):
        service = make_service()
        service.launch_browser()
        session_id = service.create_session()
        service.new_page(session_id)
        status = service.status()
        self.assertEqual(status["browser_count"], 1)
        self.assertEqual(status["session_count"], 1)
        self.assertEqual(status["page_count"], 1)
        self.assertTrue(status["runtime_started"])
        service.shutdown()

    def test_shutdown_stops_runtime_and_clears(self):
        service = make_service()
        service.launch_browser()
        runtime = service.runtime
        result = service.shutdown()
        self.assertTrue(result["stopped"])
        self.assertTrue(runtime.stopped)
        self.assertFalse(service.is_runtime_started)
        self.assertEqual(service.list_browsers(), [])
        self.assertEqual(service.status()["page_count"], 0)


# ---------------------------------------------------------------------------
# Tool layer
# ---------------------------------------------------------------------------
class TestPlaywrightToolLayer(unittest.TestCase):
    def setUp(self):
        from Extension.playwright.tools.playwright_tools import build_playwright_tools

        self.service = make_service()
        self.addCleanup(self.service.shutdown)
        self.tools = {t.name: t for t in build_playwright_tools(self.service)}

    def test_all_tools_namespaced(self):
        self.assertTrue(self.tools)
        for name in self.tools:
            self.assertTrue(name.startswith("aether.playwright."))

    def test_expected_operations_present(self):
        for suffix in (
            "browser_launch",
            "browser_close",
            "session_create",
            "session_list",
            "session_close",
            "page_new",
            "page_list",
            "page_navigate",
            "page_reload",
            "page_back",
            "page_forward",
            "page_close",
        ):
            self.assertIn("aether.playwright." + suffix, self.tools)

    def test_tool_execution_flow(self):
        launch = self.tools["aether.playwright.browser_launch"]
        result = launch.execute()
        self.assertIn("browser_id", result)

        create = self.tools["aether.playwright.session_create"]
        session = create.execute(browser_id=result["browser_id"])
        self.assertIn("session_id", session)

        new_page = self.tools["aether.playwright.page_new"]
        page = new_page.execute(session_id=session["session_id"])
        self.assertIn("page_id", page)

        nav = self.tools["aether.playwright.page_navigate"]
        state = nav.execute(page_id=page["page_id"], url="https://tool.example")
        self.assertEqual(state["url"], "https://tool.example")

        listing = self.tools["aether.playwright.page_list"].execute()
        self.assertEqual(listing["count"], 1)

    def test_navigate_requires_fields(self):
        from agent_ai.tools.base import ToolValidationError

        nav = self.tools["aether.playwright.page_navigate"]
        with self.assertRaises(ToolValidationError):
            nav.validate({})  # missing page_id + url


# ---------------------------------------------------------------------------
# Real-browser integration (skipped when unavailable)
# ---------------------------------------------------------------------------
class TestRealBrowserIntegration(unittest.TestCase):
    def test_full_lifecycle_with_real_browser(self):
        if _REAL_SYNC_PLAYWRIGHT is None:
            self.skipTest("Playwright library is not installed")
        # Inject the (pre-imported) real runtime so the service does not have
        # to resolve ``playwright`` while ``Extension`` is on sys.path.
        service = PlaywrightService(
            headless=True,
            timeout=20,
            runtime_factory=lambda: _REAL_SYNC_PLAYWRIGHT().start(),
        )
        try:
            browser_id = service.launch_browser()
        except Exception as exc:  # pragma: no cover - environment dependent
            service.shutdown()
            self.skipTest(f"Playwright browser unavailable: {exc}")
        try:
            session_id = service.create_session(browser_id)
            page_id = service.new_page(session_id)
            state = service.navigate(
                page_id, "data:text/html,<title>Hello</title><h1>Hi</h1>"
            )
            self.assertEqual(state["title"], "Hello")
            service.navigate(page_id, "data:text/html,<title>Second</title>")
            back = service.go_back(page_id)
            self.assertEqual(back["title"], "Hello")
            forward = service.go_forward(page_id)
            self.assertEqual(forward["title"], "Second")
            service.reload(page_id)
            self.assertEqual(len(service.list_pages(session_id)), 1)
            service.close_page(page_id)
            self.assertEqual(service.list_pages(session_id), [])
            service.close_session(session_id)
            self.assertEqual(service.list_sessions(), [])
        finally:
            service.shutdown()


def test_browser_launch_error_wraps_exception():
    service = make_service()

    class FailingBrowserType:
        def launch(self, **kwargs):
            raise RuntimeError("boom")

    class FailingRuntime:
        chromium = FailingBrowserType()

    service._runtime_factory = lambda: FailingRuntime()
    with unittest.TestCase().assertRaises(BrowserLaunchError):
        service.launch_browser()


if __name__ == "__main__":
    unittest.main()
