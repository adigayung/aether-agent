"""Model untuk Agent Loop.

Mendefinisikan protocol/state foundation untuk siklus:

    LLM -> action -> observation -> context -> LLM

Model di sini provider-agnostic dan tidak melakukan parsing format rapuh.
Belum ada autonomous tool calling atau eksekusi tool otomatis.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AgentStatus(str, Enum):
    """Status siklus Agent Loop."""

    RUNNING = "running"    # loop sedang berjalan
    WAITING = "waiting"    # menunggu observation/tool result
    DONE = "done"          # selesai dengan sukses
    FAILED = "failed"      # selesai dengan error


@dataclass
class AgentAction:
    """Aksi yang direpresentasikan dari response model.

    Attributes:
        name: nama aksi/tool (mis. "read_file", "final_answer").
        arguments: argumen aksi (dict).
        thought: alasan/penjelasan opsional dari model.
        raw: response mentah model (untuk audit, tidak diparse rapuh).
        id: identifier unik aksi.
    """

    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    thought: Optional[str] = None
    raw: Any = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
            "thought": self.thought,
        }


@dataclass
class AgentObservation:
    """Hasil pengamatan dari eksekusi action/tool.

    Attributes:
        action_id: id AgentAction yang menghasilkan observation ini.
        content: isi hasil (teks/objek).
        success: apakah aksi berhasil.
        error: pesan error bila gagal.
        metadata: info tambahan bebas.
        id: identifier unik observation.
    """

    content: Any = None
    action_id: Optional[str] = None
    success: bool = True
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "action_id": self.action_id,
            "success": self.success,
            "error": self.error,
            "content": self.content,
            "metadata": self.metadata,
        }


@dataclass
class AgentStep:
    """Satu iterasi: action + observation (observation bisa None bila belum ada)."""

    index: int
    action: Optional[AgentAction] = None
    observation: Optional[AgentObservation] = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "index": self.index,
            "action": self.action.to_dict() if self.action else None,
            "observation": self.observation.to_dict() if self.observation else None,
        }


@dataclass
class AgentState:
    """State keseluruhan Agent Loop.

    Attributes:
        task: task awal yang diberikan caller.
        status: status siklus (RUNNING/WAITING/DONE/FAILED).
        steps: daftar AgentStep.
        max_iterations: batas maksimum iterasi (anti infinite loop).
        result: hasil akhir (mis. teks jawaban) bila DONE.
        error: pesan error bila FAILED.
    """

    task: str = ""
    status: AgentStatus = AgentStatus.RUNNING
    steps: List[AgentStep] = field(default_factory=list)
    max_iterations: int = 10
    result: Optional[str] = None
    error: Optional[str] = None

    @property
    def iteration(self) -> int:
        """Jumlah iterasi yang sudah dijalankan."""
        return len(self.steps)

    @property
    def is_finished(self) -> bool:
        """True bila status sudah DONE atau FAILED."""
        return self.status in (AgentStatus.DONE, AgentStatus.FAILED)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "status": self.status.value,
            "iteration": self.iteration,
            "max_iterations": self.max_iterations,
            "result": self.result,
            "error": self.error,
            "steps": [step.to_dict() for step in self.steps],
        }
