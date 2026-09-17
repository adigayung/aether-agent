"""Parser HTML sederhana (stdlib, tanpa dependency baru).

Mengekstraksi title, text, dan links dari HTML secara sederhana. TIDAK membuat
crawler, TIDAK menjalankan JavaScript, TIDAK melakukan OCR.

Memakai `html.parser.HTMLParser` dari standard library.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser as _StdHTMLParser
from typing import List, Optional
from urllib.parse import urljoin

from agent_ai.browser.models import ParsedContent

# Tag yang isinya diabaikan (bukan konten teks utama).
# Catatan: "head" TIDAK di-skip karena <title> berada di dalamnya.
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
# Tag yang memisahkan blok teks (untuk whitespace).
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "nav", "ul", "ol", "table", "pre",
}
_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKLINE_RE = re.compile(r"\n\s*\n+")


class _HtmlCollector(_StdHTMLParser):
    """Kolektor title/text/links dari HTML (sederhana, tanpa JS)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str = ""
        self._in_title = False
        self._skip_depth = 0
        self._text_parts: List[str] = []
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            for key, value in attrs:
                if key.lower() == "href" and value:
                    self.links.append(value.strip())
        if tag in _BLOCK_TAGS:
            self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title += data
            return
        if data and data.strip():
            self._text_parts.append(data)

    def text(self) -> str:
        """Gabungkan teks dan normalkan whitespace."""
        raw = "".join(self._text_parts)
        raw = _WHITESPACE_RE.sub(" ", raw)
        raw = _BLANKLINE_RE.sub("\n", raw)
        return raw.strip()


class HtmlParser:
    """Parser HTML sederhana (title/text/links).

    Args:
        base_url: URL sumber untuk meresolusi link relatif menjadi absolut.
    """

    def __init__(self, base_url: str = "") -> None:
        self.base_url = base_url

    def parse(self, html: str, *, url: Optional[str] = None) -> ParsedContent:
        """Parse HTML menjadi ParsedContent.

        Args:
            html: konten HTML (string).
            url: URL sumber (override base_url).

        Returns:
            ParsedContent (title, text, links absolut).

        Raises:
            ParseError: bila input bukan string.
        """
        from agent_ai.browser.models import ParseError

        if not isinstance(html, str):
            raise ParseError("Konten HTML harus berupa string.", url=url or self.base_url)

        base = url or self.base_url
        collector = _HtmlCollector()
        try:
            collector.feed(html)
            collector.close()
        except Exception as exc:  # noqa: BLE001 - parser error -> terstruktur
            raise ParseError(f"Gagal memparse HTML: {type(exc).__name__}", url=base) from exc

        title = _WHITESPACE_RE.sub(" ", collector.title).strip()
        links = self._resolve_links(collector.links, base)

        return ParsedContent(
            title=title,
            text=collector.text(),
            links=links,
            url=base,
            metadata={"link_count": len(links)},
        )

    @staticmethod
    def _resolve_links(links: List[str], base: str) -> List[str]:
        """Resolusi link relatif -> absolut (dedupe, pertahankan urutan)."""
        resolved: List[str] = []
        seen = set()
        for link in links:
            if not link or link.startswith(("javascript:", "mailto:", "#")):
                continue
            absolute = urljoin(base, link) if base else link
            if absolute not in seen:
                seen.add(absolute)
                resolved.append(absolute)
        return resolved
