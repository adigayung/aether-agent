"""CodeIndexer: membangun CodeIndex dari sebuah project.

Read-only, deterministik, dapat di-rebuild. Tidak menjalankan command project,
tidak memakai LLM/embeddings/vector DB/SQLite.

Layer ini terpisah dari filesystem.py dan Project Discovery.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from agent_ai.codeindex.index import CodeIndex
from agent_ai.codeindex.languages import LanguageRegistry
from agent_ai.codeindex.models import FileEntry

# Directory yang selalu diabaikan saat indexing.
_IGNORED_DIRS = {
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "env",
    "node_modules",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".cache",
    "cache",
}

# Batas ukuran file agar tidak membaca file raksasa.
_MAX_FILE_BYTES = 1_000_000  # 1 MB


class CodeIndexer:
    """Membangun CodeIndex dari project root.

    Args:
        root: project root yang akan diindeks.
        languages: LanguageRegistry (default: bawaan).
        ignored_dirs: override set directory yang diabaikan.
    """

    def __init__(
        self,
        root: Path,
        languages: Optional[LanguageRegistry] = None,
        ignored_dirs: Optional[set] = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.languages = languages or LanguageRegistry.default()
        self.ignored_dirs = set(ignored_dirs) if ignored_dirs is not None else set(_IGNORED_DIRS)

    def build(self) -> CodeIndex:
        """Bangun CodeIndex dari project (deterministik, urut berdasarkan path)."""
        index = CodeIndex(root=str(self.root))
        for file_path in self._iter_source_files():
            rel = file_path.relative_to(self.root).as_posix()
            language = self.languages.detect(rel)
            if language is None:
                continue
            try:
                source = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            symbols, imports = language.parser.parse(rel, source)
            index.files.append(
                FileEntry(
                    path=rel,
                    language=language.name,
                    symbols=symbols,
                    imports=imports,
                )
            )
        index.files.sort(key=lambda f: f.path)
        return index

    def _iter_source_files(self) -> List[Path]:
        """Iterasi file source, melewati directory yang diabaikan."""
        results: List[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in self.ignored_dirs)
            for filename in sorted(filenames):
                path = Path(dirpath) / filename
                try:
                    if path.stat().st_size > _MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                results.append(path)
        return results
