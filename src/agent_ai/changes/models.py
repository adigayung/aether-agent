"""Model untuk Change/Diff Tracking Subsystem.

Provider-agnostic. Mencatat perubahan filesystem selama satu task tanpa
Git/database.

    ChangeRecord -> satu perubahan file
    ChangeSet    -> kumpulan perubahan untuk satu task_id
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ChangeType(str, Enum):
    """Jenis perubahan file."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass
class ChangeRecord:
    """Satu perubahan file.

    Attributes:
        path: path relatif terhadap root workspace.
        change_type: created/modified/deleted.
        before_hash: hash konten sebelum (None bila file baru).
        after_hash: hash konten sesudah (None bila file dihapus).
        before_size: ukuran sebelum (None bila file baru).
        after_size: ukuran sesudah (None bila file dihapus).
        timestamp: waktu perubahan (epoch detik).
        metadata: info tambahan bebas.
    """

    path: str
    change_type: ChangeType
    before_hash: Optional[str] = None
    after_hash: Optional[str] = None
    before_size: Optional[int] = None
    after_size: Optional[int] = None
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "change_type": self.change_type.value,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "before_size": self.before_size,
            "after_size": self.after_size,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


@dataclass
class ChangeSet:
    """Kumpulan perubahan untuk satu task.

    Attributes:
        task_id: identifier task (isolasi antar task).
        changes: daftar ChangeRecord.
        started_at: waktu mulai tracking.
        finished_at: waktu selesai tracking (None bila belum selesai).
    """

    task_id: str
    changes: List[ChangeRecord] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    @property
    def is_finished(self) -> bool:
        return self.finished_at is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "changes": [c.to_dict() for c in self.changes],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
