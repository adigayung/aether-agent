"""Model untuk Browser / Web Tools Subsystem (#47).

Provider-agnostic. Fondasi browser/web tool agar Agent Core nantinya dapat
meminta informasi dari web. Model di sini hanya data terstruktur untuk request,
hasil fetch, konten terparse, dan status/error.

    BrowserStatus   -> status hasil operasi (ok/error terstruktur)
    BrowserRequest  -> permintaan fetch (url + opsi bounded)
    FetchResult     -> hasil HTTP fetch (status, headers, body, ukuran)
    ParsedContent   -> konten terparse (title, text, links)
    BrowserError    -> error terstruktur (mudah dipakai Reliability/Recovery)

Tidak menyimpan chain-of-thought. Tidak ada JavaScript, crawler, search engine,
atau OCR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class BrowserStatus(str, Enum):
    """Status hasil operasi browser/web tool (terstruktur)."""

    OK = "ok"                        # operasi sukses
    INVALID_URL = "invalid_url"      # URL tidak valid / skema tidak diizinkan
    BLOCKED = "blocked"              # target diblokir (mis. host privat)
    TIMEOUT = "timeout"              # operasi melewati batas waktu
    CONNECTION_ERROR = "connection_error"  # gagal koneksi/network
    HTTP_ERROR = "http_error"        # HTTP status >= 400
    TOO_LARGE = "too_large"          # response melebihi batas ukuran
    PARSE_ERROR = "parse_error"      # konten tidak dapat diparse
    UNKNOWN_ERROR = "unknown_error"  # error tak terklasifikasi


class BrowserError(Exception):
    """Base error terstruktur untuk browser/web tool.

    Attributes:
        status: BrowserStatus terstruktur (mudah dipakai Reliability/Recovery).
        message: deskripsi singkat.
        url: URL terkait (bila ada).
        http_status: HTTP status code (bila ada).
        metadata: info tambahan bebas.
    """

    def __init__(
        self,
        status: BrowserStatus,
        message: str = "",
        *,
        url: Optional[str] = None,
        http_status: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message or status.value)
        self.status = status
        self.message = message or status.value
        self.url = url
        self.http_status = http_status
        self.metadata = metadata or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "message": self.message,
            "url": self.url,
            "http_status": self.http_status,
            "metadata": self.metadata,
        }


class InvalidUrlError(BrowserError):
    """URL tidak valid / skema tidak diizinkan."""

    def __init__(self, message: str = "", *, url: Optional[str] = None, **kw: Any) -> None:
        super().__init__(BrowserStatus.INVALID_URL, message, url=url, **kw)


class BlockedTargetError(BrowserError):
    """Target diblokir (mis. host privat/loopback)."""

    def __init__(self, message: str = "", *, url: Optional[str] = None, **kw: Any) -> None:
        super().__init__(BrowserStatus.BLOCKED, message, url=url, **kw)


class FetchTimeoutError(BrowserError):
    """Operasi fetch melewati batas waktu."""

    def __init__(self, message: str = "", *, url: Optional[str] = None, **kw: Any) -> None:
        super().__init__(BrowserStatus.TIMEOUT, message, url=url, **kw)


class FetchConnectionError(BrowserError):
    """Gagal koneksi/network."""

    def __init__(self, message: str = "", *, url: Optional[str] = None, **kw: Any) -> None:
        super().__init__(BrowserStatus.CONNECTION_ERROR, message, url=url, **kw)


class HttpStatusError(BrowserError):
    """HTTP status >= 400."""

    def __init__(
        self, message: str = "", *, url: Optional[str] = None, http_status: Optional[int] = None, **kw: Any
    ) -> None:
        super().__init__(BrowserStatus.HTTP_ERROR, message, url=url, http_status=http_status, **kw)


class ResponseTooLargeError(BrowserError):
    """Response melebihi batas ukuran."""

    def __init__(self, message: str = "", *, url: Optional[str] = None, **kw: Any) -> None:
        super().__init__(BrowserStatus.TOO_LARGE, message, url=url, **kw)


class ParseError(BrowserError):
    """Konten tidak dapat diparse."""

    def __init__(self, message: str = "", *, url: Optional[str] = None, **kw: Any) -> None:
        super().__init__(BrowserStatus.PARSE_ERROR, message, url=url, **kw)


@dataclass
class BrowserRequest:
    """Permintaan fetch (bounded).

    Attributes:
        url: URL target (http/https).
        method: metode HTTP (default GET).
        timeout: batas waktu (detik).
        max_bytes: batas ukuran response (bytes).
        headers: header tambahan (opsional).
        metadata: info tambahan bebas.
    """

    url: str = ""
    method: str = "GET"
    timeout: float = 10.0
    max_bytes: int = 2_000_000
    headers: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "method": self.method,
            "timeout": self.timeout,
            "max_bytes": self.max_bytes,
            "headers": dict(self.headers),
            "metadata": self.metadata,
        }


@dataclass
class FetchResult:
    """Hasil HTTP fetch.

    Attributes:
        url: URL final (setelah redirect).
        status: BrowserStatus.
        http_status: HTTP status code.
        headers: header response (subset).
        content: body (bytes).
        content_type: MIME type response.
        size_bytes: ukuran body (bytes).
        truncated: apakah body dipotong karena batas ukuran.
        metadata: info tambahan bebas.
    """

    url: str = ""
    status: BrowserStatus = BrowserStatus.OK
    http_status: Optional[int] = None
    headers: Dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    content_type: str = ""
    size_bytes: int = 0
    truncated: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == BrowserStatus.OK

    def text(self, encoding: str = "utf-8") -> str:
        """Decode body menjadi teks (aman, replace error)."""
        return self.content.decode(encoding, errors="replace")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status.value,
            "http_status": self.http_status,
            "headers": dict(self.headers),
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "truncated": self.truncated,
            "metadata": self.metadata,
        }


@dataclass
class ParsedContent:
    """Konten HTML terparse (sederhana).

    Attributes:
        title: judul halaman (bila ada).
        text: teks halaman (tag dihapus, whitespace dinormalkan).
        links: daftar link absolut (href).
        url: URL sumber.
        metadata: info tambahan bebas.
    """

    title: str = ""
    text: str = ""
    links: List[str] = field(default_factory=list)
    url: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "text": self.text,
            "links": list(self.links),
            "url": self.url,
            "metadata": self.metadata,
        }
