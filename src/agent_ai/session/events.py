"""Execution Events: representasi kejadian operasional selama eksekusi.

Provider-agnostic, tool-agnostic. Event bersifat immutable dan append-only.

Event HANYA menyimpan operational metadata dan hasil yang memang diperlukan.
TIDAK menyimpan chain-of-thought.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class EventType(str, Enum):
    """Jenis event eksekusi."""

    TASK_CREATED = "task_created"
    TASK_STARTED = "task_started"
    PHASE_CHANGED = "phase_changed"
    # Commentary natural dari LLM (bukan log tool). Ditempatkan pada event
    # system yang sudah ada; BUKAN event system kedua.
    AGENT_COMMENTARY = "agent_commentary"
    TOOL_CALLED = "tool_called"
    TOOL_COMPLETED = "tool_completed"
    OBSERVATION_RECEIVED = "observation_received"
    PROVIDER_REQUEST = "provider_request"
    PROVIDER_RESPONSE = "provider_response"
    VALIDATION_STARTED = "validation_started"
    VALIDATION_COMPLETED = "validation_completed"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_COMPLETED = "recovery_completed"
    CHANGE_DETECTED = "change_detected"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"


@dataclass(frozen=True)
class ExecutionEvent:
    """Satu event eksekusi (immutable).

    Attributes:
        event_id: identifier unik event.
        session_id: session tempat event terjadi.
        task_id: task tempat event terjadi (opsional bila belum ada task).
        event_type: jenis event.
        timestamp: waktu event (epoch detik).
        payload: data operasional event (hasil/metadata yang diperlukan).
        sequence: nomor urut deterministik (diisi oleh store saat append).
    """

    event_id: str
    session_id: str
    task_id: Optional[str]
    event_type: EventType
    timestamp: float = field(default_factory=time.time)
    payload: Dict[str, Any] = field(default_factory=dict)
    sequence: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "payload": self.payload,
            "sequence": self.sequence,
        }


def new_event_id() -> str:
    """Buat event_id unik."""
    return uuid.uuid4().hex


def make_event(
    session_id: str,
    event_type: EventType,
    *,
    task_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    timestamp: Optional[float] = None,
) -> ExecutionEvent:
    """Bangun ExecutionEvent baru (event_id dibuat otomatis)."""
    return ExecutionEvent(
        event_id=new_event_id(),
        session_id=session_id,
        task_id=task_id,
        event_type=event_type,
        timestamp=timestamp if timestamp is not None else time.time(),
        payload=dict(payload or {}),
    )


#: Peta status TaskLifecycle -> EventType (untuk bridge opsional).
_STATUS_TO_EVENT: Dict[str, EventType] = {
    "created": EventType.TASK_CREATED,
    "preparing": EventType.TASK_STARTED,
    "planning": EventType.PHASE_CHANGED,
    "running": EventType.PHASE_CHANGED,
    "validating": EventType.VALIDATION_STARTED,
    "completed": EventType.TASK_COMPLETED,
    "failed": EventType.TASK_FAILED,
    "cancelled": EventType.TASK_CANCELLED,
}


def event_from_lifecycle_snapshot(session_id: str, snapshot: Any) -> ExecutionEvent:
    """Bangun event dari snapshot TaskState (bridge opsional, tanpa coupling).

    Menerima objek apa pun dengan atribut `task_id`, `status`, `phase`,
    `error`, `result` (yaitu TaskState). Tidak mengimpor tasks package agar
    tetap loose-coupled.

    Args:
        session_id: session tempat event dicatat.
        snapshot: TaskState (atau objek dengan atribut serupa).

    Returns:
        ExecutionEvent yang merepresentasikan status snapshot.
    """
    status_value = getattr(getattr(snapshot, "status", None), "value", None) or str(
        getattr(snapshot, "status", "")
    )
    event_type = _STATUS_TO_EVENT.get(status_value, EventType.PHASE_CHANGED)
    phase = getattr(getattr(snapshot, "phase", None), "value", None)
    payload: Dict[str, Any] = {"status": status_value}
    if phase is not None:
        payload["phase"] = phase
    error = getattr(snapshot, "error", None)
    if error:
        payload["error"] = error
    result = getattr(snapshot, "result", None)
    if result is not None:
        payload["result"] = result
    return make_event(
        session_id=session_id,
        event_type=event_type,
        task_id=getattr(snapshot, "task_id", None),
        payload=payload,
        timestamp=getattr(snapshot, "updated_at", None),
    )

