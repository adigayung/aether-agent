# Playwright Extension

**Status: COMPLETE** — browser automation + web debugging capability for AETHER.

## Purpose

This extension gives AETHER a real browser: it can open a web project, drive it,
reproduce a problem, collect debugging evidence (console, network, DOM,
screenshots, traces), persist a logged-in session, and then verify a fix after
the source has been changed.

It is deliberately scoped: **Playwright observes, drives and debugs the
browser**; changing the project's source code stays with the ordinary AETHER
tools (`read_file`, `search_code`, `edit_file`/`write_file`, `run_command`).

## Installation

The extension is a regular Python package with its own `pyproject.toml`. It
declares **playwright** as a dependency, but does **not** run
`playwright install` automatically – that is handled separately. The extension
registers itself as the AETHER extension `aether.playwright` (API version `1`).

## Package Structure

```
Extension/playwright/
├─ pyproject.toml          # Extension package metadata & Playwright dep
├─ manifest.json           # Extension manifest (id, version, API)
├─ __init__.py             # Empty – marks package
├─ extension.py            # Extension entry-point class (lifecycle wiring)
├─ services/
│   └─ playwright_service.py  # Browser/session/page lifecycle owner
├─ tools/
│   └─ playwright_tools.py    # Tool layer exposed to the LLM
├─ skills/
│   └─ playwright/
│       └─ skill.md        # Documentation skill for this extension
├─ tests/
│   ├─ test_foundation.py                  # Task 01 contract
│   ├─ test_browser_session_page.py        # Task 02 lifecycle
│   ├─ test_snapshot_interaction.py        # Task 03 (double)
│   ├─ test_snapshot_interaction_real.py   # Task 03 (real browser)
│   ├─ test_debug_tools.py                 # Task 04 (double)
│   ├─ test_debug_tools_real.py            # Task 04 (real browser)
│   ├─ test_debug_ui.py                    # Task 05 debug UI (double)
│   ├─ test_debug_ui_real.py               # Task 05 (real browser)
│   ├─ test_state_persistence.py           # Task 05 persistence / lifecycle
│   ├─ test_integration.py                 # Task 06 full web-project workflow
│   └─ _fake_playwright.py                 # shared in-memory Playwright double
└─ README.md               # Extension overview (this file)
```

## Architecture

```
LLM
 ↓
Playwright Tool(s)        (tools/playwright_tools.py)
 ↓
PlaywrightService         (services/playwright_service.py)
 ↓
Playwright runtime        (started lazily, only on first browser_launch)
 ↓
Browser → BrowserContext (= session) → Page
```

The service is the **single owner** of the Playwright resource hierarchy:

```
PlaywrightService
├── runtime      -> Playwright engine (started lazily, on demand)
├── browsers     -> Browser instances        (browser_id)
├── sessions     -> BrowserContext instances (session_id)
└── pages        -> Page instances           (page_id)
```

Nothing Playwright-related happens on import. Every public method returns plain,
JSON-serialisable dicts; raw Playwright objects stay private
(`page_object()` / `context_object()` / `browser_object()` are internal).

## Core Features

### Task 01 — Foundation

- **Manifest** – extension identity `aether.playwright`, API version `1`.
- **Extension class** – `register` / `enable` / `disable` hooks; registration
  creates three config entries (`browser`, `headless`, `timeout`) and the
  service capability `aether.playwright.playwright_service`.
- **PlaywrightService** – configuration + stable ID helpers, nothing launched at
  import time.

### Task 02 — Browser / Session / Page management

Built on the official Playwright Python API (`BrowserType.launch()`,
`Browser.new_context()`, `BrowserContext.new_page()`, `Page.goto()` /
`reload()` / `go_back()` / `go_forward()`):

- **Lazy runtime** – `sync_playwright()` starts only when a browser is actually
  launched; the runtime is bound to one thread (the sync API is not thread-safe).
  A runtime is strictly reused by the thread that created it; when the next
  AETHER task runs on a new worker thread, the previous runtime is released
  (driver process included) and a fresh one is started on the calling thread.
  Closing the last browser releases the runtime too, so sequential tasks never
  leak Playwright state (`service.status()["runtime_releases"]` reports it).
- **Browsers** – `launch_browser()` / `close_browser()` / `list_browsers()`.
- **Sessions** (`BrowserContext`) – `create_session()` / `close_session()` /
  `list_sessions()`.
- **Pages** – `new_page()` / `close_page()` / `list_pages()` (multiple pages per
  session supported).
- **Navigation** – `navigate()` / `reload()` / `go_back()` / `go_forward()`.
- **Tool layer** – namespaced `aether.playwright.*` tools registered through
  `context.tools` in `enable()`.

### Task 03 — Snapshot, element references & interaction

The `snapshot → identify element → interact → verify` workflow.

- **Snapshot** – `browser_snapshot` returns `page_id`, `url`, `title`, an
  annotated `snapshot` tree (`- button "Save" [ref=e3]`), a `refs` map and — when
  the installed Playwright build supports it — the ARIA snapshot
  (`aria_snapshot`). Only interactive/semantic, visible elements are listed, so
  the LLM can pick elements without reading the full HTML.
- **Element references** – refs like `e1`, `e2`, … bound to one page and one
  snapshot generation. They become **stale** after navigation or when the
  element leaves the DOM; using a stale ref returns a clear `StaleRefError`
  telling the LLM to take a new snapshot.
- **Interaction tools** – `browser_click`, `browser_fill`, `browser_select`,
  `browser_press`, `browser_scroll`, `browser_extract`, `browser_wait`,
  `browser_upload`, `browser_download`, `browser_dialog`.
- **Locators** – a target is either a `ref` or an explicit `locator` with one
  semantic strategy: `role`, `label`, `text`, `placeholder`, `alt`, `title`,
  `test_id`, plus `css`/`xpath` as a fallback. Several matches produce a clear
  `AmbiguousLocatorError` unless `nth`/`first`/`last` disambiguates.
- **Extraction** – `browser_extract` returns structured values for `text`,
  `value`, `attribute`, `count`, `visible`, `enabled`, `selected`, `html`
  (or a list of those).
- **Wait** – `browser_wait` is condition-based (`visible`/`hidden`/`attached`/
  `detached`/`enabled`/`disabled`/`text`/`value`/`checked`/`url`/`load`) and uses
  Playwright's waiting mechanisms.
- **Upload / download / dialog** – `set_input_files`, `expect_download` +
  `save_as` (artifacts land in the configured `artifact_dir`), and native dialog
  handling via `page.on("dialog", …)`.

### Task 04 — Web debugging (console / network / DOM / screenshot / trace)

The `reproduce → inspect → use evidence` workflow. Evidence is captured
automatically for every page created through `page_new` and is **scoped to a
single page** so page A never mixes with page B; tracing is scoped to a session
(BrowserContext). Evidence is dropped when the page/session/browser is closed.

- **Console** – `browser_console` returns structured events
  (`timestamp`, `type`, `text`, `location`, plus `source="pageerror"`,
  `error_name` and `stack` for uncaught JavaScript exceptions). Filter by
  `type`/`level` (single value or list) and/or a text `search`; `limit` and
  `clear` are supported.
- **Network** – `browser_network` returns request/response records (`url`,
  `method`, `resource_type`, `status`, `ok`, `status_text`, `timing`,
  `failure`, `duration_ms`). Filter by `url` (substring or glob), `method`,
  `status` (e.g. `500` or `5xx`), `resource_type` and `failed`.
- **DOM inspection** – `browser_dom_inspect` inspects one element (by `ref` or
  `locator`) and returns tag, text, attributes, `visible`, `enabled`, bounding
  box and — on request — a compact HTML snippet.
- **Screenshot** – `browser_screenshot` captures full-page, viewport or element
  screenshots as artifacts and returns `path`, `filename`, `mime_type`, `width`,
  `height`.
- **Trace** – `browser_trace_start` / `browser_trace_stop` wrap Playwright's
  official tracing (`screenshots`/`snapshots`/`sources`). `trace_stop` writes a
  re-openable `.zip` artifact; `browser_trace_open` returns an open descriptor.
- **Fresh evidence** – `browser_console` / `browser_network` flush Playwright's
  pending event queue before answering, so reading evidence immediately after an
  interaction still sees the request/error that interaction produced.

### Task 05 — Debug UI & session persistence

A **generic** Playwright debug UI (no new UI framework) plus the persistence a
real workflow needs.

- **UI system reuse** – the extension registers its UI through the standard
  Extension API (`context.ui.register`) as ordinary UI capabilities:

  ```
  aether.playwright.debug_panel        (panel)
  aether.playwright.console_viewer     (viewer, log)
  aether.playwright.network_viewer     (table)
  aether.playwright.dom_viewer         (viewer, json)
  aether.playwright.screenshot_viewer  (viewer, image)
  aether.playwright.trace_viewer       (viewer, file)
  ```

  Rendering is chosen from the contribution `type` / `props.viewer_type`, never
  from the extension id, so AETHER Core and the generic UI runtime stay
  extension-agnostic.
- **Debug panel view models** – plain, JSON contracts (the AETHER UI Result
  shape) for `sessions_view`, `pages_view`, `console_view`, `network_view`,
  `dom_view`, `screenshot_view`, `trace_view`, `trace_status` and the combined
  `debug_panel(session_id=…, page_id=…)`.
- **Actions** – the panel exposes Refresh, Close Page, Clear Console, Clear
  Network, Screenshot, Start Trace, Stop Trace (and Save/Restore State); every
  action simply references an existing capability/tool — no browser lifecycle is
  duplicated in the frontend.
- **Browser state persistence** – `save_state` / `restore_state` / `load_state` /
  `list_saved_states` / `delete_saved_state` use Playwright's official
  storage-state mechanism (`BrowserContext.storage_state()` and
  `Browser.new_context(storage_state=…)`; cookies via `add_cookies`, origins via
  an init script when applied to an existing context). The JSON state is stored
  through `context.storage`, isolated **per extension** and, optionally,
  **per project** (`<project>/.aether/extensions/<ext>/…`). Cookie/localStorage
  values are never returned to the UI or logs — only key names and counts.

  ```
  session -> save_state -> restart AETHER -> restore_state
  ```

### Task 06 — Web-project debugging workflow (integration)

Task 06 validates the whole capability set against a **real local web project**
and documents the workflow as the extension's primary use case:

```
Local Web Project
      ↓
Playwright (browser_launch -> session_create -> page_new -> page_navigate)
      ↓
Observe      browser_snapshot
      ↓
Reproduce    click / fill / select / press
      ↓
Debug evidence   console / network / DOM / screenshot / trace
      ↓
Fix          ordinary AETHER tools (read_file / edit_file / run_command …)
      ↓
Reload       page_reload (after clearing the evidence buffers)
      ↓
Verify       snapshot + clean console/network + the interaction now works
```

The proof lives in `tests/test_integration.py`: it builds a small web app in
`tmp_path` with deliberate bugs —

- **BUG A** a JavaScript error (uncaught `ReferenceError` on load),
- **BUG B** an API `404` (the client calls the wrong endpoint),
- **BUG C** an API `500` (the login request omits a required field),
- **BUG D** a broken interaction (the "Add item" handler throws),

— serves it from `127.0.0.1` with a tiny local HTTP server (no internet), drives
it with real Chromium, collects the evidence, repairs the fixture source, reloads
and verifies that the console/network are clean and the interaction works. The
same suite proves page/session isolation, state persistence across a simulated
AETHER restart (project-scoped storage) and the Extension lifecycle
(register → enable → use → disable → enable, plus install → update → uninstall).

## Example workflow

> *"Open my web project, reproduce the login error, inspect the console/network,
> fix the source, reload, and verify."*

```
# 1. start the project locally (run_command) and note the URL, e.g. http://127.0.0.1:8000
# 2. observe
browser_launch()                                  -> browser_id
session_create(browser_id)                        -> session_id
page_new(session_id, "http://127.0.0.1:8000/login") -> page_id
browser_snapshot(page_id)                         -> refs (Email, Log in, …)

# 3. reproduce
browser_fill(page_id, ref="e5", value="qa@example.com")
browser_click(page_id, ref="e7")

# 4. evidence
browser_console(page_id, type="error")            -> uncaught TypeError + stack
browser_network(page_id, failed=True)             -> POST /api/login 500
browser_dom_inspect(page_id, locator={"css": "#login-error"})
browser_screenshot(page_id, full_page=True)       -> screenshot path
browser_trace_start(session_id); … ; browser_trace_stop(session_id) -> .zip

# 5. fix (ordinary AETHER tools — search_code / read_file / edit_file / run_command)
# 6. reload + verify
browser_console(page_id, clear=True); browser_network(page_id, clear=True)
page_reload(page_id)
browser_snapshot(page_id)                         # fresh refs
… re-run the interaction …
browser_console(page_id, type="error")            # -> []
browser_network(page_id, failed=True)             # -> []
```

## Tool reference

| Tool id | Purpose |
|---|---|
| `aether.playwright.browser_launch` | launch a browser, returns `browser_id` |
| `aether.playwright.browser_close` | close a browser (all when no id) |
| `aether.playwright.session_create` | create a session, returns `session_id` |
| `aether.playwright.session_list` | list sessions with their pages |
| `aether.playwright.session_close` | close a session and its pages |
| `aether.playwright.page_new` | open a page/tab, returns `page_id` |
| `aether.playwright.page_list` | list pages with url/title |
| `aether.playwright.page_navigate` | go to a URL |
| `aether.playwright.page_reload` | reload the page |
| `aether.playwright.page_back` | history back |
| `aether.playwright.page_forward` | history forward |
| `aether.playwright.page_close` | close a page |
| `aether.playwright.browser_snapshot` | snapshot the page + assign refs |
| `aether.playwright.browser_refs` | refs of the last snapshot |
| `aether.playwright.browser_click` | click a ref/locator |
| `aether.playwright.browser_fill` | fill an input/textarea |
| `aether.playwright.browser_select` | select option(s) in a `<select>` |
| `aether.playwright.browser_press` | press a key (element or page) |
| `aether.playwright.browser_scroll` | scroll into view / by delta / to top-bottom |
| `aether.playwright.browser_extract` | structured extraction |
| `aether.playwright.browser_wait` | condition-based wait |
| `aether.playwright.browser_upload` | set files on a file input |
| `aether.playwright.browser_download` | capture a download as an artifact |
| `aether.playwright.browser_dialog` | handle native dialogs |
| `aether.playwright.browser_console` | structured console/page-error events + filters |
| `aether.playwright.browser_network` | captured request/response records + filters |
| `aether.playwright.browser_dom_inspect` | inspect one element (tag/text/attrs/box/html) |
| `aether.playwright.browser_screenshot` | full-page / viewport / element screenshot |
| `aether.playwright.browser_trace_start` | start Playwright tracing on a session |
| `aether.playwright.browser_trace_stop` | stop tracing, save a re-openable `.zip` |
| `aether.playwright.browser_trace_open` | open descriptor for a trace `.zip` artifact |
| `aether.playwright.browser_debug_panel` | debug-panel view model (sessions/pages/console/network/…) |
| `aether.playwright.session_save_state` | persist a session's storage state (cookies + localStorage) |
| `aether.playwright.session_restore_state` | restore a saved storage state (new or existing session) |
| `aether.playwright.session_state_list` | list saved storage-state names |
| `aether.playwright.session_state_delete` | delete a saved storage state |

## Tests

```
python -m pytest Extension/playwright/tests -q
```

- `test_foundation.py` – the Task 01 contract.
- `test_browser_session_page.py` – the Task 02 lifecycle (fake runtime) plus a
  guarded real-browser integration test.
- `test_snapshot_interaction.py` – Task 03 snapshot/ref/interaction behaviour
  against an in-memory Playwright double (stale ref, ambiguous locator, extract,
  wait, upload/download, dialog, tool registration).
- `test_snapshot_interaction_real.py` – Task 03 end-to-end on a **real** browser
  against a local `file://` fixture (skipped when no browser is available).
- `test_debug_tools.py` – Task 04 console/network/DOM/screenshot/trace behaviour
  against an in-memory Playwright double (filters, page isolation, error cases,
  lifecycle cleanup, tool layer).
- `test_debug_tools_real.py` – Task 04 end-to-end debugging workflow on a
  **real** browser against a fully routed local fixture (no internet).
- `test_debug_ui.py` – Task 05 debug panel registration, view data
  (sessions/pages/console/network/DOM), screenshot/trace artifact presentation
  and the tool layer (in-memory double).
- `test_debug_ui_real.py` – Task 05 end-to-end flow on a **real** browser
  (launch → page → debug panel data → screenshot → trace → save state →
  restart → restore).
- `test_state_persistence.py` – Task 05 storage-state save/restore/list/delete,
  scope isolation (extension vs project), survival across a simulated restart,
  the extension disable/re-enable lifecycle and the `context.storage` facade.
- `test_integration.py` – **Task 06**: a real local web project (local HTTP
  server, real Chromium) exercising the full
  observe → reproduce → evidence → fix → reload → verify workflow, page/session
  isolation, persistence across a restart and the Extension lifecycle
  (register/enable/use/disable/enable + install/update/uninstall).
- `_fake_playwright.py` – the shared in-memory Playwright double (lifecycle,
  events, tracing and the storage-state surface) used by the deterministic tests.

All real-browser tests skip automatically when no Playwright browser is
installed, so the suite stays runnable anywhere.
