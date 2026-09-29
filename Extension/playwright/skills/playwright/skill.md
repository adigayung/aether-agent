# Playwright Extension Skill

The **Playwright Extension** is AETHER's **browser automation + web debugging**
capability. It owns the whole browser hierarchy (browser → session/context →
page) and exposes it to the LLM as namespaced, JSON-returning tools.

Division of labour — this is the important part:

| Concern | Handled by |
|---|---|
| Open/reproduce a web UI, read console/network/DOM, click/fill/select, screenshot, trace, persist a browser session | **this Playwright Extension** |
| Change the source code of the web project, run its tests/linters, reload | the normal AETHER tools (`read_file`, `search_code`, `edit_file`/`write_file`, `run_command`, …) |

Playwright **observes, drives and debugs** the browser. It never edits source
code — after collecting evidence you fix the project with the ordinary AETHER
tooling and then come back to Playwright to verify.

## Capability map

- **Task 02** — browser / session / page lifecycle (launch, navigate, close).
- **Task 03** — snapshot, element references and interaction.
- **Task 04** — debugging: console, network, DOM inspection, screenshot, trace.
- **Task 05** — generic debug UI (panel + viewers) and browser state
  persistence (save / restore storage state).
- **Task 06** — full web-project debugging workflow, validated end-to-end
  against a real local web project (the workflow documented below).

## Core Concepts

- **Configuration** – three config keys are registered:
  - `browser` (enum: `chromium`, `firefox`, `webkit`)
  - `headless` (boolean)
  - `timeout` (integer, seconds)
- **Service** – `PlaywrightService` is registered under the namespaced id
  `aether.playwright.playwright_service` and owns the whole hierarchy:

  ```
  PlaywrightService
  ├── runtime   (Playwright engine, started lazily)
  ├── browsers
  ├── sessions  (= BrowserContext)
  └── pages
  ```

  The Playwright engine is started **only** when a browser is actually
  launched; importing the extension has no side effects. The synchronous
  Playwright API must be used from a single thread (the service enforces this).
- **Tools** – the LLM never sees raw Playwright objects. It calls namespaced
  tools, each returning plain dicts.

## The web-project debugging workflow (main workflow)

Use this whenever something in a web project misbehaves. Never guess — look at
evidence, fix the source, then verify.

```
1. OBSERVE
     browser_launch -> session_create -> page_new -> page_navigate(url)
     browser_snapshot(page_id)                    # url, title, refs

2. REPRODUCE
     drive the page to the broken state with
     browser_click / browser_fill / browser_select / browser_press

3. COLLECT EVIDENCE
     browser_console(page_id, type="error")       # uncaught errors, pageerrors
     browser_network(page_id, failed=True)        # 404 / 500 / failed requests
     browser_screenshot(page_id, full_page=True)  # visual proof
     browser_trace_start(session_id) ; reproduce ; browser_trace_stop(session_id)

4. INSPECT
     browser_snapshot(page_id)                    # re-snapshot after DOM changes
     browser_dom_inspect(page_id, ref="e3")       # tag/text/attrs/box/html
     browser_extract(...)                         # read state (#status, lists, …)

5. FIX  (source code — NOT a Playwright tool)
     read_file / search_code -> edit_file / write_file on the project
     (run_command to apply migrations, rebuild, or run the project's tests)

6. RELOAD
     browser_console(page_id, clear=True)         # reset the evidence buffers
     browser_network(page_id, clear=True)
     page_reload(page_id)      # or page_navigate to a fresh URL

7. VERIFY
     browser_snapshot(page_id)                    # fresh refs
     browser_console(page_id, type="error")       # expect nothing
     browser_network(page_id, failed=True)        # expect nothing
     re-run the interaction that used to be broken -> it now works
```

Rules of thumb:

- **Always reproduce before reading evidence.** Console/network capture starts
  automatically when a page is created, so the buffer holds everything that
  happened — but only if you actually triggered the problem.
- **Reading evidence is fresh.** `browser_console` / `browser_network` flush the
  pending Playwright event queue before answering, so a read right after a
  `browser_click` still sees the request/error that click produced.
- **Clear between phases.** After capturing the "before" evidence, pass
  `clear=true` to `browser_console`/`browser_network` so the "after" read only
  contains post-fix events.
- **Evidence is per page; traces are per session.** `browser_console(page_id)`
  only ever returns page A's messages, never page B's. Two pages/sessions never
  contaminate each other, and an error (or a closed page) in one page does not
  affect the others.
- **Prefer filters over dumping everything**: `type=["warning","error"]`,
  `status="5xx"`, `failed=true`, `url="/api/login"`.
- **Always finish with an artifact** — a screenshot and/or a trace `.zip` — and
  reference the returned `path`.

### Worked example

> *"Open my web project, reproduce the login error, inspect the console/network,
> fix the source, reload, and verify."*

```
# 0. start the project locally (run_command: python manage.py runserver / npm run dev)
#    and note its URL, e.g. http://127.0.0.1:8000/

browser_launch()                               -> browser_id
session_create(browser_id)                     -> session_id
page_new(session_id, "http://127.0.0.1:8000/login")   -> page_id
browser_snapshot(page_id)                      -> refs: e5=Email, e7=Log in

# reproduce the login error
browser_fill(page_id, ref="e5", value="qa@example.com")
browser_click(page_id, ref="e7")

# evidence
browser_console(page_id, type="error")
#   -> "Uncaught TypeError: Cannot read properties of undefined (reading 'map')"
browser_network(page_id, failed=True)
#   -> POST /api/login  500   (and/or  GET /api/profile  404)
browser_dom_inspect(page_id, locator={"css": "#login-error"})
browser_screenshot(page_id, full_page=True)     -> path
browser_trace_start(session_id) ; browser_click(page_id, ref="e7") ; browser_trace_stop(session_id)

# fix the source (ordinary AETHER tools, not Playwright)
search_code("api/login") ; read_file(...) ; edit_file(...)
run_command("python manage.py test accounts")     # or npm test

# verify
browser_console(page_id, clear=True)
browser_network(page_id, clear=True)
page_reload(page_id)
browser_snapshot(page_id)                       # fresh refs (old ones are stale)
browser_fill(page_id, ref=<Email>, value="qa@example.com")
browser_click(page_id, ref=<Log in>)
browser_console(page_id, type="error")          # -> []
browser_network(page_id, failed=True)           # -> []
browser_extract(page_id, what="text", locator={"css": "#dashboard"})  # -> "Welcome"
```

## THE interaction workflow (Task 03)

Always follow this loop — never guess element handles and never read raw HTML:

```
1. snapshot      aether.playwright.browser_snapshot(page_id)
                 -> refs: e1, e2, e3 ...   (each has role + name)
2. pick a ref    choose the ref whose role/name matches what you want
3. interact      browser_click / browser_fill / browser_select / browser_press
                 / browser_scroll / browser_upload / browser_download
                 (pass ref="e3", or an explicit locator if no ref fits)
4. wait          browser_wait with a condition when the page updates
5. snapshot again after the page changes (navigation, DOM update)
```

Refs are **temporary**:

- They belong to one page and one snapshot.
- They go **stale** after navigation or when the referenced element leaves the
  DOM. Using a stale ref returns an error telling you to snapshot again — do
  exactly that.
- Never reuse refs from a previous page or a previous snapshot.

## Tools

### Browser / session / page (Task 02)

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

### Snapshot & interaction (Task 03)

| Tool id | Purpose |
|---|---|
| `aether.playwright.browser_snapshot` | snapshot + element refs (`e1`, `e2`, ...) |
| `aether.playwright.browser_refs` | refs of the last snapshot (no re-scan) |
| `aether.playwright.browser_click` | click a ref/locator |
| `aether.playwright.browser_fill` | fill an input/textarea |
| `aether.playwright.browser_select` | select option(s) by value/label/index |
| `aether.playwright.browser_press` | press a key (on an element or the page) |
| `aether.playwright.browser_scroll` | element into view, by delta, or to top/bottom |
| `aether.playwright.browser_extract` | text/value/attribute/count/visible/enabled/selected |
| `aether.playwright.browser_wait` | condition-based wait |
| `aether.playwright.browser_upload` | set files on a file input |
| `aether.playwright.browser_download` | capture a download as an artifact |
| `aether.playwright.browser_dialog` | handle alert/confirm/prompt |

### Debugging (Task 04)

| Tool id | Purpose |
|---|---|
| `aether.playwright.browser_console` | structured console/page-error events + filters |
| `aether.playwright.browser_network` | captured request/response records + filters |
| `aether.playwright.browser_dom_inspect` | inspect one element (tag/text/attrs/box/html) |
| `aether.playwright.browser_screenshot` | full-page / viewport / element screenshot |
| `aether.playwright.browser_trace_start` | start Playwright tracing on a session |
| `aether.playwright.browser_trace_stop` | stop tracing, save a re-openable `.zip` |
| `aether.playwright.browser_trace_open` | open descriptor for a trace `.zip` (reveal with the OS if requested) |

### Debug UI & persistence (Task 05)

| Tool id | Purpose |
|---|---|
| `aether.playwright.browser_debug_panel` | debug-panel view model: sessions + pages + console + network (`renderer/type/data`) |
| `aether.playwright.session_save_state` | save a session's storage state (cookies + localStorage) |
| `aether.playwright.session_restore_state` | restore a saved storage state (new or existing session) |
| `aether.playwright.session_state_list` | list saved storage-state names |
| `aether.playwright.session_state_delete` | delete a saved storage state |

## Debug UI (Task 05)

The extension ships a **generic** debug UI. It does not add a new UI framework:
it registers normal Extension UI capabilities through `context.ui.register`, so
the standard AETHER UI runtime renders them (rendering is chosen from
`type` / `props.viewer_type`, never from `aether.playwright`).

Contributions:

```
aether.playwright.debug_panel        panel  — sessions, pages, console, network
aether.playwright.console_viewer     viewer — structured console events
aether.playwright.network_viewer     table  — method / url / status / type / failed
aether.playwright.dom_viewer         viewer — DOM inspection of one element
aether.playwright.screenshot_viewer  viewer — image preview of browser_screenshot
aether.playwright.trace_viewer       viewer — .zip reference of browser_trace_stop (+ Open Trace)
```

The panel view model is produced by `PlaywrightService.debug_panel(...)`; each
sub-view is also available on its own (`sessions_view`, `pages_view`,
`console_view`, `network_view`, `dom_view`, `screenshot_view`, `trace_view`,
`trace_status`). Every view returns the AETHER UI Result contract
(`{renderer, type, data, [artifact]}`).

```python
panel = service.debug_panel(session_id=..., page_id=...)
# panel["data"]["views"]   -> [sessions, pages, console, network, ...]
# panel["data"]["sessions"] / ["pages"]      -> table results
# panel["data"]["console"] / ["network"]     -> viewer/table results
# panel["data"]["actions"]                   -> Refresh / Close Page / ...

# DOM / screenshot / trace are opt-in (a plain refresh has no side effects):
panel = service.debug_panel(session_id=..., page_id=...,
                            dom={"locator": {"css": "#go"}},
                            screenshot={"full_page": True},
                            trace={"save_dir": "..."})
```

Panel actions map to existing capabilities: **Refresh**
(`browser_debug_panel`), **Close Page** (`page_close`), **Clear Console** /
**Clear Network** (`browser_console` / `browser_network` with `clear=true`),
**Screenshot** (`browser_screenshot`), **Start / Stop Trace**
(`browser_trace_start` / `browser_trace_stop`).

## Session persistence (Task 05)

Browser state survives an AETHER restart via Playwright's **official**
storage-state mechanism and the Extension storage API — useful for "debug a
logged-in flow" without re-authenticating every run:

```
session -> browser state -> save_state -> restart AETHER -> restore_state
```

```python
# save the current session (cookies + localStorage)
session_save_state(session_id=session_id, name="login", project=None)

# on a fresh AETHER process: restore straight into a new session
session_restore_state(name="login")                # -> creates a new session

# or re-apply the state to an existing session
session_restore_state(name="login", session_id=session_id)

# housekeeping
session_state_list()                                # -> names
session_state_delete(name="login")
```

Rules:

- State is stored through `context.storage`, **isolated per extension** and,
  when `project` is given, **per project**
  (`<project>/.aether/extensions/aether.playwright/…`). Extension-scope and
  project-scope states never mix.
- `restore_state` with no `session_id` uses
  `new_context(storage_state=…)` (the path that survives a restart); with a
  `session_id` it re-applies cookies via `add_cookies` and localStorage via an
  init script.
- **Credentials are never returned** to the UI or logs — only the state name,
  scope and counts (`cookie_count` / `origin_count`). The raw state stays in
  extension storage.

## Choosing a target

Every interaction tool accepts **one** of:

- `ref` — an element reference from the latest snapshot (**preferred**), or
- `locator` — an explicit Playwright locator, using one strategy:

  ```
  {"role": "button", "name": "Save"}         # preferred (semantic)
  {"label": "Email"}
  {"text": "Sign in"}
  {"placeholder": "you@example.com"}
  {"alt": "Product photo"}
  {"title": "Close"}
  {"test_id": "submit-btn"}
  {"css": "#email"}                          # last resort
  {"xpath": "//button[1]"}                   # last resort
  ```

Prefer semantic locators (`role` + `name`) over CSS. If a locator matches
several elements the tool returns an error — make the locator more specific, or
add `"nth": 0`, `"first": true` or `"last": true`.

## Extract / select / upload / download arguments

```
browser_extract(what="text"|"value"|"attribute"|"count"|"visible"|"enabled"|"selected"|"html",
                ref=<ref>, locator=<locator>, attribute="href")
# `what` may also be a list, e.g. ["text","visible"] -> result["values"]

browser_select(ref=<ref>, locator=<locator>, value="id")   # or values=[...], label="...", index=0
# NOTE: `label`/`index`/`value` describe the <option>. The element itself is
# selected with `ref` or `locator` (e.g. locator={"label": "State"}).

browser_upload(ref=<ref>, locator=<locator>, files=["C:/tmp/a.png"])
browser_download(ref=<ref>, locator=<locator>)           # -> {"filename": ..., "path": ...}
browser_dialog(action="accept"|"dismiss", prompt_text="...", wait=false)
#  - wait=false: configure how the *next* dialogs are answered automatically.
#  - wait=true : block until the next dialog appears and answer it now.
```

## Waiting

Use conditions, not fixed sleeps:

```
browser_wait(condition="visible",  ref="e5")
browser_wait(condition="hidden",   locator={"css": "#spinner"})
browser_wait(condition="enabled",  locator={"role": "button", "name": "Save"})
browser_wait(condition="text",     locator={"css": "#result"}, text="Saved")
browser_wait(condition="value",    ref="e2", text="ok@example.com")
browser_wait(condition="url",      url="**/dashboard")
browser_wait(condition="load",     load_state="networkidle")
```

`load_state="networkidle"` is the right choice after a reload when the page
fires its own XHR/fetch calls, so you read network evidence after they settle.

## Example flow

```python
# 1. snapshot
snap = tools.execute("aether.playwright.browser_snapshot", {"page_id": page_id})
# snap["refs"] == {"e1": {"role": "textbox", "name": "Email"}, "e2": {"role": "button", "name": "Save"}, ...}

# 2/3. interact using a ref
tools.execute("aether.playwright.browser_fill",
              {"page_id": page_id, "ref": "e1", "value": "dev@example.com"})
tools.execute("aether.playwright.browser_click", {"page_id": page_id, "ref": "e2"})

# 4. wait for the page to react
tools.execute("aether.playwright.browser_wait",
              {"page_id": page_id, "condition": "text",
               "locator": {"css": "#out"}, "text": "saved:"})

# 5. snapshot again (refs from step 1 are stale once the DOM changed)
snap2 = tools.execute("aether.playwright.browser_snapshot", {"page_id": page_id})

# extracting data
tools.execute("aether.playwright.browser_extract",
              {"page_id": page_id, "what": "text", "locator": {"css": "#out"}})
```

## Debugging arguments (Task 04)

```
browser_console(page_id,
                type="error",              # or types=["warning","error"] / level=...
                search="login",            # case-insensitive text substring
                limit=50, clear=false)
# -> {count, total, types, messages:[{timestamp, type, text, location,
#                                     source?, error_name?, stack?}]}
# `source="pageerror"` marks an uncaught JavaScript exception, with its `stack`.

browser_network(page_id,
                url="/api/login",          # substring or glob (*, ?)
                method="POST",             # GET/POST/...
                status=500,                # or "5xx"
                resource_type="fetch",     # document/script/xhr/fetch/image/...
                failed=true, limit=50, clear=false)
# -> {count, total, requests:[{url, method, resource_type, status, ok,
#                              status_text, timing, failure, duration_ms}]}

browser_dom_inspect(page_id, ref="e3" | locator={"css":"#go"},
                    html=false, html_limit=2000)
# -> {tag, text, attributes, visible, enabled, bounding_box, [value/checked],
#     [role/name], [html]}

browser_screenshot(page_id, full_page=true, ref=<ref>, locator=<locator>,
                   type="png"|"jpeg", quality=80, filename=..., save_dir=...)
# -> {scope: "full_page"|"viewport"|"element", path, mime_type, width, height}

browser_trace_start(session_id, screenshots=true, snapshots=true, sources=true)
browser_trace_stop(session_id, filename=..., save_dir=...)
# -> trace_stop returns {path, mime_type: "application/zip", size_bytes, ...}
```

## Lifecycle notes

- The extension is a normal AETHER Extension: `register` declares the config
  keys, the service capability and the debug-UI contributions; `enable` lazily
  builds the service and registers every tool; `disable` shuts the service down
  and releases every browser/session/page. `disable` **never** deletes persisted
  browser state, so a later `enable` still sees it.
- `install → update → uninstall` go through the standard
  `ExtensionManager`/installer infrastructure (the package declares
  `manifest.json`, `pyproject.toml`, `__init__.py`, `extension.py`).
- Playwright browsers are a separate `playwright install` step; the extension
  does not install them automatically.

## Tests

```bash
python -m pytest Extension/playwright/tests -q
```

`test_integration.py` is the end-to-end suite: it builds a small **local web
project** in `tmp_path` (with deliberate JavaScript / 404 / 500 / broken
interaction bugs), serves it over `127.0.0.1`, and drives it with real Chromium
through the whole observe → reproduce → evidence → fix → reload → verify
workflow, plus page/session isolation, persistence and lifecycle checks. All
browser tests skip automatically when no Playwright browser is installed.
