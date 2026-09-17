"""Verifikasi Browser / Web Tools (#47).

Deterministik, tanpa network eksternal. Memakai HTTP server lokal (stdlib) di
127.0.0.1 untuk menguji HTTP success/error. Fixture kecil di
J:\\Agent_Ai\\dummy_test\\browser_fixture dan dibersihkan setelah test.

Menguji:
    1. model request/response
    2. URL validation
    3. timeout
    4. response size limit
    5. HTTP success
    6. HTTP error
    7. malformed URL
    8. parser title
    9. parser text
   10. parser links
   11. structured errors
   12. registry
   13. no filesystem access
   14. no browser framework
   15. architecture boundary

Jalankan:
    python scripts/check_browser.py
"""

from __future__ import annotations

import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.browser import (  # noqa: E402
    BlockedTargetError,
    BrowserError,
    BrowserRequest,
    BrowserStatus,
    BrowserToolRegistry,
    FetchResult,
    FetchTimeoutError,
    HtmlParser,
    HttpFetchTool,
    HttpStatusError,
    InvalidUrlError,
    ParsedContent,
    ResponseTooLargeError,
    validate_url,
)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "browser_fixture"

_HTML_PAGE = """<!DOCTYPE html>
<html>
<head><title>Halaman Uji AETHER</title>
<style>.x{color:red}</style>
<script>var secret = "jangan_diambil";</script>
</head>
<body>
<h1>Judul Besar</h1>
<p>Ini paragraf pertama.</p>
<p>Paragraf kedua dengan <a href="/relatif">link relatif</a>.</p>
<a href="https://example.com/abs">Link absolut</a>
<a href="#anchor">Anchor</a>
<a href="javascript:void(0)">JS</a>
</body>
</html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401 - senyap
        pass

    def do_GET(self):
        if self.path == "/ok":
            body = _HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/big":
            body = b"x" * 500_000
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/slow":
            import time
            time.sleep(2.0)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"late")
            except (ConnectionError, OSError):
                # Client menutup koneksi karena timeout; abaikan.
                pass
        elif self.path == "/error":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"server error")
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")


def _start_server() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "note.txt").write_text("browser fixture\n", encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Browser / Web Tools (#47) ===")
    setup_fixture()
    server, base = _start_server()
    try:
        return _run(base)
    finally:
        server.shutdown()
        teardown_fixture()


def _run(base: str) -> int:
    # allow_private=True hanya untuk server uji lokal (127.0.0.1).
    tool = HttpFetchTool(timeout=5.0, max_bytes=2_000_000, allow_private=True)

    # 1) model request/response.
    req = BrowserRequest(url=f"{base}/ok", timeout=5.0, max_bytes=100_000)
    assert req.to_dict()["url"].endswith("/ok")
    result = tool.fetch(req)
    assert isinstance(result, FetchResult), type(result)
    assert result.ok and result.status == BrowserStatus.OK
    assert result.http_status == 200
    assert result.size_bytes == len(result.content) > 0
    assert "content-type" in result.headers
    assert result.to_dict()["status"] == "ok"
    print(f"[1] model request/response OK -> status={result.status.value}, size={result.size_bytes}")

    # 2) URL validation.
    assert validate_url("https://example.com/path") == "https://example.com/path"
    assert validate_url("http://example.com") == "http://example.com"
    print("[2] URL validation OK -> http/https diterima")

    # 3) timeout.
    try:
        tool.fetch(BrowserRequest(url=f"{base}/slow", timeout=0.5))
        raise AssertionError("harus timeout")
    except FetchTimeoutError as exc:
        assert exc.status == BrowserStatus.TIMEOUT
    print("[3] timeout OK -> FetchTimeoutError")

    # 4) response size limit.
    small = HttpFetchTool(timeout=5.0, max_bytes=10_000, allow_private=True)
    res_big = small.fetch(BrowserRequest(url=f"{base}/big", max_bytes=10_000))
    assert res_big.size_bytes <= 10_000, res_big.size_bytes
    assert res_big.truncated is True, "response besar harus ditandai truncated"
    print(f"[4] response size limit OK -> size={res_big.size_bytes}, truncated={res_big.truncated}")

    # 5) HTTP success.
    res_ok = tool.fetch(BrowserRequest(url=f"{base}/ok"))
    assert res_ok.http_status == 200 and res_ok.ok
    print(f"[5] HTTP success OK -> {res_ok.http_status}")

    # 6) HTTP error.
    try:
        tool.fetch(BrowserRequest(url=f"{base}/error"))
        raise AssertionError("harus HTTP error")
    except HttpStatusError as exc:
        assert exc.status == BrowserStatus.HTTP_ERROR
        assert exc.http_status == 500
    print("[6] HTTP error OK -> HttpStatusError(500)")

    # 7) malformed URL.
    for bad in ("", "ftp://example.com", "not a url", "://nohost", "file:///etc/passwd"):
        try:
            validate_url(bad)
            raise AssertionError(f"harus menolak URL: {bad!r}")
        except InvalidUrlError:
            pass
    # Target privat/loopback diblokir (anti SSRF dasar).
    for blocked in ("http://localhost/x", "http://127.0.0.1/x", "http://10.0.0.1/x", "http://169.254.1.1/x"):
        try:
            validate_url(blocked)
            raise AssertionError(f"harus memblokir target privat: {blocked!r}")
        except BlockedTargetError:
            pass
    print("[7] malformed URL OK -> InvalidUrlError + BlockedTargetError (SSRF dasar)")

    # 8) parser title.
    parser = HtmlParser(base_url=f"{base}/ok")
    parsed = parser.parse(_HTML_PAGE)
    assert isinstance(parsed, ParsedContent)
    assert parsed.title == "Halaman Uji AETHER", parsed.title
    print(f"[8] parser title OK -> {parsed.title!r}")

    # 9) parser text (script/style diabaikan).
    assert "Ini paragraf pertama." in parsed.text, parsed.text
    assert "Paragraf kedua" in parsed.text
    assert "jangan_diambil" not in parsed.text, "isi <script> tidak boleh masuk text"
    assert "color:red" not in parsed.text, "isi <style> tidak boleh masuk text"
    print(f"[9] parser text OK -> {len(parsed.text)} chars, script/style diabaikan")

    # 10) parser links (relatif -> absolut, dedupe, skip javascript/anchor).
    assert f"{base}/relatif" in parsed.links, parsed.links
    assert "https://example.com/abs" in parsed.links
    assert all(not l.startswith("javascript:") for l in parsed.links)
    assert all(not l.endswith("#anchor") for l in parsed.links)
    print(f"[10] parser links OK -> {parsed.links}")

    # 11) structured errors.
    err = InvalidUrlError("bad", url="ftp://x")
    d = err.to_dict()
    assert d["status"] == "invalid_url" and d["url"] == "ftp://x"
    assert isinstance(err, BrowserError)
    # Semua error turunan punya status terstruktur.
    for exc_cls, expected in (
        (BlockedTargetError, BrowserStatus.BLOCKED),
        (FetchTimeoutError, BrowserStatus.TIMEOUT),
        (HttpStatusError, BrowserStatus.HTTP_ERROR),
        (ResponseTooLargeError, BrowserStatus.TOO_LARGE),
    ):
        e = exc_cls("m")
        assert e.status == expected, (exc_cls, e.status)
        assert "status" in e.to_dict()
    print("[11] structured errors OK -> BrowserError + status enum")

    # 12) registry.
    reg = BrowserToolRegistry()
    reg.register(HttpFetchTool(allow_private=True))
    assert reg.has("http_fetch")
    assert "http_fetch" in reg.list()
    assert reg.get("http_fetch").name == "http_fetch"
    assert reg.specs()[0]["name"] == "http_fetch"
    out = reg.execute("http_fetch", {"url": f"{base}/ok"})
    assert out.ok
    try:
        reg.get("tidak_ada")
        raise AssertionError("harus BrowserError")
    except BrowserError:
        pass
    print("[12] registry OK -> register/get/list/specs/execute")

    # 13) no filesystem access.
    browser_dir = SRC_DIR / "agent_ai" / "browser"
    for p in browser_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        for bad in ("open(", "Path(", "os.path", "shutil", "read_text", "write_text", "import os"):
            assert bad not in text, f"{p.name} tidak boleh mengakses filesystem: {bad}"
    print("[13] no filesystem access OK")

    # 14) no browser framework.
    for p in browser_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8").lower()
        for bad in ("selenium", "playwright", "pyppeteer", "webkit", "chromedriver", "geckodriver"):
            assert bad not in text, f"{p.name} tidak boleh memakai browser framework: {bad}"
        # Tidak menjalankan JavaScript.
        assert "execute_script" not in text, f"{p.name} tidak boleh menjalankan JavaScript"
    print("[14] no browser framework OK -> tanpa Selenium/Playwright/JS")

    # 15) architecture boundary.
    #     - core TIDAK boleh impor browser.
    core_dir = SRC_DIR / "agent_ai" / "core"
    for p in core_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "agent_ai.browser" not in text, f"core/{p.name} tidak boleh impor browser"
    #     - browser tidak boleh impor core/runtime/tools (boundary).
    for p in browser_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "agent_ai.core" not in text, f"{p.name} tidak boleh impor core"
        assert "agent_ai.runtime" not in text, f"{p.name} tidak boleh impor runtime"
        assert "agent_ai.tools" not in text, f"{p.name} tidak boleh impor tools"
        assert "subprocess" not in text, f"{p.name} tidak boleh menjalankan command"
    #     - hanya fetch.py yang boleh memakai network (requests).
    for p in browser_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        if p.name != "fetch.py":
            assert "import requests" not in text, f"{p.name} tidak boleh melakukan network"
    print("[15] architecture boundary bersih OK")

    print()
    print("[OK] Browser / Web Tools bekerja (provider-agnostic, bounded, tanpa JS).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
