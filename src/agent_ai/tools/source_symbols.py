"""Symbol & structure helper untuk read_file / search_code.

Memanfaatkan parser/index yang SUDAH ADA (`agent_ai.codeindex`) untuk
mengekstrak deklarasi (class/function/method/...) dari sebuah file secara
DETERMINISTIK (tanpa LLM/embeddings). Tidak membuat parser besar baru:

    - nama/kind/lokasi symbol berasal dari parser codeindex existing
      (PythonParser untuk .py via `ast`, GenericParser regex untuk bahasa lain),
    - rentang baris akhir symbol dihitung dengan scanner blok ringkas
      (indentasi untuk Python, brace untuk bahasa lain).

Modul ini murni helper read-only. Tidak menulis, tidak menjalankan command.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

# Batas aman saat memindai blok brace (baris) agar tidak runaway.
_BRACE_SCAN_LIMIT = 4000

# Registry bahasa codeindex dibuat sekali (deterministik, murah).
_LANG_REGISTRY = None


def _language_registry():
    """LanguageRegistry default codeindex (lazy, dibuat sekali)."""
    global _LANG_REGISTRY
    if _LANG_REGISTRY is None:
        from agent_ai.codeindex.languages import LanguageRegistry

        _LANG_REGISTRY = LanguageRegistry.default()
    return _LANG_REGISTRY


@dataclass
class SymbolSpan:
    """Symbol beserta rentang barisnya (1-based, inklusif)."""

    name: str
    kind: str
    start_line: int
    end_line: int
    parent: Optional[str] = None
    signature: Optional[str] = None

    @property
    def qualified_name(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name

    def to_dict(self) -> dict:
        data = {
            "name": self.name,
            "kind": self.kind,
            "qualified_name": self.qualified_name,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }
        if self.parent:
            data["parent"] = self.parent
        if self.signature:
            data["signature"] = self.signature
        return data


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _python_block_end(lines: List[str], start_idx: int, indent: int) -> int:
    """Baris akhir blok Python (indentasi) mulai dari `start_idx` (0-based)."""
    end_idx = start_idx
    index = start_idx + 1
    while index < len(lines):
        raw = lines[index]
        if not raw.strip():
            index += 1
            continue
        if _indent_width(raw) <= indent:
            break
        end_idx = index
        index += 1
    return end_idx


def _brace_block_end(lines: List[str], start_idx: int) -> int:
    """Baris akhir blok brace-based mulai dari `start_idx` (0-based)."""
    depth = 0
    started = False
    last = start_idx
    limit = min(len(lines), start_idx + _BRACE_SCAN_LIMIT)
    index = start_idx
    while index < limit:
        for ch in lines[index]:
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
                if started and depth <= 0:
                    return index
        if started:
            last = index
        elif index - start_idx >= 3:
            # Tidak ada blok brace dekat deklarasi (mis. signature ';').
            return start_idx
        index += 1
    return last


def detect_language_name(path: str) -> Optional[str]:
    """Nama bahasa (codeindex) untuk `path`, atau None bila tak dikenali."""
    lang = _language_registry().detect(path)
    return lang.name if lang is not None else None


def extract_symbols(path: str, source: str) -> List[SymbolSpan]:
    """Ekstrak symbol + rentang baris dari `source` (deterministik)."""
    lines = source.splitlines()
    if not lines:
        return []

    lang = _language_registry().detect(path)
    if lang is not None:
        parser = lang.parser
        style = lang.name
    else:
        # Fallback aman & ringkas: parser generic regex existing (bahasa tak
        # dikenali, mis. .vue/.svelte). Tidak membuat parser baru.
        from agent_ai.codeindex.parsers.generic import GenericParser

        parser = GenericParser("generic")
        style = "generic"

    try:
        raw_symbols, _ = parser.parse(path, source)
    except Exception:  # noqa: BLE001 - parsing tidak boleh menggagalkan read
        return []

    spans: List[SymbolSpan] = []
    for symbol in raw_symbols:
        start_idx = int(getattr(symbol, "line", 0)) - 1
        if start_idx < 0 or start_idx >= len(lines):
            continue
        if style == "python":
            end_idx = _python_block_end(lines, start_idx, _indent_width(lines[start_idx]))
        else:
            end_idx = _brace_block_end(lines, start_idx)
        kind = getattr(symbol.kind, "value", str(symbol.kind))
        spans.append(
            SymbolSpan(
                name=symbol.name,
                kind=kind,
                start_line=start_idx + 1,
                end_line=max(start_idx, end_idx) + 1,
                parent=symbol.parent,
                signature=symbol.signature,
            )
        )
    return spans


def find_symbols(path: str, source: str, name: str) -> List[SymbolSpan]:
    """Cari symbol berdasarkan nama (atau 'Parent.name'). Exact match."""
    spans = extract_symbols(path, source)
    if "." in name:
        return [s for s in spans if s.qualified_name == name]
    return [s for s in spans if s.name == name]


def enclosing_symbol(spans: List[SymbolSpan], line: int) -> Optional[SymbolSpan]:
    """Symbol paling dalam yang mencakup `line` (untuk anotasi search_code)."""
    best: Optional[SymbolSpan] = None
    for span in spans:
        if span.start_line <= line <= span.end_line:
            if best is None or span.start_line >= best.start_line:
                best = span
    return best


def build_outline(spans: List[SymbolSpan]) -> str:
    """Render outline ringkas (teks) dari daftar symbol."""
    rows: List[str] = []
    for span in spans:
        if span.parent:
            rows.append(f"  {span.name}  line {span.start_line}")
        elif span.kind in ("class", "interface", "struct", "enum"):
            rows.append(f"{span.kind} {span.name}  line {span.start_line}")
        else:
            rows.append(f"{span.name}  line {span.start_line}")
    return "\n".join(rows)


def structure_payload(path: str, source: str, total_lines: int) -> dict:
    """Payload untuk `read_file(mode='structure')`: outline tanpa body source."""
    spans = extract_symbols(path, source)
    language = detect_language_name(path)
    payload: dict = {
        "path": path,
        "language": language,
        "total_lines": total_lines,
        "symbol_count": len(spans),
        "outline": build_outline(spans),
    }
    if not spans:
        payload["note"] = (
            "Struktur symbol tidak tersedia untuk file ini "
            "(format tak dikenali atau tidak ada deklarasi)."
        )
    return payload


__all__ = [
    "SymbolSpan",
    "extract_symbols",
    "find_symbols",
    "enclosing_symbol",
    "build_outline",
    "structure_payload",
    "detect_language_name",
]
