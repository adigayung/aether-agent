"""Repository Intelligence dasar untuk Code Index.

Menambahkan relasi antar-code TANPA embeddings/RAG/LLM/vector DB/SQLite:

    - Import -> target file resolution (prioritas Python, dukung relative import).
    - Symbol reference (cari penggunaan nama symbol di source repository).
    - File dependency (dependencies/dependents).
    - related_symbols (gabungan lokasi symbol + reference).
    - Repository graph sederhana:
          File --imports--> File
          File --contains--> Symbol
          File --references--> Symbol

Semua operasi deterministik, read-only, dan rebuildable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from agent_ai.codeindex.index import CodeIndex
from agent_ai.codeindex.models import Symbol


# ---------------------------------------------------------------------------
# Model relasi
# ---------------------------------------------------------------------------
@dataclass
class Reference:
    """Sebuah penggunaan (reference) nama symbol di source repository.

    Attributes:
        name: nama symbol yang direferensikan.
        file: path file relatif tempat reference ditemukan.
        line: nomor baris (1-based).
        text: potongan baris (opsional, untuk konteks).
    """

    name: str
    file: str
    line: int
    text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "file": self.file, "line": self.line, "text": self.text}


@dataclass
class ImportResolution:
    """Hasil resolusi sebuah import ke target file (bila berhasil).

    Attributes:
        module: nama module yang diimpor (seperti tercatat di index).
        file: file sumber yang melakukan import.
        line: baris import.
        target: path file target relatif (None bila tidak ter-resolve).
        resolved: apakah resolusi berhasil.
        reason: alasan bila tidak ter-resolve (mis. "ambiguous", "not_found").
    """

    module: str
    file: str
    line: int
    target: Optional[str] = None
    resolved: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module": self.module,
            "file": self.file,
            "line": self.line,
            "target": self.target,
            "resolved": self.resolved,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Resolver import -> file
# ---------------------------------------------------------------------------
class ImportResolver:
    """Resolve import Python ke file target di dalam repository.

    Strategi (deterministik, tanpa eksekusi):
        - Absolute import: "app.utils" -> "app/utils.py" atau "app/utils/__init__.py".
        - Relative import: "from .utils import x" -> relatif terhadap file sumber.
        - Bila lebih dari satu kandidat -> ambigu, tidak dipaksa (resolved=False).
        - Bila tidak ada kandidat -> not_found.
    """

    def __init__(self, index: CodeIndex) -> None:
        self.index = index
        self._paths: Set[str] = set(index.paths())

    def resolve(self, module: str, source_file: str, level: int = 0) -> Tuple[Optional[str], str]:
        """Resolve module ke target file.

        Args:
            module: nama module (mis. "app.utils" atau "utils").
            source_file: file yang melakukan import (untuk relative import).
            level: jumlah leading dot untuk relative import (0 = absolute).

        Returns:
            Tuple (target_path | None, reason). reason kosong bila berhasil.
        """
        candidates = self._candidates(module, source_file, level)
        if not candidates:
            return None, "not_found"
        if len(candidates) > 1:
            return None, "ambiguous"
        return candidates[0], ""

    def resolve_import(self, imp, source_file: str) -> Tuple[Optional[str], str]:
        """Resolve sebuah `Import` (memakai level + names bila perlu).

        Menangani `from . import sibling` (module kosong): coba resolve
        ke submodule bernama sama dengan nama yang diimpor.
        """
        level = getattr(imp, "level", 0) or 0
        module = imp.module or ""

        if module:
            return self.resolve(module, source_file, level)

        # module kosong: `from . import a, b` -> coba tiap nama sebagai submodule.
        if level > 0 and getattr(imp, "names", None):
            found: List[str] = []
            for name in imp.names:
                target, _ = self.resolve(name, source_file, level)
                if target and target not in found:
                    found.append(target)
            if len(found) == 1:
                return found[0], ""
            if len(found) > 1:
                return None, "ambiguous"
            return None, "not_found"

        return None, "not_found"

    def _candidates(self, module: str, source_file: str, level: int) -> List[str]:
        parts = [p for p in module.split(".") if p] if module else []

        if level > 0:
            base_dir = self._relative_base(source_file, level)
            if base_dir is None:
                return []
            prefix = base_dir
        else:
            prefix = ""

        rel_parts = ([prefix] if prefix else []) + parts
        rel = "/".join(rel_parts)
        return self._match_paths(rel)

    @staticmethod
    def _relative_base(source_file: str, level: int) -> Optional[str]:
        """Direktori dasar untuk relative import (level = jumlah dot)."""
        # level 1 = direktori file itu sendiri; level 2 = naik satu, dst.
        dir_parts = source_file.split("/")[:-1]
        up = level - 1
        if up > len(dir_parts):
            return None
        if up > 0:
            dir_parts = dir_parts[:-up]
        return "/".join(dir_parts)

    def _match_paths(self, rel: str) -> List[str]:
        """Cari file yang cocok dengan module path relatif."""
        if not rel:
            return []
        matches: List[str] = []
        py_file = f"{rel}.py"
        pyi_file = f"{rel}.pyi"
        pkg_init = f"{rel}/__init__.py"
        for candidate in (py_file, pyi_file, pkg_init):
            if candidate in self._paths:
                matches.append(candidate)
        return matches


# ---------------------------------------------------------------------------
# Reference finder
# ---------------------------------------------------------------------------
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ReferenceFinder:
    """Mencari penggunaan nama symbol di source repository (deterministik).

    Reference sederhana berbasis token nama (bukan call graph sempurna).
    Definisi symbol sendiri tidak dihitung sebagai reference.
    """

    def __init__(self, index: CodeIndex, root: Optional[str] = None) -> None:
        self.index = index
        self.root = root or index.root

    def find(self, name: str, include_definitions: bool = False) -> List[Reference]:
        """Cari semua reference `name` di seluruh file yang terindeks."""
        if not name:
            return []
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        definition_sites = {
            (s.file, s.line)
            for s in self.index.find_symbols(name=name)
            if s.name == name
        }
        results: List[Reference] = []
        for entry in self.index.files:
            source = self._read(entry.path)
            if source is None:
                continue
            for lineno, line in enumerate(source.splitlines(), start=1):
                if not pattern.search(line):
                    continue
                if not include_definitions and (entry.path, lineno) in definition_sites:
                    continue
                results.append(
                    Reference(name=name, file=entry.path, line=lineno, text=line.strip())
                )
        return results

    def _read(self, path: str) -> Optional[str]:
        import os

        full = os.path.join(self.root, path)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None


# ---------------------------------------------------------------------------
# Repository graph
# ---------------------------------------------------------------------------
@dataclass
class RepositoryGraph:
    """Graph relasi sederhana antar file & symbol.

    Edge:
        - imports:    file -> file
        - contains:   file -> symbol (qualified_name)
        - references: file -> symbol (qualified_name)
    """

    file_imports: Dict[str, List[str]] = field(default_factory=dict)
    file_contains: Dict[str, List[str]] = field(default_factory=dict)
    file_references: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_imports": {k: list(v) for k, v in self.file_imports.items()},
            "file_contains": {k: list(v) for k, v in self.file_contains.items()},
            "file_references": {k: list(v) for k, v in self.file_references.items()},
        }


# ---------------------------------------------------------------------------
# Repository Intelligence facade
# ---------------------------------------------------------------------------
class RepositoryIntelligence:
    """Facade query relasi antar-code di atas CodeIndex.

    Contoh:
        ri = RepositoryIntelligence(index)
        ri.dependencies("app/main.py")   # -> ["app/service.py", ...]
        ri.dependents("app/models.py")   # -> ["app/service.py"]
        ri.related_symbols("helper")     # -> {definition, references}
    """

    def __init__(self, index: CodeIndex) -> None:
        self.index = index
        self.resolver = ImportResolver(index)
        self.references = ReferenceFinder(index)
        self._graph: Optional[RepositoryGraph] = None

    # -- import resolution --------------------------------------------------
    def resolve_imports(self, file: Optional[str] = None) -> List[ImportResolution]:
        """Resolve import (semua file atau satu file) ke target file."""
        results: List[ImportResolution] = []
        for entry in self.index.files:
            if file is not None and entry.path != file:
                continue
            for imp in entry.imports:
                target, reason = self.resolver.resolve_import(imp, entry.path)
                results.append(
                    ImportResolution(
                        module=imp.module,
                        file=entry.path,
                        line=imp.line,
                        target=target,
                        resolved=target is not None,
                        reason=reason,
                    )
                )
        return results

    @staticmethod
    def _relative_level(imp) -> int:
        """Level relative import dari sebuah Import (0 = absolute)."""
        return getattr(imp, "level", 0) or 0

    # -- file dependency ----------------------------------------------------
    def dependencies(self, file: str) -> List[str]:
        """File yang diimpor oleh `file` (target file yang ter-resolve)."""
        deps: List[str] = []
        seen: Set[str] = set()
        for res in self.resolve_imports(file=file):
            if res.resolved and res.target and res.target not in seen:
                seen.add(res.target)
                deps.append(res.target)
        return sorted(deps)

    def dependents(self, file: str) -> List[str]:
        """File yang mengimpor `file` (reverse dependency)."""
        deps: List[str] = []
        seen: Set[str] = set()
        for res in self.resolve_imports():
            if res.resolved and res.target == file and res.file not in seen:
                seen.add(res.file)
                deps.append(res.file)
        return sorted(deps)

    # -- symbol relations ---------------------------------------------------
    def related_symbols(self, symbol: str) -> Dict[str, Any]:
        """Gabungkan lokasi definisi symbol + reference yang ditemukan."""
        definitions = [s for s in self.index.find_symbols(name=symbol) if s.name == symbol]
        refs = self.references.find(symbol)
        return {
            "symbol": symbol,
            "definitions": [s.to_dict() for s in definitions],
            "references": [r.to_dict() for r in refs],
        }

    # -- graph --------------------------------------------------------------
    def graph(self) -> RepositoryGraph:
        """Bangun graph relasi (deterministik, tanpa duplikat)."""
        if self._graph is not None:
            return self._graph

        graph = RepositoryGraph()
        for entry in self.index.files:
            graph.file_contains[entry.path] = sorted(
                {s.qualified_name for s in entry.symbols}
            )
            graph.file_imports[entry.path] = self.dependencies(entry.path)

        # references: file -> symbol (hanya symbol yang terdefinisi di repo).
        symbol_names = sorted({s.name for s in self.index.all_symbols()})
        for name in symbol_names:
            for ref in self.references.find(name):
                bucket = graph.file_references.setdefault(ref.file, [])
                if name not in bucket:
                    bucket.append(name)
        for path in graph.file_references:
            graph.file_references[path] = sorted(graph.file_references[path])

        self._graph = graph
        return graph

    def to_dict(self) -> Dict[str, Any]:
        return self.graph().to_dict()
