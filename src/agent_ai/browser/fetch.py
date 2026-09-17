"""HTTP fetch sederhana (provider-agnostic, memakai `requests` yang sudah ada).

Fondasi browser/web tool: mengambil konten HTTP secara aman dan bounded.
TIDAK menjalankan JavaScript, tanpa browser otomasi, tanpa GUI,
TIDAK membuat crawler/search engine.

Keamanan:
    - Hanya skema http/https yang diizinkan.
    - Host privat/loopback/link-local diblokir (anti SSRF dasar).
    - Timeout wajib.
    - Response size dibatasi (streaming, berhenti saat melewati batas).
    - Tidak mengakses berkas lokal workspace.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urlparse

from agent_ai.browser.base import BaseBrowserTool
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
    ResponseTooLargeError,
)

# Skema yang diizinkan.
_ALLOWED_SCHEMES = {"http", "https"}
# Batas default (bounded).
_DEFAULT_TIMEOUT = 10.0
_DEFAULT_MAX_BYTES = 2_000_000
# Chunk size untuk streaming read.
_CHUNK_SIZE = 65536


def validate_url(url: str, *, allow_private: bool = False) -> str:
    """Validasi URL (skema + host) dan kembalikan URL ternormalisasi.

    Args:
        url: URL yang diminta.
        allow_private: bila True, izinkan host privat/loopback (mis. untuk
            testing lokal). Default False (aman).

    Returns:
        URL yang sudah divalidasi.

    Raises:
        InvalidUrlError: bila URL tidak valid / skema tidak diizinkan.
        BlockedTargetError: bila host adalah target privat/loopback.
    """
    if not url or not isinstance(url, str):
        raise InvalidUrlError("URL kosong atau bukan string.", url=url)

    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise InvalidUrlError(
            f"Skema URL tidak diizinkan: '{parsed.scheme or '(kosong)'}' (hanya http/https).",
            url=url,
        )
    if not parsed.hostname:
        raise InvalidUrlError("URL tidak memiliki host.", url=url)

    if not allow_private:
        _reject_private_host(parsed.hostname, url)
    return url.strip()


def _reject_private_host(hostname: str, url: str) -> None:
    """Tolak host privat/loopback/link-local (anti SSRF dasar).

    Bila hostname adalah IP literal, cek langsung. Bila domain, resolusi DNS
    dilakukan dan seluruh alamat hasil resolusi diperiksa.
    """
    candidates = []
    try:
        ip = ipaddress.ip_address(hostname)
        candidates.append(ip)
    except ValueError:
        # Bukan IP literal -> resolusi DNS (bounded, error -> connection error).
        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror as exc:
            raise FetchConnectionError(
                f"Gagal meresolusi host '{hostname}': {exc}", url=url
            ) from exc
        for info in infos:
            addr = info[4][0]
            try:
                candidates.append(ipaddress.ip_address(addr))
            except ValueError:
                continue

    for ip in candidates:
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise BlockedTargetError(
                f"Target '{hostname}' mengarah ke alamat privat/terlarang ({ip}).",
                url=url,
            )


class HttpFetchTool(BaseBrowserTool):
    """Tool fetch HTTP sederhana (bounded, tanpa JavaScript).

    Args:
        timeout: batas waktu default (detik).
        max_bytes: batas ukuran response default (bytes).
        user_agent: User-Agent default.
    """

    name = "http_fetch"
    description = "Mengambil konten HTTP(S) dari sebuah URL (tanpa JavaScript)."
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL http/https."},
            "timeout": {"type": "number", "description": "Batas waktu (detik)."},
            "max_bytes": {"type": "integer", "description": "Batas ukuran response (bytes)."},
        },
        "required": ["url"],
    }

    def __init__(
        self,
        timeout: float = _DEFAULT_TIMEOUT,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        user_agent: str = "AETHER-Browser/1.0",
        allow_private: bool = False,
    ) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.user_agent = user_agent
        # Default aman: host privat/loopback diblokir. Set True hanya untuk
        # testing lokal (mis. server di 127.0.0.1).
        self.allow_private = allow_private

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def fetch(self, request: BrowserRequest) -> FetchResult:
        """Ambil konten dari URL (bounded).

        Args:
            request: BrowserRequest.

        Returns:
            FetchResult (status OK).

        Raises:
            BrowserError: error terstruktur (invalid_url/blocked/timeout/
                connection_error/http_error/too_large).
        """
        url = validate_url(request.url, allow_private=self.allow_private)
        timeout = request.timeout or self.timeout
        max_bytes = request.max_bytes or self.max_bytes

        headers = {"User-Agent": self.user_agent}
        headers.update(request.headers or {})

        try:
            import requests
        except ImportError as exc:  # pragma: no cover - requests wajib
            raise FetchConnectionError("Library 'requests' tidak tersedia.", url=url) from exc

        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=timeout,
                stream=True,
                allow_redirects=True,
            )
        except requests.exceptions.Timeout as exc:
            raise FetchTimeoutError(f"Fetch melewati batas waktu {timeout}s.", url=url) from exc
        except requests.exceptions.RequestException as exc:
            raise FetchConnectionError(
                f"Gagal menghubungi '{url}': {type(exc).__name__}", url=url
            ) from exc

        try:
            return self._read_response(response, url, max_bytes)
        finally:
            response.close()

    def execute(self, **arguments) -> FetchResult:
        """Interface tool: fetch dari argumen (url/timeout/max_bytes)."""
        url = arguments.get("url")
        if not url:
            raise InvalidUrlError("Argumen 'url' wajib diisi.")
        request = BrowserRequest(
            url=url,
            timeout=float(arguments.get("timeout", self.timeout)),
            max_bytes=int(arguments.get("max_bytes", self.max_bytes)),
        )
        return self.fetch(request)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _read_response(self, response, url: str, max_bytes: int) -> FetchResult:
        """Baca response secara streaming dengan batas ukuran."""
        http_status = response.status_code
        if http_status >= 400:
            raise HttpStatusError(
                f"HTTP error {http_status} untuk '{url}'.",
                url=url,
                http_status=http_status,
            )

        content_type = response.headers.get("Content-Type", "")
        chunks = []
        total = 0
        truncated = False
        for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                # Simpan sampai batas, tandai truncated, hentikan pembacaan.
                remaining = max_bytes - (total - len(chunk))
                if remaining > 0:
                    chunks.append(chunk[:remaining])
                truncated = True
                break
            chunks.append(chunk)

        content = b"".join(chunks)
        return FetchResult(
            url=response.url or url,
            status=BrowserStatus.OK,
            http_status=http_status,
            headers={
                "content-type": content_type,
                "content-length": response.headers.get("Content-Length", ""),
            },
            content=content,
            content_type=content_type,
            size_bytes=len(content),
            truncated=truncated,
            metadata={"requested_url": url, "max_bytes": max_bytes},
        )
