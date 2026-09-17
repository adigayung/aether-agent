"""Repository Map: representasi ringkas repository.

Ringan dan TIDAK berisi seluruh source code. Memakai Code Index +
RepositoryIntelligence yang sudah ada.

Isi:
    - files, languages
    - symbols (ringkasan)
    - imports (ringkasan)
    - relationships (imports/contains/references)
    - entry points (deteksi deterministik)
"""

from __future__ import annotations

from typing import List, Optional

from agent_ai.codeindex.index import CodeIndex
from agent_ai.codeindex.relations import RepositoryIntelligence
from agent_ai.repointel.models import EntryPoint, RepositoryMap

#: Nama file yang dianggap entry point umum (deterministik).
_ENTRY_FILENAMES = {
    "main.py": "python_main",
    "app.py": "python_app",
    "manage.py": "django_manage",
    "cli.py": "python_cli",
    "__main__.py": "dunder_main",
    "index.js": "js_index",
    "index.ts": "ts_index",
    "main.go": "go_main",
    "main.rs": "rust_main",
}


class RepositoryMapBuilder:
    """Membangun RepositoryMap ringkas dari CodeIndex.

    Args:
        index: CodeIndex yang sudah dibangun.
        intelligence: RepositoryIntelligence opsional.
    """

    def __init__(
        self,
        index: CodeIndex,
        intelligence: Optional[RepositoryIntelligence] = None,
    ) -> None:
        self.index = index
        self.intelligence = intelligence or RepositoryIntelligence(index)

    def build(self, include_relationships: bool = True) -> RepositoryMap:
        """Bangun RepositoryMap (deterministik, tanpa source code)."""
        symbols = [
            {
                "name": s.name,
                "kind": s.kind.value,
                "file": s.file,
                "line": s.line,
                "parent": s.parent,
            }
            for s in sorted(self.index.all_symbols(), key=lambda s: (s.file, s.line, s.name))
        ]
        imports = [
            {"module": i.module, "file": i.file, "line": i.line, "names": list(i.names)}
            for i in sorted(self.index.all_imports(), key=lambda i: (i.file, i.line, i.module))
        ]

        relationships = {}
        if include_relationships:
            relationships = self.intelligence.to_dict()

        return RepositoryMap(
            root=self.index.root,
            files=self.index.paths(),
            languages=self.index.languages(),
            symbols=symbols,
            imports=imports,
            relationships=relationships,
            entry_points=self.detect_entry_points(),
            stats=self.index.stats(),
        )

    def detect_entry_points(self) -> List[EntryPoint]:
        """Deteksi entry point secara deterministik.

        Heuristik:
            - nama file cocok dengan daftar entry point umum.
            - file Python yang punya blok `if __name__ == "__main__"`.
        """
        entry_points: List[EntryPoint] = []
        for entry in self.index.files:
            filename = entry.path.split("/")[-1]
            if filename in _ENTRY_FILENAMES:
                entry_points.append(
                    EntryPoint(
                        path=entry.path,
                        kind=_ENTRY_FILENAMES[filename],
                        reason=f"nama file '{filename}'",
                    )
                )
                continue
            if entry.language == "python" and self._has_main_guard(entry.path):
                entry_points.append(
                    EntryPoint(
                        path=entry.path,
                        kind="python_main_guard",
                        reason='blok if __name__ == "__main__"',
                    )
                )
        entry_points.sort(key=lambda e: (e.path, e.kind))
        return entry_points

    def _has_main_guard(self, path: str) -> bool:
        """Cek blok `if __name__ == "__main__"` (deterministik, tanpa eksekusi)."""
        import os

        full = os.path.join(self.index.root, path)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                source = fh.read()
        except OSError:
            return False
        return '__name__' in source and '__main__' in source
