"""Model untuk Session & Execution Event Architecture.

Provider-agnostic. Fondasi untuk session history, execution history,
resume/recovery, dan UI event stream di masa depan — tanpa database/UI.

Relasi:

    Session
      └── Task (reference: task_id)
            └── Execution (events)

Session TIDAK menggantikan TaskLifecycle. Session hanya mengelompokkan task
dan menyimpan event operasional.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SessionStatus(str, Enum):
    """Status lifecycle sebuah session."""

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskReference:
    """Referensi ringan ke sebuah task di dalam session.

    Bukan pengganti TaskLifecycle; hanya pointer (task_id) + metadata ringkas.

    Attributes:
        task_id: identifier task (sama dengan TaskLifecycle.task_id).
        created_at: waktu referensi dibuat.
        metadata: info tambahan bebas (mis. deskripsi task).
    """

    task_id: str
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }


@dataclass
class Session:
    """Session: kumpulan task dalam satu konteks kerja.

    Attributes:
        session_id: identifier unik session.
        project_id: referensi project (opsional).
        created_at: waktu session dibuat.
        updated_at: waktu update terakhir.
        status: status session.
        metadata: info tambahan bebas.
        tasks: daftar TaskReference (task yang tergabung).
    """

    session_id: str
    project_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    status: SessionStatus = SessionStatus.ACTIVE
    metadata: Dict[str, Any] = field(default_factory=dict)
    tasks: List[TaskReference] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "project_id": self.project_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status.value,
            "metadata": self.metadata,
            "tasks": [t.to_dict() for t in self.tasks],
        }


def new_session_id() -> str:
    """Buat session_id unik."""
    return uuid.uuid4().hex
