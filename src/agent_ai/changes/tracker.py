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
        # task_id -> {rel_path: (hash, size)} snapshot
        self._snapshots: Dict[str, Dict[str, tuple]] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self, task_id: str) -> ChangeSet:
        """Mulai tracking untuk sebuah task_id.

        Raises:
            ValueError: bila task_id kosong.
        """
        if not task_id:
            raise ValueError("ChangeTracker butuh task_id.")
        change_set = ChangeSet(task_id=task_id)
        self._sets[task_id] = change_set
        self._snapshots[task_id] = {}
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
        current = self.snapshot(path)  # tidak menyimpan ke snapshot task

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
            else:
                before_hash, before_size = before[rel]
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

        # Deleted.
        for rel, (before_hash, before_size) in before.items():
            if rel not in current:
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
