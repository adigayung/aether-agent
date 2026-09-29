"""Task 06 — full integration: web-project debugging workflow (real browser).

This is the *integration* suite for the Playwright Extension. It builds a
complete, self-contained **local web project** inside ``tmp_path`` (no internet
access at all), serves it with a tiny local HTTP server and drives it with the
**real** Chromium engine through the extension's full capability set::

    Web Project
       ↓
    browser_launch -> session_create -> page_new -> page_navigate
       ↓
    snapshot -> interact (fill/click/select/press) -> reproduce the problem
       ↓
    console / network / DOM inspection -> screenshot -> trace -> evidence
       ↓
    fix the fixture source -> reload -> snapshot -> console/network normal

The fixture app ships four deliberate, realistic bugs (see ``APP_JS_BUGGY``):

* **BUG A** a JavaScript error (uncaught ``ReferenceError``),
* **BUG B** an API ``404`` (wrong endpoint),
* **BUG C** an API ``500`` (request misses a required field),
* **BUG D** a broken interaction (handler throws, nothing happens).

The suite also proves page/session isolation, state persistence across a
simulated AETHER restart (project-scoped storage), and the Extension lifecycle
(register/enable/use/disable/enable + install/update/uninstall through the
existing Extension infrastructure).

Every test is guarded: the whole module is skipped when no Playwright browser
can be launched, so the repository stays runnable on machines without browsers.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

EXT_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = EXT_DIR.parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _path in (str(PROJECT_ROOT), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

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
    TOOL_CLASSES,
    build_playwright_tools,
)


# ===========================================================================
# The local web project fixture (written into tmp_path by the tests)
# ===========================================================================
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Web Debug Fixture</title>
  <link rel="icon" href="data:,">
</head>
<body>
  <h1>Web Debug Fixture</h1>
  <form id="login-form">
    <label for="email">Email</label>
    <input id="email" name="email" type="email" placeholder="you@example.com">
    <label for="password">Password</label>
    <input id="password" name="password" type="password" placeholder="secret">
    <label for="role">Role</label>
    <select id="role" name="role">
      <option value="">Choose</option>
      <option value="admin">Admin</option>
      <option value="viewer">Viewer</option>
    </select>
    <button id="login" type="button">Log in</button>
  </form>
  <p id="status">idle</p>
  <button id="add-item" type="button">Add item</button>
  <ul id="items"></ul>
  <p id="items-count">0 item(s)</p>
  <script src="app.js"></script>
</body>
</html>
"""

#: The buggy version of the app: four deliberate, realistic bugs.
APP_JS_BUGGY = """(function () {
  "use strict";

  function renderItems(items) {
    var list = document.getElementById("items");
    var count = document.getElementById("items-count");
    list.innerHTML = "";
    items.forEach(function (item) {
      var li = document.createElement("li");
      li.textContent = item;
      list.appendChild(li);
    });
    count.textContent = items.length + " item(s)";
  }

  function loadItems() {
    // BUG B: the API exposes /api/data, not /api/items -> HTTP 404
    fetch("/api/items")
      .then(function (response) { return response.json(); })
      .then(function (payload) { renderItems(payload.items); })
      .catch(function (error) {
        console.error("failed to load items: " + error);
        document.getElementById("status").textContent = "items: error";
      });
  }

  function login() {
    // BUG C: the required "password" field is never sent -> server 500
    return fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: document.getElementById("email").value })
    }).then(function (response) {
      document.getElementById("status").textContent = "login: " + response.status;
      return response;
    });
  }

  function addItem() {
    // BUG D: this element does not exist -> TypeError, nothing is added
    document.getElementById("items-container").textContent = "adding...";
  }

  document.getElementById("login").addEventListener("click", login);
  document.getElementById("add-item").addEventListener("click", addItem);

  window.addEventListener("load", function () {
    loadItems();
    // BUG A: APP_CONFIG is never defined -> uncaught ReferenceError
    document.title = APP_CONFIG.appName;
  });
})();
"""

#: The fixed version of the app: same structure, every bug repaired.
APP_JS_FIXED = """(function () {
  "use strict";

  function renderItems(items) {
    var list = document.getElementById("items");
    var count = document.getElementById("items-count");
    list.innerHTML = "";
    items.forEach(function (item) {
      var li = document.createElement("li");
      li.textContent = item;
      list.appendChild(li);
    });
    count.textContent = items.length + " item(s)";
  }

  function loadItems() {
    // FIX B: use the endpoint the API really exposes
    fetch("/api/data")
      .then(function (response) { return response.json(); })
      .then(function (payload) { renderItems(payload.items); })
      .catch(function (error) {
        console.error("failed to load items: " + error);
        document.getElementById("status").textContent = "items: error";
      });
  }

  function login() {
    // FIX C: send the required password field
    return fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: document.getElementById("email").value,
        password: document.getElementById("password").value
      })
    }).then(function (response) {
      document.getElementById("status").textContent = "login: " + response.status;
      return response;
    });
  }

  function addItem() {
    // FIX D: append a real <li> and update the counter
    var list = document.getElementById("items");
    var count = document.getElementById("items-count");
    var li = document.createElement("li");
    li.textContent = "manual item";
    list.appendChild(li);
    count.textContent = list.children.length + " item(s)";
  }

  document.getElementById("login").addEventListener("click", login);
  document.getElementById("add-item").addEventListener("click", addItem);

  window.addEventListener("load", function () {
    loadItems();
    // FIX A: no undefined global
    document.title = "Web Debug Fixture";
  });
})();
"""


# ===========================================================================
# Tiny local web server (static files + a minimal JSON API) — no internet
# ===========================================================================
class _FixtureRequestHandler(BaseHTTPRequestHandler):
    """Serve the fixture project directory plus a small, deterministic API.

    Routes:

    * ``GET  /`` and ``GET /app.js`` -> static files read from disk on every
      request (so the fix + reload workflow really re-reads the source);
    * ``GET  /api/data``  -> ``200`` ``{"items": [...]}`` (the correct route);
    * ``GET  /api/*``     -> ``404`` (only exists to expose BUG B);
    * ``POST /api/login`` -> ``500`` when the required ``password`` field is
      missing (BUG C), ``200`` otherwise.

    Responses are explicitly uncacheable so a reload always sees fresh source.
    """

    server_version = "AetherFixture/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D401 - silence the test server
        """Silence request logging."""

    # -- helpers ------------------------------------------------------
    def _app_dir(self) -> Path:
        return Path(self.server.app_dir)  # type: ignore[attr-defined]

    def _send(self, status, body=b"", content_type="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def _serve_static(self, route):
        rel = "index.html" if route in ("", "/") else route.lstrip("/")
        app_dir = self._app_dir().resolve()
        target = (app_dir / rel).resolve()
        try:
            target.relative_to(app_dir)
        except ValueError:
            self._send(403, "forbidden")
            return
        if not target.is_file():
            self._send(404, "not found: " + rel)
            return
        if target.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        elif target.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        else:
            content_type = "application/octet-stream"
        self._send(200, target.read_bytes(), content_type)

    # -- routes -------------------------------------------------------
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        route = urlsplit(self.path).path
        if route == "/api/data":
            payload = {"items": ["alpha", "beta", "gamma"]}
            self._send(200, json.dumps(payload), "application/json")
            return
        if route.startswith("/api/"):
            self._send(
                404,
                json.dumps({"error": "unknown endpoint", "path": route}),
                "application/json",
            )
            return
        if route == "/favicon.ico":
            self._send(204)
            return
        self._serve_static(route)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        route = urlsplit(self.path).path
        if route == "/api/login":
            payload = self._read_json_body()
            password = str(payload.get("password") or "")
            if not password:
                # Deliberate server bug: a request without the password 500s.
                self._send(
                    500,
                    json.dumps({"error": "internal error: password is required"}),
                    "application/json",
                )
                return
            self._send(
                200,
                json.dumps({"ok": True, "email": payload.get("email")}),
                "application/json",
            )
            return
        self._send(
            404,
            json.dumps({"error": "unknown endpoint", "path": route}),
            "application/json",
        )


class _LocalWebServer:
    """A ``ThreadingHTTPServer`` bound to an ephemeral local port."""

    def __init__(self, app_dir: Path):
        self.app_dir = Path(app_dir)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureRequestHandler)
        self._httpd.app_dir = str(self.app_dir)  # type: ignore[attr-defined]
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="aether-pw-fixture", daemon=True
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, path: str = "/") -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass
        try:
            self._thread.join(timeout=5)
        except Exception:
            pass


# ===========================================================================
# Real-browser availability guard
# ===========================================================================
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


# ===========================================================================
# Shared base for tests that only need the local web project (no browser yet)
# ===========================================================================
class _LocalWebProjectCase(unittest.TestCase):
    """Base test case owning the local web project fixture + local server."""

    @classmethod
    def setUpClass(cls):
        if not _real_browser_available():
            raise unittest.SkipTest("Playwright browser is not available")

    # -- fixture helpers ---------------------------------------------
    def write_buggy_fixture(self) -> None:
        self.app_dir.mkdir(parents=True, exist_ok=True)
        (self.app_dir / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        (self.app_dir / "app.js").write_text(APP_JS_BUGGY, encoding="utf-8")

    def fix_fixture_source(self) -> None:
        """Repair the fixture source (the "fix" step of the workflow)."""
        (self.app_dir / "app.js").write_text(APP_JS_FIXED, encoding="utf-8")

    # -- lifecycle ----------------------------------------------------
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="aether_pw_it_")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        self.app_dir = self.root / "app"
        self.write_buggy_fixture()

        self.server = _LocalWebServer(self.app_dir)
        self.addCleanup(self.server.stop)

        self.artifact_dir = self.root / "artifacts"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.project_dir = self.root / "project"
        self.project_dir.mkdir(parents=True, exist_ok=True)

    @property
    def app_url(self) -> str:
        return self.server.url("/")


# ===========================================================================
# Shared base: local web project + real browser + working page
# ===========================================================================
class _WebProjectDebugCase(_LocalWebProjectCase):
    """Base test case wiring a real browser against the local fixture app."""

    def setUp(self):
        super().setUp()
        self.service = PlaywrightService(
            headless=True,
            timeout=20,
            artifact_dir=str(self.artifact_dir),
            state_dir=str(self.state_dir),
            runtime_factory=lambda: _SYNC_PLAYWRIGHT().start(),
        )
        self.addCleanup(self.service.shutdown)

        self.browser_id = self.service.launch_browser()
        self.session_id = self.service.create_session(self.browser_id)
        self.page_id = self.service.new_page(self.session_id, self.app_url)
        self.addCleanup(self._close_default_page)
        self.service.wait(self.page_id, condition="load", load_state="networkidle")

    def _close_default_page(self):
        try:
            self.service.close_page(self.page_id)
        except Exception:
            pass

    # -- assertion helpers -------------------------------------------
    def _wait_until(self, predicate, timeout=10.0, interval=0.05):
        deadline = time.time() + timeout
        result = None
        while time.time() < deadline:
            try:
                result = predicate()
            except Exception:
                result = None
            if result:
                return result
            time.sleep(interval)
        return result

    def _console_texts(self, result):
        return [str(message.get("text", "")) for message in result["messages"]]

    def _errors(self, page_id):
        return self.service.console(page_id, type="error")

    def _network_records(self, page_id, **filters):
        return self.service.network(page_id, **filters)["requests"]

    def _ref(self, snapshot, role, name):
        for ref, info in snapshot["refs"].items():
            if info.get("role") == role and info.get("name") == name:
                return ref
        raise AssertionError(
            f"no ref with role={role!r} name={name!r} in {snapshot['refs']}"
        )

    def _throw_later_in_page(self, page_id, message):
        """Raise an uncaught error asynchronously in ``page_id`` (evidence)."""
        self.service.page_object(page_id).evaluate(
            "(m) => { setTimeout(function () { throw new Error(m); }, 0); }",
            message,
        )


# ===========================================================================
# 1) End-to-end debugging workflow (the Definition of Done)
# ===========================================================================
class TestWebProjectDebuggingWorkflow(_WebProjectDebugCase):
    """Local web project -> observe -> reproduce -> evidence -> fix -> verify."""

    def test_e2e_debugging_workflow(self):
        # --- observe: snapshot -------------------------------------------------
        snapshot = self.service.snapshot(self.page_id)
        self.assertEqual(snapshot["title"], "Web Debug Fixture")
        self.assertTrue(snapshot["url"].startswith("http://127.0.0.1:"))
        self.assertGreater(snapshot["element_count"], 0)
        self.assertIn("[ref=", snapshot["snapshot"])
        email_ref = self._ref(snapshot, "textbox", "Email")
        login_ref = self._ref(snapshot, "button", "Log in")
        add_ref = self._ref(snapshot, "button", "Add item")

        # --- interact / reproduce ---------------------------------------------
        self.service.fill(self.page_id, "demo@example.com", ref=email_ref)
        self.service.click(self.page_id, ref=login_ref)     # BUG C -> 500
        self.service.click(self.page_id, ref=add_ref)       # BUG D -> TypeError

        # the login request must be visible in the network evidence
        self._wait_until(
            lambda: any(
                record.get("status") == 500
                for record in self._network_records(self.page_id, url="/api/login")
            )
        )

        # --- inspect console: JavaScript errors -------------------------------
        errors = self.service.console(self.page_id, type="error")
        error_texts = self._console_texts(errors)
        self.assertTrue(
            any("APP_CONFIG" in text for text in error_texts),
            f"expected the ReferenceError in {error_texts}",
        )
        self.assertTrue(
            any(
                "Cannot set properties of null" in text or "items-container" in text
                for text in error_texts
            ),
            f"expected the broken-interaction TypeError in {error_texts}",
        )
        page_errors = [
            message
            for message in errors["messages"]
            if message.get("source") == "pageerror"
        ]
        self.assertTrue(page_errors)
        self.assertIn("stack", page_errors[0])

        # --- inspect network: 404 + 500 ---------------------------------------
        missing = self._network_records(self.page_id, url="/api/items")
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["status"], 404)
        server_error = self._network_records(self.page_id, url="/api/login", method="POST")
        self.assertEqual(len(server_error), 1)
        self.assertEqual(server_error[0]["status"], 500)
        self.assertFalse(server_error[0]["ok"])
        failed = self._network_records(self.page_id, failed=True)
        self.assertEqual(sorted(record["status"] for record in failed), [404, 500])

        # --- inspect DOM: the bug is visible in the page state -----------------
        status = self.service.dom_inspect(self.page_id, locator={"css": "#status"})
        self.assertEqual(status["text"], "login: 500")
        items = self.service.dom_inspect(self.page_id, locator={"css": "#items"})
        self.assertEqual(items["text"], "")
        item_count = self.service.extract(
            self.page_id, what="count", locator={"css": "#items li"}
        )
        self.assertEqual(item_count["value"], 0)

        # --- capture evidence: screenshot + trace -----------------------------
        shot = self.service.screenshot(self.page_id, full_page=True)
        self.assertEqual(shot["scope"], "full_page")
        self.assertTrue(Path(shot["path"]).exists())
        trace = self._trace(buggy_step=True)
        self.assertTrue(Path(trace["path"]).exists())
        self.assertEqual(Path(trace["path"]).read_bytes()[:2], b"PK")

    def _trace(self, *, buggy_step):
        started = self.service.trace_start(self.session_id)
        self.assertTrue(started["active"])
        if buggy_step:
            self.service.reload(self.page_id)
            self.service.wait(self.page_id, condition="load", load_state="networkidle")
        stopped = self.service.trace_stop(self.session_id)
        self.assertEqual(stopped["mime_type"], "application/zip")
        return stopped

    # ------------------------------------------------------------------
    def test_fix_reload_verify_workflow(self):
        # 1) reproduce + collect evidence
        snapshot = self.service.snapshot(self.page_id)
        self.service.fill(
            self.page_id, "demo@example.com", ref=self._ref(snapshot, "textbox", "Email")
        )
        self.service.click(
            self.page_id, ref=self._ref(snapshot, "button", "Log in")
        )
        self.service.click(
            self.page_id, ref=self._ref(snapshot, "button", "Add item")
        )
        self._wait_until(
            lambda: any(
                record.get("status") == 500
                for record in self._network_records(self.page_id, url="/api/login")
            )
        )
        broken_errors = self._console_texts(self._errors(self.page_id))
        self.assertTrue(any("APP_CONFIG" in text for text in broken_errors))
        self.assertEqual(
            self.service.extract(
                self.page_id, what="count", locator={"css": "#items li"}
            )["value"],
            0,
        )

        # 2) apply the fix to the fixture source (test-only change) and clear
        #    the evidence buffers so what follows is *fresh*
        self.fix_fixture_source()
        self.service.console(self.page_id, clear=True)
        self.service.network(self.page_id, clear=True)

        # 3) reload
        self.service.reload(self.page_id)
        self.service.wait(self.page_id, condition="load", load_state="networkidle")
        self._wait_until(
            lambda: self.service.extract(
                self.page_id, what="count", locator={"css": "#items li"}
            )["value"] == 3
        )

        # 4) verify: fresh snapshot, clean console, healthy network
        snapshot2 = self.service.snapshot(self.page_id)
        self.assertGreaterEqual(snapshot2["generation"], snapshot["generation"] + 1)
        self.assertEqual(snapshot2["title"], "Web Debug Fixture")

        residual_errors = self._console_texts(self._errors(self.page_id))
        self.assertEqual(residual_errors, [], f"console not clean after fix: {residual_errors}")

        data = self._network_records(self.page_id, url="/api/data")
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["status"], 200)
        self.assertTrue(data[0]["ok"])
        self.assertEqual(self._network_records(self.page_id, url="/api/items"), [])
        self.assertEqual(self._network_records(self.page_id, status="5xx"), [])
        self.assertEqual(self._network_records(self.page_id, failed=True), [])

        # 5) the interaction that used to be broken now works
        self.service.fill(
            self.page_id, "secret", locator={"label": "Password"}
        )
        self.service.select_option(self.page_id, locator={"label": "Role"}, value="admin")
        self.service.click(self.page_id, locator={"role": "button", "name": "Log in"})
        self._wait_until(
            lambda: self.service.dom_inspect(
                self.page_id, locator={"css": "#status"}
            )["text"]
            == "login: 200"
        )
        status = self.service.dom_inspect(self.page_id, locator={"css": "#status"})
        self.assertEqual(status["text"], "login: 200")
        role = self.service.extract(
            self.page_id, what="value", locator={"label": "Role"}
        )
        self.assertEqual(role["value"], "admin")

        self.service.click(self.page_id, locator={"role": "button", "name": "Add item"})
        item_count = self.service.extract(
            self.page_id, what="count", locator={"css": "#items li"}
        )
        self.assertEqual(item_count["value"], 4)
        # no new JavaScript error from the (now working) interaction
        self.assertEqual(self._console_texts(self._errors(self.page_id)), [])

    # ------------------------------------------------------------------
    def test_full_capability_set_via_tool_layer(self):
        tools = {tool.name: tool for tool in build_playwright_tools(self.service)}
        self.assertEqual(set(tools), {cls.name for cls in TOOL_CLASSES})

        # snapshot -> interaction (click/fill/select/press) -> wait/extract
        snapshot = tools["aether.playwright.browser_snapshot"].execute(
            page_id=self.page_id
        )
        email_ref = self._ref(snapshot, "textbox", "Email")
        tools["aether.playwright.browser_fill"].execute(
            page_id=self.page_id, ref=email_ref, value="tool@example.com"
        )
        tools["aether.playwright.browser_press"].execute(
            page_id=self.page_id, ref=email_ref, key="End"
        )
        selected = tools["aether.playwright.browser_select"].execute(
            page_id=self.page_id, locator={"label": "Role"}, value="admin"
        )
        self.assertEqual(selected["selected"], ["admin"])
        tools["aether.playwright.browser_wait"].execute(
            page_id=self.page_id, condition="visible", ref=email_ref
        )
        value = tools["aether.playwright.browser_extract"].execute(
            page_id=self.page_id, ref=email_ref, what="value"
        )
        self.assertEqual(value["value"], "tool@example.com")

        # click (reproduce the login error) + console/network evidence
        login_ref = self._ref(snapshot, "button", "Log in")
        tools["aether.playwright.browser_click"].execute(
            page_id=self.page_id, ref=login_ref
        )
        self._wait_until(
            lambda: any(
                record.get("status") == 500
                for record in self._network_records(self.page_id, url="/api/login")
            )
        )
        console = tools["aether.playwright.browser_console"].execute(
            page_id=self.page_id, type="error"
        )
        self.assertTrue(any("APP_CONFIG" in str(m.get("text")) for m in console["messages"]))
        network = tools["aether.playwright.browser_network"].execute(
            page_id=self.page_id, status="5xx"
        )
        self.assertEqual(network["count"], 1)
        self.assertEqual(network["requests"][0]["url"].endswith("/api/login"), True)

        # DOM inspection
        dom = tools["aether.playwright.browser_dom_inspect"].execute(
            page_id=self.page_id, locator={"css": "#status"}
        )
        self.assertEqual(dom["text"], "login: 500")

        # screenshot + trace artifacts
        shot = tools["aether.playwright.browser_screenshot"].execute(
            page_id=self.page_id, full_page=True
        )
        self.assertTrue(Path(shot["path"]).exists())
        tools["aether.playwright.browser_trace_start"].execute(session_id=self.session_id)
        trace = tools["aether.playwright.browser_trace_stop"].execute(
            session_id=self.session_id
        )
        self.assertTrue(Path(trace["path"]).exists())

        # debug panel view model (Task 05 surface stays usable end-to-end)
        panel = tools["aether.playwright.browser_debug_panel"].execute(
            session_id=self.session_id, page_id=self.page_id
        )
        self.assertIn("data", panel)
        self.assertIn("views", panel["data"])

        # state persistence through the tool layer
        saved = tools["aether.playwright.session_save_state"].execute(
            session_id=self.session_id, name="tool-layer", project=str(self.project_dir)
        )
        self.assertTrue(saved["saved"])
        listed = tools["aether.playwright.session_state_list"].execute(
            project=str(self.project_dir)
        )
        self.assertIn("tool-layer", listed["states"])
        restored = tools["aether.playwright.session_restore_state"].execute(
            name="tool-layer",
            project=str(self.project_dir),
            session_id=self.session_id,
        )
        self.assertFalse(restored["created_session"])
        deleted = tools["aether.playwright.session_state_delete"].execute(
            name="tool-layer", project=str(self.project_dir)
        )
        self.assertTrue(deleted["deleted"])

    # ------------------------------------------------------------------
    def test_page_and_session_isolation(self):
        # page B in the same session, page C in a second session
        page_b = self.service.new_page(self.session_id, self.app_url)
        session_b = self.service.create_session(self.browser_id)
        page_c = self.service.new_page(session_b, self.app_url)
        self.service.wait(page_b, condition="load", load_state="networkidle")
        self.service.wait(page_c, condition="load", load_state="networkidle")
        try:
            self._throw_later_in_page(self.page_id, "boom-A")
            self._throw_later_in_page(page_b, "boom-B")
            self._throw_later_in_page(page_c, "boom-C")

            for page, marker in (
                (self.page_id, "boom-A"),
                (page_b, "boom-B"),
                (page_c, "boom-C"),
            ):
                self._wait_until(
                    lambda p=page, m=marker: any(
                        m in text for text in self._console_texts(self._errors(p))
                    )
                )

            texts_a = self._console_texts(self._errors(self.page_id))
            texts_b = self._console_texts(self._errors(page_b))
            texts_c = self._console_texts(self._errors(page_c))
            self.assertTrue(any("boom-A" in t for t in texts_a))
            self.assertTrue(any("boom-B" in t for t in texts_b))
            self.assertTrue(any("boom-C" in t for t in texts_c))
            self.assertFalse(any("boom-B" in t or "boom-C" in t for t in texts_a))
            self.assertFalse(any("boom-A" in t or "boom-C" in t for t in texts_b))
            self.assertFalse(any("boom-A" in t or "boom-B" in t for t in texts_c))

            # the app bug itself is reproduced independently on every page
            for page in (self.page_id, page_b, page_c):
                self.assertTrue(
                    any("APP_CONFIG" in t for t in self._console_texts(self._errors(page)))
                )

            # network evidence is per page: issue a page-identifiable request from
            # two different pages and check the records never cross over
            self.service.page_object(self.page_id).evaluate(
                "() => fetch('/api/data?page=A')"
            )
            self.service.page_object(page_b).evaluate(
                "() => fetch('/api/data?page=B')"
            )
            for page, marker in ((self.page_id, "page=A"), (page_b, "page=B")):
                self._wait_until(
                    lambda p=page, m=marker: any(
                        m in record["url"]
                        for record in self._network_records(p)
                    )
                )
            urls_a = [record["url"] for record in self._network_records(self.page_id)]
            urls_b = [record["url"] for record in self._network_records(page_b)]
            self.assertTrue(any(url.endswith("/api/items") for url in urls_a))
            self.assertTrue(urls_b)
            self.assertTrue(any("page=A" in url for url in urls_a))
            self.assertFalse(any("page=B" in url for url in urls_a))
            self.assertTrue(any("page=B" in url for url in urls_b))
            self.assertFalse(any("page=A" in url for url in urls_b))

            # each session owns its own traces view
            self.assertNotEqual(self.session_id, session_b)
            self.assertEqual(
                {handle["session_id"] for handle in self.service.list_sessions()},
                {self.session_id, session_b},
            )

            # closing an erroring page must not break the others
            self.service.close_page(page_b)
            snapshot_c = self.service.snapshot(page_c)
            self.assertEqual(snapshot_c["page_id"], page_c)
            snapshot_a = self.service.snapshot(self.page_id)
            self.assertEqual(snapshot_a["page_id"], self.page_id)

            # closing session B must not affect session A
            self.service.close_session(session_b)
            self.assertEqual(
                [handle["session_id"] for handle in self.service.list_sessions()],
                [self.session_id],
            )
            self.service.snapshot(self.page_id)
            with self.assertRaises(Exception):
                self.service.snapshot(page_c)
        finally:
            for pid in (page_b, page_c):
                try:
                    self.service.close_page(pid)
                except Exception:
                    pass
            try:
                self.service.close_session(session_b)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def test_state_persistence_survives_restart(self):
        context = self.service.context_object(self.session_id)
        context.add_cookies(
            [
                {
                    "name": "aether_fixture",
                    "value": "persisted",
                    "url": self.server.base_url,
                }
            ]
        )
        self.service.page_object(self.page_id).evaluate(
            "() => window.localStorage.setItem('theme', 'dark')"
        )

        summary = self.service.save_state(
            self.session_id, name="login", project=str(self.project_dir)
        )
        self.assertTrue(summary["saved"])
        self.assertEqual(summary["scope"], "project")
        self.assertGreaterEqual(summary["cookie_count"], 1)
        # project-scoped state must never leak into the extension scope
        self.assertIn("login", self.service.list_saved_states(project=str(self.project_dir)))
        self.assertNotIn("login", self.service.list_saved_states())

        # "restart AETHER": a brand new service over the same state directory
        self.service.shutdown()
        restarted = PlaywrightService(
            headless=True,
            timeout=20,
            artifact_dir=str(self.artifact_dir),
            state_dir=str(self.state_dir),
            runtime_factory=lambda: _SYNC_PLAYWRIGHT().start(),
        )
        try:
            self.assertIn(
                "login", restarted.list_saved_states(project=str(self.project_dir))
            )
            browser_id = restarted.launch_browser()
            restored = restarted.restore_state(
                name="login",
                project=str(self.project_dir),
                browser_id=browser_id,
            )
            self.assertTrue(restored["restored"])
            self.assertTrue(restored["created_session"])
            restored_session = restored["session_id"]
            cookies = restarted.context_object(restored_session).cookies()
            self.assertTrue(
                any(cookie["name"] == "aether_fixture" for cookie in cookies)
            )

            # the restored session really continues with the saved data
            page_id = restarted.new_page(restored_session, self.app_url)
            try:
                restarted.wait(page_id, condition="load", load_state="networkidle")
                document_cookie = restarted.page_object(page_id).evaluate(
                    "() => document.cookie"
                )
                self.assertIn("aether_fixture=persisted", document_cookie)
                theme = restarted.page_object(page_id).evaluate(
                    "() => window.localStorage.getItem('theme')"
                )
                self.assertEqual(theme, "dark")
            finally:
                restarted.close_page(page_id)
        finally:
            restarted.shutdown()


# ===========================================================================
# 2) Extension lifecycle: register -> enable -> use -> disable -> enable
# ===========================================================================
class TestExtensionLifecycleIntegration(_LocalWebProjectCase):
    """The Playwright extension works through the real Extension infrastructure.

    Only **one** synchronous Playwright runtime may be live at a time in a
    process, so this test deliberately does not use the shared base browser: the
    extension's own service owns the single real Chromium runtime.
    """

    def _build_context(self):
        from agent_ai.extensions.capabilities import CapabilityRegistry
        from agent_ai.extensions.config import ConfigValueStore
        from agent_ai.extensions.context import ExtensionContext
        from agent_ai.extensions.manifest import load_manifest

        manifest = load_manifest(EXT_DIR / "manifest.json")
        registry = CapabilityRegistry()
        context = ExtensionContext(
            extension_root=EXT_DIR,
            manifest=manifest,
            capability_registry=registry,
            aether_root=self.root / "aether",
            extensions_dir=self.root / "extensions",
            config_store=ConfigValueStore(db_path=str(self.root / "config.db")),
        )
        return registry, context

    def _extension(self):
        module = importlib.import_module("Extension.playwright.extension")
        extension = module.extension
        # Isolate this test from any earlier wiring of the module singleton.
        if extension.service is not None:
            try:
                extension.service.shutdown()
            except Exception:
                pass
        extension._service = None
        extension._tools_registered = False
        extension._context = None
        self.addCleanup(self._reset_extension, extension)
        return extension

    @staticmethod
    def _reset_extension(extension):
        if extension.service is not None:
            try:
                extension.service.shutdown()
            except Exception:
                pass
        extension._service = None
        extension._tools_registered = False

    def test_register_enable_use_disable_enable(self):
        registry, context = self._build_context()
        extension = self._extension()

        # --- register --------------------------------------------------
        extension.register(context)
        self.assertEqual(context.config.get_definition("browser")["type"], "enum")
        self.assertIn("chromium", context.config.get_definition("browser")["choices"])
        self.assertIsNotNone(
            context.services.get("aether.playwright.playwright_service")
        )
        self.assertTrue(context.ui.exists("aether.playwright.debug_panel"))

        # --- enable ----------------------------------------------------
        extension.enable(context)
        tool_ids = {
            record.id
            for record in registry.list_by_extension("aether.playwright")
            if record.type == "tool"
        }
        self.assertEqual(tool_ids, {cls.name for cls in TOOL_CLASSES})
        service_record = context.services.get("aether.playwright.playwright_service")
        self.assertIs(service_record.metadata.get("service_instance"), extension.service)

        # --- use (through the registered capability metadata) ----------
        launch_record = context.tools.get("aether.playwright.browser_launch")
        launch_tool = launch_record.metadata["_tool_instance"]
        launched = launch_tool.execute(headless=True)
        session_id = extension.service.create_session(launched["browser_id"])
        page_id = extension.service.new_page(session_id, self.app_url)
        snapshot_tool = context.tools.get(
            "aether.playwright.browser_snapshot"
        ).metadata["_tool_instance"]
        snapshot = snapshot_tool.execute(page_id=page_id)
        self.assertEqual(snapshot["title"], "Web Debug Fixture")
        self.assertGreater(snapshot["element_count"], 0)
        extension.service.close_page(page_id)

        # --- disable ---------------------------------------------------
        extension.disable(context)
        self.assertFalse(extension.service.is_runtime_started)
        self.assertEqual(extension.service.status()["browser_count"], 0)

        # --- enable again (re-usable) ----------------------------------
        extension.enable(context)
        browser_id = extension.service.launch_browser()
        session_id = extension.service.create_session(browser_id)
        page_id = extension.service.new_page(session_id, self.app_url)
        try:
            extension.service.wait(page_id, condition="load", load_state="networkidle")
            errors = extension.service.console(page_id, type="error")
            self.assertTrue(
                any("APP_CONFIG" in str(m.get("text")) for m in errors["messages"])
            )
        finally:
            extension.service.close_page(page_id)
        self.assertTrue(extension.service.is_runtime_started)


# ===========================================================================
# 3) Extension package lifecycle: install -> update -> uninstall
# ===========================================================================
class TestExtensionInstallLifecycle(unittest.TestCase):
    """Install/update/uninstall the Playwright package via ExtensionManager."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git") is None:
            raise unittest.SkipTest("git is not available")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="aether_pw_ext_")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo_v1 = self.root / "playwright-v1"
        self.repo_v2 = self.root / "playwright-v2"
        self._make_repo(self.repo_v1, "0.1.0")
        self._make_repo(self.repo_v2, "0.2.0")

    def _make_repo(self, dest: Path, version: str) -> None:
        """Snapshot the extension package into a local git repo fixture."""
        shutil.copytree(
            EXT_DIR,
            dest,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
        )
        manifest_path = dest / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = version
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        git = [
            "git",
            "-c",
            "user.email=aether-tests@example.com",
            "-c",
            "user.name=AETHER Tests",
        ]
        subprocess.run(["git", "init", "-q"], cwd=str(dest), check=True, capture_output=True)
        subprocess.run(git + ["add", "-A"], cwd=str(dest), check=True, capture_output=True)
        subprocess.run(
            git + ["commit", "-q", "-m", f"playwright {version}"],
            cwd=str(dest),
            check=True,
            capture_output=True,
        )

    def test_install_update_uninstall(self):
        from agent_ai.extensions.capabilities import CapabilityRegistry
        from agent_ai.extensions.config import ConfigValueStore
        from agent_ai.extensions.lifecycle import get_lifecycle_store
        from agent_ai.extensions.manager import ExtensionManager
        from agent_ai.extensions.registry import ExtensionRegistry

        extensions_dir = self.root / "extensions"
        registry = ExtensionRegistry()
        manager = ExtensionManager(
            registry=registry,
            capability_registry=CapabilityRegistry(),
            lifecycle_store=get_lifecycle_store(db_path=str(self.root / "lifecycle.db")),
            aether_root=self.root / "aether",
            extensions_dir=extensions_dir,
            config_store=ConfigValueStore(db_path=str(self.root / "config.db")),
        )

        # --- install ---------------------------------------------------
        status = manager.install(str(self.repo_v1))
        self.assertEqual(status["id"], "aether.playwright")
        self.assertTrue(registry.exists("aether.playwright"))
        installed_record = registry.get("aether.playwright")
        self.assertEqual(installed_record.manifest.version, "0.1.0")
        installed_dir = Path(installed_record.source)
        self.assertTrue((installed_dir / "manifest.json").is_file())
        self.assertTrue((installed_dir / "extension.py").is_file())
        self.assertTrue((installed_dir / "services" / "playwright_service.py").is_file())

        # --- update ----------------------------------------------------
        manager.update("aether.playwright", str(self.repo_v2))
        self.assertEqual(registry.get("aether.playwright").manifest.version, "0.2.0")
        self.assertTrue(Path(registry.get("aether.playwright").source).is_dir())

        # --- uninstall -------------------------------------------------
        result = manager.uninstall("aether.playwright")
        self.assertEqual(result["status"], "uninstalled")
        self.assertFalse(registry.exists("aether.playwright"))
        self.assertFalse(installed_dir.exists())


if __name__ == "__main__":
    unittest.main()
