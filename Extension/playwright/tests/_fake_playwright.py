"""Reusable in-memory Playwright double for the Task 05 tests.

This is a small, dependency-free stand-in for the real Playwright engine that
covers everything the Playwright Extension touches:

* the browser / session (BrowserContext) / page lifecycle,
* page events (console / pageerror / request / response / requestfailed),
* a locator surface good enough for DOM inspection and screenshots,
* Playwright tracing,
* the BrowserContext **storage-state** surface used by Task 05
  (``storage_state`` / ``add_cookies`` / ``add_init_script`` / ``cookies``)
  plus ``new_context(storage_state=...)``.

It keeps the Task 05 tests deterministic and fully offline. The real-browser
counterpart lives in ``test_debug_ui_real.py``.
"""

from __future__ import annotations

from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_png(width: int, height: int) -> bytes:
    """A minimal but valid PNG header so dimension parsing works offline."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = (
        b"\x00\x00\x00\rIHDR"
        + int(width).to_bytes(4, "big")
        + int(height).to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )
    return signature + ihdr


# ---------------------------------------------------------------------------
# Elements / locators
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Page events
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Page / context / browser
# ---------------------------------------------------------------------------
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
            return [el for el in self.elements if wanted in (el.attrs.get("class") or "").split()]
        return []


class FakeContext:
    """A BrowserContext double with the storage-state surface Task 05 needs."""

    def __init__(self, browser, storage_state=None):
        self._browser = browser
        self._pages = []
        self.closed = False
        self.tracing = FakeTracing()
        self.init_scripts = []
        self.cookies_store = []
        self.origins_store = {}
        self.initial_storage_state = None
        if isinstance(storage_state, dict):
            self.initial_storage_state = storage_state
            self.cookies_store = [dict(c) for c in storage_state.get("cookies", [])]
            for origin in storage_state.get("origins", []):
                if isinstance(origin, dict) and origin.get("origin"):
                    bucket = self.origins_store.setdefault(origin["origin"], {})
                    for kv in origin.get("localStorage", []):
                        bucket[kv["name"]] = kv["value"]

    # -- pages ----------------------------------------------------------
    def new_page(self):
        if self.closed:
            raise RuntimeError("context is closed")
        page = FakePage(self)
        self._pages.append(page)
        return page

    def close(self):
        self.closed = True
        self._pages = []

    # -- storage state --------------------------------------------------
    def set_local_storage(self, origin, items):
        bucket = self.origins_store.setdefault(origin, {})
        for kv in items:
            bucket[kv["name"]] = kv["value"]

    def set_cookie(self, cookie):
        self.cookies_store.append(dict(cookie))

    def storage_state(self, path=None):
        return {
            "cookies": [dict(c) for c in self.cookies_store],
            "origins": [
                {
                    "origin": origin,
                    "localStorage": [{"name": k, "value": v} for k, v in items.items()],
                }
                for origin, items in self.origins_store.items()
            ],
        }

    def add_cookies(self, cookies):
        for cookie in cookies:
            self.cookies_store.append(dict(cookie))

    def add_init_script(self, script=None, **kwargs):
        self.init_scripts.append(script)

    def cookies(self, urls=None):
        return [dict(c) for c in self.cookies_store]


class FakeBrowser:
    def __init__(self):
        self._contexts = []
        self.closed = False
        self.last_context_kwargs = None

    def new_context(self, **kwargs):
        self.last_context_kwargs = dict(kwargs)
        context = FakeContext(self, storage_state=kwargs.get("storage_state"))
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def make_service(tmp_dir, **kwargs):
    """Build a service backed by the fake runtime + a temp state dir."""
    from Extension.playwright.services.playwright_service import PlaywrightService

    service = PlaywrightService(
        runtime_factory=FakePlaywright,
        artifact_dir=str(tmp_dir),
        state_dir=kwargs.pop("state_dir", str(tmp_dir)),
        **kwargs,
    )
    browser_id = service.launch_browser()
    session_id = service.create_session(browser_id)
    return service, session_id


def add_page(service, session_id, elements=None):
    page_id = service.new_page(session_id)
    page = service.page_object(page_id)
    if elements:
        page.elements = list(elements)
    return page_id, page
