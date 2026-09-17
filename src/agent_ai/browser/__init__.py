"""Browser / Web Tools Subsystem AETHER (#47).

Fondasi browser/web tool provider-agnostic agar Agent Core nantinya dapat
meminta informasi dari web.

    from agent_ai.browser import (
        HttpFetchTool,
        HtmlParser,
        BrowserToolRegistry,
        BrowserRequest,
        FetchResult,
        ParsedContent,
    )

Prinsip:
    - Provider/model agnostic.
    - Memakai dependency yang sudah ada (`requests`), tanpa browser framework.
    - Tidak menjalankan JavaScript, tanpa browser otomasi, tanpa GUI.
    - Tidak ada crawler / search engine / OCR.
    - Timeout wajib, response size dibatasi, URL divalidasi (anti SSRF dasar).
    - Error terstruktur (BrowserError) untuk Reliability/Recovery.
    - Tidak mengakses filesystem workspace.
"""

from agent_ai.browser.base import BaseBrowserTool
from agent_ai.browser.fetch import HttpFetchTool, validate_url
from agent_ai.browser.models import (
    BlockedTargetError,
    BrowserError,
    BrowserRequest,
    BrowserStatus,
    FetchConnectionError,
    FetchResult,
    FetchTimeoutError,
    HttpStatusError,
    InvalidUrlError,
    ParseError,
    ParsedContent,
    ResponseTooLargeError,
)
from agent_ai.browser.parser import HtmlParser
from agent_ai.browser.registry import BrowserToolRegistry

__all__ = [
    "BaseBrowserTool",
    "HttpFetchTool",
    "HtmlParser",
    "BrowserToolRegistry",
    "BrowserRequest",
    "FetchResult",
    "ParsedContent",
    "BrowserStatus",
    "BrowserError",
    "InvalidUrlError",
    "BlockedTargetError",
    "FetchTimeoutError",
    "FetchConnectionError",
    "HttpStatusError",
    "ResponseTooLargeError",
    "ParseError",
    "validate_url",
]
