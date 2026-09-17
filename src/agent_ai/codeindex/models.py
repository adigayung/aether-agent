"""Model data untuk Code Index.

Representasi terstruktur (deterministik, tanpa LLM/embeddings) dari sebuah
project: file, symbol (class/function/method), import, dan relasi import.

Semua model bersifat plain data (dataclass) agar mudah di-serialize/rebuild.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SymbolKind(str, Enum):
    """Jenis symbol yang diindeks."""

    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    INTERFACE = "interface"
    STRUCT = "struct"
    ENUM = "enum"
    VARIABLE = "variable"


@dataclass
class Symbol:
    """Sebuah symbol (class/function/method/...) beserta lokasinya.

    Attributes:
        name: nama symbol.
        kind: jenis symbol.
        file: path file relatif terhadap project root.
        line: nomor baris (1-based) tempat symbol didefinisikan.
        parent: nama class/container induk (untuk method), opsional.
        signature: signature ringkas (opsional, bila mudah diperoleh).
    """

    name: str
    kind: SymbolKind
    file: str
    line: int
    parent: Optional[str] = None
    signature: Optional[str] = None

    @property
    def qualified_name(self) -> str:
        """Nama lengkap (Parent.name bila ada)."""
        return f"{self.parent}.{self.name}" if self.parent else self.name

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "file": self.file,
            "line": self.line,
            "parent": self.parent,
            "signature": self.signature,
        }


@dataclass
class Import:
    """Sebuah import/module dependency yang terdeteksi secara deterministik.

    Attributes:
        module: nama module/package yang diimpor.
        file: path file relatif terhadap project root.
        line: nomor baris (1-based).
        names: nama-nama yang diimpor (opsional).
        level: jumlah leading dot untuk relative import (0 = absolute).
    """

    module: str
    file: str
    line: int
    names: List[str] = field(default_factory=list)
    level: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module": self.module,
            "file": self.file,
            "line": self.line,
            "names": self.names,
            "level": self.level,
        }


@dataclass
class FileEntry:
    """Entri satu file dalam index.

    Attributes:
        path: path file relatif terhadap project root.
        language: nama bahasa (mis. "python").
        symbols: daftar symbol di file ini.
        imports: daftar import di file ini.
    """

    path: str
    language: str
    symbols: List[Symbol] = field(default_factory=list)
    imports: List[Import] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "language": self.language,
            "symbols": [s.to_dict() for s in self.symbols],
            "imports": [i.to_dict() for i in self.imports],
        }

