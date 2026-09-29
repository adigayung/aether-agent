"""Playwright tool layer — the thin bridge between the LLM and
:class:`PlaywrightService`.

Every tool:

* has a **namespaced** name (``aether.playwright.<action>``) so it is a valid
  AETHER capability;
* exposes a JSON-schema-like ``input_schema``;
* delegates straight to the service and returns plain dicts — **never** raw
  Playwright objects.

Task 02 tools cover the browser / session / page / navigation lifecycle.
Task 03 adds the ``snapshot -> identify element -> interact -> verify``
workflow: ``browser_snapshot`` plus the interaction tools (click, fill, select,
press, scroll, extract, wait, upload, download, dialog). Task 04 adds the
``reproduce -> inspect`` debugging workflow: ``browser_console``,
``browser_network``, ``browser_dom_inspect``, ``browser_screenshot`` and the
``browser_trace_start`` / ``browser_trace_stop`` pair. Task 05 adds the debug
panel view model (``browser_debug_panel``) and browser state persistence
(``session_save_state`` / ``session_restore_state`` / ``session_state_list`` /
``session_state_delete``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

from agent_ai.tools.base import BaseTool

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids runtime import
    from ..services.playwright_service import PlaywrightService

#: Extension namespace every tool id must start with.
NAMESPACE = "aether.playwright."


class _PlaywrightTool(BaseTool):
    """Common base for Playwright tools: holds the shared service instance."""

    #: Namespaced tool id; subclasses override.
    name: str = "aether.playwright.tool"
    description: str = ""
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, service: PlaywrightService) -> None:
        self._service = service

    @property
    def service(self) -> PlaywrightService:
        return self._service


# ---------------------------------------------------------------------------
# Shared input fragments
# ---------------------------------------------------------------------------
#: Target selectors accepted by every interaction tool: a ``ref`` coming from
#: ``browser_snapshot``, or an explicit ``locator`` (semantic locators first).
_LOCATOR_PROPERTIES: Dict[str, Any] = {
    "ref": {
        "type": "string",
        "description": "Element reference from browser_snapshot (e.g. 'e1'). Preferred.",
    },
    "locator": {
        "type": "object",
        "description": (
            "Explicit Playwright locator. Provide exactly one strategy key "
            "(role/label/text/placeholder/alt/title/test_id/css/xpath). "
            "Semantic locators are preferred over CSS."
        ),
        "properties": {
            "role": {"type": "string", "description": "ARIA role (preferred)."},
            "name": {"type": "string", "description": "Accessible name for a role locator."},
            "label": {"type": "string", "description": "Form label."},
            "text": {"type": "string", "description": "Visible text."},
            "placeholder": {"type": "string"},
            "alt": {"type": "string", "description": "Image alt text."},
            "title": {"type": "string"},
            "test_id": {"type": "string", "description": "data-testid value."},
            "css": {"type": "string", "description": "CSS selector (last resort)."},
            "xpath": {"type": "string", "description": "XPath selector (last resort)."},
            "exact": {"type": "boolean", "description": "Match name/text exactly (default false)."},
            "has_text": {"type": "string", "description": "Additional text filter."},
            "nth": {"type": "integer", "description": "0-based index when several elements match."},
            "first": {"type": "boolean", "description": "Use the first match."},
            "last": {"type": "boolean", "description": "Use the last match."},
        },
    },
}

_PAGE_ID_PROPERTY: Dict[str, Any] = {
    "page_id": {"type": "string", "description": "Target page id."},
}

_TIMEOUT_PROPERTY: Dict[str, Any] = {
    "timeout": {"type": "number", "description": "Timeout in seconds (optional)."},
}


def _schema(
    properties: Dict[str, Any], *, extra_required: List[str] | None = None
) -> Dict[str, Any]:
    """Build an input schema with page_id + locator and the given extras."""
    merged: Dict[str, Any] = {}
    merged.update(_PAGE_ID_PROPERTY)
    merged.update(_LOCATOR_PROPERTIES)
    merged.update(properties)
    merged.update(_TIMEOUT_PROPERTY)
    required = ["page_id"] + list(extra_required or [])
    return {"type": "object", "properties": merged, "required": required}


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------
class LaunchBrowserTool(_PlaywrightTool):
    name = NAMESPACE + "browser_launch"
    description = (
        "Launch a Playwright browser engine and return its browser_id. "
        "Starts the Playwright runtime lazily on first use."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "browser": {
                "type": "string",
                "enum": ["chromium", "firefox", "webkit"],
                "description": "Browser engine (defaults to the extension config).",
            },
            "headless": {
                "type": "boolean",
                "description": "Run headless (defaults to the extension config).",
            },
        },
    }

    def execute(self, **arguments: Any) -> Any:
        browser = arguments.get("browser")
        headless = arguments.get("headless")
        browser_id = self._service.launch_browser(browser, headless)
        return {"browser_id": browser_id, "browser": self._service.browser_info(browser_id)}


class CloseBrowserTool(_PlaywrightTool):
    name = NAMESPACE + "browser_close"
    description = (
        "Close a browser (and its sessions/pages). Omit browser_id to close "
        "every launched browser."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "browser_id": {"type": "string", "description": "Browser to close."},
        },
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.close_browser(arguments.get("browser_id"))


# ---------------------------------------------------------------------------
# Sessions (BrowserContext)
# ---------------------------------------------------------------------------
class CreateSessionTool(_PlaywrightTool):
    name = NAMESPACE + "session_create"
    description = (
        "Create an isolated browser session (BrowserContext) and return its "
        "session_id. A browser is launched on demand when none exists."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "browser_id": {
                "type": "string",
                "description": "Browser to create the session on (optional).",
            },
        },
    }

    def execute(self, **arguments: Any) -> Any:
        session_id = self._service.create_session(arguments.get("browser_id"))
        return {"session_id": session_id, "session": self._service.session_info(session_id)}


class ListSessionsTool(_PlaywrightTool):
    name = NAMESPACE + "session_list"
    description = "List open browser sessions (BrowserContexts) and their pages."
    input_schema = {
        "type": "object",
        "properties": {
            "browser_id": {
                "type": "string",
                "description": "Only list sessions of this browser (optional).",
            },
        },
    }

    def execute(self, **arguments: Any) -> Any:
        sessions = self._service.list_sessions(arguments.get("browser_id"))
        return {"sessions": sessions, "count": len(sessions)}


class CloseSessionTool(_PlaywrightTool):
    name = NAMESPACE + "session_close"
    description = "Close a session (BrowserContext) and all of its pages."
    input_schema = {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session to close."},
        },
        "required": ["session_id"],
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.close_session(arguments["session_id"])


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
class NewPageTool(_PlaywrightTool):
    name = NAMESPACE + "page_new"
    description = (
        "Open a new page/tab inside a session and return its page_id. "
        "Optionally navigate to a URL immediately."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session to open the page in (optional)."},
            "url": {"type": "string", "description": "URL to navigate to after opening (optional)."},
        },
    }

    def execute(self, **arguments: Any) -> Any:
        page_id = self._service.new_page(arguments.get("session_id"), arguments.get("url"))
        return {"page_id": page_id, "page": self._service.page_info(page_id)}


class ListPagesTool(_PlaywrightTool):
    name = NAMESPACE + "page_list"
    description = "List open pages/tabs with their url and title."
    input_schema = {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Only list pages of this session (optional)."},
        },
    }

    def execute(self, **arguments: Any) -> Any:
        pages = self._service.list_pages(arguments.get("session_id"))
        return {"pages": pages, "count": len(pages)}


class NavigateTool(_PlaywrightTool):
    name = NAMESPACE + "page_navigate"
    description = "Navigate a page to a URL and return its resulting state."
    input_schema = {
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "Target page."},
            "url": {"type": "string", "description": "URL to load."},
            "wait_until": {
                "type": "string",
                "enum": ["commit", "domcontentloaded", "load", "networkidle"],
                "description": "When to consider navigation finished (default 'load').",
            },
            "timeout": {"type": "number", "description": "Timeout in seconds (optional)."},
        },
        "required": ["page_id", "url"],
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.navigate(
            arguments["page_id"],
            arguments["url"],
            wait_until=arguments.get("wait_until"),
            timeout=arguments.get("timeout"),
        )


class ReloadTool(_PlaywrightTool):
    name = NAMESPACE + "page_reload"
    description = "Reload the current page."
    input_schema = {
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "Target page."},
            "wait_until": {
                "type": "string",
                "enum": ["commit", "domcontentloaded", "load", "networkidle"],
            },
            "timeout": {"type": "number", "description": "Timeout in seconds (optional)."},
        },
        "required": ["page_id"],
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.reload(
            arguments["page_id"],
            wait_until=arguments.get("wait_until"),
            timeout=arguments.get("timeout"),
        )


class GoBackTool(_PlaywrightTool):
    name = NAMESPACE + "page_back"
    description = "Navigate the page back in its history."
    input_schema = {
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "Target page."},
            "timeout": {"type": "number", "description": "Timeout in seconds (optional)."},
        },
        "required": ["page_id"],
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.go_back(
            arguments["page_id"],
            timeout=arguments.get("timeout"),
        )


class GoForwardTool(_PlaywrightTool):
    name = NAMESPACE + "page_forward"
    description = "Navigate the page forward in its history."
    input_schema = {
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "Target page."},
            "timeout": {"type": "number", "description": "Timeout in seconds (optional)."},
        },
        "required": ["page_id"],
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.go_forward(
            arguments["page_id"],
            timeout=arguments.get("timeout"),
        )


class ClosePageTool(_PlaywrightTool):
    name = NAMESPACE + "page_close"
    description = "Close a single page/tab."
    input_schema = {
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "Page to close."},
        },
        "required": ["page_id"],
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.close_page(arguments["page_id"])


# ---------------------------------------------------------------------------
# Task 03 — Snapshot + element references
# ---------------------------------------------------------------------------
class BrowserSnapshotTool(_PlaywrightTool):
    name = NAMESPACE + "browser_snapshot"
    description = (
        "Capture a compact, LLM-friendly snapshot of a page: url, title, an "
        "annotated element tree and element references (e1, e2, ...) usable by "
        "every interaction tool. Refs are temporary: take a new snapshot after "
        "navigation or DOM changes."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "Target page id."},
            "mode": {
                "type": "string",
                "enum": ["ai", "default", "off"],
                "description": (
                    "Snapshot mode. 'ai' (default) also returns Playwright's "
                    "ARIA snapshot when available; 'off' skips it."
                ),
            },
            "selector": {
                "type": "string",
                "description": "Subtree to snapshot (default 'body').",
            },
            "max_elements": {
                "type": "integer",
                "description": "Maximum number of elements to reference.",
            },
        },
        "required": ["page_id"],
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.snapshot(
            arguments["page_id"],
            mode=arguments.get("mode") or "ai",
            selector=arguments.get("selector") or "body",
            max_elements=arguments.get("max_elements"),
        )


class SnapshotRefsTool(_PlaywrightTool):
    name = NAMESPACE + "browser_refs"
    description = (
        "Return the element references of the most recent snapshot for a page, "
        "without re-scanning the DOM."
    )
    input_schema = {
        "type": "object",
        "properties": {"page_id": {"type": "string"}},
        "required": ["page_id"],
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.snapshot_refs(arguments["page_id"])


# ---------------------------------------------------------------------------
# Task 03 — Interaction tools
# ---------------------------------------------------------------------------
class ClickTool(_PlaywrightTool):
    name = NAMESPACE + "browser_click"
    description = "Click an element identified by a snapshot ref or an explicit locator."
    input_schema = _schema(
        {
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "click_count": {"type": "integer", "description": "Number of clicks (default 1)."},
            "force": {"type": "boolean", "description": "Skip actionability checks."},
        }
    )

    def execute(self, **arguments: Any) -> Any:
        return self._service.click(
            arguments["page_id"],
            ref=arguments.get("ref"),
            locator=arguments.get("locator"),
            button=arguments.get("button") or "left",
            click_count=arguments.get("click_count") or 1,
            force=bool(arguments.get("force", False)),
            timeout=arguments.get("timeout"),
        )


class FillTool(_PlaywrightTool):
    name = NAMESPACE + "browser_fill"
    description = "Fill an input/textarea identified by a snapshot ref or an explicit locator."
    input_schema = _schema(
        {
            "value": {"type": "string", "description": "Text to type."},
            "clear": {
                "type": "boolean",
                "description": "Replace the value (true, default) or append to it (false).",
            },
        },
        extra_required=["value"],
    )

    def execute(self, **arguments: Any) -> Any:
        return self._service.fill(
            arguments["page_id"],
            arguments["value"],
            ref=arguments.get("ref"),
            locator=arguments.get("locator"),
            clear=bool(arguments.get("clear", True)),
            timeout=arguments.get("timeout"),
        )


class SelectTool(_PlaywrightTool):
    name = NAMESPACE + "browser_select"
    description = "Select option(s) in a <select> element by value, label or index."
    input_schema = _schema(
        {
            "value": {"type": "string"},
            "values": {"type": "array", "items": {"type": "string"}},
            "label": {"type": "string"},
            "index": {"type": "integer"},
        }
    )

    def execute(self, **arguments: Any) -> Any:
        return self._service.select_option(
            arguments["page_id"],
            ref=arguments.get("ref"),
            locator=arguments.get("locator"),
            value=arguments.get("value"),
            values=arguments.get("values"),
            label=arguments.get("label"),
            index=arguments.get("index"),
            timeout=arguments.get("timeout"),
        )


class PressTool(_PlaywrightTool):
    name = NAMESPACE + "browser_press"
    description = (
        "Press a keyboard key, optionally on a specific element (otherwise on "
        "the page itself, e.g. 'Escape', 'Tab')."
    )
    input_schema = _schema({"key": {"type": "string", "description": "Key, e.g. 'Enter'."}}, extra_required=["key"])

    def execute(self, **arguments: Any) -> Any:
        return self._service.press(
            arguments["page_id"],
            arguments["key"],
            ref=arguments.get("ref"),
            locator=arguments.get("locator"),
            timeout=arguments.get("timeout"),
        )


class ScrollTool(_PlaywrightTool):
    name = NAMESPACE + "browser_scroll"
    description = (
        "Scroll: pass a ref/locator to scroll an element into view, or use "
        "delta_x/delta_y for the window, or to='top'/'bottom'."
    )
    input_schema = _schema(
        {
            "delta_x": {"type": "number"},
            "delta_y": {"type": "number"},
            "to": {"type": "string", "enum": ["top", "bottom"]},
            "behavior": {"type": "string", "enum": ["auto", "smooth", "instant"]},
        }
    )

    def execute(self, **arguments: Any) -> Any:
        return self._service.scroll(
            arguments["page_id"],
            ref=arguments.get("ref"),
            locator=arguments.get("locator"),
            delta_x=arguments.get("delta_x") or 0,
            delta_y=arguments.get("delta_y") or 0,
            to=arguments.get("to"),
            behavior=arguments.get("behavior") or "auto",
            timeout=arguments.get("timeout"),
        )


class ExtractTool(_PlaywrightTool):
    name = NAMESPACE + "browser_extract"
    description = (
        "Extract structured data from an element: text, value, attribute, "
        "count, visible, enabled, selected or html."
    )
    input_schema = _schema(
        {
            "what": {
                "type": "string",
                "enum": [
                    "text",
                    "value",
                    "attribute",
                    "count",
                    "visible",
                    "enabled",
                    "selected",
                    "html",
                    "all",
                ],
                "description": "What to extract (default 'text').",
            },
            "attribute": {
                "type": "string",
                "description": "Attribute name when what='attribute'.",
            },
        }
    )

    def execute(self, **arguments: Any) -> Any:
        return self._service.extract(
            arguments["page_id"],
            what=arguments.get("what") or "text",
            ref=arguments.get("ref"),
            locator=arguments.get("locator"),
            attribute=arguments.get("attribute"),
            timeout=arguments.get("timeout"),
        )


class WaitTool(_PlaywrightTool):
    name = NAMESPACE + "browser_wait"
    description = (
        "Wait for a condition using Playwright's waiting mechanisms: "
        "visible/hidden/attached/detached (element), enabled/disabled/text/value/"
        "checked (element), url, or load state."
    )
    input_schema = _schema(
        {
            "condition": {
                "type": "string",
                "enum": [
                    "visible",
                    "hidden",
                    "attached",
                    "detached",
                    "enabled",
                    "disabled",
                    "text",
                    "value",
                    "checked",
                    "url",
                    "load",
                ],
                "description": "Condition to wait for.",
            },
            "text": {"type": "string", "description": "Expected text/value."},
            "url": {"type": "string", "description": "URL (glob/regex) for condition='url'."},
            "load_state": {
                "type": "string",
                "enum": ["load", "domcontentloaded", "networkidle", "commit"],
            },
            "exact": {"type": "boolean", "description": "Exact match for text/value."},
        },
        extra_required=["condition"],
    )

    def execute(self, **arguments: Any) -> Any:
        return self._service.wait(
            arguments["page_id"],
            condition=arguments["condition"],
            state=arguments.get("state"),
            ref=arguments.get("ref"),
            locator=arguments.get("locator"),
            text=arguments.get("text"),
            url=arguments.get("url"),
            load_state=arguments.get("load_state"),
            exact=bool(arguments.get("exact", False)),
            timeout=arguments.get("timeout"),
        )


class UploadTool(_PlaywrightTool):
    name = NAMESPACE + "browser_upload"
    description = "Upload file(s) to a file input via set_input_files."
    input_schema = _schema(
        {"files": {"type": "array", "items": {"type": "string"}, "description": "Absolute file paths."}},
        extra_required=["files"],
    )

    def execute(self, **arguments: Any) -> Any:
        return self._service.upload(
            arguments["page_id"],
            arguments["files"],
            ref=arguments.get("ref"),
            locator=arguments.get("locator"),
            timeout=arguments.get("timeout"),
        )


class DownloadTool(_PlaywrightTool):
    name = NAMESPACE + "browser_download"
    description = (
        "Trigger a download by clicking a ref/locator (or just wait for one) and "
        "save it as an artifact; returns filename and local path."
    )
    input_schema = _schema(
        {
            "save_dir": {"type": "string", "description": "Directory to store the file in."},
            "filename": {"type": "string", "description": "Override the saved filename."},
        }
    )

    def execute(self, **arguments: Any) -> Any:
        return self._service.download(
            arguments["page_id"],
            ref=arguments.get("ref"),
            locator=arguments.get("locator"),
            save_dir=arguments.get("save_dir"),
            filename=arguments.get("filename"),
            timeout=arguments.get("timeout"),
        )


class DialogTool(_PlaywrightTool):
    name = NAMESPACE + "browser_dialog"
    description = (
        "Handle native dialogs (alert/confirm/prompt): configure the response "
        "(accept/dismiss, optional prompt text) and/or wait for the next dialog."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "Target page id."},
            "action": {
                "type": "string",
                "enum": ["accept", "dismiss"],
                "description": "How to answer dialogs (default 'accept').",
            },
            "prompt_text": {
                "type": "string",
                "description": "Text to type into a prompt() before accepting.",
            },
            "wait": {
                "type": "boolean",
                "description": "Block until the next dialog appears and handle it now.",
            },
            "timeout": {"type": "number", "description": "Timeout in seconds (optional)."},
        },
        "required": ["page_id"],
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.dialog(
            arguments["page_id"],
            action=arguments.get("action") or "accept",
            prompt_text=arguments.get("prompt_text"),
            wait=bool(arguments.get("wait", False)),
            timeout=arguments.get("timeout"),
        )


# ---------------------------------------------------------------------------
# Task 04 — Debugging tools (console / network / DOM / screenshot / trace)
# ---------------------------------------------------------------------------
class BrowserConsoleTool(_PlaywrightTool):
    name = NAMESPACE + "browser_console"
    description = (
        "Return structured browser console/page-error events captured for a page "
        "(timestamp, type, text, location). Filter by type/level "
        "(log/debug/info/warning/error) and/or a text substring. Raw Playwright "
        "objects are never returned."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "Target page id."},
            "type": {
                "type": "string",
                "description": "Filter by event type: log|debug|info|warning|error (a single value or a list).",
            },
            "types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filter by several event types at once.",
            },
            "level": {"type": "string", "description": "Alias of 'type'."},
            "search": {
                "type": "string",
                "description": "Only events whose text contains this substring (case-insensitive).",
            },
            "limit": {
                "type": "integer",
                "description": "Return only the most recent N events.",
            },
            "clear": {
                "type": "boolean",
                "description": "Clear the captured buffer after reading (default false).",
            },
        },
        "required": ["page_id"],
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.console(
            arguments["page_id"],
            type=arguments.get("type"),
            types=arguments.get("types"),
            level=arguments.get("level"),
            search=arguments.get("search"),
            limit=arguments.get("limit"),
            clear=bool(arguments.get("clear", False)),
        )


class BrowserNetworkTool(_PlaywrightTool):
    name = NAMESPACE + "browser_network"
    description = (
        "Return structured request/response records captured for a page (url, "
        "method, status, resource type, timing, failure). Filter by url, method, "
        "status (e.g. 500 or '5xx'), resource type or failed-only."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "page_id": {"type": "string", "description": "Target page id."},
            "url": {
                "type": "string",
                "description": "Filter by url substring (or glob with * / ?).",
            },
            "method": {"type": "string", "description": "Filter by HTTP method (e.g. GET, POST)."},
            "status": {
                "type": "string",
                "description": "Filter by status: exact code (500) or class ('5xx').",
            },
            "resource_type": {
                "type": "string",
                "description": "Filter by resource type (document/script/xhr/fetch/image/stylesheet/...).",
            },
            "failed": {
                "type": "boolean",
                "description": "Only requests that failed (network error or status >= 400).",
            },
            "limit": {
                "type": "integer",
                "description": "Return only the most recent N records.",
            },
            "clear": {
                "type": "boolean",
                "description": "Clear the captured buffer after reading (default false).",
            },
        },
        "required": ["page_id"],
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.network(
            arguments["page_id"],
            url=arguments.get("url"),
            method=arguments.get("method"),
            status=arguments.get("status"),
            resource_type=arguments.get("resource_type"),
            failed=arguments.get("failed"),
            limit=arguments.get("limit"),
            clear=bool(arguments.get("clear", False)),
        )


class BrowserDomInspectTool(_PlaywrightTool):
    name = NAMESPACE + "browser_dom_inspect"
    description = (
        "Inspect a single element (by ref from browser_snapshot or an explicit "
        "locator): tag, text, attributes, visible, enabled, bounding box and — "
        "when requested — a compact HTML snippet."
    )
    input_schema = _schema(
        {
            "html": {
                "type": "boolean",
                "description": "Also include a (truncated) inner-HTML snippet.",
            },
            "html_limit": {
                "type": "integer",
                "description": "Maximum characters of HTML to return (default 2000).",
            },
        }
    )

    def execute(self, **arguments: Any) -> Any:
        return self._service.dom_inspect(
            arguments["page_id"],
            ref=arguments.get("ref"),
            locator=arguments.get("locator"),
            html=bool(arguments.get("html", False)),
            html_limit=arguments.get("html_limit") or 2000,
            timeout=arguments.get("timeout"),
        )


class BrowserScreenshotTool(_PlaywrightTool):
    name = NAMESPACE + "browser_screenshot"
    description = (
        "Capture a screenshot (full page, viewport, or one element by ref/locator) "
        "and save it as an artifact. Returns path, mime_type, width and height."
    )
    input_schema = _schema(
        {
            "full_page": {
                "type": "boolean",
                "description": "Capture the full scrollable page (ignored for element shots).",
            },
            "type": {
                "type": "string",
                "enum": ["png", "jpeg"],
                "description": "Image format (default png).",
            },
            "quality": {
                "type": "integer",
                "description": "JPEG quality (0-100), only for type='jpeg'.",
            },
            "filename": {"type": "string", "description": "Override the saved filename."},
            "save_dir": {"type": "string", "description": "Directory to store the file in."},
        }
    )

    def execute(self, **arguments: Any) -> Any:
        return self._service.screenshot(
            arguments["page_id"],
            full_page=bool(arguments.get("full_page", False)),
            ref=arguments.get("ref"),
            locator=arguments.get("locator"),
            type=arguments.get("type") or "png",
            quality=arguments.get("quality"),
            filename=arguments.get("filename"),
            save_dir=arguments.get("save_dir"),
            timeout=arguments.get("timeout"),
        )


class BrowserTraceStartTool(_PlaywrightTool):
    name = NAMESPACE + "browser_trace_start"
    description = (
        "Start Playwright tracing on a session (BrowserContext). Captures "
        "screenshots, DOM snapshots and (optionally) sources for later analysis "
        "with browser_trace_stop."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session to trace (optional)."},
            "screenshots": {"type": "boolean", "description": "Capture screenshots (default true)."},
            "snapshots": {"type": "boolean", "description": "Capture DOM snapshots (default true)."},
            "sources": {"type": "boolean", "description": "Include source files (default true)."},
            "name": {"type": "string", "description": "Optional trace display name."},
            "title": {"type": "string", "description": "Optional trace title."},
        },
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.trace_start(
            arguments.get("session_id"),
            screenshots=bool(arguments.get("screenshots", True)),
            snapshots=bool(arguments.get("snapshots", True)),
            sources=bool(arguments.get("sources", True)),
            name=arguments.get("name"),
            title=arguments.get("title"),
        )


class BrowserTraceStopTool(_PlaywrightTool):
    name = NAMESPACE + "browser_trace_stop"
    description = (
        "Stop tracing and save the trace as a re-openable zip artifact. Returns "
        "the artifact path, filename, mime_type and size."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session to stop tracing (optional)."},
            "filename": {"type": "string", "description": "Override the saved trace filename."},
            "save_dir": {"type": "string", "description": "Directory to store the trace in."},
        },
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.trace_stop(
            arguments.get("session_id"),
            filename=arguments.get("filename"),
            save_dir=arguments.get("save_dir"),
        )


class BrowserTraceOpenTool(_PlaywrightTool):
    name = NAMESPACE + "browser_trace_open"
    description = (
        "Return an open descriptor for a trace .zip artifact (path, existence, "
        "mime type, viewer hint and an OS open command). By default nothing is "
        "launched; pass reveal=true to open/reveal the file with the OS."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Trace .zip path (defaults to the session's last trace)."},
            "session_id": {"type": "string", "description": "Session whose last trace to open (optional)."},
            "reveal": {"type": "boolean", "description": "Actually open/reveal the file with the OS (default false)."},
        },
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.trace_open(
            path=arguments.get("path"),
            session_id=arguments.get("session_id"),
            reveal=bool(arguments.get("reveal", False)),
        )


# ---------------------------------------------------------------------------
# Task 05 — Debug UI panel + browser state persistence
# ---------------------------------------------------------------------------
class BrowserDebugPanelTool(_PlaywrightTool):
    name = NAMESPACE + "browser_debug_panel"
    description = (
        "Return the generic Playwright debug panel view model: sessions and "
        "pages tables plus the console/network views of the selected page. "
        "Optionally includes a DOM inspection and/or captures a screenshot or "
        "stops an active trace as an artifact. The result follows the AETHER "
        "UI Result contract (renderer/type/data) so the generic UI can render "
        "it with no Playwright-specific frontend."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Selected session id (optional)."},
            "page_id": {"type": "string", "description": "Selected page/tab id (optional)."},
            "console": {
                "type": "object",
                "description": "Console filters (type/types/level/search/limit/clear).",
            },
            "network": {
                "type": "object",
                "description": "Network filters (url/method/status/resource_type/failed/limit/clear).",
            },
            "dom": {
                "type": "object",
                "description": "DOM inspection options (ref/locator/html/html_limit).",
            },
            "screenshot": {
                "type": "object",
                "description": "Screenshot options (full_page/type/quality/ref/locator); captures when present.",
            },
            "trace": {
                "type": "object",
                "description": "Trace-stop options (filename/save_dir); stops the active trace when present.",
            },
        },
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.debug_panel(
            session_id=arguments.get("session_id"),
            page_id=arguments.get("page_id"),
            console=arguments.get("console"),
            network=arguments.get("network"),
            dom=arguments.get("dom"),
            screenshot=arguments.get("screenshot"),
            trace=arguments.get("trace"),
        )


class SessionSaveStateTool(_PlaywrightTool):
    name = NAMESPACE + "session_save_state"
    description = (
        "Save a session's browser storage state (cookies + localStorage) so it "
        "survives an AETHER restart. Stored through the Extension storage API, "
        "isolated per extension and (optionally) per project. Credentials are "
        "never returned — only counts."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session to save (optional)."},
            "name": {"type": "string", "description": "State name (defaults to the session id)."},
            "project": {"type": "string", "description": "Project scope (filesystem path) for the saved state (optional)."},
        },
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.save_state(
            arguments.get("session_id"),
            name=arguments.get("name"),
            project=arguments.get("project"),
        )


class SessionRestoreStateTool(_PlaywrightTool):
    name = NAMESPACE + "session_restore_state"
    description = (
        "Restore a saved browser storage state. Without session_id a new session "
        "is created straight from the state (Playwright new_context(storage_state)); "
        "with a session_id the state is re-applied to that context."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Saved state name to restore."},
            "project": {"type": "string", "description": "Project scope the state was saved under (optional)."},
            "session_id": {"type": "string", "description": "Existing session to apply the state to (optional)."},
            "browser_id": {"type": "string", "description": "Browser to create the restored session on (optional)."},
            "apply_origins": {"type": "boolean", "description": "Also re-apply localStorage (default true)."},
        },
        "required": ["name"],
    }

    def execute(self, **arguments: Any) -> Any:
        return self._service.restore_state(
            name=arguments["name"],
            project=arguments.get("project"),
            session_id=arguments.get("session_id"),
            browser_id=arguments.get("browser_id"),
            apply_origins=bool(arguments.get("apply_origins", True)),
        )


class SessionStateListTool(_PlaywrightTool):
    name = NAMESPACE + "session_state_list"
    description = "List the names of saved browser storage states (optionally per project)."
    input_schema = {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "Project scope to list (optional)."},
        },
    }

    def execute(self, **arguments: Any) -> Any:
        names = self._service.list_saved_states(project=arguments.get("project"))
        return {"states": names, "count": len(names)}


class SessionStateDeleteTool(_PlaywrightTool):
    name = NAMESPACE + "session_state_delete"
    description = "Delete a saved browser storage state by name."
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Saved state name to delete."},
            "project": {"type": "string", "description": "Project scope (optional)."},
        },
        "required": ["name"],
    }

    def execute(self, **arguments: Any) -> Any:
        deleted = self._service.delete_saved_state(
            arguments["name"], project=arguments.get("project")
        )
        return {"name": arguments["name"], "deleted": bool(deleted)}


#: Ordered list of tool classes exposed by this extension.
TOOL_CLASSES: List[type] = [
    LaunchBrowserTool,
    CloseBrowserTool,
    CreateSessionTool,
    ListSessionsTool,
    CloseSessionTool,
    NewPageTool,
    ListPagesTool,
    NavigateTool,
    ReloadTool,
    GoBackTool,
    GoForwardTool,
    ClosePageTool,
    # Task 03
    BrowserSnapshotTool,
    SnapshotRefsTool,
    ClickTool,
    FillTool,
    SelectTool,
    PressTool,
    ScrollTool,
    ExtractTool,
    WaitTool,
    UploadTool,
    DownloadTool,
    DialogTool,
    # Task 04
    BrowserConsoleTool,
    BrowserNetworkTool,
    BrowserDomInspectTool,
    BrowserScreenshotTool,
    BrowserTraceStartTool,
    BrowserTraceStopTool,
    BrowserTraceOpenTool,
    # Task 05
    BrowserDebugPanelTool,
    SessionSaveStateTool,
    SessionRestoreStateTool,
    SessionStateListTool,
    SessionStateDeleteTool,
]


def build_playwright_tools(service: PlaywrightService) -> List[BaseTool]:
    """Instantiate every Playwright tool bound to ``service``."""
    return [cls(service) for cls in TOOL_CLASSES]


__all__ = ["build_playwright_tools", "TOOL_CLASSES", "NAMESPACE"]
