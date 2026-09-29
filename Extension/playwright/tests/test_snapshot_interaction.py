"""Task 03 — deterministic tests for snapshot & interaction (no real browser).

A small in-memory Playwright double (page + locators + accessibility) drives
the :class:`PlaywrightService` so the snapshot / element-reference / interaction
logic can be exercised deterministically, including the failure modes: stale
ref, unknown ref, multiple match, invalid locator and missing target.

The real-browser counterpart lives in ``test_snapshot_interaction_real.py``.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

EXT_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = EXT_DIR.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Extension.playwright.services.playwright_service import (  # noqa: E402
    AmbiguousLocatorError,
    InvalidLocatorError,
    LocatorNotFoundError,
    PlaywrightService,
    RefNotFoundError,
    StaleRefError,
)
from Extension.playwright.tools.playwright_tools import (  # noqa: E402
    build_playwright_tools,
)


# ===========================================================================
# Fake Playwright DOM model
# ===========================================================================
TAG_ROLES = {
    "a": "link",
    "button": "button",
    "select": "combobox",
    "textarea": "textbox",
    "h1": "heading",
    "h2": "heading",
    "nav": "navigation",
    "main": "main",
    "img": "img",
    "output": "status",
    "option": "option",
}
INPUT_ROLES = {
    "text": "textbox",
    "search": "searchbox",
    "checkbox": "checkbox",
    "radio": "radio",
    "submit": "button",
    "file": "fileinput",
    "range": "slider",
}
SNAPSHOT_ROLES = set(TAG_ROLES.values()) | set(INPUT_ROLES.values())


class FakeElement:
    def __init__(
        self,
        tag,
        *,
        role=None,
        name=None,
        text="",
        value="",
        element_id=None,
        classes=None,
        attrs=None,
        visible=True,
        enabled=True,
        checked=False,
        selected=False,
        input_type=None,
        placeholder=None,
        href=None,
        label=None,
        triggers_download=False,
    ):
        self.tag = tag.lower()
        self.attrs = dict(attrs or {})
        if element_id:
            self.attrs["id"] = element_id
        if classes:
            self.attrs["class"] = " ".join(classes)
        if placeholder:
            self.attrs["placeholder"] = placeholder
        if href:
            self.attrs["href"] = href
        self._explicit_role = role
        self.name = name if name is not None else (text or value)
        self.label = label
        self.text = text
        self.value = value
        self.visible = visible
        self.enabled = enabled
        self.checked = checked
        self.selected = selected
        self.input_type = input_type
        self.triggers_download = triggers_download
        self.on_click = None
        # observation
        self.ref = None
        self.clicks = 0
        self.filled = None
        self.selected_values = []
        self.pressed = []
        self.scrolled_into_view = 0
        self.uploaded_files = None

    @property
    def role(self):
        if self._explicit_role:
            return self._explicit_role
        if self.tag == "input":
            return INPUT_ROLES.get(self.input_type or "text", "textbox")
        return TAG_ROLES.get(self.tag)


class FakeLocator:
    def __init__(self, page, elements):
        self.page = page
        self._elements = list(elements)

    # -- selection ------------------------------------------------------
    def count(self):
        return len(self._elements)

    @property
    def first(self):
        return FakeLocator(self.page, self._elements[:1])

    @property
    def last(self):
        return FakeLocator(self.page, self._elements[-1:])

    def nth(self, index):
        return FakeLocator(self.page, [self._elements[index]])

    def all(self):
        return [FakeLocator(self.page, [el]) for el in self._elements]

    def filter(self, has_text=None):
        if has_text is None:
            return self
        return FakeLocator(
            self.page, [el for el in self._elements if has_text in el.text]
        )

    # -- actions --------------------------------------------------------
    def _one(self):
        if len(self._elements) != 1:
            raise RuntimeError(f"expected exactly one element, got {len(self._elements)}")
        return self._elements[0]

    def click(self, **kwargs):
        el = self._one()
        if not el.visible or not el.enabled:
            raise RuntimeError("element is not actionable")
        el.clicks += 1
        if el.triggers_download:
            self.page.pending_download = FakeDownload()
        if el.on_click is not None:
            el.on_click(el)
        if el.attrs.get("href"):
            self.page.url = el.attrs["href"]

    def fill(self, value, **kwargs):
        el = self._one()
        if el.tag not in ("input", "textarea"):
            raise RuntimeError("element is not fillable")
        el.value = value
        el.filled = value

    def input_value(self, **kwargs):
        return self._one().value

    def select_option(self, **kwargs):
        el = self._one()
        if "value" in kwargs:
            value = kwargs["value"]
            selected = list(value) if isinstance(value, list) else [value]
        elif "label" in kwargs:
            selected = [kwargs["label"]]
        elif "index" in kwargs:
            selected = [f"option-{kwargs['index']}"]
        else:
            selected = []
        el.selected_values = selected
        return selected

    def press(self, key, **kwargs):
        self._one().pressed.append(key)

    def scroll_into_view_if_needed(self, **kwargs):
        self._one().scrolled_into_view += 1

    def inner_text(self, **kwargs):
        return self._one().text

    def text_content(self, **kwargs):
        return self._one().text

    def inner_html(self, **kwargs):
        el = self._one()
        return f"<{el.tag}>{el.text}</{el.tag}>"

    def get_attribute(self, name, **kwargs):
        return self._one().attrs.get(name)

    def is_visible(self, **kwargs):
        return self._one().visible

    def is_enabled(self, **kwargs):
        return self._one().enabled

    def is_checked(self, **kwargs):
        return self._one().checked

    def set_input_files(self, files, **kwargs):
        self._one().uploaded_files = list(files)

    def wait_for(self, state="visible", **kwargs):
        el = self._elements[0] if self._elements else None
        satisfied = {
            "visible": bool(el and el.visible),
            "hidden": not bool(el and el.visible),
            "attached": el is not None,
            "detached": el is None,
        }.get(state, False)
        if not satisfied:
            raise TimeoutError(f"wait_for({state}) timed out")


class FakeExpect:
    """Minimal stand-in for ``playwright.sync_api.expect``."""

    def __init__(self, target):
        self._target = target

    def _el(self):
        return self._target._elements[0]

    def to_be_enabled(self, timeout=None):
        if not self._el().enabled:
            raise AssertionError("element is not enabled")

    def to_be_disabled(self, timeout=None):
        if self._el().enabled:
            raise AssertionError("element is not disabled")

    def to_have_text(self, text, timeout=None):
        if self._el().text != text:
            raise AssertionError(f"text mismatch: {self._el().text!r} != {text!r}")

    def to_contain_text(self, text, timeout=None):
        if text not in self._el().text:
            raise AssertionError(f"{text!r} not in {self._el().text!r}")

    def to_have_value(self, value, timeout=None):
        if self._el().value != value:
            raise AssertionError(f"value mismatch: {self._el().value!r} != {value!r}")

    def to_be_checked(self, timeout=None):
        if not self._el().checked:
            raise AssertionError("element is not checked")


def fake_expect(target):
    return FakeExpect(target)


class FakeDownload:
    def __init__(self, filename="report.txt", payload=b"payload-bytes"):
        self.suggested_filename = filename
        self.url = "data:application/octet-stream"
        self._payload = payload
        self._path = None

    def path(self):
        if self._path is None:
            fd, path = tempfile.mkstemp()
            os.write(fd, self._payload)
            os.close(fd)
            self._path = path
        return self._path

    def save_as(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(self._payload)


class FakeDownloadContext:
    def __init__(self, page, timeout=None):
        self.page = page
        self.timeout = timeout
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            return False
        if self.page.pending_download is None:
            raise TimeoutError("no download was triggered")
        self.value = self.page.pending_download
        self.page.pending_download = None
        return False


class FakeDialog:
    def __init__(self, dtype="alert", message="hello", default_value=""):
        self.type = dtype
        self.message = message
        self.default_value = default_value
        self.accepted = None
        self.dismissed = False

    def accept(self, prompt_text=None):
        self.accepted = "" if prompt_text is None else prompt_text

    def dismiss(self):
        self.dismissed = True


class FakeMouse:
    def __init__(self, page):
        self.page = page
        self.wheels = []

    def wheel(self, delta_x, delta_y):
        self.wheels.append((delta_x, delta_y))


class FakeKeyboard:
    def __init__(self, page):
        self.page = page
        self.presses = []

    def press(self, key):
        self.presses.append(key)


class FakeAccessibility:
    def __init__(self, page):
        self.page = page

    def snapshot(self):
        return {
            "role": "WebArea",
            "name": self.page._title,
            "children": [
                {"role": el.role, "name": el.name}
                for el in self.page.elements
                if el.visible and el.role
            ],
        }


class FakeResponse:
    def __init__(self, status=200, ok=True):
        self.status = status
        self.ok = ok


class FakePage:
    def __init__(self, context):
        self._context = context
        self.url = "about:blank"
        self._title = "blank"
        self._closed = False
        self.elements = []
        self.pending_download = None
        self.dialog_handlers = []
        self.pending_dialogs = []
        self.load_state = "load"
        self.mouse = FakeMouse(self)
        self.keyboard = FakeKeyboard(self)
        self.accessibility = FakeAccessibility(self)
        self._aria_snapshot = "  - button \"Save\"\n"
        self._history = []
        self._index = -1
        self.set_timeouts = []

    # -- page basics ----------------------------------------------------
    def title(self):
        return self._title

    def is_closed(self):
        return self._closed

    def close(self):
        self._closed = True

    def goto(self, url, **kwargs):
        self._history = self._history[: self._index + 1]
        self._history.append(url)
        self._index = len(self._history) - 1
        self.url = url
        self._title = url
        return FakeResponse()

    def reload(self, **kwargs):
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

    def aria_snapshot(self, **kwargs):
        return self._aria_snapshot

    def evaluate(self, expression, arg=None):
        text = expression if isinstance(expression, str) else ""
        if "AETHER_SNAPSHOT_SCAN" in text:
            return self._scan()
        if "AETHER_SCROLL_TO" in text:
            self.scrolled_to = True
            return True
        return None

    def _scan(self):
        for el in self.elements:
            el.ref = None
        nodes = []
        for el in self.elements:
            role = el.role
            if not role or role not in SNAPSHOT_ROLES or not el.visible:
                continue
            ref = f"e{len(nodes) + 1}"
            el.ref = ref
            node = {"ref": ref, "role": role, "name": el.name, "tag": el.tag}
            if el.value:
                node["value"] = el.value
            if el.input_type:
                node["input_type"] = el.input_type
            if el.attrs.get("placeholder"):
                node["placeholder"] = el.attrs["placeholder"]
            if el.attrs.get("href"):
                node["href"] = el.attrs["href"]
            if not el.enabled:
                node["disabled"] = True
            if el.checked:
                node["checked"] = True
            nodes.append(node)
        return {"marker": "AETHER_SNAPSHOT_SCAN", "nodes": nodes, "count": len(nodes)}

    # -- locators -------------------------------------------------------
    def locator(self, selector):
        return FakeLocator(self, self._match(selector))

    def _match(self, selector):
        selector = selector.strip()
        if selector.startswith("[data-aether-ref="):
            ref = selector.split('"', 2)[1]
            return [el for el in self.elements if el.ref == ref]
        if selector in ("body", "*"):
            return list(self.elements)
        tag = None
        attr = None
        value = None
        body = selector
        if "#" in body:
            body, _, value = body.partition("#")
            attr = "id"
        elif "." in body:
            body, _, value = body.partition(".")
            attr = "class"
        tag = body or None
        result = []
        for el in self.elements:
            if tag and el.tag != tag:
                continue
            if attr == "id" and el.attrs.get("id") != value:
                continue
            if attr == "class" and value not in (el.attrs.get("class") or "").split():
                continue
            result.append(el)
        return result

    def _by_role(self, role, name=None, exact=False, **kwargs):
        def matches(el):
            if el.role != role:
                return False
            if name is None:
                return True
            if exact:
                return el.name == name
            return name in (el.name or "")

        return FakeLocator(self, [el for el in self.elements if matches(el)])

    get_by_role = None  # replaced below (needs default arg)
    get_by_label = None
    get_by_text = None
    get_by_placeholder = None
    get_by_alt_text = None
    get_by_title = None
    get_by_test_id = None

    # -- waits / events -------------------------------------------------
    def wait_for_load_state(self, state="load", timeout=None):
        self.set_timeouts.append(state)
        if state == "fail":
            raise TimeoutError("load state not reached")

    def wait_for_url(self, url, timeout=None):
        if not self._url_matches(url):
            raise TimeoutError(f"url did not match {url}")

    def _url_matches(self, pattern):
        if not pattern:
            return True
        if pattern == self.url:
            return True
        if pattern.startswith("**") and self.url.endswith(pattern[2:]):
            return True
        return False

    def on(self, event, handler):
        if event == "dialog":
            self.dialog_handlers.append(handler)

    def remove_listener(self, event, handler):
        if event == "dialog" and handler in self.dialog_handlers:
            self.dialog_handlers.remove(handler)

    def wait_for_event(self, event, timeout=None):
        if event == "dialog":
            if not self.pending_dialogs:
                raise TimeoutError("no dialog queued")
            return self.pending_dialogs.pop(0)
        raise TimeoutError(f"unsupported event {event}")

    def queue_dialog(self, dialog):
        self.pending_dialogs.append(dialog)

    def fire_dialog(self, dialog):
        for handler in list(self.dialog_handlers):
            handler(dialog)
        return dialog

    def expect_download(self, timeout=None):
        return FakeDownloadContext(self, timeout)


def _get_by_role(self, role, name=None, exact=False, **kwargs):
    return self._by_role(role, name=name, exact=exact)


def _get_by_attr(attr):
    def getter(self, value, exact=False, **kwargs):
        return FakeLocator(self, [el for el in self.elements if el.attrs.get(attr) == value])

    return getter


def _get_by_text(self, text, exact=False, **kwargs):
    return FakeLocator(
        self,
        [el for el in self.elements if (el.text == text if exact else text in (el.text or ""))],
    )


FakePage.get_by_role = _get_by_role
FakePage.get_by_label = _get_by_attr("label")
FakePage.get_by_placeholder = _get_by_attr("placeholder")
FakePage.get_by_alt_text = _get_by_attr("alt")
FakePage.get_by_title = _get_by_attr("title")
FakePage.get_by_test_id = _get_by_attr("data-testid")
FakePage.get_by_text = _get_by_text


class FakeContext:
    def __init__(self, browser):
        self._browser = browser
        self._pages = []
        self.closed = False

    def new_page(self):
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
        ctx = FakeContext(self)
        self._contexts.append(ctx)
        return ctx

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
# Fixture + helpers
# ===========================================================================
def build_fixture_page():
    """A small page mimicking the shapes of a real form."""
    email = FakeElement(
        "input",
        element_id="email",
        label="Email",
        name="Email",
        placeholder="you@example.com",
        input_type="text",
    )
    # label association is exposed through FakeElement.label -> get_by_label
    email.attrs["label"] = "Email"
    state = FakeElement(
        "select",
        element_id="state",
        name="State",
        label="State",
        attrs={"label": "State"},
    )
    save = FakeElement("button", element_id="save", name="Save", text="Save")
    out = FakeElement("output", element_id="out", name="idle", text="idle")
    item1 = FakeElement("button", classes=["item"], name="Item", text="Item")
    item2 = FakeElement("button", classes=["item"], name="Item", text="Item")
    home = FakeElement("a", name="Home", text="Home", href="https://example.com")
    hidden = FakeElement("button", element_id="hidden-target", name="Hidden", visible=False)
    upload = FakeElement(
        "input",
        element_id="upload",
        name="Upload file",
        input_type="file",
    )
    download = FakeElement(
        "a",
        element_id="download-link",
        name="Download",
        text="Download",
        triggers_download=True,
    )
    disabled = FakeElement(
        "button", element_id="disabled-btn", name="Disabled action", text="Disabled action", enabled=False
    )
    agree = FakeElement(
        "input", element_id="agree", name="Agree", input_type="checkbox"
    )

    def on_save(_el):
        out.text = f"saved:{email.value}"
        out.name = out.text

    save.on_click = on_save
    page_elements = [email, state, save, out, item1, item2, home, hidden, upload, download, disabled, agree]
    return page_elements


def make_service(tmp_dir):
    service = PlaywrightService(
        runtime_factory=FakePlaywright,
        expect_factory=lambda: fake_expect,
        artifact_dir=str(tmp_dir),
    )
    page_id = service.new_page(url="about:blank")
    page = service.page_object(page_id)
    page.elements = build_fixture_page()
    return service, page_id, page


def ref_named(snapshot, name, role=None):
    for ref, info in snapshot["refs"].items():
        if info["name"] == name and (role is None or info["role"] == role):
            return ref
    raise AssertionError(f"no ref {name!r} in {snapshot['refs']}")


# ===========================================================================
# Tests
# ===========================================================================
class TestToolRegistration(unittest.TestCase):
    def test_task03_tools_registered_and_namespaced(self):
        tools = {t.name: t for t in build_playwright_tools(PlaywrightService())}
        expected = [
            "browser_snapshot",
            "browser_refs",
            "browser_click",
            "browser_fill",
            "browser_select",
            "browser_press",
            "browser_scroll",
            "browser_extract",
            "browser_wait",
            "browser_upload",
            "browser_download",
            "browser_dialog",
        ]
        for suffix in expected:
            self.assertIn("aether.playwright." + suffix, tools)
        for name in tools:
            self.assertTrue(name.startswith("aether.playwright."))

    def test_tool_schemas_require_page_id(self):
        tools = {t.name: t for t in build_playwright_tools(PlaywrightService())}
        for suffix in ("browser_snapshot", "browser_click", "browser_fill"):
            schema = tools["aether.playwright." + suffix].input_schema
            self.assertIn("page_id", schema["required"])
        self.assertIn("value", tools["aether.playwright.browser_fill"].input_schema["required"])

    def test_validation_rejects_missing_required(self):
        from agent_ai.tools.base import ToolValidationError

        tools = {t.name: t for t in build_playwright_tools(PlaywrightService())}
        with self.assertRaises(ToolValidationError):
            tools["aether.playwright.browser_snapshot"].validate({})


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.page_id, self.page = make_service(self._tmp.name)

    def test_snapshot_shape(self):
        snap = self.service.snapshot(self.page_id)
        self.assertEqual(snap["page_id"], self.page_id)
        self.assertIn("url", snap)
        self.assertIn("title", snap)
        self.assertIn("snapshot", snap)
        self.assertIn("refs", snap)
        self.assertIn("aria_snapshot", snap)
        self.assertEqual(snap["element_count"], len(snap["refs"]))
        self.assertIn("[ref=", snap["snapshot"])
        import json

        json.dumps(snap)

    def test_snapshot_skips_hidden_elements(self):
        snap = self.service.snapshot(self.page_id)
        self.assertNotIn("Hidden", [info["name"] for info in snap["refs"].values()])

    def test_snapshot_generation_increases(self):
        first = self.service.snapshot(self.page_id)
        second = self.service.snapshot(self.page_id)
        self.assertGreater(second["generation"], first["generation"])

    def test_snapshot_refs_helper(self):
        snap = self.service.snapshot(self.page_id)
        refs = self.service.snapshot_refs(self.page_id)
        self.assertEqual(set(refs["refs"]), set(snap["refs"]))
        self.assertEqual(refs["generation"], snap["generation"])

    def test_snapshot_refs_without_snapshot(self):
        with self.assertRaises(RefNotFoundError):
            self.service.snapshot_refs(self.page_id)

    def test_accessibility_fallback(self):
        self.page._aria_snapshot = None
        self.page.aria_snapshot = lambda **kw: None
        self.page.locator = lambda selector: FakeLocator(self.page, [])
        snap = self.service.snapshot(self.page_id)
        self.assertIn("button", snap["aria_snapshot"])

    def test_mode_off_disables_aria(self):
        snap = self.service.snapshot(self.page_id, mode="off")
        self.assertIsNone(snap["aria_snapshot"])


class TestWorkflow(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.page_id, self.page = make_service(self._tmp.name)

    def test_snapshot_fill_click_wait_snapshot(self):
        snap = self.service.snapshot(self.page_id)
        email_ref = ref_named(snap, "Email")
        save_ref = ref_named(snap, "Save")

        self.service.fill(self.page_id, "fake@example.com", ref=email_ref)
        self.assertEqual(self.page.elements[0].value, "fake@example.com")

        self.service.click(self.page_id, ref=save_ref)
        self.assertEqual(self.page.elements[2].clicks, 1)

        self.service.wait(
            self.page_id, condition="text", locator={"css": "output"}, text="saved:"
        )
        value = self.service.extract(
            self.page_id, what="text", locator={"css": "output"}
        )
        self.assertEqual(value["value"], "saved:fake@example.com")

        snap2 = self.service.snapshot(self.page_id)
        self.assertGreater(snap2["generation"], snap["generation"])

    def test_fill_clear_false_appends(self):
        snap = self.service.snapshot(self.page_id)
        email_ref = ref_named(snap, "Email")
        self.service.fill(self.page_id, "abc", ref=email_ref)
        self.service.fill(self.page_id, "def", ref=email_ref, clear=False)
        self.assertEqual(self.page.elements[0].value, "abcdef")


class TestRefErrors(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.page_id, self.page = make_service(self._tmp.name)

    def test_unknown_ref(self):
        self.service.snapshot(self.page_id)
        with self.assertRaises(RefNotFoundError):
            self.service.click(self.page_id, ref="e999")

    def test_ref_without_snapshot(self):
        with self.assertRaises(RefNotFoundError):
            self.service.click(self.page_id, ref="e1")

    def test_stale_ref_after_dom_change(self):
        snap = self.service.snapshot(self.page_id)
        email_ref = ref_named(snap, "Email")
        for el in self.page.elements:
            el.ref = None  # simulate a fresh DOM without our attributes
        with self.assertRaises(StaleRefError):
            self.service.fill(self.page_id, "x", ref=email_ref)

    def test_ref_invalidated_by_navigation(self):
        snap = self.service.snapshot(self.page_id)
        email_ref = ref_named(snap, "Email")
        self.service.navigate(self.page_id, "https://example.com/other")
        with self.assertRaises(RefNotFoundError):
            self.service.click(self.page_id, ref=email_ref)


class TestLocatorErrors(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.page_id, self.page = make_service(self._tmp.name)

    def test_neither_ref_nor_locator(self):
        with self.assertRaises(InvalidLocatorError):
            self.service.click(self.page_id)

    def test_both_ref_and_locator(self):
        self.service.snapshot(self.page_id)
        with self.assertRaises(InvalidLocatorError):
            self.service.click(self.page_id, ref="e1", locator={"text": "Save"})

    def test_unsupported_locator(self):
        with self.assertRaises(InvalidLocatorError):
            self.service.click(self.page_id, locator={"nonsense": "x"})

    def test_not_found(self):
        with self.assertRaises(LocatorNotFoundError):
            self.service.click(self.page_id, locator={"role": "button", "name": "Nope"})

    def test_ambiguous(self):
        with self.assertRaises(AmbiguousLocatorError):
            self.service.click(self.page_id, locator={"role": "button", "name": "Item"})

    def test_ambiguous_resolved_with_nth_first_last(self):
        for spec in (
            {"role": "button", "name": "Item", "nth": 1},
            {"role": "button", "name": "Item", "first": True},
            {"role": "button", "name": "Item", "last": True},
        ):
            result = self.service.click(self.page_id, locator=spec)
            self.assertEqual(result["action"], "click")
        self.assertEqual(self.page.elements[4].clicks + self.page.elements[5].clicks, 3)

    def test_nth_out_of_range(self):
        with self.assertRaises(LocatorNotFoundError):
            self.service.click(
                self.page_id, locator={"role": "button", "name": "Item", "nth": 5}
            )

    def test_semantic_locator_strategies(self):
        self.service.fill(self.page_id, "v", locator={"label": "Email"})
        self.assertEqual(self.page.elements[0].value, "v")
        self.service.fill(self.page_id, "w", locator={"placeholder": "you@example.com"})
        self.assertEqual(self.page.elements[0].value, "w")
        self.service.click(self.page_id, locator={"text": "Save"})
        self.assertEqual(self.page.elements[2].clicks, 1)
        self.service.fill(self.page_id, "z", locator={"css": "#email"})
        self.assertEqual(self.page.elements[0].value, "z")
        self.service.click(self.page_id, locator={"role": "button", "name": "Save"})
        self.assertEqual(self.page.elements[2].clicks, 2)


class TestInteraction(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.page_id, self.page = make_service(self._tmp.name)

    def test_select(self):
        result = self.service.select_option(
            self.page_id, locator={"css": "#state"}, value="id"
        )
        self.assertEqual(result["selected"], ["id"])
        self.assertEqual(self.page.elements[1].selected_values, ["id"])
        by_label = self.service.select_option(
            self.page_id, locator={"css": "#state"}, label="Indonesia"
        )
        self.assertEqual(by_label["selected"], ["Indonesia"])
        by_index = self.service.select_option(
            self.page_id, locator={"css": "#state"}, index=2
        )
        self.assertEqual(by_index["selected"], ["option-2"])

    def test_select_requires_option(self):
        with self.assertRaises(InvalidLocatorError):
            self.service.select_option(self.page_id, locator={"css": "#state"})

    def test_press_on_element(self):
        result = self.service.press(
            self.page_id, "Enter", locator={"css": "#email"}
        )
        self.assertEqual(result["scope"], "element")
        self.assertEqual(self.page.elements[0].pressed, ["Enter"])

    def test_press_on_page(self):
        result = self.service.press(self.page_id, "Escape")
        self.assertEqual(result["scope"], "page")
        self.assertEqual(self.page.keyboard.presses, ["Escape"])

    def test_scroll_delta(self):
        result = self.service.scroll(self.page_id, delta_x=10, delta_y=250)
        self.assertEqual(result["delta_y"], 250)
        self.assertEqual(self.page.mouse.wheels, [(10.0, 250.0)])

    def test_scroll_to_bottom(self):
        result = self.service.scroll(self.page_id, to="bottom")
        self.assertEqual(result["to"], "bottom")
        self.assertTrue(getattr(self.page, "scrolled_to", False))

    def test_scroll_element_into_view(self):
        result = self.service.scroll(self.page_id, locator={"css": "#save"})
        self.assertTrue(result["into_view"])
        self.assertEqual(self.page.elements[2].scrolled_into_view, 1)

    def test_extract_text_value_attribute_count(self):
        self.assertEqual(
            self.service.extract(self.page_id, what="text", locator={"css": "#save"})[
                "value"
            ],
            "Save",
        )
        self.service.fill(self.page_id, "e@x.y", locator={"css": "#email"})
        self.assertEqual(
            self.service.extract(self.page_id, what="value", locator={"css": "#email"})[
                "value"
            ],
            "e@x.y",
        )
        self.assertEqual(
            self.service.extract(
                self.page_id, what="attribute", attribute="href", locator={"text": "Home"}
            )["value"],
            "https://example.com",
        )
        count = self.service.extract(
            self.page_id, what="count", locator={"role": "button", "name": "Item"}
        )
        self.assertEqual(count["value"], 2)

    def test_extract_visible_enabled_selected(self):
        self.assertTrue(
            self.service.extract(
                self.page_id, what="visible", locator={"css": "#save"}
            )["value"]
        )
        self.assertFalse(
            self.service.extract(
                self.page_id, what="enabled", locator={"css": "#disabled-btn"}
            )["value"]
        )
        self.assertFalse(
            self.service.extract(
                self.page_id, what="selected", locator={"css": "#agree"}
            )["value"]
        )

    def test_extract_attribute_requires_name(self):
        with self.assertRaises(InvalidLocatorError):
            self.service.extract(
                self.page_id, what="attribute", locator={"css": "#save"}
            )

    def test_extract_list_of_fields(self):
        result = self.service.extract(
            self.page_id, what=["text", "visible"], locator={"css": "#save"}
        )
        self.assertEqual(result["values"]["text"], "Save")
        self.assertTrue(result["values"]["visible"])

    def test_extract_count_only_allows_multiple(self):
        result = self.service.extract(
            self.page_id, what="count", locator={"role": "button", "name": "Item"}
        )
        self.assertEqual(result["value"], 2)

    def test_extract_all(self):
        result = self.service.extract(
            self.page_id, what="all", locator={"css": "#save"}
        )
        values = result["values"]
        self.assertEqual(values["text"], "Save")
        self.assertEqual(values["count"], 1)
        self.assertTrue(values["visible"])
        self.assertTrue(values["enabled"])

    def test_upload(self):
        result = self.service.upload(
            self.page_id, ["a.txt", "b.txt"], locator={"css": "#upload"}
        )
        self.assertEqual(result["count"], 2)
        self.assertEqual(self.page.elements[8].uploaded_files, ["a.txt", "b.txt"])

    def test_upload_requires_files(self):
        with self.assertRaises(InvalidLocatorError):
            self.service.upload(self.page_id, [], locator={"css": "#upload"})

    def test_download(self):
        result = self.service.download(
            self.page_id, locator={"css": "#download-link"}
        )
        self.assertTrue(Path(result["path"]).exists())
        self.assertEqual(result["filename"], "report.txt")
        self.assertEqual(Path(result["path"]).read_bytes(), b"payload-bytes")

    def test_download_custom_filename(self):
        result = self.service.download(
            self.page_id,
            locator={"css": "#download-link"},
            filename="custom.bin",
        )
        self.assertEqual(result["filename"], "custom.bin")

    def test_dialog_configure_and_autohandle(self):
        config = self.service.dialog(self.page_id, action="accept")
        self.assertTrue(config["configured"])
        dialog = self.page.fire_dialog(FakeDialog("alert", "boom"))
        self.assertEqual(dialog.accepted, "")
        records = self.service.dialogs(self.page_id)
        self.assertEqual(records[-1]["message"], "boom")
        self.assertTrue(records[-1]["handled"])

    def test_dialog_dismiss_and_prompt_text(self):
        self.service.dialog(self.page_id, action="dismiss")
        dialog = self.page.fire_dialog(FakeDialog("confirm", "sure?"))
        self.assertTrue(dialog.dismissed)
        self.service.dialog(self.page_id, action="accept", prompt_text="AETHER")
        dialog2 = self.page.fire_dialog(FakeDialog("prompt", "name?", "anon"))
        self.assertEqual(dialog2.accepted, "AETHER")

    def test_dialog_wait_for_next(self):
        self.page.queue_dialog(FakeDialog("prompt", "name?", "anon"))
        result = self.service.dialog(
            self.page_id, action="accept", prompt_text="bob", wait=True
        )
        self.assertTrue(result["handled"])
        self.assertEqual(result["type"], "prompt")
        self.assertEqual(result["message"], "name?")

    def test_dialog_invalid_action(self):
        with self.assertRaises(InvalidLocatorError):
            self.service.dialog(self.page_id, action="explode")


class TestWait(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.page_id, self.page = make_service(self._tmp.name)

    def test_wait_locator_states(self):
        self.assertTrue(
            self.service.wait(
                self.page_id, condition="visible", locator={"css": "#save"}
            )["condition"]
            == "visible"
        )
        self.service.wait(
            self.page_id, condition="attached", locator={"css": "#save"}
        )
        self.service.wait(
            self.page_id, condition="hidden", locator={"css": "#hidden-target"}
        )
        self.service.wait(
            self.page_id, condition="detached", locator={"css": ".missing"}
        )

    def test_wait_enabled_disabled(self):
        self.service.wait(
            self.page_id, condition="enabled", locator={"css": "#save"}
        )
        self.service.wait(
            self.page_id, condition="disabled", locator={"css": "#disabled-btn"}
        )

    def test_wait_text_and_value(self):
        self.page.elements[3].text = "saved:xyz"
        self.service.wait(
            self.page_id, condition="text", locator={"css": "output"}, text="saved:"
        )
        self.service.fill(self.page_id, "q", locator={"css": "#email"})
        self.service.wait(
            self.page_id,
            condition="value",
            locator={"css": "#email"},
            text="q",
        )
        with self.assertRaises(Exception):
            self.service.wait(
                self.page_id, condition="text", locator={"css": "output"}, text="nope"
            )

    def test_wait_url_and_load(self):
        self.service.wait(self.page_id, condition="url", url="about:blank")
        self.service.wait(self.page_id, condition="load", load_state="networkidle")
        self.assertIn("networkidle", self.page.set_timeouts)

    def test_wait_unsupported_condition(self):
        with self.assertRaises(InvalidLocatorError):
            self.service.wait(self.page_id, condition="teleport")

    def test_wait_text_requires_text(self):
        with self.assertRaises(InvalidLocatorError):
            self.service.wait(self.page_id, condition="text", locator={"css": "#save"})


class TestExtensionRegistration(unittest.TestCase):
    """The Task 03 tools must be exposed through ``context.tools``."""

    def test_enable_registers_task03_tools_through_context(self):
        import importlib

        from agent_ai.extensions.capabilities import CapabilityRegistry
        from agent_ai.extensions.context import ExtensionContext
        from agent_ai.extensions.manifest import load_manifest

        manifest = load_manifest(EXT_DIR / "manifest.json")
        registry = CapabilityRegistry()
        context = ExtensionContext(
            extension_root=EXT_DIR, manifest=manifest, capability_registry=registry
        )
        module = importlib.import_module("Extension.playwright.extension")
        extension = module.PlaywrightExtension()
        extension.register(context)
        extension.enable(context)
        try:
            registered = {record.id for record in context.tools.list()}
            for suffix in (
                "browser_snapshot",
                "browser_refs",
                "browser_click",
                "browser_fill",
                "browser_select",
                "browser_press",
                "browser_scroll",
                "browser_extract",
                "browser_wait",
                "browser_upload",
                "browser_download",
                "browser_dialog",
            ):
                self.assertIn("aether.playwright." + suffix, registered)
            # namespaced capabilities only
            self.assertTrue(all(name.startswith("aether.playwright.") for name in registered))
            # the service capability still exposes the live service instance
            service_cap = context.services.get("aether.playwright.playwright_service")
            self.assertIsNotNone(service_cap)
            self.assertIsInstance(
                service_cap.metadata.get("service_instance"), PlaywrightService
            )
        finally:
            extension.disable(context)


if __name__ == "__main__":
    unittest.main()
