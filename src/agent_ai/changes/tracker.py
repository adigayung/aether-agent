"""ChangeTracker: melacak perubahan filesystem selama satu task.

Provider-agnostic. Berbasis kondisi filesystem (hashing + size), BUKAN
sekadar mencatat action tool. Read-only terhadap source project: tracker
tidak pernah menulis/mengubah file.

Isolasi task: setiap task_id punya ChangeSet sendiri.

Tidak membuat Git dependency, database, atau filesystem engine baru.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Dict, List, Optional

from agent_ai.changes.models import ChangeRecord, ChangeSet, ChangeType
from agent_ai.tools.filesystem import _iter_files, _resolve_within_root


def _hash_file(path: Path) -> Optional[str]:
    """Hash konten file (sha1). None bila file tidak ada/bukan file."""
    if not path.is_file():
        return None
    h = hashlib.sha1()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _file_size(path: Path) -> Optional[int]:
    """Ukuran file. None bila tidak ada/bukan file."""
    if not path.is_file():
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None


class ChangeTracker:
    """Melacak perubahan filesystem per task.

    Args:
        root: root workspace (boundary). Default: root project.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        from agent_ai.tools.filesystem import _DEFAULT_ROOT

        self.root = Path(root) if root else _DEFAULT_ROOT
        # task_id -> ChangeSet
        self._sets: Dict[str, ChangeSet] = {}
        # task_id -> {rel_path: (hash, size)} snapshot (kondisi "sebelum")
        self._snapshots: Dict[str, Dict[str, tuple]] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self, task_id: str) -> ChangeSet:
        """Mulai tracking untuk sebuah task_id (idempoten).

        Aman dipanggil ULANG untuk task_id yang sama (mis. gateway menyiapkan
        baseline lewat `snapshot(".", task_id)` lalu runtime memanggil `start`
        lagi saat eksekusi berjalan). Baseline snapshot yang SUDAH ada TIDAK
        dihapus: menghapusnya akan membuat file yang sudah dikenal terlihat
        'created' (perubahan semu). Untuk task_id BARU, ChangeSet dan snapshot
        dimulai dari kosong.

        Raises:
            ValueError: bila task_id kosong.
        """
        if not task_id:
            raise ValueError("ChangeTracker butuh task_id.")
        existing = self._sets.get(task_id)
        if existing is not None:
            # Sudah pernah di-start: pertahankan ChangeSet + baseline snapshot.
            self._snapshots.setdefault(task_id, {})
            return existing
        change_set = ChangeSet(task_id=task_id)
        self._sets[task_id] = change_set
        self._snapshots.setdefault(task_id, {})
        return change_set

    def finish(self, task_id: str) -> Optional[ChangeSet]:
        """Selesaikan tracking untuk task_id (set finished_at)."""
        change_set = self._sets.get(task_id)
        if change_set is None:
            return None
        change_set.finished_at = time.time()
        return change_set

    def get_changes(self, task_id: str) -> Optional[ChangeSet]:
        """Ambil ChangeSet untuk task_id (None bila tidak ada)."""
        return self._sets.get(task_id)

    def tracked_paths(self, task_id: str) -> List[str]:
        """Path kandidat yang sudah tercatat untuk task (bounded, deterministik).

        Dipakai untuk mendeteksi file yang dibuat lewat jalur yang TIDAK
        melaporkan path (mis. `run_command`): hasil command di-scan HANYA pada
        prefix path yang sudah menjadi kandidat task, bukan seluruh project.
        Path di luar scope task tidak pernah dilaporkan (tidak mengarang
        daftar perubahan).
        """
        return list(self._snapshots.get(task_id, {}))

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #
    def snapshot(self, path: str, task_id: Optional[str] = None) -> Dict[str, tuple]:
        """Ambil snapshot (hash, size) untuk sebuah path.

        Args:
            path: path relatif terhadap root (file atau directory).
            task_id: bila diisi, snapshot disimpan untuk task tersebut.

        Returns:
            Dict {rel_path: (hash, size)}. Untuk file tunggal, satu entri.
        """
        target = _resolve_within_root(path, self.root)
        result: Dict[str, tuple] = {}

        if target.is_file():
            rel = str(target.relative_to(self.root.resolve()))
            result[rel] = (_hash_file(target), _file_size(target))
        elif target.is_dir():
            for file_path in _iter_files(target):
                rel = str(file_path.relative_to(self.root.resolve()))
                result[rel] = (_hash_file(file_path), _file_size(file_path))

        if task_id is not None:
            self._snapshots.setdefault(task_id, {}).update(result)
        return result

    # ------------------------------------------------------------------ #
    # Detection
    # ------------------------------------------------------------------ #
    def detect_changes(
        self,
        task_id: str,
        path: str = ".",
    ) -> List[ChangeRecord]:
        """Deteksi perubahan dengan membandingkan snapshot awal vs kondisi kini.

        Read-only: hanya membaca filesystem, tidak mengubah file.

        Args:
            task_id: task yang dilacak.
            path: path yang diperiksa (default seluruh root).

        Returns:
            Daftar ChangeRecord (created/modified/deleted).
        """
        before = self._snapshots.get(task_id, {})
        current = self.snapshot(path)  # disimpan sebagai basis, bukan keputusan

        records: List[ChangeRecord] = []
        now = time.time()

        # Created & modified.
        for rel, (after_hash, after_size) in current.items():
            if rel not in before:
                records.append(ChangeRecord(
                    path=rel,
                    change_type=ChangeType.CREATED,
                    before_hash=None,
                    after_hash=after_hash,
                    before_size=None,
                    after_size=after_size,
                    timestamp=now,
                ))
                continue
            base = before[rel]
            before_hash, before_size = base[0], base[1]
            # Perubahan ditentukan dari KONTEN yang tersedia (hash + ukuran),
            # bukan timestamp/mtime: hash berbeda -> isi file benar-benar
            # berubah (created/modified/deleted). Bila hash sama, file identik
            # walau mtime berubah -> TIDAK dilaporkan.
            if before_hash != after_hash:
                records.append(ChangeRecord(
                    path=rel,
                    change_type=ChangeType.MODIFIED,
                    before_hash=before_hash,
                    after_hash=after_hash,
                    before_size=before_size,
                    after_size=after_size,
                    timestamp=now,
                ))

        # Deleted. Perbandingan DIBATASI pada scope `path` yang diperiksa:
        # snapshot task dapat memuat file yang di-track untuk path yang lebih
        # sempit (mis. snapshot per-file saat tool mutasi), sehingga file di
        # luar scope tidak boleh dilaporkan 'deleted' hanya karena tidak
        # muncul pada pemindaian sub-path ini.
        scope = _resolve_within_root(path, self.root)
        for rel, base in before.items():
            if rel in current:
                continue
            before_hash, before_size = base[0], base[1]
            try:
                candidate = _resolve_within_root(rel, self.root)
            except Exception:  # noqa: BLE001 - path di luar root diabaikan
                continue
            if candidate != scope and scope not in candidate.parents:
                continue
            records.append(ChangeRecord(
                path=rel,
                change_type=ChangeType.DELETED,
                before_hash=before_hash,
                after_hash=None,
                before_size=before_size,
                after_size=None,
                timestamp=now,
            ))

        return sorted(records, key=lambda r: r.path)

    def record_change(self, task_id: str, record: ChangeRecord) -> None:
        """Catat satu ChangeRecord secara eksplisit ke ChangeSet task.

        Raises:
            KeyError: bila task_id belum di-start.
        """
        change_set = self._sets.get(task_id)
        if change_set is None:
            raise KeyError(f"Task '{task_id}' belum di-start.")
        change_set.changes.append(record)

    def track(self, task_id: str, path: str = ".") -> List[ChangeRecord]:
        """Deteksi perubahan dan catat ke ChangeSet task.

        Convenience: detect_changes + record_change untuk tiap record.

        Returns:
            Daftar ChangeRecord yang baru dicatat.
        """
        records = self.detect_changes(task_id, path=path)
        for record in records:
            self.record_change(task_id, record)
        return records
