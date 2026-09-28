"""File Write Lock — shared in-process lock for AETHER write tools.

Tujuan: mencegah dua Agent/Task paralel melakukan operasi write pada file
yang sama secara bersamaan melalui AETHER write tools (write_file,
edit_file, delete_file, move_file).

Karakteristik:
    - Shared singleton: SATU mekanisme untuk semua Agent dalam process/runtime
      yang sama. Tidak ada AgentLockManager / TaskLockManager terpisah.
    - Lock sementara selama operasi write berlangsung (bukan selama task).
    - Atomic acquire: check + set di dalam satu mutex, sehingga tidak ada race
      ``check-free -> lock`` yang bisa dilalui dua Agent bersamaan.
    - Release wajib via finally, termasuk saat write gagal.
    - Normalisasi path konsisten: ``os.path.normcase(os.path.normpath(...))``
      + posix slash, sehingga ``src/example.py`` dan ``./src/example.py``
      dianggap file yang sama. Untuk akurasi lintas root, caller mengirim
      absolute path yang sudah di-resolve (``Path.resolve()``); normalisasi
      tetap diterapkan.
    - Tidak ada ownership subsystem terpisah; hanya ``path -> owner`` mapping.
    - Read tools tetap bebas; run_command tidak diblokir.

Tidak membuat transaction system, tidak ada auto-retry, tidak ada state
task baru. LLM tetap pengambil keputusan setelah menerima error lock.
"""

from __future__ import annotations

import os
import threading
import contextvars
from typing import Dict, Optional

# ContextVar opsional untuk task_id / owner identity yang dikirim runtime.
# Bila tidak di-set, fallback ke thread identifier.
_current_task_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "aether_file_lock_task_id", default=None
)


def set_current_task_id(task_id: Optional[str]):
    """Set owner task_id untuk context saat ini (opsional, best-effort)."""
    return _current_task_id.set(task_id)


def get_current_owner() -> str:
    """Ambil identitas owner untuk operasi saat ini."""
    tid = _current_task_id.get(None)
    if tid:
        return str(tid)
    return f"thread-{threading.get_ident()}"


def _normalize_key(path: str) -> str:
    """Normalisasi path untuk kunci lock (cross-path konsisten).

    Menggunakan ``os.path.normcase`` + ``os.path.normpath`` yang sudah
    tersedia di AETHER (tool_coordinator), lalu posix slash.
    """
    text = str(path).strip()
    if not text:
        return text
    try:
        text = os.path.normcase(os.path.normpath(text))
    except Exception:
        pass
    return text.replace("\\", "/").strip()


class FileWriteLockManager:
    """Manager lock file tunggal (shared, thread-safe)."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._locked: Dict[str, str] = {}  # normalized_path -> owner

    def _key(self, path: str) -> str:
        return _normalize_key(path)

    def acquire(self, path: str, owner: Optional[str] = None) -> bool:
        """Coba acquire lock untuk ``path``. Atomic. True bila berhasil."""
        if owner is None:
            owner = get_current_owner()
        key = self._key(path)
        with self._mutex:
            if key in self._locked:
                return False
            self._locked[key] = owner
            return True

    def release(self, path: str, owner: Optional[str] = None) -> None:
        """Lepas lock untuk ``path`` bila owner cocok (atau tanpa check bila None)."""
        key = self._key(path)
        with self._mutex:
            current = self._locked.get(key)
            if current is None:
                return
            # Hanya owner yang boleh melepas; bila owner None, lepas apa adanya
            # (dipakai cleanup best-effort). Bila owner mismatch, jangan lepas
            # agar tidak melepas lock milik task lain.
            if owner is not None and current != owner:
                return
            self._locked.pop(key, None)

    def is_locked(self, path: str) -> bool:
        key = self._key(path)
        with self._mutex:
            return key in self._locked

    def get_owner(self, path: str) -> Optional[str]:
        key = self._key(path)
        with self._mutex:
            return self._locked.get(key)

    def acquire_multiple(self, paths: list[str], owner: Optional[str] = None) -> bool:
        """Atomically acquire semua ``paths`` (sorted untuk hindari deadlock).

        Jika salah satu sudah locked, tidak ada yang di-lock (no partial).
        """
        if owner is None:
            owner = get_current_owner()
        keys = [_normalize_key(p) for p in paths]
        # Deduplicate dan sort untuk deadlock avoidance
        unique_sorted = sorted(set(keys))
        with self._mutex:
            for k in unique_sorted:
                if k in self._locked:
                    return False
            for k in unique_sorted:
                self._locked[k] = owner
            return True

    def release_multiple(self, paths: list[str], owner: Optional[str] = None) -> None:
        keys = [_normalize_key(p) for p in paths]
        for k in set(keys):
            self.release(k, owner)

    def _clear_all(self) -> None:
        """Hapus semua lock (hanya untuk test)."""
        with self._mutex:
            self._locked.clear()


# Singleton global — SATU mekanisme untuk semua Agent dalam process.
manager = FileWriteLockManager()

# Alias fungsional untuk kemudahan impor
acquire = manager.acquire
release = manager.release
is_locked = manager.is_locked
get_owner = manager.get_owner
