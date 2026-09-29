"""Task 03 — real-browser integration tests for snapshot & interaction.

These tests drive the *real* Playwright engine against a local, static HTML
fixture (``file://`` URL, no network) and prove the full workflow::

    launch -> session -> page -> navigate -> snapshot -> fill -> click
           -> wait -> snapshot

plus the edge cases (stale ref, multiple match, invalid locator, extract,
select, press, scroll, upload/download, dialog). The whole class is skipped
when no Playwright browser can be launched in the environment.
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


FIXTURE_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>AETHER Playwright Demo</title></head>
<body>
  <h1>Playwright Demo</h1>
  <form>
    <label for="email">Email</label>
    <input id="email" name="email" type="text" placeholder="you@example.com">
    <label for="state">State</label>
    <select id="state" name="state">
      <option value="">Choose</option>
      <option value="id">Indonesia</option>
      <option value="sg">Singapore</option>
    </select>
    <button id="save" type="button">Save</button>
    <button id="disabled-btn" type="button" disabled>Disabled action</button>
  </form>
  <output id="out">idle</output>
  <div>
    <button class="item" type="button">Item</button>
    <button class="item" type="button">Item</button>
  </div>
  <a id="home" href="https://example.com">Home</a>
  <label for="agree">Agree</label>
  <input id="agree" type="checkbox">
  <div id="hidden-target" style="display:none">Hidden content</div>
  <input id="upload" type="file" aria-label="Upload file">
  <a id="download-link"
     href="data:text/plain;base64,aGVsbG8gd29ybGQ="
     download="hello.txt">Download</a>
  <button id="alert-btn" type="button" onclick="alert('hello dialog')">Alert</button>
  <button id="prompt-btn" type="button"
          onclick="window.promptResult = prompt('Your name?', 'anon')">Prompt</button>
  <div style="height: 2000px"></div>
  <button id="bottom-btn" type="button">Bottom</button>
  <script>
    document.getElementById('save').addEventListener('click', function () {
      document.getElementById('out').textContent =
        'saved:' + document.getElementById('email').value;
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


class TestSnapshotInteractionReal(unittest.TestCase):
    """End-to-end Task 03 behaviour on a real Chromium browser."""

    @classmethod
    def setUpClass(cls):
        if not _real_browser_available():
            raise unittest.SkipTest("Playwright browser is not available")
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        (root / "artifacts").mkdir(exist_ok=True)
        fixture = root / "fixture.html"
        fixture.write_text(FIXTURE_HTML, encoding="utf-8")
        cls.fixture_url = fixture.as_uri()
        cls.upload_file = root / "upload-me.txt"
        cls.upload_file.write_text("upload payload", encoding="utf-8")
        cls.service = PlaywrightService(
            headless=True,
            timeout=20,
            artifact_dir=str(root / "artifacts"),
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

    def setUp(self):
        self.page_id = self.service.new_page(self.session_id, self.fixture_url)

    def tearDown(self):
        try:
            self.service.close_page(self.page_id)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _snapshot(self, **kwargs):
        return self.service.snapshot(self.page_id, **kwargs)

    def _ref(self, name, *, role=None, snapshot=None):
        snap = snapshot or self._snapshot()
        for ref, info in snap["refs"].items():
            if info.get("name") == name and (role is None or info.get("role") == role):
                return ref
        raise AssertionError(f"no ref named {name!r} in {snap['refs']}")

    # ------------------------------------------------------------------
    # Definition of Done: snapshot -> refs -> fill/click -> wait -> snapshot
    # ------------------------------------------------------------------
    def test_workflow_snapshot_fill_click_wait_snapshot(self):
        snap = self._snapshot()
        self.assertEqual(snap["page_id"], self.page_id)
        self.assertEqual(snap["title"], "AETHER Playwright Demo")
        self.assertTrue(snap["url"].startswith("file://"))
        self.assertGreater(snap["element_count"], 0)
        self.assertIn("Email", [info["name"] for info in snap["refs"].values()])
        # snapshot text carries the refs so the LLM can pick one
        self.assertIn("[ref=", snap["snapshot"])

        email_ref = self._ref("Email", snapshot=snap)
        save_ref = self._ref("Save", snapshot=snap)

        filled = self.service.fill(self.page_id, "dev@example.com", ref=email_ref)
        self.assertEqual(filled["action"], "fill")
        clicked = self.service.click(self.page_id, ref=save_ref)
        self.assertEqual(clicked["action"], "click")

        self.service.wait(
            self.page_id,
            condition="text",
            locator={"css": "#out"},
            text="saved:dev@example.com",
        )

        # the page changed (DOM mutation) -> fresh snapshot gives fresh refs
        snap2 = self._snapshot()
        self.assertGreaterEqual(snap2["generation"], snap["generation"] + 1)
        out_ref = self._ref("saved:dev@example.com", role="status", snapshot=snap2)
        value = self.service.extract(self.page_id, what="text", ref=out_ref)
        self.assertEqual(value["value"], "saved:dev@example.com")

    def test_snapshot_tool_layer_flow(self):
        tools = {tool.name: tool for tool in build_playwright_tools(self.service)}
        snap = tools["aether.playwright.browser_snapshot"].execute(page_id=self.page_id)
        self.assertIn("refs", snap)
        refs = snap["refs"]
        email_ref = next(r for r, i in refs.items() if i["name"] == "Email")
        tools["aether.playwright.browser_fill"].execute(
            page_id=self.page_id, ref=email_ref, value="tool@example.com"
        )
        extracted = tools["aether.playwright.browser_extract"].execute(
            page_id=self.page_id, ref=email_ref, what="value"
        )
        self.assertEqual(extracted["value"], "tool@example.com")

    # ------------------------------------------------------------------
    # Snapshot shape / modes
    # ------------------------------------------------------------------
    def test_snapshot_returns_aria_and_refs(self):
        snap = self._snapshot()
        self.assertEqual(snap["mode"], "ai")
        # Playwright's ARIA snapshot is used when available (mode="ai").
        self.assertIsInstance(snap["aria_snapshot"], str)
        # refs are JSON-serialisable
        import json

        json.dumps(snap)
        for ref, info in snap["refs"].items():
            self.assertTrue(ref.startswith("e"))
            self.assertIn("role", info)

    def test_snapshot_selector_subtree(self):
        snap = self._snapshot(selector="#hidden-target")
        self.assertEqual(snap["element_count"], 0)

    def test_snapshot_mode_off_skips_aria(self):
        snap = self._snapshot(mode="off")
        self.assertIsNone(snap["aria_snapshot"])

    # ------------------------------------------------------------------
    # Stale / invalid references
    # ------------------------------------------------------------------
    def test_stale_ref_after_navigation(self):
        email_ref = self._ref("Email")
        self.service.navigate(self.page_id, "data:text/html,<title>Other</title><p>hi</p>")
        with self.assertRaises(RefNotFoundError):
            self.service.click(self.page_id, ref=email_ref)

    def test_stale_ref_after_dom_change(self):
        email_ref = self._ref("Email")
        # remove the element behind the ref without navigating
        self.service.page_object(self.page_id).evaluate(
            "() => document.getElementById('email').remove()"
        )
        with self.assertRaises(StaleRefError):
            self.service.fill(self.page_id, "x", ref=email_ref)

    def test_ref_before_snapshot(self):
        fresh_page = self.service.new_page(self.session_id, self.fixture_url)
        try:
            with self.assertRaises(RefNotFoundError):
                self.service.click(fresh_page, ref="e1")
        finally:
            self.service.close_page(fresh_page)

    def test_unknown_ref(self):
        self._snapshot()
        with self.assertRaises(RefNotFoundError):
            self.service.click(self.page_id, ref="e999")

    # ------------------------------------------------------------------
    # Locator strategies / ambiguity
    # ------------------------------------------------------------------
    def test_semantic_locators(self):
        # role + name
        self.service.fill(
            self.page_id, "a@b.c", locator={"role": "textbox", "name": "Email"}
        )
        self.assertEqual(
            self.service.extract(
                self.page_id, what="value", locator={"role": "textbox", "name": "Email"}
            )["value"],
            "a@b.c",
        )
        # label
        self.service.fill(self.page_id, "x", locator={"label": "Email"})
        # placeholder
        self.service.fill(
            self.page_id, "y", locator={"placeholder": "you@example.com"}
        )
        # text
        self.service.click(self.page_id, locator={"text": "Save"})
        # css (last resort)
        self.service.fill(self.page_id, "z", locator={"css": "#email"})
        self.assertEqual(
            self.service.extract(self.page_id, what="value", locator={"css": "#email"})[
                "value"
            ],
            "z",
        )

    def test_ambiguous_locator_error(self):
        with self.assertRaises(AmbiguousLocatorError):
            self.service.click(
                self.page_id, locator={"role": "button", "name": "Item"}
            )

    def test_ambiguous_locator_disentangled_with_nth(self):
        result = self.service.click(
            self.page_id, locator={"role": "button", "name": "Item", "nth": 1}
        )
        self.assertEqual(result["action"], "click")
        count = self.service.extract(
            self.page_id, what="count", locator={"role": "button", "name": "Item"}
        )
        self.assertEqual(count["value"], 2)

    def test_invalid_locator_error(self):
        with self.assertRaises(InvalidLocatorError):
            self.service.click(self.page_id, locator={"nonsense": "x"})
        with self.assertRaises(InvalidLocatorError):
            self.service.click(self.page_id)  # neither ref nor locator

    def test_locator_not_found(self):
        with self.assertRaises(LocatorNotFoundError):
            self.service.click(self.page_id, locator={"role": "button", "name": "Nope"})

    # ------------------------------------------------------------------
    # Extract
    # ------------------------------------------------------------------
    def test_extract_variants(self):
        self.service.fill(
            self.page_id, "val", locator={"role": "textbox", "name": "Email"}
        )
        text = self.service.extract(
            self.page_id, what="text", locator={"text": "Save"}
        )
        self.assertEqual(text["value"], "Save")

        value = self.service.extract(
            self.page_id, what="value", locator={"role": "textbox", "name": "Email"}
        )
        self.assertEqual(value["value"], "val")

        href = self.service.extract(
            self.page_id, what="attribute", attribute="href", locator={"text": "Home"}
        )
        self.assertEqual(href["value"], "https://example.com")

        count = self.service.extract(
            self.page_id, what="count", locator={"role": "button", "name": "Item"}
        )
        self.assertEqual(count["value"], 2)

        visible = self.service.extract(
            self.page_id, what="visible", locator={"text": "Save"}
        )
        self.assertTrue(visible["value"])
        hidden = self.service.extract(
            self.page_id, what="visible", locator={"css": "#hidden-target"}
        )
        self.assertFalse(hidden["value"])

        enabled = self.service.extract(
            self.page_id, what="enabled", locator={"text": "Save"}
        )
        self.assertTrue(enabled["value"])
        disabled = self.service.extract(
            self.page_id, what="enabled", locator={"text": "Disabled action"}
        )
        self.assertFalse(disabled["value"])

        selected = self.service.extract(
            self.page_id, what="selected", locator={"role": "checkbox", "name": "Agree"}
        )
        self.assertFalse(selected["value"])
        self.service.click(
            self.page_id, locator={"role": "checkbox", "name": "Agree"}
        )
        selected2 = self.service.extract(
            self.page_id, what="selected", locator={"role": "checkbox", "name": "Agree"}
        )
        self.assertTrue(selected2["value"])

    def test_extract_multi_fields(self):
        result = self.service.extract(
            self.page_id,
            what=["text", "visible", "enabled"],
            locator={"text": "Save"},
        )
        self.assertEqual(result["values"]["text"], "Save")
        self.assertTrue(result["values"]["visible"])

    # ------------------------------------------------------------------
    # Select / press / scroll
    # ------------------------------------------------------------------
    def test_select_option(self):
        result = self.service.select_option(
            self.page_id, locator={"label": "State"}, value="id"
        )
        self.assertEqual(result["selected"], ["id"])
        value = self.service.extract(
            self.page_id, what="value", locator={"label": "State"}
        )
        self.assertEqual(value["value"], "id")

    def test_press_on_element_and_page(self):
        email_ref = self._ref("Email")
        self.service.fill(self.page_id, "abc", ref=email_ref)
        self.service.press(self.page_id, "End", ref=email_ref)
        self.service.fill(self.page_id, "xyz", ref=email_ref)
        page_press = self.service.press(self.page_id, "Escape")
        self.assertEqual(page_press["scope"], "page")

    def test_scroll(self):
        by_delta = self.service.scroll(self.page_id, delta_y=500)
        self.assertEqual(by_delta["delta_y"], 500)
        to_bottom = self.service.scroll(self.page_id, to="bottom")
        self.assertEqual(to_bottom["to"], "bottom")
        bottom_ref = self._ref("Bottom")
        into_view = self.service.scroll(self.page_id, ref=bottom_ref)
        self.assertTrue(into_view["into_view"])

    # ------------------------------------------------------------------
    # Upload / download / dialog
    # ------------------------------------------------------------------
    def test_upload(self):
        upload_ref = self._ref("Upload file")
        result = self.service.upload(
            self.page_id, [str(self.upload_file)], ref=upload_ref
        )
        self.assertEqual(result["count"], 1)

    def test_download(self):
        download_ref = self._ref("Download")
        result = self.service.download(self.page_id, ref=download_ref)
        self.assertTrue(Path(result["path"]).exists())
        self.assertEqual(result["filename"], "hello.txt")
        self.assertEqual(
            Path(result["path"]).read_text(encoding="utf-8"), "hello world"
        )

    def test_dialog_auto_accept(self):
        self.service.dialog(self.page_id, action="accept")
        self.service.click(self.page_id, locator={"text": "Alert"})
        records = self.service.dialogs(self.page_id)
        self.assertTrue(records)
        self.assertEqual(records[-1]["type"], "alert")
        self.assertEqual(records[-1]["message"], "hello dialog")
        self.assertTrue(records[-1]["handled"])

    def test_dialog_wait_for_next(self):
        # schedule a prompt shortly after, then wait for it
        self.service.page_object(self.page_id).evaluate(
            "() => setTimeout(() => prompt('Your name?', 'anon'), 150)"
        )
        result = self.service.dialog(self.page_id, action="accept", prompt_text="AETHER", wait=True)
        self.assertTrue(result["handled"])
        self.assertEqual(result["type"], "prompt")

    # ------------------------------------------------------------------
    # Wait conditions
    # ------------------------------------------------------------------
    def test_wait_conditions(self):
        self.service.wait(
            self.page_id, condition="visible", locator={"text": "Save"}
        )
        self.service.wait(
            self.page_id, condition="attached", locator={"text": "Save"}
        )
        self.service.wait(
            self.page_id, condition="hidden", locator={"css": "#hidden-target"}
        )
        self.service.wait(
            self.page_id, condition="enabled", locator={"text": "Save"}
        )
        self.service.wait(
            self.page_id, condition="disabled", locator={"text": "Disabled action"}
        )
        self.service.wait(self.page_id, condition="load", load_state="load")
        self.service.wait(self.page_id, condition="url", url="**/fixture.html")

        self.service.fill(
            self.page_id, "w@x.y", locator={"role": "textbox", "name": "Email"}
        )
        self.service.wait(
            self.page_id,
            condition="value",
            locator={"role": "textbox", "name": "Email"},
            text="w@x.y",
        )
        self.service.click(self.page_id, locator={"text": "Save"})
        self.service.wait(
            self.page_id, condition="text", locator={"css": "#out"}, text="saved:w@x.y"
        )


if __name__ == "__main__":
    unittest.main()
