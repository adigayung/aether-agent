"""CodeIndex: container hasil indexing + query/search.

Read-only terhadap source project. Deterministik. Dapat di-rebuild.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent_ai.codeindex.models import FileEntry, Import, Symbol, SymbolKind


@dataclass
class CodeIndex:
    """Index terstruktur sebuah project.

    Attributes:
        root: path project root (absolut) saat indexing.
        files: daftar FileEntry (urut berdasarkan path).
    """

    root: str = ""
    files: List[FileEntry] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Lookup dasar
    # ------------------------------------------------------------------ #
    def file(self, path: str) -> Optional[FileEntry]:
        """Ambil FileEntry berdasarkan path relatif."""
        for entry in self.files:
            if entry.path == path:
                return entry
        return None

    def paths(self) -> List[str]:
        return [f.path for f in self.files]

    def languages(self) -> List[str]:
        return sorted({f.language for f in self.files})

    def all_symbols(self) -> List[Symbol]:
        return [s for f in self.files for s in f.symbols]

    def all_imports(self) -> List[Import]:
        return [i for f in self.files for i in f.imports]

    # ------------------------------------------------------------------ #
    # Query / search
    # ------------------------------------------------------------------ #
    def find_symbols(
        self,
        name: Optional[str] = None,
        kind: Optional[SymbolKind] = None,
        file: Optional[str] = None,
    ) -> List[Symbol]:
        """Cari symbol berdasarkan nama (substring, case-insensitive), kind, file."""
        results: List[Symbol] = []
        needle = name.lower() if name else None
        for entry in self.files:
            if file is not None and entry.path != file:
                continue
            for symbol in entry.symbols:
                if needle is not None and needle not in symbol.name.lower():
                    continue
                if kind is not None and symbol.kind != kind:
                    continue
                results.append(symbol)
        return results

    def find_imports(self, module: Optional[str] = None) -> List[Import]:
        """Cari import berdasarkan nama module (substring, case-insensitive)."""
        needle = module.lower() if module else None
        results: List[Import] = []
        for entry in self.files:
            for imp in entry.imports:
                if needle is not None and needle not in imp.module.lower():
                    continue
                results.append(imp)
        return results

    def search(self, query: str) -> Dict[str, Any]:
        """Query gabungan: symbol + import yang cocok dengan `query`."""
        return {
            "query": query,
            "symbols": [s.to_dict() for s in self.find_symbols(name=query)],
            "imports": [i.to_dict() for i in self.find_imports(module=query)],
        }

    # ------------------------------------------------------------------ #
    # Repository Intelligence (relasi antar-code)
    # ------------------------------------------------------------------ #
    def _intelligence(self):
        """Lazy RepositoryIntelligence facade (import lokal, hindari siklus)."""
        from agent_ai.codeindex.relations import RepositoryIntelligence

        if getattr(self, "_ri_cache", None) is None:
            self._ri_cache = RepositoryIntelligence(self)
        return self._ri_cache

    def resolve_imports(self, file: Optional[str] = None) -> List[Dict[str, Any]]:
        """Resolve import (semua file atau satu file) ke target file."""
        return [r.to_dict() for r in self._intelligence().resolve_imports(file=file)]

    def dependencies(self, file: str) -> List[str]:
        """File yang diimpor oleh `file` (target file yang ter-resolve)."""
        return self._intelligence().dependencies(file)

    def dependents(self, file: str) -> List[str]:
        """File yang mengimpor `file` (reverse dependency)."""
        return self._intelligence().dependents(file)
    def related_symbols(self, symbol: str) -> Dict[str, Any]:
        """Gabungkan lokasi definisi symbol + reference yang ditemukan."""
        return self._intelligence().related_symbols(symbol)

    def graph(self) -> Dict[str, Any]:
        """Graph relasi sederhana (imports/contains/references)."""
        return self._intelligence().to_dict()

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "file_count": len(self.files),
            "languages": self.languages(),
            "files": [f.to_dict() for f in self.files],
        }

    def stats(self) -> Dict[str, Any]:
        return {
            "files": len(self.files),
            "symbols": len(self.all_symbols()),
            "imports": len(self.all_imports()),
            "languages": self.languages(),
        }

