"""TaskLifecycle: state machine eksplisit untuk lifecycle sebuah task.

Provider-agnostic. Mengelola identitas task + transition status yang valid,
timestamp, error/failure info, dan final result metadata.

Ini orchestration metadata, BUKAN pengganti AgentLoop/Runtime/Reliability/
Validation/Changes/Planning. Tidak ada database/persistence.

Transition yang diizinkan:

    CREATED    -> PREPARING, CANCELLED, FAILED
    PREPARING  -> PLANNING, RUNNING, FAILED, CANCELLED
    PLANNING   -> RUNNING, FAILED, CANCELLED
    RUNNING    -> VALIDATING, COMPLETED, FAILED, CANCELLED
    VALIDATING -> COMPLETED, FAILED, CANCELLED
    COMPLETED  -> (terminal)
    FAILED     -> (terminal)
    CANCELLED  -> (terminal)
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from agent_ai.tasks.models import (
    TERMINAL_STATUSES,
    TaskPhase,
    TaskState,
    TaskStatus,
    new_task_id,
)

#: Peta transition yang valid: status -> himpunan status tujuan.
_ALLOWED_TRANSITIONS: Dict[TaskStatus, frozenset] = {
    TaskStatus.CREATED: frozenset({TaskStatus.PREPARING, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.PREPARING: frozenset({
        TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.PLANNING: frozenset({
        TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.VALIDATING, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.VALIDATING: frozenset({
        TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

#: Fase default untuk tiap status.
_STATUS_PHASE: Dict[TaskStatus, TaskPhase] = {
    TaskStatus.CREATED: TaskPhase.NONE,
    TaskStatus.PREPARING: TaskPhase.PREPARATION,
    TaskStatus.PLANNING: TaskPhase.PLANNING,
    TaskStatus.RUNNING: TaskPhase.EXECUTION,
    TaskStatus.VALIDATING: TaskPhase.VALIDATION,
    TaskStatus.COMPLETED: TaskPhase.FINALIZATION,
    TaskStatus.FAILED: TaskPhase.FINALIZATION,
    TaskStatus.CANCELLED: TaskPhase.FINALIZATION,
}


class InvalidTransitionError(ValueError):
    """Transition status task tidak valid."""


class TaskLifecycle:
    """Mengelola lifecycle satu task.

    Args:
        task: deskripsi task.
        task_id: identifier opsional (default: dibuat otomatis).
        metadata: execution metadata awal (opsional).
    """

    def __init__(
        self,
        task: str,
        task_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not task or not task.strip():
            raise ValueError("TaskLifecycle butuh task yang tidak kosong.")
        now = time.time()
        self._state = TaskState(
            task_id=task_id or new_task_id(),
            task=task.strip(),
            status=TaskStatus.CREATED,
            phase=TaskPhase.NONE,
            created_at=now,
            updated_at=now,
            timestamps={TaskStatus.CREATED.value: now},
            metadata=dict(metadata or {}),
        )

    # ------------------------------------------------------------------ #
    # Identity / snapshot
    # ------------------------------------------------------------------ #
    @property
    def task_id(self) -> str:
        return self._state.task_id

    @property
    def status(self) -> TaskStatus:
        return self._state.status

    @property
    def phase(self) -> TaskPhase:
        return self._state.phase

    @property
    def is_terminal(self) -> bool:
        return self._state.is_terminal

    def snapshot(self) -> TaskState:
        """Kembalikan salinan state saat ini (agar tidak dimutasi dari luar)."""
        s = self._state
        return TaskState(
            task_id=s.task_id,
            task=s.task,
            status=s.status,
            phase=s.phase,
            created_at=s.created_at,
            updated_at=s.updated_at,
            timestamps=dict(s.timestamps),
            metadata=dict(s.metadata),
            result=s.result,
            error=s.error,
            failure=dict(s.failure) if s.failure else None,
        )

    def to_dict(self) -> Dict[str, Any]:
        return self._state.to_dict()

    # ------------------------------------------------------------------ #
    # Transitions
    # ------------------------------------------------------------------ #
    def can_transition(self, to: TaskStatus) -> bool:
        """True bila transition dari status saat ini ke `to` diizinkan."""
        return to in _ALLOWED_TRANSITIONS.get(self._state.status, frozenset())

    def transition(
        self,
        to: TaskStatus,
        *,
        phase: Optional[TaskPhase] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TaskState:
        """Lakukan transition status secara eksplisit.

        Args:
            to: status tujuan.
            phase: fase detail (opsional; default mengikuti status).
            metadata: metadata tambahan yang digabung ke state.

        Returns:
            Snapshot state setelah transition.

        Raises:
            InvalidTransitionError: bila transition tidak valid.
        """
        if not self.can_transition(to):
            raise InvalidTransitionError(
                f"Transition tidak valid: {self._state.status.value} -> {to.value}."
            )
        now = time.time()
        self._state.status = to
        self._state.phase = phase or _STATUS_PHASE.get(to, TaskPhase.NONE)
        self._state.updated_at = now
        self._state.timestamps[to.value] = now
        if metadata:
            self._state.metadata.update(metadata)
        return self.snapshot()

    def set_phase(self, phase: TaskPhase) -> None:
        """Set fase detail tanpa mengubah status (mis. TOOL_EXECUTION)."""
        self._state.phase = phase
        self._state.updated_at = time.time()

    def update_metadata(self, metadata: Dict[str, Any]) -> None:
        """Gabungkan metadata ke state."""
        self._state.metadata.update(metadata or {})
        self._state.updated_at = time.time()

    # ------------------------------------------------------------------ #
    # Terminal helpers
    # ------------------------------------------------------------------ #
    def complete(self, result: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> TaskState:
        """Tandai task COMPLETED dengan hasil akhir.

        Raises:
            InvalidTransitionError: bila transition tidak valid.
        """
        state = self.transition(TaskStatus.COMPLETED, metadata=metadata)
        self._state.result = result
        return self.snapshot()

    def fail(
        self,
        error: str,
        *,
        failure: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TaskState:
        """Tandai task FAILED dengan informasi kegagalan.

        Raises:
            InvalidTransitionError: bila transition tidak valid.
        """
        self.transition(TaskStatus.FAILED, metadata=metadata)
        self._state.error = error
        self._state.failure = dict(failure) if failure else {"error": error}
        return self.snapshot()

    def cancel(self, reason: Optional[str] = None) -> TaskState:
        """Tandai task CANCELLED.

        Raises:
            InvalidTransitionError: bila transition tidak valid.
        """
        self.transition(TaskStatus.CANCELLED)
        if reason:
            self._state.metadata["cancel_reason"] = reason
        return self.snapshot()
