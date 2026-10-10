"""Model untuk Agent Runtime Layer.

Memisahkan model/status runtime dari execution logic (runtime.py).

Runtime menyatukan layer yang sudah ada menjadi coding agent nyata:

    PreparedTask -> AgentRuntime -> Plan Step -> LLM -> ToolExecutor
        -> Observation -> LLM -> ... -> DONE / FAILED

Model di sini plain data (dataclass), provider-agnostic, dan TIDAK menyimpan
hidden chain-of-thought.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class RuntimeStatus(str, Enum):
    """Status keseluruhan Agent Runtime."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    # Dihentikan secara kooperatif (user stop) pada safe boundary. Dibedakan
    # dari FAILED agar tidak diperlakukan sebagai kegagalan/retry.
    CANCELLED = "cancelled"


@dataclass
class RuntimeProgress:
    """Progress runtime (step + iterasi).

    Attributes:
        current_step: judul step yang sedang berjalan (None bila tidak ada).
        current_step_id: id step yang sedang berjalan.
        completed_steps: daftar judul step yang selesai.
        failed_step: judul step yang gagal (None bila tidak ada).
        iteration: jumlah iterasi LLM/tool yang sudah dijalankan.
    """

    current_step: Optional[str] = None
    current_step_id: Optional[str] = None
    completed_steps: List[str] = field(default_factory=list)
    failed_step: Optional[str] = None
    iteration: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current_step": self.current_step,
            "current_step_id": self.current_step_id,
            "completed_steps": list(self.completed_steps),
            "failed_step": self.failed_step,
            "iteration": self.iteration,
        }


@dataclass
class RuntimeResult:
    """Hasil akhir Agent Runtime.

    Attributes:
        status: status runtime.
        result: hasil akhir (teks) bila COMPLETED.
        error: pesan error bila FAILED.
        progress: progress runtime.
        steps: daftar step plan (serialized) beserta statusnya.
        iterations: jumlah iterasi total.
    """

    status: RuntimeStatus
    result: Optional[str] = None
    error: Optional[str] = None
    progress: RuntimeProgress = field(default_factory=RuntimeProgress)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    # Validation (opsional, #42). None bila validation tidak dijalankan.
    # Berisi ValidationResult.to_dict() (model validation yang sudah ada).
    validation: Optional[Dict[str, Any]] = None
    # Jumlah siklus validation/replan yang dijalankan (0 bila tidak ada).
    validation_cycles: int = 0
    # Execution Policy (fast/balanced/deep) — informasi/strategi kerja, BUKAN
    # keputusan loop. Berisi ExecutionPolicyState.to_dict() (requested_mode,
    # effective_mode, reason, escalations). None bila policy tidak di-resolve
    # (mis. runtime/fake lama) -> backward compatible.
    policy: Optional[Dict[str, Any]] = None
    # Evidence-Based Self-Verification (continuous loop) — ringkasan evidence
    # TERSTRUKTUR dari langkah agent (fakta, BUKAN keputusan). Additive; None di
    # jalur legacy/tanpa evidence. Tidak mengubah `result`/`status`.
    evidence: Optional[Dict[str, Any]] = None

    @property
    def success(self) -> bool:
        return self.status == RuntimeStatus.COMPLETED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "progress": self.progress.to_dict(),
            "steps": self.steps,
            "iterations": self.iterations,
            "validation": self.validation,
            "validation_cycles": self.validation_cycles,
            "policy": self.policy,
            "evidence": self.evidence,
        }
