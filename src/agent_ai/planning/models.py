"""Model untuk Task Planning Layer.

Mendefinisikan struktur execution plan yang dihasilkan dari request user:

    Task -> TaskPlanner -> TaskPlan -> (AgentLoop + Tool Executor)

Model di sini provider-agnostic dan TIDAK menyimpan chain-of-thought.
Planning Layer hanya memahami task secara struktural, menghasilkan langkah
kerja, dan melacak status langkah. Eksekusi tetap dilakukan Agent Loop.

#41 memperkaya model:
    - PlanStep: id, objective, dependencies, prerequisites, expected/actual
      outcome, retry/replan info, metadata.
    - StepStatus: PENDING, READY, RUNNING, COMPLETED, FAILED, BLOCKED, SKIPPED.
    - TaskPlan: revision ringan (revision number, previous revision, reason,
      changed steps) + dependency-aware readiness.

Status TASK tetap menjadi tanggung jawab tasks/ (TaskLifecycle). Model di sini
TIDAK membuat TaskLifecycle kedua.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class StepStatus(str, Enum):
    """Status sebuah langkah dalam plan."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class PlanStatus(str, Enum):
    """Status keseluruhan sebuah plan."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


#: Status step yang dianggap terminal (tidak akan berjalan lagi).
TERMINAL_STEP_STATUSES = {StepStatus.COMPLETED, StepStatus.SKIPPED, StepStatus.FAILED}


@dataclass
class PlanStep:
    """Satu langkah kerja dalam sebuah plan.

    Attributes:
        title: judul singkat langkah.
        description: penjelasan langkah (opsional).
        status: status langkah (default PENDING).
        metadata: info tambahan bebas (mis. tool hint, target file).
        id: identifier unik langkah.
        objective: tujuan terukur langkah (opsional).
        dependencies: id step lain yang harus selesai sebelum step ini.
        prerequisites: kondisi yang harus terpenuhi (deskriptif).
        expected_outcome: hasil yang diharapkan (deskriptif).
        actual_outcome: hasil nyata setelah eksekusi (diisi runtime/replanner).
        retry_count: jumlah percobaan ulang.
        replan_reason: alasan step terakhir di-replan (bila ada).
    """

    title: str
    description: str = ""
    status: StepStatus = StepStatus.PENDING
    metadata: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    objective: str = ""
    dependencies: List[str] = field(default_factory=list)
    prerequisites: List[str] = field(default_factory=list)
    expected_outcome: str = ""
    actual_outcome: Optional[str] = None
    retry_count: int = 0
    replan_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "objective": self.objective,
            "dependencies": list(self.dependencies),
            "prerequisites": list(self.prerequisites),
            "expected_outcome": self.expected_outcome,
            "actual_outcome": self.actual_outcome,
            "retry_count": self.retry_count,
            "replan_reason": self.replan_reason,
            "metadata": self.metadata,
        }


@dataclass
class PlanRevision:
    """Catatan revisi plan (versioned ringan, tanpa persistence).

    Attributes:
        revision: nomor revisi (mulai 1).
        previous_revision: nomor revisi sebelumnya (None untuk revisi awal).
        reason: alasan revisi (struktural, bukan chain-of-thought).
        changed_steps: ringkasan step yang berubah (added/removed/modified).
        metadata: info tambahan bebas.
    """

    revision: int
    previous_revision: Optional[int] = None
    reason: str = ""
    changed_steps: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "revision": self.revision,
            "previous_revision": self.previous_revision,
            "reason": self.reason,
            "changed_steps": self.changed_steps,
            "metadata": self.metadata,
        }


@dataclass
class TaskPlan:
    """Execution plan terstruktur untuk sebuah task.

    Attributes:
        task: task/request user.
        steps: daftar PlanStep.
        status: status keseluruhan plan.
        metadata: info tambahan bebas (mis. sumber context).
        revision: nomor revisi plan saat ini (mulai 1).
        previous_revision: nomor revisi sebelumnya (None untuk revisi awal).
        revision_reason: alasan revisi terakhir.
        revisions: riwayat revisi (PlanRevision).
    """

    task: str = ""
    steps: List[PlanStep] = field(default_factory=list)
    status: PlanStatus = PlanStatus.PENDING
    metadata: Dict[str, Any] = field(default_factory=dict)
    revision: int = 1
    previous_revision: Optional[int] = None
    revision_reason: str = ""
    revisions: List[PlanRevision] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #
    def get_step(self, step_id: str) -> PlanStep:
        """Ambil step berdasarkan id.

        Raises:
            KeyError: bila step tidak ditemukan.
        """
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(f"Step '{step_id}' tidak ditemukan di plan.")

    def _resolve(self, step: Any) -> PlanStep:
        """Terima PlanStep atau id string, kembalikan PlanStep."""
        if isinstance(step, PlanStep):
            if step not in self.steps:
                raise KeyError("PlanStep tidak berasal dari plan ini.")
            return step
        return self.get_step(str(step))

    # ------------------------------------------------------------------ #
    # Dependency-aware readiness
    # ------------------------------------------------------------------ #
    def dependencies_satisfied(self, step: Any) -> bool:
        """True bila semua dependency step sudah COMPLETED/SKIPPED."""
        target = self._resolve(step)
        for dep_id in target.dependencies:
            try:
                dep = self.get_step(dep_id)
            except KeyError:
                return False
            if dep.status not in (StepStatus.COMPLETED, StepStatus.SKIPPED):
                return False
        return True

    def refresh_readiness(self) -> None:
        """Tandai step PENDING yang dependency-nya siap menjadi READY.

        Step PENDING yang dependency-nya belum selesai tetap PENDING.
        """
        for step in self.steps:
            if step.status == StepStatus.PENDING and self.dependencies_satisfied(step):
                step.status = StepStatus.READY

    def ready_steps(self) -> List[PlanStep]:
        """Step yang siap dijalankan (READY)."""
        return [s for s in self.steps if s.status == StepStatus.READY]

    # ------------------------------------------------------------------ #
    # Lifecycle step
    # ------------------------------------------------------------------ #
    def start_step(self, step: Any) -> PlanStep:
        """Tandai step sebagai RUNNING (plan -> RUNNING)."""
        target = self._resolve(step)
        target.status = StepStatus.RUNNING
        self.status = PlanStatus.RUNNING
        return target

    def complete_step(self, step: Any, actual_outcome: Optional[str] = None) -> PlanStep:
        """Tandai step sebagai COMPLETED; plan COMPLETED bila semua selesai."""
        target = self._resolve(step)
        target.status = StepStatus.COMPLETED
        if actual_outcome is not None:
            target.actual_outcome = actual_outcome
        self.refresh_readiness()
        self._refresh_status()
        return target

    def fail_step(self, step: Any, error: Optional[str] = None) -> PlanStep:
        """Tandai step sebagai FAILED (plan -> FAILED)."""
        target = self._resolve(step)
        target.status = StepStatus.FAILED
        if error is not None:
            target.metadata["error"] = error
        self.status = PlanStatus.FAILED
        return target

    def block_step(self, step: Any, reason: Optional[str] = None) -> PlanStep:
        """Tandai step sebagai BLOCKED (mis. dependency gagal)."""
        target = self._resolve(step)
        target.status = StepStatus.BLOCKED
        if reason is not None:
            target.metadata["blocked_reason"] = reason
        return target

    def skip_step(self, step: Any, reason: Optional[str] = None) -> PlanStep:
        """Tandai step sebagai SKIPPED."""
        target = self._resolve(step)
        target.status = StepStatus.SKIPPED
        if reason is not None:
            target.metadata["skip_reason"] = reason
        self.refresh_readiness()
        self._refresh_status()
        return target

    def _refresh_status(self) -> None:
        """Perbarui status plan berdasarkan status step (tanpa menimpa FAILED)."""
        if self.status == PlanStatus.FAILED:
            return
        if not self.steps:
            self.status = PlanStatus.PENDING
            return
        terminal = {StepStatus.COMPLETED, StepStatus.SKIPPED}
        if all(s.status in terminal for s in self.steps):
            self.status = PlanStatus.COMPLETED
        elif any(s.status == StepStatus.RUNNING for s in self.steps):
            self.status = PlanStatus.RUNNING

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def is_complete(self) -> bool:
        """True bila semua step berada di status terminal (completed/skipped)."""
        if not self.steps:
            return False
        terminal = {StepStatus.COMPLETED, StepStatus.SKIPPED}
        return all(s.status in terminal for s in self.steps)

    def next_pending(self) -> Optional[PlanStep]:
        """Step PENDING/READY pertama (untuk dipandu Agent Loop)."""
        for step in self.steps:
            if step.status in (StepStatus.PENDING, StepStatus.READY):
                return step
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "status": self.status.value,
            "step_count": len(self.steps),
            "revision": self.revision,
            "previous_revision": self.previous_revision,
            "revision_reason": self.revision_reason,
            "steps": [s.to_dict() for s in self.steps],
            "revisions": [r.to_dict() for r in self.revisions],
            "metadata": self.metadata,
        }
