"""Parser generic (regex-based) untuk bahasa non-Python.

Deterministik dan tanpa dependency eksternal. Cukup untuk menemukan
class/function/method/import pada PHP, JS/TS, Java/Kotlin, C#, C/C++.

Parser ini sengaja konservatif: hanya pola deklarasi umum yang dikenali.
"""

from __future__ import annotations

import re
from typing import List, Tuple

from agent_ai.codeindex.models import Import, Symbol, SymbolKind
from agent_ai.codeindex.parsers.base import LanguageParser

# Pola deklarasi class/struct/interface/enum (umum lintas bahasa).
_CLASS_RE = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+|internal\s+|abstract\s+|final\s+|"
    r"static\s+|export\s+|default\s+)*"
    r"(class|interface|struct|enum)\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)

# Pola deklarasi function/method (umum lintas bahasa).
_FUNC_RE = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+|internal\s+|static\s+|final\s+|"
    r"abstract\s+|virtual\s+|override\s+|async\s+|export\s+|function\s+|def\s+|"
    r"fun\s+)*"
    r"(?:[A-Za-z_][\w<>\[\],\.\?\s]*\s+)?"
    r"([A-Za-z_]\w*)\s*\([^;{]*\)\s*(?::\s*[\w<>\[\],\.\?\s]+)?\s*\{",
    re.MULTILINE,
)

# Import umum: import ... / use ... / #include ...
_IMPORT_RES = [
    re.compile(r"^\s*import\s+([\w\.\*\{\}\s,]+?)\s*;?\s*$", re.MULTILINE),
    re.compile(r"^\s*from\s+([\w\.]+)\s+import\s+([\w\*\{\}\s,]+)", re.MULTILINE),
    re.compile(r"^\s*use\s+([\w\\]+)", re.MULTILINE),
    re.compile(r"^\s*#include\s+[<\"]([^>\"]+)[>\"]", re.MULTILINE),
    re.compile(r"^\s*require\s*\(?\s*['\"]([^'\"]+)['\"]", re.MULTILINE),
]

# Kata kunci yang bukan nama function.
_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "sizeof", "new",
    "do", "else", "try", "case", "default", "foreach", "function",
}


class GenericParser(LanguageParser):
    """Parser regex-based generic untuk bahasa non-Python."""

    def __init__(self, language: str) -> None:
        self.language = language

    def parse(self, path: str, source: str) -> Tuple[List[Symbol], List[Import]]:
        symbols: List[Symbol] = []
        imports: List[Import] = []

        for match in _CLASS_RE.finditer(source):
            kind_word, name = match.group(1), match.group(2)
            kind = {
                "class": SymbolKind.CLASS,
                "interface": SymbolKind.INTERFACE,
                "struct": SymbolKind.STRUCT,
                "enum": SymbolKind.ENUM,
            }[kind_word]
            symbols.append(
                Symbol(
                    name=name,
                    kind=kind,
                    file=path,
                    line=self._line_of(source, match.start()),
                )
            )

        for match in _FUNC_RE.finditer(source):
            name = match.group(1)
            if name in _KEYWORDS:
                continue
            symbols.append(
                Symbol(
                    name=name,
                    kind=SymbolKind.FUNCTION,
                    file=path,
                    line=self._line_of(source, match.start()),
                )
            )

        for regex in _IMPORT_RES:
            for match in regex.finditer(source):
                module = match.group(1).strip()
                if not module:
                    continue
                imports.append(
                    Import(
                        module=module,
                        file=path,
                        line=self._line_of(source, match.start()),
                    )
                )

        return symbols, imports

    @staticmethod
    def _line_of(source: str, index: int) -> int:
        return source.count("\n", 0, index) + 1
