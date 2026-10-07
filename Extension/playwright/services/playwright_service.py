"""
Playwright Service — browser lifecycle, snapshot, element references and
interaction for the AETHER Playwright Extension.

The service is the single owner of the Playwright resource hierarchy:

    PlaywrightService
    ├── runtime      -> Playwright engine (started lazily, on demand)
    ├── browsers     -> Browser instances            (browser_id)
    ├── sessions     -> BrowserContext instances     (session_id)
    ├── pages        -> Page instances               (page_id)
    └── refs         -> element references           (page_id -> e1, e2, ...)

Layers, one object:

* ``_PlaywrightServiceCore`` — Task 02 lifecycle (launch / session / page /
  navigation).
* ``_SnapshotInteractionMixin`` — Task 03 snapshot, element refs, interaction,
  structured extraction and condition-based waits.
* ``_DebugMixin`` — Task 04 debugging (console, network, DOM, screenshot, trace).
* ``_DebugUIMixin`` — Task 05 generic debug-panel view models.
* ``_PersistenceMixin`` — Task 05 browser storage-state persistence.

``PlaywrightService`` combines them so every browser operation the LLM performs
goes through a single service.

Design rules:

* **No side effects on import.** The ``playwright`` engine is imported and
  started *only* when a browser is actually launched. Importing this module
  (or the Extension that uses it) never spawns a browser process and never
  calls ``sync_playwright().start()``.
* **Official Playwright API.** Uses the public Python API directly:
  ``BrowserType.launch()`` -> ``Browser.new_context()`` ->
  ``BrowserContext.new_page()``, ``page.goto()`` / ``reload()`` and the Locator
  API (``get_by_role``, ``click``, ``fill``, ``set_input_files``, ``wait_for``,
  ``select_option``, ...).
* **Abstraction for the LLM.** Every public method returns plain,
  JSON-serialisable ``dict`` values (never raw Playwright objects). Raw
  handles stay private and are only reachable through explicit ``*_object()``
  accessors reserved for internal use.
* **Explicit lifecycle.** A session is an explicit ``BrowserContext`` and a
  page is an explicit ``Page`` created from that context — no
  ``browser.new_page()`` convenience shortcut.
* **Temporary references.** Refs are scoped to one page + one snapshot and are
  invalidated by navigation; stale refs raise a clear error so the caller can
  take a fresh snapshot.
* **Thread-affine runtime (one runtime per worker thread).** Playwright's
  synchronous API is a *greenlet fibre* created by ``sync_playwright().start()``
  and it may only be driven — even ``stop()`` — from the very thread that
  created it. A runtime therefore belongs to the thread that started it
  ("generation"): the service reuses it only for that same thread, releases it
  (driver process included) when its thread is gone, and starts a brand new
  runtime on the calling thread. That lets several sequential AETHER tasks —
  each running on its own worker thread — share one service instance.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import signal
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------
class PlaywrightServiceError(Exception):
    """Base error for all Playwright service failures."""


class BrowserLaunchError(PlaywrightServiceError):
    """Raised when a browser engine could not be launched."""


class BrowserNotFoundError(PlaywrightServiceError):
    """Raised when a referenced ``browser_id`` is unknown."""


class SessionNotFoundError(PlaywrightServiceError):
    """Raised when a referenced ``session_id`` is unknown."""


class PageNotFoundError(PlaywrightServiceError):
    """Raised when a referenced ``page_id`` is unknown."""


class PageOperationError(PlaywrightServiceError):
    """Raised when a navigation/action on a page failed."""


class SnapshotError(PlaywrightServiceError):
    """Raised when a DOM snapshot could not be captured."""


class RefNotFoundError(PlaywrightServiceError):
    """Raised when an element reference is unknown for a page.

    This happens when no snapshot was taken yet, when the ref never existed,
    or when the page was navigated (which invalidates all refs).
    """


class StaleRefError(PlaywrightServiceError):
    """Raised when a known reference no longer resolves to an element.

    The DOM changed (navigation, big re-render, ...) after the snapshot was
    taken. The LLM must call ``browser_snapshot`` again to get fresh refs.
    """


class InvalidLocatorError(PlaywrightServiceError):
    """Raised when a locator specification is missing or malformed."""


class LocatorNotFoundError(PlaywrightServiceError):
    """Raised when a locator matches no element."""


class AmbiguousLocatorError(PlaywrightServiceError):
    """Raised when a locator matches several elements with no way to pick one."""


class InteractionError(PlaywrightServiceError):
    """Raised when an interaction (click/fill/upload/download/...) fails."""


class WaitTimeoutError(PlaywrightServiceError):
    """Raised when a condition-based wait did not become true in time."""


# ---------------------------------------------------------------------------
# Debugging errors (Task 04)
# ---------------------------------------------------------------------------
class DebugError(PlaywrightServiceError):
    """Base error for the Task 04 debugging surface (console/network/DOM/…)."""


class ConsoleCaptureError(DebugError):
    """Raised when console output could not be captured for a page."""


class NetworkCaptureError(DebugError):
    """Raised when network activity could not be captured for a page."""


class DomInspectionError(DebugError):
    """Raised when a DOM inspection could not be completed."""


class DomElementNotFoundError(DebugError):
    """Raised when the element to inspect does not exist on the page."""


class ScreenshotError(DebugError):
    """Raised when a screenshot could not be captured or saved."""


class TraceError(DebugError):
    """Base error for the Playwright tracing surface."""


class TraceNotActiveError(TraceError):
    """Raised when a trace is stopped (or read) while none is active."""


class TraceAlreadyActiveError(TraceError):
    """Raised when tracing is started while a trace is already active."""


# ---------------------------------------------------------------------------
# Persistence errors (Task 05)
# ---------------------------------------------------------------------------
class StateError(PlaywrightServiceError):
    """Base error for the browser storage-state persistence surface (Task 05)."""


class StateStoreError(StateError):
    """Raised when the browser storage-state backend is missing or fails."""


class StateNotFoundError(StateError):
    """Raised when no saved browser state exists for the requested key."""


# ---------------------------------------------------------------------------
# Element references (Task 03)
# ---------------------------------------------------------------------------
@dataclass
class ElementRef:
    """A temporary, snapshot-scoped handle to an element on a page."""

    ref: str
    role: str
    name: str
    tag: str
    position: int
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ref": self.ref,
            "role": self.role,
            "name": self.name,
            "tag": self.tag,
        }
        payload.update(self.details)
        return payload


@dataclass
class RefRegistry:
    """All refs produced by the most recent snapshot of one page."""

    page_id: str
    url: Optional[str]
    generation: int
    refs: Dict[str, ElementRef] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_id": self.page_id,
            "url": self.url,
            "generation": self.generation,
            "refs": {ref: el.to_dict() for ref, el in self.refs.items()},
        }


# ---------------------------------------------------------------------------
# Page-side scripts (kept as plain expression strings so they can be handed to
# ``page.evaluate``). ``page.evaluate`` types the argument as an expression,
# therefore both scripts below are self-invoking function expressions.
# ---------------------------------------------------------------------------
#: Collects a compact, LLM-friendly element list and tags every element with a
#: temporary ``data-aether-ref`` attribute (``e1``, ``e2``, ...). The marker
#: comment is used by the test doubles to recognise the scan call.
SNAPSHOT_SCAN_JS = r"""
(() => {
  /* AETHER_SNAPSHOT_SCAN */
  const REF_ATTR = 'data-aether-ref';
  const MAX = __MAX__;
  const ROOT_SELECTOR = __SELECTOR__;

  const TAGS = {
    a: 'link', button: 'button', select: 'combobox', textarea: 'textbox',
    img: 'img', h1: 'heading', h2: 'heading', h3: 'heading', h4: 'heading',
    h5: 'heading', h6: 'heading', nav: 'navigation', main: 'main',
    header: 'banner', footer: 'contentinfo', form: 'form', table: 'table',
    ul: 'list', ol: 'list', li: 'listitem', dialog: 'dialog', option: 'option',
    summary: 'button', details: 'group', output: 'status',
    progress: 'progressbar', fieldset: 'group', figure: 'figure', iframe: 'iframe'
  };
  const ROLES = [
    'link', 'button', 'textbox', 'searchbox', 'checkbox', 'radio', 'combobox',
    'listbox', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'tab', 'switch',
    'slider', 'spinbutton', 'option', 'heading', 'img', 'form', 'navigation',
    'main', 'banner', 'contentinfo', 'table', 'list', 'listitem', 'grid',
    'gridcell', 'treeitem', 'progressbar', 'status', 'dialog', 'alertdialog',
    'group', 'figure', 'iframe', 'tabpanel', 'fileinput', 'columnheader',
    'row', 'rowheader', 'cell', 'region', 'article'
  ];
  const INPUT_ROLES = {
    text: 'textbox', search: 'searchbox', email: 'textbox', url: 'textbox',
    tel: 'textbox', password: 'textbox', number: 'spinbutton', checkbox: 'checkbox',
    radio: 'radio', range: 'slider', submit: 'button', button: 'button',
    reset: 'button', file: 'fileinput', image: 'button', color: 'button',
    date: 'textbox', 'datetime-local': 'textbox', month: 'textbox', week: 'textbox',
    time: 'textbox'
  };

  function roleOf(el) {
    const explicit = el.getAttribute && el.getAttribute('role');
    if (explicit) return explicit.trim().toLowerCase();
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      return INPUT_ROLES[t] || 'textbox';
    }
    return TAGS[tag] || null;
  }

  function isVisible(el) {
    if (!el.getBoundingClientRect) return true;
    let style = null;
    try { style = window.getComputedStyle(el); } catch (e) { style = null; }
    if (style) {
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      if (style.opacity !== '' && parseFloat(style.opacity) === 0) return false;
    }
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    return true;
  }

  function labelText(el) {
    const id = el.getAttribute && el.getAttribute('id');
    if (id && document.querySelector) {
      try {
        const lab = document.querySelector('label[for="' + String(id).replace(/"/g, '\\"') + '"]');
        if (lab) return (lab.textContent || '').trim();
      } catch (e) { /* invalid selector - ignore */ }
    }
    let parent = el.parentElement;
    let hops = 0;
    while (parent && hops < 3) {
      if (parent.tagName && parent.tagName.toLowerCase() === 'label') {
        return (parent.textContent || '').trim();
      }
      parent = parent.parentElement;
      hops += 1;
    }
    return '';
  }

  function nameOf(el, role) {
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelledby = el.getAttribute && el.getAttribute('aria-labelledby');
    if (labelledby && document.getElementById) {
      const parts = labelledby.split(/\s+/).map(function (i) {
        const t = document.getElementById(i);
        return t ? (t.textContent || '').trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    if (tag === 'img') {
      const alt = el.getAttribute('alt');
      if (alt) return alt.trim();
    }
    if (role === 'button' && tag === 'input') {
      const v = el.getAttribute('value');
      if (v) return v.trim();
    }
    const labelled = labelText(el);
    if (labelled) return labelled;
    const ph = el.getAttribute && el.getAttribute('placeholder');
    if (ph && !((el.textContent || '').trim())) return ph.trim();
    const title = el.getAttribute && el.getAttribute('title');
    if (title) return title.trim();
    const text = (el.textContent || '').trim();
    if (text) return text.slice(0, 120);
    const value = el.getAttribute && el.getAttribute('value');
    if (value) return value.trim();
    if (ph) return ph.trim();
    return '';
  }

  const root = (ROOT_SELECTOR && document.querySelector(ROOT_SELECTOR)) ||
               document.body || document.documentElement;
  const previous = document.querySelectorAll('[' + REF_ATTR + ']');
  for (let i = 0; i < previous.length; i++) {
    previous[i].removeAttribute(REF_ATTR);
  }

  const nodes = [];
  if (root && root.querySelectorAll) {
    const all = root.querySelectorAll('*');
    for (let i = 0; i < all.length; i++) {
      if (MAX > 0 && nodes.length >= MAX) break;
      const el = all[i];
      const role = roleOf(el);
      if (!role || ROLES.indexOf(role) === -1) continue;
      if (!isVisible(el)) continue;
      const name = nameOf(el, role);
      const ref = 'e' + (nodes.length + 1);
      try { el.setAttribute(REF_ATTR, ref); } catch (e) { continue; }
      const tag = (el.tagName || '').toLowerCase();
      const node = { ref: ref, role: role, name: name, tag: tag };
      if ((tag === 'input' || tag === 'textarea' || tag === 'select') &&
          typeof el.value === 'string' && el.value !== '') {
        node.value = el.value;
      }
      if (tag === 'input') node.input_type = (el.getAttribute('type') || 'text').toLowerCase();
      const phv = el.getAttribute('placeholder');
      if (phv) node.placeholder = phv;
      const href = el.getAttribute && el.getAttribute('href');
      if (href && role === 'link') node.href = href;
      if (el.hasAttribute && el.hasAttribute('disabled')) node.disabled = true;
      if (typeof el.checked === 'boolean' && el.checked) node.checked = true;
      if (role === 'heading' && tag.length === 2) {
        const lvl = parseInt(tag.charAt(1), 10);
        if (!isNaN(lvl)) node.level = lvl;
      }
      nodes.push(node);
    }
  }
  return { marker: 'AETHER_SNAPSHOT_SCAN', nodes: nodes, count: nodes.length };
})()
"""

#: Scrolls the window to an absolute position (used for ``to='top'``/``'bottom'``).
SCROLL_TO_JS = r"""
(() => {
  /* AETHER_SCROLL_TO */
  window.scrollTo({ left: __X__, top: __Y__, behavior: __BEHAVIOR__ });
  return true;
})()
"""

#: Inspects a single element through ``Locator.evaluate`` and returns a compact,
#: LLM-friendly description (tag, text, attributes, bounding box, value/checked).
#: The marker comment is used by the test doubles to recognise the call.
DOM_INSPECT_JS = r"""
(el) => {
  /* AETHER_DOM_INSPECT */
  const attrs = {};
  if (el.attributes) {
    for (let i = 0; i < el.attributes.length; i++) {
      const a = el.attributes[i];
      attrs[a.name] = a.value;
    }
  }
  let box = null;
  if (el.getBoundingClientRect) {
    const r = el.getBoundingClientRect();
    box = { x: r.x, y: r.y, width: r.width, height: r.height };
  }
  const out = {
    tag: (el.tagName || '').toLowerCase(),
    text: (el.innerText || el.textContent || '').trim(),
    attributes: attrs,
    bounding_box: box,
  };
  if (typeof el.value !== 'undefined' && el.value !== null) out.value = el.value;
  if (typeof el.checked === 'boolean') out.checked = el.checked;
  if (typeof el.disabled === 'boolean') out.disabled = el.disabled;
  if (typeof el.selected === 'boolean') out.selected = el.selected;
  try {
    const markup = el.outerHTML || '';
    out.html = markup.length > 20000 ? markup.slice(0, 20000) : markup;
  } catch (e) {
    out.html = null;
  }
  return out;
}
"""

#: Locator states that Playwright can wait for natively via ``Locator.wait_for``.
LOCATOR_WAIT_STATES = ("visible", "hidden", "attached", "detached")

#: Load states accepted by ``Page.wait_for_load_state``.
LOAD_STATES = ("load", "domcontentloaded", "networkidle", "commit")


# ---------------------------------------------------------------------------
# Internal handles — the service's bookkeeping records. They wrap the raw
# Playwright objects but are never handed to the LLM directly.
# ---------------------------------------------------------------------------
@dataclass
class BrowserHandle:
    browser_id: str
    engine: str
    headless: bool
    browser: Any

    def to_dict(self) -> Dict[str, Any]:
        connected = False
        try:
            connected = bool(self.browser.is_connected())
        except Exception:
            connected = False
        return {
            "browser_id": self.browser_id,
            "engine": self.engine,
            "headless": self.headless,
            "connected": connected,
        }


@dataclass
class SessionHandle:
    session_id: str
    browser_id: str
    context: Any

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "browser_id": self.browser_id,
        }


@dataclass
class PageHandle:
    page_id: str
    session_id: str
    page: Any


# ---------------------------------------------------------------------------
# Runtime helpers — releasing a synchronous Playwright runtime that can no
# longer be driven through the API (its owning worker thread has ended).
# ---------------------------------------------------------------------------
def _runtime_driver_process(runtime: Any) -> Optional[Any]:
    """Return the Playwright *driver* subprocess behind a sync runtime.

    ``sync_playwright().start()`` returns the ``Playwright`` facade, whose
    pipeline is ``._impl_obj -> ._connection -> ._transport -> ._proc``. The
    lookup is deliberately defensive: a runtime injected through
    ``runtime_factory`` (test double) has no driver and simply yields ``None``.
    """
    if runtime is None:
        return None
    impl = getattr(runtime, "_impl_obj", None) or runtime
    connection = getattr(impl, "_connection", None)
    transport = getattr(connection, "_transport", None)
    return getattr(transport, "_proc", None)


def _terminate_runtime_driver(runtime: Any) -> bool:
    """Best-effort release of an abandoned runtime's driver process.

    Used only when the runtime's owning thread is gone, where ``stop()`` is
    impossible (greenlets cannot be switched across threads) and would leak the
    driver/browser processes for the rest of the AETHER process lifetime.
    """
    process = _runtime_driver_process(runtime)
    if process is None:
        return False
    for action in ("kill", "terminate"):
        callback = getattr(process, action, None)
        if not callable(callback):
            continue
        try:
            callback()
            return True
        except Exception:
            continue
    pid = getattr(process, "pid", None)
    if not pid:
        return False
    try:
        os.kill(int(pid), signal.SIGTERM)
        return True
    except Exception:
        return False


class _PlaywrightServiceCore:
    """Owns the Playwright browser / session / page lifecycle (Task 02).

    Parameters
    ----------
    browser:
        Default browser engine (``chromium`` / ``firefox`` / ``webkit``).
    headless:
        Default headless mode.
    timeout:
        Default timeout in **seconds** applied to navigation actions.
    config:
        Optional mapping merged over the three defaults above.
    runtime_factory:
        Optional zero-argument callable returning a Playwright runtime. Only
        used for testing/injection; production leaves it ``None`` so the real
        ``sync_playwright().start()`` is used lazily.

    Attributes
    ----------
    config: dict
        Effective configuration (browser / headless / timeout).
    _runtime: Any
        The Playwright runtime, or ``None`` until a browser is launched.
    _browsers: dict[str, BrowserHandle]
    _contexts: dict[str, SessionHandle]
        BrowserContexts keyed by ``session_id`` (kept under the historical
        name ``_contexts`` for backward compatibility; exposed as
        :attr:`sessions`).
    _pages: dict[str, PageHandle]
    """

    def __init__(
        self,
        *,
        browser: str = "chromium",
        headless: bool = True,
        timeout: int = 30,
        snapshot_max_elements: int = 200,
        artifact_dir: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        runtime_factory: Optional[Callable[[], Any]] = None,
        expect_factory: Optional[Callable[[], Any]] = None,
        storage: Optional[Any] = None,
        state_dir: Optional[str] = None,
        extension_id: str = "aether.playwright",
    ) -> None:
        cfg: Dict[str, Any] = {
            "browser": browser,
            "headless": headless,
            "timeout": timeout,
            "snapshot_max_elements": snapshot_max_elements,
            "artifact_dir": artifact_dir,
        }
        if config:
            cfg.update(config)
        self.config: Dict[str, Any] = cfg

        # Task 05 — persistence. ``storage`` is the real AETHER Extension
        # storage facade (``context.storage``); ``state_dir`` is a plain
        # filesystem fallback so the service stays usable/testable without a
        # full ExtensionContext. Neither is required until persistence is used.
        self._extension_id = extension_id or "aether.playwright"
        if state_dir is None:
            state_dir = self.config.get("state_dir")
        self._browser_state_store: Optional["BrowserStateStore"] = None
        if storage is not None or state_dir is not None:
            try:
                self._browser_state_store = _new_state_store(
                    storage=storage, base_dir=state_dir, extension_id=self._extension_id
                )
            except Exception:
                self._browser_state_store = None

        # Runtime is created lazily; nothing Playwright-related happens here.
        # ``(runtime, owner thread)`` is one *generation*: Playwright's sync API
        # is a greenlet fibre bound to the thread that started it, so a runtime
        # may only be reused by that very thread (see ``_ensure_runtime``).
        self._runtime: Optional[Any] = None
        self._runtime_thread: Optional[int] = None
        self._runtime_owner: Optional[threading.Thread] = None
        self._runtime_generation: int = 0
        #: Bounded history of runtime releases (observability for diagnostics).
        self._runtime_releases: List[Dict[str, Any]] = []
        self._runtime_factory = runtime_factory
        # Optional injection point for ``playwright.sync_api.expect`` (used by
        # the condition-based wait, and by the test doubles).
        self._expect_factory = expect_factory

        # Backward-compatible attribute exposed by the Task 01 foundation.
        self._browser: Optional[Any] = None

        self._browsers: Dict[str, BrowserHandle] = {}
        self._contexts: Dict[str, SessionHandle] = {}
        self._pages: Dict[str, PageHandle] = {}

        self._default_browser_id: Optional[str] = None
        self._default_session_id: Optional[str] = None

        # Task 03 state: element refs (per page), dialog policies/history.
        self._refs: Dict[str, RefRegistry] = {}
        self._ref_generation: int = 0
        self._dialog_policies: Dict[str, Dict[str, Any]] = {}
        self._dialog_handlers: Dict[str, Any] = {}
        self._dialogs: Dict[str, List[Dict[str, Any]]] = {}

        # Task 04 debug state. Console/network evidence is bound to a page
        # (``page_id``); tracing is bound to a session (``session_id``) because
        # Playwright tracing lives on the BrowserContext. Keeping them keyed
        # this way guarantees page A never mixes with page B.
        self._console: Dict[str, List[Dict[str, Any]]] = {}
        self._network: Dict[str, List[Dict[str, Any]]] = {}
        #: page_id -> {id(request) -> network record} for request/response pairing.
        self._network_pending: Dict[str, Dict[int, Dict[str, Any]]] = {}
        #: page_id -> {event -> handler} for the listeners we attached.
        self._debug_listeners: Dict[str, Dict[str, Any]] = {}
        #: session_id -> trace state ({active, started_at, path, ...}).
        self._traces: Dict[str, Dict[str, Any]] = {}

        # The synchronous Playwright API is not thread-safe; guard all
        # bookkeeping and runtime access with a re-entrant lock.
        self._lock = threading.RLock()

    # -----------------------------------------------------------------
    # Identifier helpers — stable IDs for browsers / sessions / pages.
    # -----------------------------------------------------------------
    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"

    def new_browser_id(self) -> str:
        return self._new_id("browser")

    def new_session_id(self) -> str:
        return self._new_id("session")

    def new_context_id(self) -> str:
        # Context == session in this design; kept for API compatibility.
        return self._new_id("context")

    def new_page_id(self) -> str:
        return self._new_id("page")

    # -----------------------------------------------------------------
    # Read-only views of the internal structure.
    # -----------------------------------------------------------------
    @property
    def runtime(self) -> Optional[Any]:
        """The Playwright runtime, or ``None`` until a browser is launched."""
        return self._runtime

    @property
    def is_runtime_started(self) -> bool:
        return self._runtime is not None

    @property
    def browsers(self) -> Dict[str, BrowserHandle]:
        return self._browsers

    @property
    def sessions(self) -> Dict[str, SessionHandle]:
        return self._contexts

    @property
    def pages(self) -> Dict[str, PageHandle]:
        return self._pages

    # -----------------------------------------------------------------
    # Runtime
    # -----------------------------------------------------------------
    def _runtime_owner_is_current_thread(self) -> bool:
        """True when the live runtime may be driven by the calling thread."""
        return (
            self._runtime is not None
            and self._runtime_owner is threading.current_thread()
        )

    def _thread_conflict_message(self, action: str) -> str:
        """Explain a cross-thread use of the thread-bound Playwright runtime."""
        owner = self._runtime_owner
        owner_name = owner.name if owner is not None else "<unknown>"
        current = threading.current_thread()
        return (
            "Playwright runtime is bound to thread "
            f"{self._runtime_thread} ({owner_name}) but used from thread "
            f"{threading.get_ident()} ({current.name}) while handling "
            f"'{action}'. The synchronous Playwright API must be used from a "
            "single thread: keep every Playwright call of one task on the same "
            "worker thread, or close the browser so the runtime is released."
        )

    def _ensure_runtime_thread(self, action: str) -> None:
        """Make sure the *current* thread may drive the live runtime.

        A runtime whose owning worker thread has ended can neither be used nor
        stopped through the synchronous API (its greenlet fibre died with the
        thread), so it is released together with everything it created. The
        caller then sees an empty service and can start a fresh runtime on its
        own thread. While the owning thread is still alive the call is refused
        with a clear error, because the runtime is genuinely in use elsewhere.
        """
        with self._lock:
            if self._runtime is None or self._runtime_owner is threading.current_thread():
                return
            owner = self._runtime_owner
            if owner is not None and owner.is_alive():
                raise PlaywrightServiceError(self._thread_conflict_message(action))
            self._detach_runtime(reason=f"abandoned_runtime_thread:{action}")

    def _ensure_runtime(self) -> Any:
        """Return a Playwright runtime **usable by the calling thread**.

        The runtime is created lazily on first use and is strictly bound to the
        AETHER worker thread that created it. When the next task runs on a new
        worker thread, the leftover runtime of the finished one is released
        (driver process included) and a fresh runtime is started here — so one
        service instance survives many sequential tasks without leaking the
        previous Playwright driver.
        """
        with self._lock:
            if self._runtime_owner_is_current_thread():
                return self._runtime
            if self._runtime is not None:
                owner = self._runtime_owner
                if owner is not None and owner.is_alive():
                    raise PlaywrightServiceError(self._thread_conflict_message("runtime"))
                # The previous task's worker thread is gone: its runtime cannot
                # be reused (nor stopped) from here — release it and start a new
                # one on this thread.
                self._detach_runtime(reason="previous_task_thread_ended")
            return self._start_runtime()

    def _start_runtime(self) -> Any:
        """Start a fresh runtime on the calling thread and record its owner."""
        if self._runtime_factory is not None:
            runtime = self._runtime_factory()
        else:
            # Local import: never executed unless a browser is used.
            from playwright.sync_api import sync_playwright

            runtime = sync_playwright().start()
        self._runtime = runtime
        self._runtime_thread = threading.get_ident()
        self._runtime_owner = threading.current_thread()
        self._runtime_generation += 1
        return runtime

    def _detach_runtime(self, *, reason: str) -> Dict[str, Any]:
        """Drop the current runtime generation and release what it owns.

        Everything created by that runtime (browsers, sessions, pages and the
        derived caches) is forgotten, so no stale Playwright object is ever
        reused. The runtime itself is stopped from its owning thread; when that
        thread is gone the driver subprocess is terminated instead, because the
        synchronous API cannot be driven from a foreign thread.
        """
        with self._lock:
            runtime = self._runtime
            owner = self._runtime_owner
            generation = self._runtime_generation
            self._runtime = None
            self._runtime_thread = None
            self._runtime_owner = None
            self._reset_runtime_resources()
            outcome: Dict[str, Any] = {"reason": reason, "released": False, "mode": "none"}
            if runtime is None:
                return outcome
            if owner is None or owner is threading.current_thread():
                try:
                    runtime.stop()
                except Exception:  # noqa: BLE001 - release is best effort
                    outcome["mode"] = "stop_failed"
                else:
                    outcome["released"] = True
                    outcome["mode"] = "stopped"
            else:
                outcome["released"] = _terminate_runtime_driver(runtime)
                outcome["mode"] = (
                    "driver_terminated" if outcome["released"] else "release_failed"
                )
            self._record_runtime_release(outcome, generation, owner)
            return outcome

    def _record_runtime_release(
        self, outcome: Dict[str, Any], generation: int, owner: Optional[threading.Thread]
    ) -> None:
        """Keep a short, bounded history of runtime releases (observability)."""
        record = {
            "generation": generation,
            "reason": outcome["reason"],
            "released": outcome["released"],
            "mode": outcome["mode"],
            "owner_thread": owner.name if owner is not None else None,
            "owner_thread_id": getattr(owner, "ident", None),
        }
        self._runtime_releases.append(record)
        del self._runtime_releases[:-20]

    def _reset_runtime_resources(self) -> None:
        """Forget every browser/session/page and all derived caches."""
        self._browsers.clear()
        self._contexts.clear()
        self._pages.clear()
        self._browser = None
        self._default_browser_id = None
        self._default_session_id = None
        self._refs.clear()
        self._dialog_policies.clear()
        self._dialog_handlers.clear()
        self._dialogs.clear()
        self._console.clear()
        self._network.clear()
        self._network_pending.clear()
        self._debug_listeners.clear()
        self._traces.clear()

    def start_runtime(self) -> Any:
        """Explicitly start the runtime (still lazy-friendly)."""
        return self._ensure_runtime()

    # -----------------------------------------------------------------
    # Browsers
    # -----------------------------------------------------------------
    def launch_browser(
        self,
        browser: Optional[str] = None,
        headless: Optional[bool] = None,
        *,
        args: Optional[List[str]] = None,
        timeout: Optional[float] = None,
        **launch_options: Any,
    ) -> str:
        """Launch a browser and return its ``browser_id``.

        The runtime is started here (first launch) if it is not running yet.
        """
        with self._lock:
            runtime = self._ensure_runtime()
            engine = browser or self.config.get("browser") or "chromium"
            if headless is None:
                headless = bool(self.config.get("headless", True))
            browser_type = getattr(runtime, engine, None)
            if browser_type is None:
                raise BrowserLaunchError(f"Unknown browser engine '{engine}'")

            options: Dict[str, Any] = dict(launch_options)
            if args is not None:
                options["args"] = list(args)
            if timeout is not None:
                options["timeout"] = self._ms(timeout)

            try:
                browser_obj = browser_type.launch(headless=bool(headless), **options)
            except Exception as exc:  # noqa: BLE001 - wrap into service error
                raise BrowserLaunchError(
                    f"Failed to launch browser '{engine}': {type(exc).__name__}: {exc}"
                ) from exc

            browser_id = self.new_browser_id()
            self._browsers[browser_id] = BrowserHandle(
                browser_id=browser_id,
                engine=str(engine),
                headless=bool(headless),
                browser=browser_obj,
            )
            if self._default_browser_id is None:
                self._default_browser_id = browser_id
                self._browser = browser_obj
            return browser_id

    def close_browser(self, browser_id: Optional[str] = None) -> Dict[str, Any]:
        """Close one browser (all of them when ``browser_id`` is omitted).

        Closing the **last** browser also releases the Playwright runtime
        (driver process included), so a finished task leaves no Playwright
        state behind; the next Playwright call starts a fresh runtime on its
        own thread.
        """
        with self._lock:
            self._ensure_runtime_thread("browser_close")
            if browser_id is None:
                ids = list(self._browsers)
                results = [self._close_browser(bid) for bid in ids]
                payload: Dict[str, Any] = {"closed_browsers": ids, "results": results}
            else:
                payload = self._close_browser(browser_id)
            if not self._browsers:
                release = self._detach_runtime(reason="no_browsers_left")
                payload["runtime_released"] = release["released"]
                payload["runtime_release_mode"] = release["mode"]
            return payload

    def _close_browser(self, browser_id: str) -> Dict[str, Any]:
        handle = self._browsers.pop(browser_id, None)
        if handle is None:
            raise BrowserNotFoundError(f"Browser '{browser_id}' not found")
        session_ids = [sid for sid, s in self._contexts.items() if s.browser_id == browser_id]
        debug_page_ids = [
            pid for pid, p in self._pages.items() if p.session_id in session_ids
        ]
        for sid in session_ids:
            self._contexts.pop(sid, None)
        if session_ids:
            self._pages = {
                pid: p for pid, p in self._pages.items() if p.session_id not in session_ids
            }
        self._drop_debug(page_ids=debug_page_ids, session_ids=session_ids)
        try:
            handle.browser.close()
        except Exception:
            pass
        self._sync_defaults()
        return {
            "browser_id": browser_id,
            "closed": True,
            "sessions_closed": session_ids,
        }

    def list_browsers(self) -> List[Dict[str, Any]]:
        """Return metadata for every launched browser."""
        with self._lock:
            self._ensure_runtime_thread("browser_list")
            result = []
            for bid, handle in self._browsers.items():
                info = handle.to_dict()
                info["session_count"] = sum(
                    1 for s in self._contexts.values() if s.browser_id == bid
                )
                result.append(info)
            return result

    def browser_info(self, browser_id: str) -> Dict[str, Any]:
        with self._lock:
            self._ensure_runtime_thread("browser_info")
            handle = self._browsers.get(browser_id)
            if handle is None:
                raise BrowserNotFoundError(f"Browser '{browser_id}' not found")
            info = handle.to_dict()
            info["session_count"] = sum(
                1 for s in self._contexts.values() if s.browser_id == browser_id
            )
            return info

    def browser_object(self, browser_id: Optional[str] = None) -> Any:
        """Return the raw Browser (internal use only — never for the LLM)."""
        with self._lock:
            self._ensure_runtime_thread("browser_object")
            bid = self._resolve_browser_id(browser_id)
            return self._browsers[bid].browser

    # -----------------------------------------------------------------
    # Sessions (BrowserContext)
    # -----------------------------------------------------------------
    def create_session(
        self,
        browser_id: Optional[str] = None,
        **context_options: Any,
    ) -> str:
        """Create an isolated BrowserContext and return its ``session_id``."""
        with self._lock:
            self._ensure_runtime_thread("session_create")
            bid = self._resolve_browser_id(browser_id)
            handle = self._browsers[bid]
            try:
                context = handle.browser.new_context(**context_options)
            except Exception as exc:  # noqa: BLE001
                raise PlaywrightServiceError(
                    f"Failed to create session on browser '{bid}': "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            session_id = self.new_session_id()
            self._contexts[session_id] = SessionHandle(
                session_id=session_id,
                browser_id=bid,
                context=context,
            )
            if self._default_session_id is None:
                self._default_session_id = session_id
            return session_id

    # Friendly alias — "session" and "context" are the same concept here.
    def new_context(self, browser_id: Optional[str] = None, **context_options: Any) -> str:
        return self.create_session(browser_id, **context_options)

    def close_session(self, session_id: str) -> Dict[str, Any]:
        """Close a session (BrowserContext) and all of its pages."""
        with self._lock:
            self._ensure_runtime_thread("session_close")
            handle = self._contexts.pop(session_id, None)
            if handle is None:
                raise SessionNotFoundError(f"Session '{session_id}' not found")
            page_ids = [pid for pid, p in self._pages.items() if p.session_id == session_id]
            for pid in page_ids:
                self._pages.pop(pid, None)
            self._drop_debug(page_ids=page_ids, session_ids=[session_id])
            try:
                handle.context.close()
            except Exception:
                pass
            self._sync_defaults()
            return {
                "session_id": session_id,
                "closed": True,
                "pages_closed": page_ids,
            }

    def list_sessions(self, browser_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return metadata for sessions (optionally filtered by browser)."""
        with self._lock:
            result = []
            for sid, handle in self._contexts.items():
                if browser_id is not None and handle.browser_id != browser_id:
                    continue
                info = handle.to_dict()
                info["page_ids"] = [
                    pid for pid, p in self._pages.items() if p.session_id == sid
                ]
                info["page_count"] = len(info["page_ids"])
                result.append(info)
            return result

    def session_info(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            handle = self._contexts.get(session_id)
            if handle is None:
                raise SessionNotFoundError(f"Session '{session_id}' not found")
            info = handle.to_dict()
            info["page_ids"] = [
                pid for pid, p in self._pages.items() if p.session_id == session_id
            ]
            info["page_count"] = len(info["page_ids"])
            return info

    def context_object(self, session_id: Optional[str] = None) -> Any:
        """Return the raw BrowserContext (internal use only)."""
        with self._lock:
            self._ensure_runtime_thread("context_object")
            sid = self._resolve_session_id(session_id)
            return self._contexts[sid].context

    # -----------------------------------------------------------------
    # Pages
    # -----------------------------------------------------------------
    def new_page(
        self,
        session_id: Optional[str] = None,
        url: Optional[str] = None,
        *,
        wait_until: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """Create a page/tab inside a session and return its ``page_id``."""
        with self._lock:
            self._ensure_runtime_thread("page_new")
            sid = self._resolve_session_id(session_id)
            handle = self._contexts[sid]
            try:
                page = handle.context.new_page()
            except Exception as exc:  # noqa: BLE001
                raise PlaywrightServiceError(
                    f"Failed to create page in session '{sid}': "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            page_id = self.new_page_id()
            self._pages[page_id] = PageHandle(
                page_id=page_id,
                session_id=sid,
                page=page,
            )
            # Task 04: start capturing console/network evidence immediately, so
            # the tool layer can inspect it *after* the problem is reproduced.
            self._ensure_debug_listeners(page_id, page)
            if url:
                self.navigate(page_id, url, wait_until=wait_until, timeout=timeout)
            return page_id

    # Friendly alias.
    def create_page(self, session_id: Optional[str] = None, url: Optional[str] = None, **kw: Any) -> str:
        return self.new_page(session_id, url, **kw)

    def close_page(self, page_id: str) -> Dict[str, Any]:
        """Close a single page and drop it from the registry."""
        with self._lock:
            self._ensure_runtime_thread("page_close")
            handle = self._pages.pop(page_id, None)
            if handle is None:
                raise PageNotFoundError(f"Page '{page_id}' not found")
            self._drop_debug(page_ids=[page_id])
            try:
                handle.page.close()
            except Exception:
                pass
            return {
                "page_id": page_id,
                "session_id": handle.session_id,
                "closed": True,
            }

    def list_pages(self, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return metadata for pages.

        When ``session_id`` is omitted, pages of *all* sessions are returned.
        """
        with self._lock:
            self._ensure_runtime_thread("page_list")
            result = []
            for pid, handle in list(self._pages.items()):
                if session_id is not None and handle.session_id != session_id:
                    continue
                result.append(self._page_state(pid))
            return result

    def page_info(self, page_id: str) -> Dict[str, Any]:
        with self._lock:
            return self._page_state(page_id)

    def page_object(self, page_id: str) -> Any:
        """Return the raw Page (internal use only — for later snapshot/click)."""
        with self._lock:
            return self._require_page(page_id)

    # -----------------------------------------------------------------
    # Navigation
    # -----------------------------------------------------------------
    def navigate(
        self,
        page_id: str,
        url: str,
        *,
        wait_until: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Navigate a page to ``url``."""
        with self._lock:
            page = self._require_page(page_id)
            try:
                response = page.goto(
                    url,
                    wait_until=wait_until or "load",
                    timeout=self._ms(timeout),
                )
            except Exception as exc:  # noqa: BLE001
                raise PageOperationError(
                    f"navigate to '{url}' failed: {type(exc).__name__}: {exc}"
                ) from exc
            self._invalidate_refs(page_id)
            return self._page_state(page_id, response=response)

    # Friendly alias.
    def goto(self, page_id: str, url: str, **kw: Any) -> Dict[str, Any]:
        return self.navigate(page_id, url, **kw)

    def reload(
        self,
        page_id: str,
        *,
        wait_until: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Reload the current page."""
        with self._lock:
            page = self._require_page(page_id)
            try:
                response = page.reload(
                    wait_until=wait_until,
                    timeout=self._ms(timeout),
                )
            except Exception as exc:  # noqa: BLE001
                raise PageOperationError(
                    f"reload failed: {type(exc).__name__}: {exc}"
                ) from exc
            self._invalidate_refs(page_id)
            return self._page_state(page_id, response=response)

    def go_back(
        self,
        page_id: str,
        *,
        wait_until: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Navigate back in history."""
        return self._history_navigation(page_id, "go_back", wait_until, timeout)

    def go_forward(
        self,
        page_id: str,
        *,
        wait_until: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Navigate forward in history."""
        return self._history_navigation(page_id, "go_forward", wait_until, timeout)

    def _history_navigation(
        self,
        page_id: str,
        direction: str,
        wait_until: Optional[str],
        timeout: Optional[float],
    ) -> Dict[str, Any]:
        with self._lock:
            page = self._require_page(page_id)
            try:
                action = getattr(page, direction)
                response = action(wait_until=wait_until, timeout=self._ms(timeout))
            except Exception as exc:  # noqa: BLE001
                raise PageOperationError(
                    f"{direction} failed: {type(exc).__name__}: {exc}"
                ) from exc
            state = self._page_state(page_id, response=response)
            state["history_moved"] = response is not None
            self._invalidate_refs(page_id)
            return state

    # -----------------------------------------------------------------
    # Whole-service lifecycle
    # -----------------------------------------------------------------
    def shutdown(self) -> Dict[str, Any]:
        """Close everything and release the Playwright runtime.

        Safe to call from any thread, including a thread that does not own the
        current runtime (e.g. an Extension disable coming from the UI while a
        task thread created it): the runtime is stopped when possible, and its
        driver process is terminated when the owning thread is already gone.
        """
        with self._lock:
            for bid in list(self._browsers):
                try:
                    self._close_browser(bid)
                except Exception:
                    pass
            outcome = self._detach_runtime(reason="shutdown")
            return {
                "stopped": outcome["released"],
                "runtime_released": outcome["released"],
                "runtime_release_mode": outcome["mode"],
            }

    # Friendly aliases for cleanup.
    def close(self) -> Dict[str, Any]:
        return self.shutdown()

    def stop(self) -> Dict[str, Any]:
        return self.shutdown()

    def status(self) -> Dict[str, Any]:
        """Return a snapshot summary of the service state."""
        with self._lock:
            return {
                "runtime_started": self._runtime is not None,
                "runtime_generation": self._runtime_generation,
                "runtime_thread": self._runtime_thread,
                "runtime_owner": (
                    self._runtime_owner.name if self._runtime_owner is not None else None
                ),
                "runtime_releases": [dict(item) for item in self._runtime_releases],
                "browser_count": len(self._browsers),
                "session_count": len(self._contexts),
                "page_count": len(self._pages),
                "browsers": list(self._browsers),
                "sessions": list(self._contexts),
                "pages": list(self._pages),
                "default_browser_id": self._default_browser_id,
                "default_session_id": self._default_session_id,
                "config": dict(self.config),
            }

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------
    def _resolve_browser_id(self, browser_id: Optional[str]) -> str:
        if browser_id is not None:
            if browser_id not in self._browsers:
                raise BrowserNotFoundError(f"Browser '{browser_id}' not found")
            return browser_id
        if self._default_browser_id in self._browsers:
            return self._default_browser_id  # type: ignore[return-value]
        if self._browsers:
            return next(iter(self._browsers))
        # No browser yet -> launch one on demand.
        return self.launch_browser()

    def _resolve_session_id(self, session_id: Optional[str]) -> str:
        if session_id is not None:
            if session_id not in self._contexts:
                raise SessionNotFoundError(f"Session '{session_id}' not found")
            return session_id
        if self._default_session_id in self._contexts:
            return self._default_session_id  # type: ignore[return-value]
        if self._contexts:
            return next(iter(self._contexts))
        # No session yet -> create one on demand.
        return self.create_session()

    def _require_page(self, page_id: str) -> Any:
        self._ensure_runtime_thread("page_use")
        handle = self._pages.get(page_id)
        if handle is None:
            raise PageNotFoundError(f"Page '{page_id}' not found")
        return handle.page

    def _page_state(self, page_id: str, response: Optional[Any] = None) -> Dict[str, Any]:
        self._ensure_runtime_thread("page_state")
        handle = self._pages.get(page_id)
        if handle is None:
            raise PageNotFoundError(f"Page '{page_id}' not found")
        page = handle.page
        state: Dict[str, Any] = {
            "page_id": page_id,
            "session_id": handle.session_id,
            "url": None,
            "title": None,
            "closed": False,
        }
        try:
            state["closed"] = bool(page.is_closed())
        except Exception:
            state["closed"] = False
        if not state["closed"]:
            try:
                state["url"] = page.url
            except Exception:
                state["url"] = None
            try:
                state["title"] = page.title()
            except Exception:
                state["title"] = None
        if response is not None:
            try:
                state["status"] = response.status
            except Exception:
                pass
            try:
                state["ok"] = response.ok
            except Exception:
                pass
        return state

    def _sync_defaults(self) -> None:
        """Refresh the default browser/session pointer after removals."""
        if self._default_browser_id not in self._browsers:
            self._default_browser_id = next(iter(self._browsers), None)
        if self._default_session_id not in self._contexts:
            self._default_session_id = next(iter(self._contexts), None)
        if self._default_browser_id is not None:
            self._browser = self._browsers[self._default_browser_id].browser
        else:
            self._browser = None

    def _ms(self, timeout: Optional[float]) -> int:
        """Convert a timeout in seconds to Playwright's millisecond unit."""
        if timeout is None:
            timeout = self.config.get("timeout", 30)
        try:
            return int(float(timeout) * 1000)
        except (TypeError, ValueError):
            return 30000


# ===========================================================================
# Task 03 — Snapshot, element references and interaction
# ===========================================================================
class _SnapshotInteractionMixin:
    """Snapshot / element-ref / interaction API for :class:`PlaywrightService`.

    Split out purely for readability; the mixin is mixed into the service so
    the LLM-facing contract ("all browser operations go through
    ``PlaywrightService``") stays intact. It relies on the attributes and
    helpers defined by the service (``_require_page``, ``_lock``, ``_ms``, ...).
    """

    # -----------------------------------------------------------------
    # Snapshot + element references
    # -----------------------------------------------------------------
    def snapshot(
        self,
        page_id: str,
        *,
        mode: str = "ai",
        selector: str = "body",
        max_elements: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Capture a compact, LLM-friendly snapshot of ``page_id``.

        The snapshot assigns temporary refs (``e1``, ``e2``, ...) to the
        interactive / semantic elements of the page and returns the ARIA
        snapshot produced by Playwright (``aria_snapshot`` / accessibility
        tree) when the installed version provides one.
        """
        with self._lock:
            page = self._require_page(page_id)
            state = self._page_state(page_id)
            limit = max_elements
            if limit is None:
                try:
                    limit = int(self.config.get("snapshot_max_elements", 200))
                except (TypeError, ValueError):
                    limit = 200
            nodes = self._scan_elements(page, selector=selector, max_elements=limit)

            refs: Dict[str, ElementRef] = {}
            lines: List[str] = []
            for index, node in enumerate(nodes, start=1):
                ref = str(node.get("ref") or f"e{index}")
                details = {
                    key: value
                    for key, value in node.items()
                    if key not in ("ref", "role", "name", "tag")
                }
                element = ElementRef(
                    ref=ref,
                    role=str(node.get("role") or "generic"),
                    name=str(node.get("name") or ""),
                    tag=str(node.get("tag") or ""),
                    position=index,
                    details=details,
                )
                refs[ref] = element
                lines.append(self._format_ref_line(element))

            self._ref_generation += 1
            self._refs[page_id] = RefRegistry(
                page_id=page_id,
                url=state.get("url"),
                generation=self._ref_generation,
                refs=refs,
            )
            aria = self._capture_aria_snapshot(page, selector, mode)
            return {
                "page_id": page_id,
                "url": state.get("url"),
                "title": state.get("title"),
                "mode": mode,
                "generation": self._ref_generation,
                "snapshot": "\n".join(lines),
                "aria_snapshot": aria,
                "refs": {ref: el.to_dict() for ref, el in refs.items()},
                "element_count": len(refs),
            }

    def snapshot_refs(self, page_id: str) -> Dict[str, Any]:
        """Return the refs of the most recent snapshot for ``page_id``."""
        with self._lock:
            registry = self._refs.get(page_id)
            if registry is None:
                raise RefNotFoundError(
                    f"No snapshot refs are available for page '{page_id}'. "
                    "Call aether.playwright.browser_snapshot first."
                )
            return registry.to_dict()

    def _scan_elements(
        self, page: Any, *, selector: str, max_elements: int
    ) -> List[Dict[str, Any]]:
        script = SNAPSHOT_SCAN_JS.replace("__MAX__", str(int(max_elements))).replace(
            "__SELECTOR__", json.dumps(selector)
        )
        try:
            data = page.evaluate(script)
        except Exception as exc:  # noqa: BLE001 - wrap into a service error
            raise SnapshotError(
                f"Failed to capture snapshot: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            return []
        nodes = data.get("nodes")
        if not isinstance(nodes, list):
            return []
        return [node for node in nodes if isinstance(node, dict)]

    def _capture_aria_snapshot(
        self, page: Any, selector: str, mode: str
    ) -> Optional[str]:
        """Best-effort Playwright ARIA/accessibility snapshot (AI-friendly).

        Tries, in order: ``Page.aria_snapshot()``, ``Locator.aria_snapshot()``
        and the accessibility tree (``Page.accessibility.snapshot()``).
        Returns ``None`` when the installed Playwright build exposes none of
        them — the annotated ``snapshot`` tree is always available regardless.
        """
        if mode == "off":
            return None
        page_fn = getattr(page, "aria_snapshot", None)
        if callable(page_fn):
            try:
                text = page_fn()
                if text is not None:
                    return str(text)
            except Exception:
                pass
        try:
            locator = page.locator(selector)
        except Exception:
            locator = None
        if locator is not None:
            locator_fn = getattr(locator, "aria_snapshot", None)
            if callable(locator_fn):
                try:
                    text = locator_fn()
                    if text is not None:
                        return str(text)
                except Exception:
                    pass
        accessibility = getattr(page, "accessibility", None)
        snapshot_fn = getattr(accessibility, "snapshot", None) if accessibility else None
        if callable(snapshot_fn):
            try:
                tree = snapshot_fn()
            except Exception:
                tree = None
            if tree is not None:
                try:
                    lines: List[str] = []
                    self._aria_tree_lines(tree, lines, 0)
                    if lines:
                        return "\n".join(lines)
                except Exception:
                    pass
        return None

    def _aria_tree_lines(self, node: Any, lines: List[str], depth: int) -> None:
        if not isinstance(node, dict):
            return
        role = node.get("role") or "generic"
        name = node.get("name") or ""
        parts = [f"{'  ' * depth}- {role}"]
        if name:
            parts.append(f'"{name}"')
        for extra in ("value", "checked", "disabled", "expanded", "pressed", "selected"):
            if node.get(extra) not in (None, "", False):
                parts.append(f"[{extra}={node[extra]}]")
        lines.append(" ".join(parts))
        for child in node.get("children") or []:
            self._aria_tree_lines(child, lines, depth + 1)

    @staticmethod
    def _format_ref_line(element: ElementRef) -> str:
        parts = [f'- {element.role}']
        if element.name:
            parts.append(f'"{element.name}"')
        parts.append(f"[ref={element.ref}]")
        details = element.details or {}
        if details.get("input_type") and details["input_type"] != "text":
            parts.append(f"type={details['input_type']}")
        if details.get("placeholder"):
            parts.append(f'placeholder="{details["placeholder"]}"')
        if details.get("value") not in (None, ""):
            parts.append(f'value="{details["value"]}"')
        if details.get("href"):
            parts.append(f'href="{details["href"]}"')
        if details.get("level"):
            parts.append(f"level={details['level']}")
        if details.get("checked"):
            parts.append("checked")
        if details.get("disabled"):
            parts.append("disabled")
        return " ".join(parts)

    def _invalidate_refs(self, page_id: str) -> None:
        """Drop refs for a page (called after navigation/reload)."""
        self._refs.pop(page_id, None)

    # -----------------------------------------------------------------
    # Locator resolution
    # -----------------------------------------------------------------
    def _locator_for_ref(self, page_id: str, page: Any, ref: Any) -> Any:
        registry = self._refs.get(page_id)
        if registry is None:
            raise RefNotFoundError(
                f"No snapshot refs are available for page '{page_id}'. "
                "Call aether.playwright.browser_snapshot first."
            )
        ref = str(ref)
        element = registry.refs.get(ref)
        if element is None:
            raise RefNotFoundError(
                f"Unknown ref '{ref}' for page '{page_id}' "
                f"(snapshot generation {registry.generation}). "
                "Take a new snapshot and use one of its refs."
            )
        return page.locator(f'[data-aether-ref="{ref}"]'), element

    def _build_locator(self, page: Any, spec: Any) -> Any:
        if spec is None:
            raise InvalidLocatorError("A locator specification is required")
        if isinstance(spec, str):
            return page.locator(spec)
        if not isinstance(spec, dict):
            raise InvalidLocatorError("'locator' must be an object or a CSS string")
        spec = {key: value for key, value in spec.items() if value is not None}
        exact = bool(spec.get("exact", False))
        if spec.get("role"):
            kwargs: Dict[str, Any] = {"exact": exact}
            if spec.get("name") is not None:
                kwargs["name"] = spec["name"]
            for extra in (
                "checked",
                "disabled",
                "expanded",
                "pressed",
                "selected",
                "level",
                "include_hidden",
            ):
                if extra in spec:
                    kwargs[extra] = spec[extra]
            locator = page.get_by_role(str(spec["role"]), **kwargs)
        elif "label" in spec:
            locator = page.get_by_label(spec["label"], exact=exact)
        elif "placeholder" in spec:
            locator = page.get_by_placeholder(spec["placeholder"], exact=exact)
        elif "alt" in spec:
            locator = page.get_by_alt_text(spec["alt"], exact=exact)
        elif "title" in spec:
            locator = page.get_by_title(spec["title"], exact=exact)
        elif "test_id" in spec:
            locator = page.get_by_test_id(spec["test_id"])
        elif "text" in spec:
            locator = page.get_by_text(spec["text"], exact=exact)
        elif "css" in spec:
            locator = page.locator(spec["css"])
        elif "xpath" in spec:
            locator = page.locator("xpath=" + str(spec["xpath"]))
        else:
            raise InvalidLocatorError(
                "Unsupported locator. Provide one strategy among "
                "role/label/text/placeholder/alt/title/test_id/css/xpath."
            )
        if spec.get("has_text") is not None:
            locator = locator.filter(has_text=spec["has_text"])
        return locator

    def _select_single(
        self, locator: Any, spec: Any, *, description: str, count: Optional[int] = None
    ) -> Any:
        if count is None:
            count = locator.count()
        spec = spec if isinstance(spec, dict) else {}
        if count == 0:
            raise LocatorNotFoundError(
                f"{description} matched no element. Check the locator or take a fresh snapshot."
            )
        if spec.get("nth") is not None:
            index = int(spec["nth"])
            if index < 0 or index >= count:
                raise LocatorNotFoundError(
                    f"{description} matched {count} element(s); nth={index} is out of range."
                )
            return locator.nth(index)
        if spec.get("first"):
            return locator.first
        if spec.get("last"):
            return locator.last
        if count > 1:
            raise AmbiguousLocatorError(
                f"{description} matched {count} elements. Use a more specific locator "
                "or set 'nth'/'first'/'last' to disambiguate."
            )
        return locator.first

    def _resolve_target(
        self,
        page_id: str,
        ref: Any = None,
        locator: Any = None,
        *,
        require_present: bool = True,
    ) -> Any:
        """Resolve a ref/locator into a single Playwright Locator.

        Raises:
            InvalidLocatorError: when neither/both of ``ref``/``locator`` given.
            RefNotFoundError / StaleRefError: for invalid/stale refs.
            LocatorNotFoundError / AmbiguousLocatorError: for bad locators.
        """
        page = self._require_page(page_id)
        if ref and locator is not None:
            raise InvalidLocatorError("Provide either 'ref' or 'locator', not both")
        if not ref and locator is None:
            raise InvalidLocatorError(
                "Provide a 'ref' from browser_snapshot or an explicit 'locator'"
            )
        if ref:
            raw, element = self._locator_for_ref(page_id, page, ref)
            count = raw.count()
            if count == 0:
                raise StaleRefError(
                    f"Ref '{element.ref}' ({element.role} \"{element.name}\") is stale: "
                    "the element is no longer in the DOM (page navigated or DOM changed). "
                    "Call aether.playwright.browser_snapshot again and use a fresh ref."
                )
            return raw.first
        raw = self._build_locator(page, locator)
        if not require_present:
            return raw.first
        spec = locator if isinstance(locator, dict) else {}
        return self._select_single(raw, spec, description="locator")

    # -----------------------------------------------------------------
    # Interactions
    # -----------------------------------------------------------------
    def click(
        self,
        page_id: str,
        *,
        ref: Any = None,
        locator: Any = None,
        button: str = "left",
        click_count: int = 1,
        force: bool = False,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Click an element identified by a ref or an explicit locator."""
        ms = self._ms(timeout)
        with self._lock:
            page = self._require_page(page_id)
            url_before = self._safe_url(page)
            target = self._resolve_target(page_id, ref, locator)
            try:
                target.click(
                    button=button or "left",
                    click_count=int(click_count or 1),
                    force=bool(force),
                    timeout=ms,
                )
            except PlaywrightServiceError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise InteractionError(
                    f"click failed: {type(exc).__name__}: {exc}"
                ) from exc
            url_after = self._safe_url(page)
            return {
                "page_id": page_id,
                "action": "click",
                "button": button or "left",
                "navigated": bool(url_after is not None and url_after != url_before),
                "url": url_after,
            }

    def fill(
        self,
        page_id: str,
        value: Any,
        *,
        ref: Any = None,
        locator: Any = None,
        clear: bool = True,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Fill an input/textarea. ``clear=False`` appends to the current value."""
        ms = self._ms(timeout)
        with self._lock:
            target = self._resolve_target(page_id, ref, locator)
            try:
                if clear is False:
                    try:
                        current = target.input_value(timeout=ms)
                    except Exception:
                        current = ""
                    target.fill(f"{current}{value}", timeout=ms)
                else:
                    target.fill(value, timeout=ms)
            except PlaywrightServiceError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise InteractionError(
                    f"fill failed: {type(exc).__name__}: {exc}"
                ) from exc
            return {"page_id": page_id, "action": "fill", "value": value}

    def select_option(
        self,
        page_id: str,
        *,
        ref: Any = None,
        locator: Any = None,
        value: Any = None,
        values: Optional[List[Any]] = None,
        label: Any = None,
        index: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Select option(s) in a ``<select>`` (by value / label / index)."""
        ms = self._ms(timeout)
        kwargs: Dict[str, Any] = {"timeout": ms}
        if values is not None:
            kwargs["value"] = list(values)
        elif value is not None:
            kwargs["value"] = value
        elif label is not None:
            kwargs["label"] = label
        elif index is not None:
            kwargs["index"] = int(index)
        else:
            raise InvalidLocatorError(
                "select requires one of 'value', 'values', 'label' or 'index'"
            )
        with self._lock:
            target = self._resolve_target(page_id, ref, locator)
            try:
                selected = target.select_option(**kwargs)
            except PlaywrightServiceError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise InteractionError(
                    f"select failed: {type(exc).__name__}: {exc}"
                ) from exc
        try:
            selected_list = list(selected)
        except TypeError:
            selected_list = [selected]
        return {"page_id": page_id, "action": "select", "selected": selected_list}

    def press(
        self,
        page_id: str,
        key: str,
        *,
        ref: Any = None,
        locator: Any = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Press a keyboard key, either on an element or on the page."""
        ms = self._ms(timeout)
        with self._lock:
            if ref or locator is not None:
                target = self._resolve_target(page_id, ref, locator)
                try:
                    target.press(key, timeout=ms)
                except PlaywrightServiceError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise InteractionError(
                        f"press failed: {type(exc).__name__}: {exc}"
                    ) from exc
                scope = "element"
            else:
                page = self._require_page(page_id)
                try:
                    page.keyboard.press(key)
                except Exception as exc:  # noqa: BLE001
                    raise InteractionError(
                        f"press failed: {type(exc).__name__}: {exc}"
                    ) from exc
                scope = "page"
            return {
                "page_id": page_id,
                "action": "press",
                "key": key,
                "scope": scope,
            }

    def scroll(
        self,
        page_id: str,
        *,
        ref: Any = None,
        locator: Any = None,
        delta_x: float = 0,
        delta_y: float = 0,
        to: Optional[str] = None,
        behavior: str = "auto",
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Scroll an element into view, the window by delta, or to top/bottom."""
        ms = self._ms(timeout)
        with self._lock:
            page = self._require_page(page_id)
            if ref or locator is not None:
                target = self._resolve_target(page_id, ref, locator)
                try:
                    target.scroll_into_view_if_needed(timeout=ms)
                except PlaywrightServiceError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise InteractionError(
                        f"scroll failed: {type(exc).__name__}: {exc}"
                    ) from exc
                return {
                    "page_id": page_id,
                    "action": "scroll",
                    "into_view": True,
                }
            if to in ("top", "bottom"):
                y = "0" if to == "top" else "document.body.scrollHeight"
                script = (
                    SCROLL_TO_JS.replace("__X__", "0")
                    .replace("__Y__", y)
                    .replace("__BEHAVIOR__", json.dumps(behavior or "auto"))
                )
                try:
                    page.evaluate(script)
                except Exception as exc:  # noqa: BLE001
                    raise InteractionError(
                        f"scroll failed: {type(exc).__name__}: {exc}"
                    ) from exc
                return {"page_id": page_id, "action": "scroll", "to": to}
            try:
                page.mouse.wheel(float(delta_x or 0), float(delta_y or 0))
            except Exception as exc:  # noqa: BLE001
                raise InteractionError(
                    f"scroll failed: {type(exc).__name__}: {exc}"
                ) from exc
            return {
                "page_id": page_id,
                "action": "scroll",
                "delta_x": float(delta_x or 0),
                "delta_y": float(delta_y or 0),
            }

    def extract(
        self,
        page_id: str,
        *,
        what: Any = "text",
        ref: Any = None,
        locator: Any = None,
        attribute: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Extract structured data from an element.

        ``what`` is one of ``text``, ``value``, ``attribute``, ``count``,
        ``visible``, ``enabled``, ``selected``, ``html`` or ``all`` (or a list
        of those). ``count`` never needs a unique match.
        """
        ms = self._ms(timeout)
        requested = what
        if isinstance(requested, str) and requested.strip().lower() == "all":
            # "all" is sugar for the common scalar fields (attribute needs a name).
            requested = ["text", "value", "count", "visible", "enabled", "selected", "html"]
            wanted = list(requested)
        elif isinstance(requested, str):
            wanted = [requested]
        elif isinstance(requested, (list, tuple)):
            wanted = [str(item) for item in requested]
        else:
            raise InvalidLocatorError("'what' must be a string or a list of strings")

        with self._lock:
            page = self._require_page(page_id)
            if ref and locator is not None:
                raise InvalidLocatorError("Provide either 'ref' or 'locator', not both")
            if not ref and locator is None:
                raise InvalidLocatorError(
                    "Provide a 'ref' from browser_snapshot or an explicit 'locator'"
                )
            if ref:
                raw, element = self._locator_for_ref(page_id, page, ref)
                matches = raw.count()
                if matches == 0:
                    raise StaleRefError(
                        f"Ref '{element.ref}' is stale: the element is no longer in "
                        "the DOM. Take a new snapshot."
                    )
                target = raw.first
                spec = {}
            else:
                raw = self._build_locator(page, locator)
                matches = raw.count()
                spec = locator if isinstance(locator, dict) else {}
                target = None

            needs_target = [item for item in wanted if item.strip().lower() != "count"]
            if needs_target and target is None:
                target = self._select_single(
                    raw, spec, description="locator", count=matches
                )

            values: Dict[str, Any] = {}
            for item in wanted:
                key = item.strip().lower()
                if key == "count":
                    values["count"] = matches
                    continue
                if target is None:
                    raise InvalidLocatorError(
                        f"extract '{key}' needs a single element to read from"
                    )
                try:
                    values[key] = self._extract_one(
                        target, key, attribute=attribute, timeout=ms
                    )
                except PlaywrightServiceError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if requested == "all" or isinstance(requested, (list, tuple)):
                        values[key] = None
                        values.setdefault("errors", {})[key] = str(exc)
                    else:
                        raise InteractionError(
                            f"extract '{key}' failed: {type(exc).__name__}: {exc}"
                        ) from exc

            result: Dict[str, Any] = {
                "page_id": page_id,
                "action": "extract",
                "matches": matches,
            }
            if isinstance(requested, str) and len(wanted) == 1:
                result["what"] = wanted[0]
                result["value"] = values.get(wanted[0].strip().lower())
            else:
                result["values"] = values
            return result

    def _extract_one(
        self, target: Any, key: str, *, attribute: Optional[str], timeout: int
    ) -> Any:
        if key == "text":
            return target.inner_text(timeout=timeout)
        if key == "value":
            return target.input_value(timeout=timeout)
        if key == "attribute":
            if not attribute:
                raise InvalidLocatorError("extract 'attribute' requires an 'attribute' name")
            return target.get_attribute(attribute, timeout=timeout)
        if key == "visible":
            return bool(target.is_visible(timeout=timeout))
        if key == "enabled":
            return bool(target.is_enabled(timeout=timeout))
        if key == "selected" or key == "checked":
            return bool(target.is_checked(timeout=timeout))
        if key == "html":
            return target.inner_html(timeout=timeout)
        if key == "all":
            return None
        raise InvalidLocatorError(
            f"Unsupported extract target '{key}'. Use text/value/attribute/count/"
            "visible/enabled/selected/html/all."
        )

    def wait(
        self,
        page_id: str,
        *,
        condition: Optional[str] = None,
        state: Optional[str] = None,
        ref: Any = None,
        locator: Any = None,
        text: Optional[str] = None,
        url: Optional[str] = None,
        load_state: Optional[str] = None,
        exact: bool = False,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Condition-based wait built on Playwright waiting mechanisms."""
        ms = self._ms(timeout)
        cond = (condition or state or "").strip().lower()
        with self._lock:
            page = self._require_page(page_id)

            if cond in ("", "load", "load_state") or cond in LOAD_STATES:
                effective = load_state or (
                    cond if cond in LOAD_STATES else "load"
                )
                try:
                    page.wait_for_load_state(effective, timeout=ms)
                except Exception as exc:  # noqa: BLE001
                    raise WaitTimeoutError(
                        f"wait for load state '{effective}' timed out: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                return {
                    "page_id": page_id,
                    "action": "wait",
                    "condition": cond or "load",
                    "load_state": effective,
                }

            if cond in ("url", "url_matches", "url_contains"):
                if not url:
                    raise InvalidLocatorError("wait condition 'url' requires a 'url' value")
                target_url = url if cond != "url_contains" else f"**{url}**"
                try:
                    page.wait_for_url(target_url, timeout=ms)
                except Exception as exc:  # noqa: BLE001
                    raise WaitTimeoutError(
                        f"wait for url '{target_url}' timed out: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                return {
                    "page_id": page_id,
                    "action": "wait",
                    "condition": "url",
                    "url": url,
                }

            if cond in LOCATOR_WAIT_STATES:
                target = self._resolve_target(
                    page_id, ref, locator, require_present=False
                )
                try:
                    target.wait_for(state=cond, timeout=ms)
                except PlaywrightServiceError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise WaitTimeoutError(
                        f"wait for state '{cond}' timed out: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                return {
                    "page_id": page_id,
                    "action": "wait",
                    "condition": cond,
                }

            if cond in (
                "enabled",
                "disabled",
                "text",
                "has_text",
                "contain_text",
                "value",
                "checked",
            ):
                target = self._resolve_target(page_id, ref, locator)
                expect = self._expect()
                try:
                    if cond == "enabled":
                        expect(target).to_be_enabled(timeout=ms)
                    elif cond == "disabled":
                        expect(target).to_be_disabled(timeout=ms)
                    elif cond == "value":
                        if text is None:
                            raise InvalidLocatorError(
                                "wait condition 'value' requires a 'text' value"
                            )
                        expect(target).to_have_value(text, timeout=ms)
                    elif cond == "checked":
                        expect(target).to_be_checked(timeout=ms)
                    else:
                        if text is None:
                            raise InvalidLocatorError(
                                "wait condition 'text' requires a 'text' value"
                            )
                        if exact:
                            expect(target).to_have_text(text, timeout=ms)
                        else:
                            expect(target).to_contain_text(text, timeout=ms)
                except PlaywrightServiceError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise WaitTimeoutError(
                        f"wait for '{cond}' timed out: {type(exc).__name__}: {exc}"
                    ) from exc
                return {
                    "page_id": page_id,
                    "action": "wait",
                    "condition": cond,
                    "text": text,
                }

        raise InvalidLocatorError(
            f"Unsupported wait condition '{condition or state}'. Use visible/hidden/"
            "attached/detached/enabled/disabled/text/value/checked/url/load."
        )

    def upload(
        self,
        page_id: str,
        files: Any,
        *,
        ref: Any = None,
        locator: Any = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Set files on a ``<input type=file>`` via ``set_input_files``."""
        if not files:
            raise InvalidLocatorError("'files' is required")
        if isinstance(files, (str, Path)):
            file_list = [str(files)]
        else:
            file_list = [str(item) for item in files]
        ms = self._ms(timeout)
        with self._lock:
            target = self._resolve_target(page_id, ref, locator)
            try:
                target.set_input_files(file_list, timeout=ms)
            except PlaywrightServiceError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise InteractionError(
                    f"upload failed: {type(exc).__name__}: {exc}"
                ) from exc
            return {
                "page_id": page_id,
                "action": "upload",
                "files": file_list,
                "count": len(file_list),
            }

    def download(
        self,
        page_id: str,
        *,
        ref: Any = None,
        locator: Any = None,
        save_dir: Optional[str] = None,
        filename: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Trigger (optional) and capture a download, saving it as an artifact."""
        ms = self._ms(timeout)
        with self._lock:
            page = self._require_page(page_id)
            try:
                context = page.expect_download(timeout=ms)
                with context as info:
                    if ref or locator is not None:
                        target = self._resolve_target(page_id, ref, locator)
                        target.click(timeout=ms)
                download = info.value
            except PlaywrightServiceError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise InteractionError(
                    f"download failed: {type(exc).__name__}: {exc}"
                ) from exc

            suggested: Optional[str] = None
            try:
                suggested = download.suggested_filename
            except Exception:
                suggested = None
            name = filename or suggested or "download"
            directory = Path(save_dir) if save_dir else self._artifact_dir()
            destination = Path(directory) / name
            try:
                download.save_as(str(destination))
            except Exception:
                source = None
                try:
                    source = download.path()
                except Exception:
                    source = None
                if not source:
                    raise InteractionError(
                        "download could not be saved: no path available"
                    )
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
                except Exception as exc:  # noqa: BLE001
                    raise InteractionError(
                        f"download could not be saved: {type(exc).__name__}: {exc}"
                    ) from exc
            result: Dict[str, Any] = {
                "page_id": page_id,
                "action": "download",
                "filename": destination.name,
                "path": str(destination),
                "suggested_filename": suggested,
            }
            try:
                result["url"] = download.url
            except Exception:
                pass
            return result

    def dialog(
        self,
        page_id: str,
        *,
        action: str = "accept",
        prompt_text: Optional[str] = None,
        wait: bool = False,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Configure how native dialogs are handled (and optionally wait)."""
        ms = self._ms(timeout)
        action = (action or "accept").strip().lower()
        if action not in ("accept", "dismiss"):
            raise InvalidLocatorError("dialog action must be 'accept' or 'dismiss'")
        with self._lock:
            page = self._require_page(page_id)
            self._dialog_policies[page_id] = {
                "action": action,
                "prompt_text": prompt_text,
            }
            if not wait:
                self._ensure_dialog_handler(page_id, page)
                return {
                    "page_id": page_id,
                    "action": "dialog",
                    "configured": True,
                    "dialog_action": action,
                    "handled": False,
                }
            # Waiting for the next dialog: detach any auto-handler so this call
            # performs the response itself instead of racing with it.
            existing = self._dialog_handlers.pop(page_id, None)
            if existing is not None:
                try:
                    page.remove_listener("dialog", existing)
                except Exception:
                    pass
            try:
                handle = page.wait_for_event("dialog", timeout=ms)
            except Exception as exc:  # noqa: BLE001
                raise InteractionError(
                    f"no dialog appeared: {type(exc).__name__}: {exc}"
                ) from exc
            info = self._handle_dialog(page_id, handle)
            result = {
                "page_id": page_id,
                "action": "dialog",
                "configured": True,
                "dialog_action": action,
            }
            result.update(info)
            return result

    def dialogs(self, page_id: str) -> List[Dict[str, Any]]:
        """Return the dialogs observed so far for ``page_id``."""
        with self._lock:
            return list(self._dialogs.get(page_id, []))

    def _ensure_dialog_handler(self, page_id: str, page: Any) -> None:
        if page_id in self._dialog_handlers:
            return

        def handler(dialog: Any) -> None:
            self._handle_dialog(page_id, dialog)

        try:
            page.on("dialog", handler)
        except Exception:
            return
        self._dialog_handlers[page_id] = handler

    def _handle_dialog(self, page_id: str, dialog: Any) -> Dict[str, Any]:
        info: Dict[str, Any] = {}
        for attr in ("type", "message", "default_value"):
            value: Any = None
            try:
                value = getattr(dialog, attr)
                if callable(value):
                    value = value()
            except Exception:
                value = None
            info[attr] = value
        policy = self._dialog_policies.get(page_id) or {"action": "accept"}
        try:
            if policy.get("action") == "dismiss":
                dialog.dismiss()
            elif policy.get("prompt_text") is not None:
                dialog.accept(policy["prompt_text"])
            else:
                dialog.accept()
            info["handled"] = True
        except Exception as exc:  # noqa: BLE001
            info["handled"] = False
            info["error"] = f"{type(exc).__name__}: {exc}"
        self._dialogs.setdefault(page_id, []).append(info)
        return info

    # -----------------------------------------------------------------
    # Small helpers
    # -----------------------------------------------------------------
    def _expect(self) -> Any:
        if self._expect_factory is not None:
            return self._expect_factory()
        from playwright.sync_api import expect

        return expect

    def _artifact_dir(self) -> Path:
        configured = self.config.get("artifact_dir")
        if configured:
            directory = Path(str(configured))
        else:
            import tempfile

            directory = Path(tempfile.gettempdir()) / "aether-playwright-artifacts"
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return directory

    @staticmethod
    def _safe_url(page: Any) -> Optional[str]:
        try:
            return page.url
        except Exception:
            return None


# ===========================================================================
# Task 04 — Debugging: console, network, DOM inspection, screenshot, trace
# ===========================================================================
class _DebugMixin:
    """Debug evidence API for :class:`PlaywrightService` (Task 04).

    Adds the ``reproduce → inspect`` surface:

    * **console** — structured console/page-error events (type, text, location).
    * **network** — request/response records (url, method, status, resource
      type, timing, failure).
    * **DOM inspection** — tag/text/attributes/visibility/box/HTML of one element.
    * **screenshot** — full page, viewport or element, saved as an artifact.
    * **trace** — Playwright tracing (screenshots/snapshots/sources).

    Evidence is captured automatically for every page created through
    :meth:`new_page` and is **scoped to one page** so page A never mixes with
    page B. Traces are scoped to a session (they live on the BrowserContext).
    Everything is dropped when the page/session/browser is closed.
    Reading evidence (:meth:`console` / :meth:`network`) first flushes the
    pending Playwright event queue, so a read right after an interaction still
    reflects the request/error that interaction produced.
    """

    # -----------------------------------------------------------------
    # Listeners / capture
    # -----------------------------------------------------------------
    def _ensure_debug_listeners(self, page_id: str, page: Any) -> None:
        """Attach console/network listeners to ``page`` exactly once."""
        if page_id in self._debug_listeners:
            return
        on = getattr(page, "on", None)
        if not callable(on):
            # Test double or engine without an event emitter: keep empty buffers.
            self._console.setdefault(page_id, [])
            self._network.setdefault(page_id, [])
            self._network_pending.setdefault(page_id, {})
            return

        handlers: Dict[str, Any] = {}

        def _console(msg: Any) -> None:
            self._record_console(page_id, msg)

        def _pageerror(err: Any) -> None:
            self._record_page_error(page_id, err)

        def _request(request: Any) -> None:
            self._record_request(page_id, request)

        def _response(response: Any) -> None:
            self._record_response(page_id, response)

        def _request_failed(request: Any) -> None:
            self._record_request_failed(page_id, request)

        for event, handler in (
            ("console", _console),
            ("pageerror", _pageerror),
            ("request", _request),
            ("response", _response),
            ("requestfailed", _request_failed),
        ):
            try:
                on(event, handler)
                handlers[event] = handler
            except Exception:
                pass

        self._debug_listeners[page_id] = handlers
        self._console.setdefault(page_id, [])
        self._network.setdefault(page_id, [])
        self._network_pending.setdefault(page_id, {})

    @staticmethod
    def _flush_events(page: Any) -> None:
        """Let Playwright dispatch already-arrived page events.

        The synchronous Playwright API only runs its message loop while one of
        its own calls is executing, so reading evidence immediately after an
        interaction (``click`` -> ``console``/``network``) could otherwise miss
        console/network events that the browser already sent. A couple of no-op
        round-trips flush that queue without changing page state. Engines that
        do not expose ``wait_for_timeout`` (test doubles) are left untouched.
        """
        waiter = getattr(page, "wait_for_timeout", None)
        if not callable(waiter):
            return
        for _ in range(2):
            try:
                waiter(0)
            except Exception:
                return

    def _record_console(self, page_id: str, msg: Any) -> None:
        record: Dict[str, Any] = {
            "timestamp": time.time(),
            "type": self._normalize_console_type(self._attr(msg, "type")),
            "text": str(self._attr(msg, "text", "") or ""),
        }
        location = self._normalize_location(self._attr(msg, "location"))
        if location:
            record["location"] = location
        self._console.setdefault(page_id, []).append(record)

    def _record_page_error(self, page_id: str, error: Any) -> None:
        message = self._attr(error, "message") or self._attr(error, "name") or str(error)
        record: Dict[str, Any] = {
            "timestamp": time.time(),
            "type": "error",
            "text": str(message),
            "source": "pageerror",
        }
        name = self._attr(error, "name")
        if name:
            record["error_name"] = str(name)
        stack = self._attr(error, "stack")
        if stack:
            record["stack"] = str(stack)[:2000]
        self._console.setdefault(page_id, []).append(record)

    def _record_request(self, page_id: str, request: Any) -> None:
        method = self._attr(request, "method") or "GET"
        record: Dict[str, Any] = {
            "index": 0,
            "method": str(method).upper(),
            "url": self._attr(request, "url"),
            "resource_type": self._attr(request, "resource_type"),
            "status": None,
            "ok": None,
            "status_text": None,
            "failure": None,
            "started_at": time.time(),
            "finished_at": None,
            "duration_ms": None,
            "timing": self._normalize_timing(self._attr(request, "timing")),
        }
        entries = self._network.setdefault(page_id, [])
        record["index"] = len(entries) + 1
        entries.append(record)
        try:
            self._network_pending.setdefault(page_id, {})[id(request)] = record
        except Exception:
            pass

    def _find_pending(self, page_id: str, request: Any) -> Optional[Dict[str, Any]]:
        pending = self._network_pending.setdefault(page_id, {})
        record = pending.pop(id(request), None)
        if record is not None:
            return record
        # Fallback: pair by method+url when object identity is not preserved.
        url = self._attr(request, "url")
        method = str(self._attr(request, "method") or "").upper()
        for candidate in self._network.get(page_id, []):
            if (
                candidate.get("status") is None
                and candidate.get("url") == url
                and candidate.get("method") == method
            ):
                return candidate
        return None

    def _record_response(self, page_id: str, response: Any) -> None:
        request = self._attr(response, "request")
        record = self._find_pending(page_id, request) if request is not None else None
        if record is None:
            # A response without a recorded request: synthesise a minimal record.
            record = {
                "index": 0,
                "method": str(self._attr(request, "method") or "GET").upper()
                if request is not None
                else "GET",
                "url": self._attr(response, "url")
                or (self._attr(request, "url") if request is not None else None),
                "resource_type": self._attr(request, "resource_type")
                if request is not None
                else None,
                "status": None,
                "ok": None,
                "status_text": None,
                "failure": None,
                "started_at": None,
                "finished_at": None,
                "duration_ms": None,
                "timing": None,
            }
            entries = self._network.setdefault(page_id, [])
            record["index"] = len(entries) + 1
            entries.append(record)
        record["status"] = self._attr(response, "status")
        record["ok"] = self._attr(response, "ok")
        record["status_text"] = self._attr(response, "status_text")
        if record.get("url") is None:
            record["url"] = self._attr(response, "url")
        record["finished_at"] = time.time()
        if record.get("started_at"):
            record["duration_ms"] = round(
                (record["finished_at"] - record["started_at"]) * 1000, 3
            )

    def _record_request_failed(self, page_id: str, request: Any) -> None:
        record = self._find_pending(page_id, request)
        if record is None:
            record = {
                "index": 0,
                "method": str(self._attr(request, "method") or "GET").upper(),
                "url": self._attr(request, "url"),
                "resource_type": self._attr(request, "resource_type"),
                "status": None,
                "ok": None,
                "status_text": None,
                "failure": None,
                "started_at": None,
                "finished_at": None,
                "duration_ms": None,
                "timing": None,
            }
            entries = self._network.setdefault(page_id, [])
            record["index"] = len(entries) + 1
            entries.append(record)
        failure = self._attr(request, "failure")
        record["failure"] = str(failure) if failure else "request failed"
        record["finished_at"] = time.time()
        if record.get("started_at"):
            record["duration_ms"] = round(
                (record["finished_at"] - record["started_at"]) * 1000, 3
            )

    # -----------------------------------------------------------------
    # Console tool
    # -----------------------------------------------------------------
    def console(
        self,
        page_id: str,
        *,
        type: Any = None,
        types: Any = None,
        level: Any = None,
        search: Any = None,
        limit: Any = None,
        clear: bool = False,
    ) -> Dict[str, Any]:
        """Return captured console/page-error events for ``page_id``.

        ``type``/``types``/``level`` filter by event type
        (``log``/``debug``/``info``/``warning``/``error``); ``search`` filters by
        a case-insensitive substring of the message text.
        """
        with self._lock:
            page = self._require_page(page_id)
            session_id = self._pages[page_id].session_id
            self._ensure_debug_listeners(page_id, page)
            # Deliver events the browser has already sent before reading them.
            self._flush_events(page)
            events = list(self._console.get(page_id, []))
            total = len(events)
            wanted = self._normalize_types(type, types, level)
            filtered = events
            if wanted:
                filtered = [event for event in filtered if event.get("type") in wanted]
            if search:
                needle = str(search).lower()
                filtered = [
                    event
                    for event in filtered
                    if needle in str(event.get("text", "")).lower()
                ]
            if limit:
                try:
                    count = int(limit)
                except (TypeError, ValueError):
                    count = 0
                if count > 0:
                    filtered = filtered[-count:]
            result: Dict[str, Any] = {
                "page_id": page_id,
                "session_id": session_id,
                "count": len(filtered),
                "total": total,
                "types": sorted({event.get("type") for event in events if event.get("type")}),
                "messages": [dict(event) for event in filtered],
                "filters": {
                    "types": sorted(wanted) if wanted else None,
                    "search": search,
                    "limit": limit,
                },
            }
            if clear:
                removed = len(self._console.get(page_id, []))
                self._console[page_id] = []
                result["cleared"] = removed
            return result

    # -----------------------------------------------------------------
    # Network tool
    # -----------------------------------------------------------------
    def network(
        self,
        page_id: str,
        *,
        url: Any = None,
        method: Any = None,
        status: Any = None,
        resource_type: Any = None,
        failed: Any = None,
        limit: Any = None,
        clear: bool = False,
    ) -> Dict[str, Any]:
        """Return captured request/response records for ``page_id``."""
        with self._lock:
            page = self._require_page(page_id)
            session_id = self._pages[page_id].session_id
            self._ensure_debug_listeners(page_id, page)
            # Deliver events the browser has already sent before reading them.
            self._flush_events(page)
            records = list(self._network.get(page_id, []))
            total = len(records)
            filtered = list(records)
            if url:
                filtered = [r for r in filtered if self._match_url(r.get("url"), url)]
            if method:
                methods = self._normalize_set(method, upper=True)
                filtered = [
                    r for r in filtered if str(r.get("method") or "").upper() in methods
                ]
            if resource_type:
                kinds = self._normalize_set(resource_type, lower=True)
                filtered = [
                    r
                    for r in filtered
                    if str(r.get("resource_type") or "").lower() in kinds
                ]
            if status is not None:
                filtered = [
                    r for r in filtered if self._match_status(r.get("status"), status)
                ]
            if failed:
                filtered = [r for r in filtered if self._is_failed(r)]
            if limit:
                try:
                    count = int(limit)
                except (TypeError, ValueError):
                    count = 0
                if count > 0:
                    filtered = filtered[-count:]
            result: Dict[str, Any] = {
                "page_id": page_id,
                "session_id": session_id,
                "count": len(filtered),
                "total": total,
                "requests": [dict(record) for record in filtered],
                "filters": {
                    "url": url,
                    "method": method,
                    "status": status,
                    "resource_type": resource_type,
                    "failed": bool(failed) if failed is not None else None,
                    "limit": limit,
                },
            }
            if clear:
                removed = len(self._network.get(page_id, []))
                self._network[page_id] = []
                self._network_pending[page_id] = {}
                result["cleared"] = removed
            return result

    # -----------------------------------------------------------------
    # DOM inspection tool
    # -----------------------------------------------------------------
    def dom_inspect(
        self,
        page_id: str,
        *,
        ref: Any = None,
        locator: Any = None,
        html: bool = False,
        html_limit: int = 2000,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Inspect a single element (by ``ref`` or ``locator``)."""
        ms = self._ms(timeout)
        with self._lock:
            page = self._require_page(page_id)
            try:
                target = self._resolve_target(page_id, ref, locator)
            except LocatorNotFoundError as exc:
                raise DomElementNotFoundError(str(exc)) from exc

            try:
                info = target.evaluate(DOM_INSPECT_JS)
            except PlaywrightServiceError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise DomInspectionError(
                    f"DOM inspection failed: {type(exc).__name__}: {exc}"
                ) from exc
            if not isinstance(info, dict):
                info = {}

            result: Dict[str, Any] = {
                "page_id": page_id,
                "session_id": self._pages[page_id].session_id,
                "ref": ref,
                "tag": info.get("tag"),
                "text": info.get("text"),
                "attributes": info.get("attributes") or {},
                "bounding_box": info.get("bounding_box"),
            }
            for key in ("value", "checked", "disabled", "selected"):
                if key in info:
                    result[key] = info[key]
            if ref:
                registry = self._refs.get(page_id)
                element = registry.refs.get(str(ref)) if registry else None
                if element is not None:
                    result["role"] = element.role
                    result["name"] = element.name
            try:
                result["visible"] = bool(target.is_visible(timeout=ms))
            except Exception:
                result["visible"] = None
            try:
                result["enabled"] = bool(target.is_enabled(timeout=ms))
            except Exception:
                result["enabled"] = None
            if html:
                markup = info.get("html")
                markup = "" if markup is None else str(markup)
                limit = int(html_limit) if html_limit else 0
                if limit > 0:
                    result["html"] = markup[:limit]
                else:
                    result["html"] = markup
                result["html_truncated"] = bool(limit > 0 and len(markup) > limit)
            return result

    # -----------------------------------------------------------------
    # Screenshot tool
    # -----------------------------------------------------------------
    def screenshot(
        self,
        page_id: str,
        *,
        full_page: bool = False,
        ref: Any = None,
        locator: Any = None,
        type: str = "png",
        quality: Any = None,
        filename: Optional[str] = None,
        save_dir: Optional[str] = None,
        clip: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Capture a full-page, viewport or element screenshot as an artifact."""
        ms = self._ms(timeout)
        image_type = (type or "png").strip().lower()
        if image_type == "jpg":
            image_type = "jpeg"
        if image_type not in ("png", "jpeg"):
            raise ScreenshotError("type must be 'png' or 'jpeg'")
        with self._lock:
            page = self._require_page(page_id)
            scope = "viewport"
            target = None
            if ref or locator is not None:
                try:
                    target = self._resolve_target(page_id, ref, locator)
                except LocatorNotFoundError as exc:
                    raise DomElementNotFoundError(str(exc)) from exc
                scope = "element"
            elif full_page:
                scope = "full_page"

            options: Dict[str, Any] = {"type": image_type}
            if quality is not None:
                options["quality"] = int(quality)
            if clip is not None and target is None:
                options["clip"] = clip
            try:
                if target is not None:
                    data = target.screenshot(**options)
                else:
                    data = page.screenshot(full_page=bool(full_page), **options)
            except PlaywrightServiceError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise ScreenshotError(
                    f"screenshot failed: {type(exc).__name__}: {exc}"
                ) from exc
            data = bytes(data)
            mime = "image/jpeg" if image_type == "jpeg" else "image/png"
            suffix = "jpg" if image_type == "jpeg" else "png"
            name = filename or f"screenshot-{scope}-{int(time.time() * 1000)}.{suffix}"
            directory = Path(save_dir) if save_dir else self._artifact_dir()
            destination = Path(directory) / name
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            except Exception as exc:  # noqa: BLE001
                raise ScreenshotError(
                    f"screenshot could not be saved: {type(exc).__name__}: {exc}"
                ) from exc
            width, height = self._image_dimensions(data, mime)
            return {
                "page_id": page_id,
                "session_id": self._pages[page_id].session_id,
                "scope": scope,
                "path": str(destination),
                "filename": destination.name,
                "mime_type": mime,
                "width": width,
                "height": height,
                "bytes": len(data),
                "full_page": bool(full_page),
            }

    # -----------------------------------------------------------------
    # Trace tools
    # -----------------------------------------------------------------
    def trace_start(
        self,
        session_id: Optional[str] = None,
        *,
        screenshots: bool = True,
        snapshots: bool = True,
        sources: bool = True,
        name: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start Playwright tracing on a session (BrowserContext)."""
        with self._lock:
            self._ensure_runtime_thread("trace_start")
            sid = self._resolve_session_id(session_id)
            if self._traces.get(sid, {}).get("active"):
                raise TraceAlreadyActiveError(
                    f"Trace is already active for session '{sid}'. "
                    "Call aether.playwright.browser_trace_stop first."
                )
            context = self._contexts[sid].context
            tracing = getattr(context, "tracing", None)
            if tracing is None or not hasattr(tracing, "start"):
                raise TraceError(
                    "This browser context does not expose Playwright tracing"
                )
            options: Dict[str, Any] = {
                "screenshots": bool(screenshots),
                "snapshots": bool(snapshots),
                "sources": bool(sources),
            }
            if name:
                options["name"] = name
            if title:
                options["title"] = title
            try:
                tracing.start(**options)
            except Exception as exc:  # noqa: BLE001
                raise TraceError(
                    f"Failed to start tracing: {type(exc).__name__}: {exc}"
                ) from exc
            state = {
                "active": True,
                "started_at": time.time(),
                "screenshots": bool(screenshots),
                "snapshots": bool(snapshots),
                "sources": bool(sources),
                "name": name,
                "title": title,
            }
            self._traces[sid] = state
            return {
                "session_id": sid,
                "active": True,
                "started_at": state["started_at"],
                "screenshots": state["screenshots"],
                "snapshots": state["snapshots"],
                "sources": state["sources"],
                "name": name,
                "title": title,
            }

    def trace_stop(
        self,
        session_id: Optional[str] = None,
        *,
        filename: Optional[str] = None,
        save_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Stop tracing and save the trace as a re-openable artifact (zip)."""
        with self._lock:
            self._ensure_runtime_thread("trace_stop")
            sid = self._resolve_session_id(session_id)
            state = self._traces.get(sid)
            if not state or not state.get("active"):
                raise TraceNotActiveError(
                    f"No active trace for session '{sid}'. "
                    "Call aether.playwright.browser_trace_start first."
                )
            context = self._contexts[sid].context
            tracing = getattr(context, "tracing", None)
            if tracing is None or not hasattr(tracing, "stop"):
                raise TraceError(
                    "This browser context does not expose Playwright tracing"
                )
            name = filename or f"trace-{sid}-{int(time.time() * 1000)}.zip"
            directory = Path(save_dir) if save_dir else self._artifact_dir()
            destination = Path(directory) / name
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                tracing.stop(path=str(destination))
            except Exception as exc:  # noqa: BLE001
                raise TraceError(
                    f"Failed to stop tracing: {type(exc).__name__}: {exc}"
                ) from exc
            state["active"] = False
            state["stopped_at"] = time.time()
            state["path"] = str(destination)
            size: Optional[int] = None
            try:
                size = destination.stat().st_size
            except Exception:
                size = None
            state["size_bytes"] = size
            return {
                "session_id": sid,
                "active": False,
                "path": str(destination),
                "filename": destination.name,
                "mime_type": "application/zip",
                "size_bytes": size,
                "started_at": state.get("started_at"),
                "stopped_at": state.get("stopped_at"),
                "screenshots": state.get("screenshots"),
                "snapshots": state.get("snapshots"),
                "sources": state.get("sources"),
            }

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------
    @staticmethod
    def _attr(obj: Any, name: str, default: Any = None) -> Any:
        """Read ``obj.name`` (property or method) defensively."""
        if obj is None:
            return default
        try:
            value = getattr(obj, name)
        except Exception:
            return default
        if callable(value):
            try:
                value = value()
            except Exception:
                return default
        return value

    @staticmethod
    def _normalize_console_type(value: Any) -> str:
        kind = str(value or "log").strip().lower()
        if kind == "warn":
            return "warning"
        return kind or "log"

    @staticmethod
    def _normalize_location(location: Any) -> Optional[Dict[str, Any]]:
        if not location:
            return None
        if isinstance(location, dict):
            url = location.get("url")
            line = location.get("lineNumber", location.get("line"))
            column = location.get("columnNumber", location.get("column"))
        else:
            url = getattr(location, "url", None)
            line = getattr(location, "line_number", None) or getattr(location, "line", None)
            column = getattr(location, "column_number", None) or getattr(
                location, "column", None
            )
        out = {"url": url, "line": line, "column": column}
        return {key: val for key, val in out.items() if val is not None}

    @staticmethod
    def _normalize_timing(timing: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(timing, dict):
            return None
        return {
            key: (float(value) if isinstance(value, (int, float)) else value)
            for key, value in timing.items()
        }

    @staticmethod
    def _normalize_set(values: Any, *, upper: bool = False, lower: bool = False):
        out = set()
        items = values if isinstance(values, (list, tuple, set)) else [values]
        for item in items:
            if item is None:
                continue
            text = str(item).strip()
            if not text:
                continue
            if upper:
                text = text.upper()
            elif lower:
                text = text.lower()
            out.add(text)
        return out

    def _normalize_types(self, *sources: Any):
        out = set()
        for source in sources:
            for item in self._normalize_set(source):
                out.add(self._normalize_console_type(item))
        return out

    @staticmethod
    def _match_url(value: Any, pattern: Any) -> bool:
        url = str(value or "")
        patterns = pattern if isinstance(pattern, (list, tuple, set)) else [pattern]
        for raw in patterns:
            if raw is None:
                continue
            text = str(raw)
            if not text:
                continue
            if "*" in text or "?" in text:
                if fnmatch.fnmatch(url, text):
                    return True
            elif text.lower() in url.lower():
                return True
        return False

    @staticmethod
    def _match_status(value: Any, spec: Any) -> bool:
        specs = spec if isinstance(spec, (list, tuple, set)) else [spec]
        for raw in specs:
            if raw is None:
                continue
            if isinstance(raw, bool):
                continue
            if isinstance(raw, int):
                if value == raw:
                    return True
                continue
            text = str(raw).strip().lower()
            if not text:
                continue
            if text.isdigit():
                if value == int(text):
                    return True
            elif len(text) == 3 and text.endswith("xx") and text[0].isdigit():
                low = int(text[0]) * 100
                if value is not None and low <= int(value) <= low + 99:
                    return True
            elif text.startswith(">=") and text[2:].strip().isdigit():
                if value is not None and int(value) >= int(text[2:].strip()):
                    return True
            elif text.startswith("<") and text[1:].strip().isdigit():
                if value is not None and int(value) < int(text[1:].strip()):
                    return True
        return False

    @staticmethod
    def _is_failed(record: Dict[str, Any]) -> bool:
        if record.get("failure"):
            return True
        status = record.get("status")
        if status is None:
            return False
        try:
            return int(status) == 0 or int(status) >= 400
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _image_dimensions(data: bytes, mime: str) -> tuple:
        """Best-effort width/height from PNG/JPEG bytes (no external deps)."""
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            width = int.from_bytes(data[16:20], "big")
            height = int.from_bytes(data[20:24], "big")
            return width, height
        if data[:2] == b"\xff\xd8":  # JPEG
            index = 2
            length = len(data)
            while index + 9 < length:
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                if marker in (
                    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
                ):
                    height = int.from_bytes(data[index + 5:index + 7], "big")
                    width = int.from_bytes(data[index + 7:index + 9], "big")
                    return width, height
                if index + 4 > length:
                    break
                segment = int.from_bytes(data[index + 2:index + 4], "big")
                index += 2 + segment
        return None, None

    def _drop_debug(self, *, page_ids=(), session_ids=()) -> None:
        """Forget captured debug evidence (called when pages/sessions close)."""
        for page_id in list(page_ids):
            self._console.pop(page_id, None)
            self._network.pop(page_id, None)
            self._network_pending.pop(page_id, None)
            self._debug_listeners.pop(page_id, None)
        for session_id in list(session_ids):
            self._traces.pop(session_id, None)


# ===========================================================================
# Task 05 — Persistence: browser storage state (cookies + localStorage)
# ===========================================================================
_STATE_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_STATE_KEY_PREFIX = "browser_state"


def _state_safe_name(name: Any) -> str:
    """Sanitise a state name into a filesystem/capability-safe token."""
    text = str(name if name is not None else "").strip().replace("\\", "/")
    text = text.split("/")[-1]
    text = _STATE_SAFE_RE.sub("_", text)
    return text.strip("._-") or "default"


def _atomic_json_write(path: Path, value: Any) -> None:
    """Write ``value`` as JSON atomically (temp file + os.replace)."""
    text = json.dumps(value, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile

    handle_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".aether_state_", suffix=".tmp"
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


class _FacadeStateNamespace:
    """Adapts an AETHER ``ExtensionStorage`` / ``ProjectScopedStorage`` facade.

    Both expose the same minimal surface (``set`` / ``get`` / ``delete`` /
    ``exists`` / ``list_keys``); this thin wrapper keeps the store independent
    from the exact facade type.
    """

    def __init__(self, facade: Any) -> None:
        self._facade = facade

    def set(self, key: str, value: Any) -> None:
        self._facade.set(key, value)

    def get(self, key: str, default: Any = None) -> Any:
        return self._facade.get(key, default)

    def delete(self, key: str) -> bool:
        return bool(self._facade.delete(key))

    def exists(self, key: str) -> bool:
        return bool(self._facade.exists(key))

    def keys(self) -> List[str]:
        return list(self._facade.list_keys())


class _FileStateNamespace:
    """A tiny atomic JSON store under ``<base>/state`` (standalone/test use)."""

    def __init__(self, base: Path) -> None:
        self._base = Path(base)

    def _path(self, key: str) -> Path:
        return self._base / "state" / f"{key}.json"

    def set(self, key: str, value: Any) -> None:
        _atomic_json_write(self._path(key), value)

    def get(self, key: str, default: Any = None) -> Any:
        path = self._path(key)
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def delete(self, key: str) -> bool:
        path = self._path(key)
        try:
            if path.exists():
                path.unlink()
                return True
        except OSError:
            pass
        return False

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def keys(self) -> List[str]:
        directory = self._base / "state"
        if not directory.exists():
            return []
        return sorted(path.stem for path in directory.glob("*.json"))


class BrowserStateStore:
    """Persist Playwright storage state, isolated by extension/session/project.

    Backed either by an AETHER Extension storage facade (``context.storage`` —
    the production path) or by a plain filesystem directory (``base_dir``) so
    the service stays usable and testable without a full ExtensionContext.

    Isolation:

    * **extension scope** (default) — one namespace per ``extension_id`` (the
      facade already isolates each extension);
    * **project scope** — ``<project>/.aether/extensions/<ext>/…`` through
      ``storage.project(path)`` (facade) or ``<base>/projects/<name>`` (file).

    Only the JSON storage state itself is persisted. Nothing is ever returned to
    the UI/log except key names and counts, because cookies may hold
    credentials.
    """

    KEY_PREFIX = _STATE_KEY_PREFIX

    def __init__(
        self,
        storage: Optional[Any] = None,
        base_dir: Optional[Any] = None,
        extension_id: str = "aether.playwright",
    ) -> None:
        self._storage = storage
        self._base_dir = Path(base_dir) if base_dir else None
        self.extension_id = extension_id

    # -- naming / scope -------------------------------------------------
    @staticmethod
    def safe_name(name: Any) -> str:
        return _state_safe_name(name)

    def _key(self, name: Any) -> str:
        return f"{self.KEY_PREFIX}.{self.safe_name(name)}"

    def _view(self, project: Optional[Any]):
        if self._storage is not None:
            if project:
                return _FacadeStateNamespace(self._storage.project(str(project)))
            return _FacadeStateNamespace(self._storage)
        if self._base_dir is None:
            raise StateStoreError(
                "No browser state storage is configured (attach context.storage "
                "or pass a state_dir)"
            )
        if project:
            return _FileStateNamespace(
                self._base_dir / "projects" / self.safe_name(project)
            )
        return _FileStateNamespace(self._base_dir)

    # -- operations -----------------------------------------------------
    def save(
        self, name: Any, state: Any, *, project: Optional[Any] = None
    ) -> Dict[str, Any]:
        if not isinstance(state, dict):
            raise StateStoreError("browser storage state must be a JSON object")
        key = self._key(name)
        view = self._view(project)
        try:
            view.set(key, state)
        except StateStoreError:
            raise
        except Exception as exc:
            raise StateStoreError(
                f"could not persist browser state: {type(exc).__name__}: {exc}"
            ) from exc
        cookies = state.get("cookies") or []
        origins = state.get("origins") or []
        return {
            "name": self.safe_name(name),
            "key": key,
            "scope": "project" if project else "extension",
            "project": str(project) if project else None,
            "cookie_count": len(cookies),
            "origin_count": len(origins),
            "saved": True,
        }

    def load(
        self, name: Any, *, project: Optional[Any] = None
    ) -> Optional[Dict[str, Any]]:
        view = self._view(project)
        try:
            state = view.get(self._key(name))
        except StateStoreError:
            raise
        except Exception as exc:
            raise StateStoreError(
                f"could not read browser state: {type(exc).__name__}: {exc}"
            ) from exc
        return state if isinstance(state, dict) else None

    def delete(self, name: Any, *, project: Optional[Any] = None) -> bool:
        try:
            return bool(self._view(project).delete(self._key(name)))
        except Exception:
            return False

    def exists(self, name: Any, *, project: Optional[Any] = None) -> bool:
        try:
            return bool(self._view(project).exists(self._key(name)))
        except Exception:
            return False

    def list(self, *, project: Optional[Any] = None) -> List[str]:
        try:
            keys = self._view(project).keys()
        except Exception:
            keys = []
        prefix = self.KEY_PREFIX + "."
        return sorted(key[len(prefix):] for key in keys if key.startswith(prefix))


def _new_state_store(storage=None, base_dir=None, extension_id="aether.playwright") -> BrowserStateStore:
    """Factory used by the service to build its :class:`BrowserStateStore`."""
    return BrowserStateStore(storage=storage, base_dir=base_dir, extension_id=extension_id)


# ===========================================================================
# Task 05 — Persistence mixin (save / restore / list browser state)
# ===========================================================================
class _PersistenceMixin:
    """Browser storage-state persistence for :class:`PlaywrightService`.

    Uses Playwright's official ``BrowserContext.storage_state()`` /
    ``new_context(storage_state=…)`` mechanisms and stores the resulting JSON
    through the AETHER Extension storage facade. It never returns cookie values
    to the caller — only names, scopes and counts.
    """

    def _require_state_store(self) -> "BrowserStateStore":
        store = getattr(self, "_browser_state_store", None)
        if store is None:
            raise StateStoreError(
                "Browser state persistence is not configured for this service"
            )
        return store

    @staticmethod
    def _read_storage_state(context: Any) -> Dict[str, Any]:
        getter = getattr(context, "storage_state", None)
        if not callable(getter):
            raise StateError("This browser context does not expose storage_state()")
        try:
            state = getter()
        except Exception as exc:
            raise StateError(
                f"could not read storage state: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(state, dict):
            raise StateError("storage_state() did not return a mapping")
        return state

    @staticmethod
    def _storage_state_init_script(origins: Any) -> str:
        """Return a JS init script that re-applies localStorage per origin."""
        payload = json.dumps(
            [
                {
                    "origin": origin.get("origin"),
                    "localStorage": origin.get("localStorage") or [],
                }
                for origin in (origins or [])
                if isinstance(origin, dict) and origin.get("origin")
            ]
        )
        return (
            "(() => { const data = " + payload + ";"
            " const entry = data.find(function (e) { return e.origin === window.location.origin; });"
            " if (!entry) return;"
            " for (const kv of entry.localStorage) {"
            " try { window.localStorage.setItem(kv.name, kv.value); } catch (e) {} }"
            " })();"
        )

    @classmethod
    def _apply_state_to_context(cls, context: Any, state: Dict[str, Any], *, apply_origins: bool = True) -> Dict[str, Any]:
        """Re-apply a storage state to an existing BrowserContext."""
        applied: Dict[str, Any] = {"cookies_applied": False, "origins_applied": False}
        cookies = list(state.get("cookies") or [])
        add_cookies = getattr(context, "add_cookies", None)
        if cookies and callable(add_cookies):
            try:
                add_cookies(cookies)
                applied["cookies_applied"] = True
            except Exception:
                pass
        origins = list(state.get("origins") or [])
        add_init_script = getattr(context, "add_init_script", None)
        if apply_origins and origins and callable(add_init_script):
            try:
                add_init_script(cls._storage_state_init_script(origins))
                applied["origins_applied"] = True
            except Exception:
                pass
        return applied

    # -- public API -----------------------------------------------------
    def save_state(self, session_id: Optional[str] = None, *, name: Optional[str] = None, project: Optional[str] = None) -> Dict[str, Any]:
        """Save a session's browser storage state (cookies + localStorage)."""
        with self._lock:
            self._ensure_runtime_thread("state_save")
            sid = self._resolve_session_id(session_id)
            context = self._contexts[sid].context
            state = self._read_storage_state(context)
            store = self._require_state_store()
            summary = store.save(name or sid, state, project=project)
            summary["session_id"] = sid
            return summary

    def load_state(self, name: str, *, project: Optional[str] = None) -> Dict[str, Any]:
        """Load a previously saved storage state (raises when it is missing)."""
        store = self._require_state_store()
        state = store.load(name, project=project)
        if state is None:
            raise StateNotFoundError(
                f"No saved browser state named '{store.safe_name(name)}'"
                + (f" for project '{project}'" if project else "")
            )
        return state

    def has_state(self, name: str, *, project: Optional[str] = None) -> bool:
        """Return whether a storage state with ``name`` exists."""
        return self._require_state_store().exists(name, project=project)

    def list_saved_states(self, *, project: Optional[str] = None) -> List[str]:
        """Return the names of every saved storage state (optionally per project)."""
        return self._require_state_store().list(project=project)

    def delete_saved_state(self, name: str, *, project: Optional[str] = None) -> bool:
        """Delete a saved storage state. Returns True when something was removed."""
        return self._require_state_store().delete(name, project=project)

    def restore_state(
        self,
        *,
        name: str,
        project: Optional[str] = None,
        session_id: Optional[str] = None,
        browser_id: Optional[str] = None,
        apply_origins: bool = True,
    ) -> Dict[str, Any]:
        """Restore a saved storage state.

        With ``session_id`` omitted a **new** session is created straight from
        the saved state (Playwright's official ``new_context(storage_state=…)``
        path) — this is what survives an AETHER restart. With an existing
        ``session_id`` the state is re-applied to that context: cookies via
        ``add_cookies`` and localStorage via an init script.
        """
        with self._lock:
            self._ensure_runtime_thread("state_restore")
            state = self.load_state(name, project=project)
            cookies = list(state.get("cookies") or [])
            origins = list(state.get("origins") or [])
            summary: Dict[str, Any] = {
                "name": self._require_state_store().safe_name(name),
                "scope": "project" if project else "extension",
                "project": str(project) if project else None,
                "cookie_count": len(cookies),
                "origin_count": len(origins),
                "restored": True,
            }
            if session_id is None:
                new_sid = self.create_session(browser_id, storage_state=state)
                summary.update(
                    {
                        "session_id": new_sid,
                        "method": "new_context",
                        "created_session": True,
                        "cookies_applied": bool(cookies),
                        "origins_applied": bool(origins),
                    }
                )
                return summary
            sid = self._resolve_session_id(session_id)
            applied = self._apply_state_to_context(
                self._contexts[sid].context, state, apply_origins=apply_origins
            )
            summary.update(
                {"session_id": sid, "method": "add_cookies", "created_session": False}
            )
            summary.update(applied)
            return summary


# ===========================================================================
# Task 05 — Debug UI view models (sessions/pages/console/network/DOM/…)
# ===========================================================================
def _open_command_hint(path: str) -> List[str]:
    """Return a platform-appropriate command that opens ``path`` (no side effect)."""
    if os.name == "nt":  # Windows
        return ["cmd", "/c", "start", "", path]
    if os.name == "posix":
        return ["open", path] if os.path.exists("/usr/bin/open") else ["xdg-open", path]
    return []


def _reveal_path(path: str) -> bool:
    """Best-effort reveal/open of ``path`` with the OS. Never raises."""
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            import subprocess

            command = _open_command_hint(path)
            if not command:
                return False
            subprocess.Popen(command)
        return True
    except Exception:
        return False


#: Actions the debug panel exposes — each references an existing capability.
DEBUG_PANEL_ACTIONS: List[Dict[str, Any]] = [
    {"id": "aether.playwright.browser_debug_panel", "title": "Refresh"},
    {"id": "aether.playwright.page_close", "title": "Close Page"},
    {"id": "aether.playwright.browser_console", "title": "Clear Console", "arguments": {"clear": True}},
    {"id": "aether.playwright.browser_network", "title": "Clear Network", "arguments": {"clear": True}},
    {"id": "aether.playwright.browser_screenshot", "title": "Screenshot", "arguments": {"full_page": True}},
    {"id": "aether.playwright.browser_trace_start", "title": "Start Trace"},
    {"id": "aether.playwright.browser_trace_stop", "title": "Stop Trace"},
    {"id": "aether.playwright.session_save_state", "title": "Save State"},
    {"id": "aether.playwright.session_restore_state", "title": "Restore State"},
]


class _DebugUIMixin:
    """Generic debug-panel view models (Task 05).

    Produces plain, JSON-serialisable view contracts (``renderer`` / ``type`` /
    ``data`` — the AETHER UI Result contract) so the generic Extension UI
    runtime can render them with no Playwright-specific frontend code.
    """

    #: Views the debug panel knows how to produce.
    DEBUG_VIEWS = ("sessions", "pages", "console", "network", "dom", "screenshot", "trace")

    @staticmethod
    def _ui_result(renderer: str, rtype: str, data: Any, *, artifact: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Build a UI Result, reusing the shared contract when available."""
        try:
            from agent_ai.extensions.ui import UIResult

            return UIResult(renderer=renderer, type=rtype, data=data, artifact=artifact).to_dict()
        except Exception:  # pragma: no cover - standalone fallback
            out: Dict[str, Any] = {"renderer": renderer, "type": rtype, "data": data}
            if artifact is not None:
                out["artifact"] = artifact
            return out

    def _ui_table(self, columns: List[Dict[str, Any]], rows: List[Dict[str, Any]], *, title: str = "", selected: Optional[str] = None) -> Dict[str, Any]:
        data: Dict[str, Any] = {"columns": list(columns), "rows": list(rows)}
        if title:
            data["title"] = title
        if selected is not None:
            data["selected"] = selected
        return self._ui_result("table", "table", data)

    def _ui_viewer(self, viewer_type: str, payload: Any, *, artifact: Optional[Dict[str, Any]] = None, title: str = "", **extra: Any) -> Dict[str, Any]:
        data: Dict[str, Any] = {"viewer_type": viewer_type, "payload": payload}
        if title:
            data["title"] = title
        data.update(extra)
        return self._ui_result("viewer", viewer_type, data, artifact=artifact)

    # -- individual views ----------------------------------------------
    def sessions_view(self, browser_id: Optional[str] = None) -> Dict[str, Any]:
        """Sessions view (table contract)."""
        rows = [
            {
                "session_id": session.get("session_id"),
                "browser_id": session.get("browser_id"),
                "page_count": session.get("page_count", 0),
            }
            for session in self.list_sessions(browser_id)
        ]
        return self._ui_table(
            [
                {"key": "session_id", "title": "Session"},
                {"key": "browser_id", "title": "Browser"},
                {"key": "page_count", "title": "Pages"},
            ],
            rows,
            title="Sessions",
        )

    def pages_view(self, session_id: Optional[str] = None, *, selected_page_id: Optional[str] = None) -> Dict[str, Any]:
        """Pages / tabs view (table contract) with the active page flag."""
        rows = []
        for page in self.list_pages(session_id):
            rows.append(
                {
                    "page_id": page.get("page_id"),
                    "session_id": page.get("session_id"),
                    "url": page.get("url"),
                    "title": page.get("title"),
                    "active": page.get("page_id") == selected_page_id,
                    "closed": bool(page.get("closed")),
                }
            )
        return self._ui_table(
            [
                {"key": "page_id", "title": "Page"},
                {"key": "title", "title": "Title"},
                {"key": "url", "title": "URL"},
                {"key": "active", "title": "Active"},
            ],
            rows,
            title="Pages / Tabs",
            selected=selected_page_id,
        )

    def console_view(self, page_id: str, **filters: Any) -> Dict[str, Any]:
        """Console viewer (viewer contract, type ``log``)."""
        result = self.console(page_id, **filters)
        payload: Dict[str, Any] = {
            "page_id": result["page_id"],
            "session_id": result["session_id"],
            "count": result["count"],
            "total": result["total"],
            "types": result["types"],
            "filters": result["filters"],
            "entries": list(result["messages"]),
        }
        if "cleared" in result:
            payload["cleared"] = result["cleared"]
        return self._ui_viewer("log", payload, title="Console")

    def network_view(self, page_id: str, **filters: Any) -> Dict[str, Any]:
        """Network viewer (table contract: method/url/status/type/failed)."""
        result = self.network(page_id, **filters)
        rows = []
        for record in result["requests"]:
            rows.append(
                {
                    "method": record.get("method"),
                    "url": record.get("url"),
                    "status": record.get("status"),
                    "resource_type": record.get("resource_type"),
                    "failed": self._is_failed(record),
                }
            )
        return self._ui_table(
            [
                {"key": "method", "title": "Method"},
                {"key": "url", "title": "URL"},
                {"key": "status", "title": "Status"},
                {"key": "resource_type", "title": "Type"},
                {"key": "failed", "title": "Failed"},
            ],
            rows,
            title="Network",
        )

    def dom_view(self, page_id: str, *, ref: Any = None, locator: Any = None, html: bool = True, html_limit: int = 2000, timeout: Optional[float] = None) -> Dict[str, Any]:
        """DOM inspector (viewer contract, type ``json``)."""
        result = self.dom_inspect(
            page_id, ref=ref, locator=locator, html=html, html_limit=html_limit, timeout=timeout
        )
        return self._ui_viewer("json", result, title="DOM Inspector")

    def screenshot_view(self, page_id: str, **options: Any) -> Dict[str, Any]:
        """Screenshot viewer (viewer contract, type ``image``) + artifact ref."""
        result = self.screenshot(page_id, **options)
        payload = {
            "path": result["path"],
            "filename": result["filename"],
            "width": result.get("width"),
            "height": result.get("height"),
            "scope": result.get("scope"),
            "mime_type": result.get("mime_type"),
            "page_id": result.get("page_id"),
            "session_id": result.get("session_id"),
        }
        artifact = {
            "artifact_id": result["filename"],
            "name": result["filename"],
            "mime_type": result.get("mime_type", ""),
            "size": result.get("bytes"),
            "metadata": {
                "path": result["path"],
                "width": result.get("width"),
                "height": result.get("height"),
                "scope": result.get("scope"),
            },
        }
        return self._ui_viewer("image", payload, artifact=artifact, title="Screenshot")

    def trace_status(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """Return the (non-secret) trace state of a session."""
        with self._lock:
            sid = self._resolve_session_id(session_id)
            state = dict(self._traces.get(sid) or {})
            return {
                "session_id": sid,
                "active": bool(state.get("active")),
                "path": state.get("path"),
                "size_bytes": state.get("size_bytes"),
                "started_at": state.get("started_at"),
                "stopped_at": state.get("stopped_at"),
            }

    def trace_open(self, *, path: Optional[str] = None, session_id: Optional[str] = None, reveal: bool = False) -> Dict[str, Any]:
        """Return an open descriptor for a trace ``.zip`` artifact.

        Resolves the artifact from an explicit ``path`` or from the last trace
        of a session and returns a generic action descriptor the host can use to
        open it (Playwright trace viewer / OS). By default nothing is launched;
        pass ``reveal=True`` to actually reveal the file with the OS opener.
        """
        with self._lock:
            target_path = path
            if target_path is None:
                sid = self._resolve_session_id(session_id)
                target_path = (self._traces.get(sid) or {}).get("path")
            if not target_path:
                raise TraceError(
                    "No trace artifact to open; call "
                    "aether.playwright.browser_trace_stop first"
                )
            target = Path(str(target_path))
            exists = target.exists()
            descriptor: Dict[str, Any] = {
                "path": str(target),
                "exists": exists,
                "mime_type": "application/zip",
                "viewer": "playwright-trace",
                "open_command": _open_command_hint(str(target)),
            }
            if reveal and exists:
                descriptor["revealed"] = _reveal_path(str(target))
            return descriptor

    def trace_view(self, session_id: Optional[str] = None, **options: Any) -> Dict[str, Any]:
        """Trace viewer (viewer contract, type ``file``) + artifact ref.

        Stops the active trace, saves the ``.zip`` artifact and returns a
        reference plus an ``open_action`` the host can dispatch to open it.
        """
        result = self.trace_stop(session_id, **options)
        payload = {
            "path": result["path"],
            "filename": result["filename"],
            "size_bytes": result.get("size_bytes"),
            "mime_type": result.get("mime_type"),
            "session_id": result.get("session_id"),
            "open_action": "aether.playwright.browser_trace_open",
        }
        artifact = {
            "artifact_id": result["filename"],
            "name": result["filename"],
            "mime_type": result.get("mime_type", "application/zip"),
            "size": result.get("size_bytes"),
            "metadata": {"path": result["path"], "session_id": result.get("session_id")},
        }
        return self._ui_viewer("file", payload, artifact=artifact, title="Trace")

    # -- combined panel -------------------------------------------------
    def debug_panel(
        self,
        *,
        session_id: Optional[str] = None,
        page_id: Optional[str] = None,
        console: Optional[Dict[str, Any]] = None,
        network: Optional[Dict[str, Any]] = None,
        dom: Optional[Dict[str, Any]] = None,
        screenshot: Optional[Dict[str, Any]] = None,
        trace: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Return the whole Playwright debug panel view model.

        Combines the sessions and pages tables with the console/network views of
        the selected page. ``dom`` / ``screenshot`` / ``trace`` are only added
        (and thus only executed) when explicitly requested, so a plain refresh
        has no side effects.
        """
        with self._lock:
            sid: Optional[str] = None
            if session_id is not None or self._contexts:
                sid = self._resolve_session_id(session_id)
            pid = page_id
            if pid is None and sid is not None:
                page_ids = [p for p, handle in self._pages.items() if handle.session_id == sid]
                pid = page_ids[0] if page_ids else None
            browser_id = self._contexts[sid].browser_id if sid in self._contexts else None
            panel: Dict[str, Any] = {
                "title": "Playwright Debug Panel",
                "views": list(self.DEBUG_VIEWS),
                "actions": [dict(action) for action in DEBUG_PANEL_ACTIONS],
                "session_id": sid,
                "page_id": pid,
                "browser_id": browser_id,
                "selected_page_id": pid,
                "sessions": self.sessions_view(browser_id),
                "pages": self.pages_view(sid, selected_page_id=pid),
            }
            if pid is not None:
                panel["console"] = self.console_view(pid, **(console or {}))
                panel["network"] = self.network_view(pid, **(network or {}))
                if dom is not None:
                    panel["dom"] = self.dom_view(pid, **(dom or {}))
            if pid is not None and screenshot is not None:
                panel["screenshot"] = self.screenshot_view(pid, **(screenshot or {}))
            if sid is not None:
                if trace is not None:
                    panel["trace"] = self.trace_view(sid, **(trace or {}))
                else:
                    panel["trace_state"] = self.trace_status(sid)
            return self._ui_result("panel", "panel", panel)


# Mix the Task 03 snapshot/interaction API, the Task 04 debugging API, the
# Task 05 debug-UI view models and the browser-state persistence API into the
# service so the whole browser surface stays on a single object.
class PlaywrightService(
    _DebugUIMixin,
    _PersistenceMixin,
    _DebugMixin,
    _SnapshotInteractionMixin,
    _PlaywrightServiceCore,
):
    """Public Playwright service.

    Combines the Task 02 lifecycle (browser / session / page / navigation) with
    the Task 03 surface (snapshot, element refs, interaction, structured
    extraction and condition-based waits), the Task 04 debugging surface
    (console, network, DOM inspection, screenshot, trace) and the Task 05
    surface (debug-panel view models + browser-state persistence) behind one
    object, so every browser operation the LLM performs goes through this single
    service.
    """


__all__ = [
    "PlaywrightService",
    "PlaywrightServiceError",
    "BrowserLaunchError",
    "BrowserNotFoundError",
    "SessionNotFoundError",
    "PageNotFoundError",
    "PageOperationError",
    "SnapshotError",
    "RefNotFoundError",
    "StaleRefError",
    "InvalidLocatorError",
    "LocatorNotFoundError",
    "AmbiguousLocatorError",
    "InteractionError",
    "WaitTimeoutError",
    "DebugError",
    "ConsoleCaptureError",
    "NetworkCaptureError",
    "DomInspectionError",
    "DomElementNotFoundError",
    "ScreenshotError",
    "TraceError",
    "TraceNotActiveError",
    "TraceAlreadyActiveError",
    "StateError",
    "StateStoreError",
    "StateNotFoundError",
    "BrowserStateStore",
    "DEBUG_PANEL_ACTIONS",
    "ElementRef",
    "RefRegistry",
    "BrowserHandle",
    "SessionHandle",
    "PageHandle",
    "DOM_INSPECT_JS",
]
