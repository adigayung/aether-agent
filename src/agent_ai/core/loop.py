"""Agent Loop (fondasi).

Mengelola siklus iteration/action/observation dengan status yang jelas dan
batas maksimum iterasi. Loop ini TIDAK memanggil provider, TIDAK mengeksekusi
tool, dan TIDAK memparse response model secara otomatis.

Caller (atau orkestrator di masa depan) yang menggerakkan loop:

    loop = AgentLoop(task="...", max_iterations=5)
    loop.start()
    action = AgentAction(name="read_file", arguments={"path": "x"})
    loop.record_action(action)          # status -> WAITING
    loop.record_observation(obs)        # status -> RUNNING
    loop.finish(result="...")           # status -> DONE
"""

from __future__ import annotations

from typing import Any, List, Optional

from agent_ai.core.models import (
    AgentAction,
    AgentObservation,
    AgentState,
    AgentStatus,
    AgentStep,
)


class MaxIterationsExceeded(Exception):
    """Dilempar bila loop melewati batas maksimum iterasi."""


class AgentLoop:
    """State machine minimal untuk Agent Loop.

    Args:
        task: task awal.
        max_iterations: batas maksimum iterasi (harus >= 1).
    """

    def __init__(self, task: str = "", max_iterations: int = 10) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations harus >= 1.")
        self.state = AgentState(task=task, max_iterations=max_iterations)

    # ------------------------------------------------------------------ #
    # Status helpers
    # ------------------------------------------------------------------ #
    @property
    def status(self) -> AgentStatus:
        return self.state.status

    @property
    def iteration(self) -> int:
        return self.state.iteration

    @property
    def is_finished(self) -> bool:
        return self.state.is_finished

    def start(self) -> AgentState:
        """Mulai loop (status -> RUNNING)."""
        if self.state.is_finished:
            raise RuntimeError("Loop sudah selesai; tidak bisa di-start ulang.")
        self.state.status = AgentStatus.RUNNING
        return self.state

    # ------------------------------------------------------------------ #
    # Step management
    # ------------------------------------------------------------------ #
    def _ensure_running(self) -> None:
        if self.state.is_finished:
            raise RuntimeError(f"Loop sudah selesai (status={self.state.status.value}).")
        if self.state.iteration >= self.state.max_iterations:
            self.fail("Batas maksimum iterasi tercapai.")
            raise MaxIterationsExceeded(
                f"Melebihi max_iterations={self.state.max_iterations}."
            )

    def record_action(self, action: AgentAction) -> AgentStep:
        """Catat action sebagai step baru (status -> WAITING).

        Raises:
            MaxIterationsExceeded: bila batas iterasi tercapai.
            RuntimeError: bila loop sudah selesai.
        """
        self._ensure_running()
        step = AgentStep(index=self.state.iteration, action=action)
        self.state.steps.append(step)
        self.state.status = AgentStatus.WAITING
        return step

    def record_observation(self, observation: AgentObservation) -> AgentStep:
        """Lampirkan observation ke step terakhir (status -> RUNNING).

        Raises:
            RuntimeError: bila belum ada action atau loop sudah selesai.
        """
        if self.state.is_finished:
            raise RuntimeError(f"Loop sudah selesai (status={self.state.status.value}).")
        if not self.state.steps:
            raise RuntimeError("Belum ada action; tidak bisa mencatat observation.")
        step = self.state.steps[-1]
        step.observation = observation
        self.state.status = AgentStatus.RUNNING
        return step

    # ------------------------------------------------------------------ #
    # Termination
    # ------------------------------------------------------------------ #
    def finish(self, result: Optional[str] = None) -> AgentState:
        """Selesaikan loop dengan sukses (status -> DONE)."""
        self.state.status = AgentStatus.DONE
        self.state.result = result
        return self.state

    def fail(self, error: str) -> AgentState:
        """Selesaikan loop dengan error (status -> FAILED)."""
        self.state.status = AgentStatus.FAILED
        self.state.error = error
        return self.state

    def cancel(self, reason: Optional[str] = None) -> AgentState:
        """Hentikan loop secara kooperatif (status -> CANCELLED).

        Dipakai saat pembatalan (user stop) terdeteksi di safe boundary.
        Ini BUKAN kegagalan: status dibedakan agar runtime/gateway dapat
        melaporkan CANCELLED (bukan FAILED) dan tidak melakukan retry.
        """
        self.state.status = AgentStatus.CANCELLED
        self.state.error = reason
        return self.state

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def steps(self) -> List[AgentStep]:
        """Daftar step yang sudah tercatat."""
        return list(self.state.steps)

    def to_dict(self) -> dict:
        """Representasi state loop."""
        return self.state.to_dict()

    def __repr__(self) -> str:  # pragma: no cover - bantuan debug
        return (
            f"<AgentLoop status={self.state.status.value} "
            f"iteration={self.state.iteration}/{self.state.max_iterations}>"
        )
