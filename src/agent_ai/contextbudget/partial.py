"""Partial File Reading: baca bagian file yang relevan saja.

Mendukung:
    - line range (start_line/end_line)
    - symbol/range (berdasarkan Symbol dari Code Index)
    - relevant section (sekitar baris relevan)

Reuse `_resolve_within_root()` dari tools/filesystem.py untuk boundary
keamanan. Read-only, deterministik.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_ai.tools.filesystem import _resolve_within_root


@dataclass
class PartialRead:
    """Hasil partial read sebuah file.

    Attributes:
        path: path relatif.
        start_line: baris awal (1-based, inklusif).
        end_line: baris akhir (1-based, inklusif).
        total_lines: total baris file.
        content: isi yang dibaca.
        truncated: True bila dipotong karena limit.
    """

    path: str
    start_line: int
    end_line: int
    total_lines: int
    content: str
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "total_lines": self.total_lines,
            "truncated": self.truncated,
        }


class PartialReader:
    """Membaca bagian relevan dari file (read-only, boundary-aware).

    Args:
        root: root workspace (boundary).
        default_limit: batas jumlah baris default untuk partial read.
    """

    def __init__(self, root: Path, default_limit: int = 200) -> None:
        self.root = Path(root)
        self.default_limit = max(int(default_limit), 1)

    def read_lines(
        self,
        path: str,
        start_line: int = 1,
        end_line: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> Optional[PartialRead]:
        """Baca rentang baris dari file.

        Returns:
            PartialRead, atau None bila file tidak ada/tidak bisa dibaca.
        """
        target = _resolve_within_root(path, self.root)
        if not target.is_file():
            return None
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        lines = text.splitlines()
        total = len(lines)
        start = max(int(start_line), 1)
        end = int(end_line) if end_line is not None else total
        end = min(end, total)
        if end < start:
            end = start

        cap = limit if limit is not None else self.default_limit
        truncated = False
        if end - start + 1 > cap:
            end = start + cap - 1
            truncated = True

        content = "\n".join(lines[start - 1 : end])
        return PartialRead(
            path=path,
            start_line=start,
            end_line=end,
            total_lines=total,
            content=content,
            truncated=truncated,
        )

    def read_symbol(
        self,
        path: str,
        symbol_line: int,
        symbol_end_line: Optional[int] = None,
        context: int = 0,
        limit: Optional[int] = None,
    ) -> Optional[PartialRead]:
        """Baca bagian file di sekitar sebuah symbol.

        Args:
            path: path file.
            symbol_line: baris definisi symbol (1-based).
            symbol_end_line: baris akhir symbol (bila diketahui).
            context: jumlah baris konteks sebelum/sesudah.
            limit: batas baris.
        """
        start = max(int(symbol_line) - context, 1)
        if symbol_end_line is not None:
            end = int(symbol_end_line) + context
        else:
            cap = limit if limit is not None else self.default_limit
            end = start + cap - 1
        return self.read_lines(path, start_line=start, end_line=end, limit=limit)

    def read_section(
        self,
        path: str,
        center_line: int,
        before: int = 20,
        after: int = 20,
        limit: Optional[int] = None,
    ) -> Optional[PartialRead]:
        """Baca bagian relevan di sekitar sebuah baris."""
        start = max(int(center_line) - before, 1)
        end = int(center_line) + after
        return self.read_lines(path, start_line=start, end_line=end, limit=limit)
