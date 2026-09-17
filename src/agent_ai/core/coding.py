"""Coding Task abstraction (ringan).

Representasi minimal untuk sebuah task coding yang dijalankan melalui
AgentOrchestrator:

    User Task -> CodingTask -> AgentOrchestrator -> LLM -> Tool -> Observation
        -> LLM -> ... -> Task Result

CodingTask TIDAK mengelola iteration/action/observation (itu tetap tugas
AgentLoop). Ia hanya membungkus request + status + progress + hasil.

    from agent_ai.core.coding import CodingTask

    task = CodingTask(request="Perbaiki bug pada calculator")
    task.start()
    ...
    task.complete(result="...", iterations=6)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent_ai.core.models import AgentStatus


@dataclass
class CodingTask:
    """Task coding ringan (request + status + progress + hasil).

    Attributes:
        request: permintaan/task dari user.
        status: status task (mengikuti AgentStatus).
        iterations: jumlah iterasi yang sudah dijalankan.
        result: hasil akhir bila selesai.
        error: pesan error bila gagal.
        steps: ringkasan langkah (opsional, dari orchestrator).
        metadata: info tambahan bebas.
    """

    request: str
    status: AgentStatus = AgentStatus.RUNNING
    iterations: int = 0
    result: Optional[str] = None
    error: Optional[str] = None
    steps: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> "CodingTask":
        """Tandai task mulai berjalan."""
        self.status = AgentStatus.RUNNING
        return self

    def update_progress(self, iterations: int) -> "CodingTask":
        """Perbarui progress iterasi."""
        self.iterations = iterations
        return self

    def complete(
        self,
        result: Optional[str] = None,
        iterations: Optional[int] = None,
        steps: Optional[List[Dict[str, Any]]] = None,
    ) -> "CodingTask":
        """Tandai task selesai dengan sukses."""
        self.status = AgentStatus.DONE
        self.result = result
        if iterations is not None:
            self.iterations = iterations
        if steps is not None:
            self.steps = steps
        return self

    def fail(
        self,
        error: str,
        iterations: Optional[int] = None,
        steps: Optional[List[Dict[str, Any]]] = None,
    ) -> "CodingTask":
        """Tandai task gagal."""
        self.status = AgentStatus.FAILED
        self.error = error
        if iterations is not None:
            self.iterations = iterations
        if steps is not None:
            self.steps = steps
        return self

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    @property
    def is_complete(self) -> bool:
        """True bila task sudah selesai (DONE/FAILED)."""
        return self.status in (AgentStatus.DONE, AgentStatus.FAILED)

    @property
    def success(self) -> bool:
        return self.status == AgentStatus.DONE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request": self.request,
            "status": self.status.value,
            "iterations": self.iterations,
            "result": self.result,
            "error": self.error,
            "steps": self.steps,
            "metadata": self.metadata,
        }
