"""Model untuk Repository Intelligence v2.

Provider-agnostic, deterministik, read-only. Plain data (dataclass) agar
mudah di-serialize/rebuild. TIDAK ada embeddings/vector DB/SQLite/LLM.

    SymbolRelation      -> relasi symbol (definition/reference/import/dependency)
    DependencyNode      -> node hasil dependency expansion (file + depth)
    EntryPoint          -> kandidat entry point yang terdeteksi deterministik
    RepositoryMap       -> representasi ringkas repository (tanpa source code)
    DependencyQueryResult -> hasil query dependency yang bounded
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class RelationKind(str, Enum):
    """Jenis relasi symbol/file."""

    DEFINITION = "definition"
    REFERENCE = "reference"
    IMPORT = "import"
    DEPENDENCY = "dependency"


@dataclass
class SymbolRelation:
    """Satu relasi yang melibatkan sebuah symbol.

    Attributes:
        symbol: nama symbol.
        kind: jenis relasi.
        file: file tempat relasi berada.
        line: baris (1-based), 0 bila tidak relevan.
        target: target relasi (mis. file dependency), opsional.
        detail: info tambahan bebas.
    """

    symbol: str
    kind: RelationKind
    file: str
    line: int = 0
    target: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "kind": self.kind.value,
            "file": self.file,
            "line": self.line,
            "target": self.target,
            "detail": self.detail,
        }


@dataclass
class DependencyNode:
    """Node hasil dependency expansion.

    Attributes:
        path: path file relatif.
        depth: jarak dari file asal (0 = file asal).
        direction: "forward" (dependency) atau "backward" (dependent).
        via: file perantara yang menyebabkan node ini masuk (opsional).
    """

    path: str
    depth: int = 0
    direction: str = "forward"
    via: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "depth": self.depth,
            "direction": self.direction,
            "via": self.via,
        }


@dataclass
class EntryPoint:
    """Kandidat entry point yang terdeteksi secara deterministik.

    Attributes:
        path: path file relatif.
        kind: jenis entry point (mis. "python_main", "dunder_main", "index").
        reason: alasan deteksi.
    """

    path: str
    kind: str
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "reason": self.reason}


@dataclass
class RepositoryMap:
    """Representasi ringkas repository (tanpa source code).

    Attributes:
        root: project root.
        files: daftar path file.
        languages: daftar bahasa.
        symbols: ringkasan symbol (name/kind/file/line).
        imports: ringkasan import (module/file/line).
        relationships: ringkasan relasi (imports/contains/references).
        entry_points: kandidat entry point.
        stats: statistik ringkas.
    """

    root: str = ""
    files: List[str] = field(default_factory=list)
    languages: List[str] = field(default_factory=list)
    symbols: List[Dict[str, Any]] = field(default_factory=list)
    imports: List[Dict[str, Any]] = field(default_factory=list)
    relationships: Dict[str, Any] = field(default_factory=dict)
    entry_points: List[EntryPoint] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "files": list(self.files),
            "languages": list(self.languages),
            "symbols": list(self.symbols),
            "imports": list(self.imports),
            "relationships": self.relationships,
            "entry_points": [e.to_dict() for e in self.entry_points],
            "stats": self.stats,
        }


@dataclass
class DependencyQueryResult:
    """Hasil query dependency yang bounded & deterministik.

    Attributes:
        target: target query (file atau symbol).
        target_type: "file" atau "symbol".
        files: file terkait (urut deterministik).
        symbols: symbol terkait (bila target symbol).
        dependencies: file yang menjadi dependency target.
        dependents: file yang bergantung pada target.
        relations: relasi symbol (definition/reference/import/dependency).
        truncated: True bila hasil dipotong karena batas.
        metadata: info batas/statistik.
    """

    target: str
    target_type: str = "file"
    files: List[str] = field(default_factory=list)
    symbols: List[Dict[str, Any]] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    dependents: List[str] = field(default_factory=list)
    relations: List[SymbolRelation] = field(default_factory=list)
    truncated: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target": self.target,
            "target_type": self.target_type,
            "files": list(self.files),
            "symbols": list(self.symbols),
            "dependencies": list(self.dependencies),
            "dependents": list(self.dependents),
            "relations": [r.to_dict() for r in self.relations],
            "truncated": self.truncated,
            "metadata": self.metadata,
        }
