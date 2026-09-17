"""Definisi bahasa + registry parser adapter.

Abstraction ini memungkinkan penambahan parser bahasa baru tanpa mengubah
core index. Setiap bahasa diwakili oleh `Language` (ekstensi + adapter).

    from agent_ai.codeindex.languages import LanguageRegistry

    registry = LanguageRegistry.default()
    lang = registry.detect("app/main.py")   # -> Language("python", ...)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from agent_ai.codeindex.parsers.base import LanguageParser


@dataclass
class Language:
    """Sebuah bahasa + parser adapter-nya.

    Attributes:
        name: nama bahasa (mis. "python").
        extensions: daftar ekstensi file (mis. [".py"]).
        parser: adapter parser untuk bahasa ini.
    """

    name: str
    extensions: List[str]
    parser: LanguageParser


class LanguageRegistry:
    """Registry bahasa berdasarkan ekstensi file."""

    def __init__(self) -> None:
        self._by_ext: Dict[str, Language] = {}
        self._by_name: Dict[str, Language] = {}

    def register(self, language: Language) -> None:
        """Daftarkan sebuah bahasa."""
        self._by_name[language.name] = language
        for ext in language.extensions:
            self._by_ext[ext.lower()] = language

    def detect(self, path: str) -> Optional[Language]:
        """Deteksi bahasa dari path file (berdasarkan ekstensi)."""
        lower = path.lower()
        for ext, lang in self._by_ext.items():
            if lower.endswith(ext):
                return lang
        return None

    def get(self, name: str) -> Optional[Language]:
        return self._by_name.get(name)

    def languages(self) -> List[str]:
        return sorted(self._by_name)

    def extensions(self) -> List[str]:
        return sorted(self._by_ext)

    @classmethod
    def default(cls) -> "LanguageRegistry":
        """Registry default dengan parser bawaan."""
        from agent_ai.codeindex.parsers.generic import GenericParser
        from agent_ai.codeindex.parsers.python import PythonParser

        registry = cls()
        registry.register(
            Language(name="python", extensions=[".py", ".pyi"], parser=PythonParser())
        )
        # Bahasa lain memakai parser generic (regex-based, deterministik).
        registry.register(
            Language(
                name="php",
                extensions=[".php"],
                parser=GenericParser("php"),
            )
        )
        registry.register(
            Language(
                name="javascript",
                extensions=[".js", ".jsx", ".mjs", ".cjs"],
                parser=GenericParser("javascript"),
            )
        )
        registry.register(
            Language(
                name="typescript",
                extensions=[".ts", ".tsx"],
                parser=GenericParser("typescript"),
            )
        )
        registry.register(
            Language(
                name="java",
                extensions=[".java"],
                parser=GenericParser("java"),
            )
        )
        registry.register(
            Language(
                name="kotlin",
                extensions=[".kt", ".kts"],
                parser=GenericParser("kotlin"),
            )
        )
        registry.register(
            Language(
                name="csharp",
                extensions=[".cs"],
                parser=GenericParser("csharp"),
            )
        )
        registry.register(
            Language(
                name="c",
                extensions=[".c", ".h"],
                parser=GenericParser("c"),
            )
        )
        registry.register(
            Language(
                name="cpp",
                extensions=[".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"],
                parser=GenericParser("cpp"),
            )
        )
        return registry
