"""Read cache ringan (per task/session) untuk mendeteksi read_file duplikat.

Tujuan: menghindari mengirim source yang SAMA dua kali ke LLM dalam satu
task/session. Ini BUKAN "second brain" dan bukan cache lintas-task:

    - Scope = SATU instance per task/session. Instance dibuat fresh oleh
      `build_registry()` / `build_consultant_registry()` (dipanggil per task),
      BUKAN singleton global. Tool yang dikonstruksi langsung tanpa cache
      (mis. di script verifier) berperilaku persis seperti sebelumnya
      (dedup tidak aktif) -> backward compatible.
    - Dua rentang BERBEDA tidak pernah dianggap duplikat: kunci = (start, end).
    - Konten yang berubah (mis. setelah edit_file) TIDAK dianggap duplikat:
      selain (start, end), dibandingkan juga digest konten yang benar-benar
      dikirim. `edit_file`/`write_file`/`delete_file`/`move_file` juga
      memanggil `invalidate()` untuk path yang berubah.

Modul ini murni struktur data (tanpa I/O) dan tidak menyimpan source lintas
task.
"""

from __future__ import annotations

import hashlib
from typing import Dict, Optional, Tuple

#: Kunci satu rentang baca: (start_line, end_line) 1-based inklusif.
RangeKey = Tuple[Optional[int], Optional[int]]


class ToolReadCache:
    """Catatan read_file per task: path -> {rentang -> digest konten}."""

    def __init__(self) -> None:
        self._ranges: Dict[str, Dict[RangeKey, str]] = {}

    # ------------------------------------------------------------------ #
    # Digest
    # ------------------------------------------------------------------ #
    @staticmethod
    def _digest(content: str) -> str:
        return hashlib.sha1(content.encode("utf-8", "replace")).hexdigest()

    # ------------------------------------------------------------------ #
    # Query / record
    # ------------------------------------------------------------------ #
    def is_duplicate(
        self,
        rel_path: str,
        start: Optional[int],
        end: Optional[int],
        content: str,
    ) -> bool:
        """True bila (path, start, end) sudah pernah dibaca dengan konten IDENTIK."""
        entry = self._ranges.get(rel_path)
        if not entry:
            return False
        stored = entry.get((start, end))
        if stored is None:
            return False
        return stored == self._digest(content)

    def record(
        self,
        rel_path: str,
        start: Optional[int],
        end: Optional[int],
        content: str,
    ) -> None:
        """Catat bahwa (path, start, end) sudah dikirim dengan konten `content`."""
        self._ranges.setdefault(rel_path, {})[(start, end)] = self._digest(content)

    # ------------------------------------------------------------------ #
    # Invalidation
    # ------------------------------------------------------------------ #
    def invalidate(self, rel_path: Optional[str]) -> None:
        """Lupakan seluruh rentang untuk satu path (dipakai setelah mutasi file)."""
        if not rel_path:
            return
        self._ranges.pop(rel_path, None)

    def reset(self) -> None:
        """Kosongkan seluruh cache (scope tetap satu instance/task)."""
        self._ranges.clear()

    def __len__(self) -> int:  # pragma: no cover - introspection
        return sum(len(v) for v in self._ranges.values())


__all__ = ["ToolReadCache", "RangeKey"]
