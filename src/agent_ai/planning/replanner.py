"""Replanner: evaluasi & revisi plan secara adaptif.

Replanner TIDAK mengeksekusi apa pun (bukan executor). Ia menerima plan +
observation/result, mengevaluasi apakah plan masih valid, dan menghasilkan
revisi plan baru bila perlu.

    Planner -> Plan -> Runtime -> Execution -> Observation -> Replanner
        -> Updated Plan -> Runtime

Prinsip:
    - Provider-agnostic, deterministik (berbasis aturan), bounded.
    - Tidak menjalankan tool/terminal/filesystem/Git/provider.
    - Tidak menyimpan chain-of-thought; alasan perubahan struktural/metadata.
    - Mempertahankan step COMPLETED; menandai step tidak relevan; menambah
      step baru; mengubah dependency step yang belum berjalan.
    - Versioned ringan (revision number, previous revision, reason, changed
      steps). Tanpa database/persistence baru.
    - TIDAK menduplikasi Reliability Manager: keputusan retry/recovery tetap
      milik reliability. Replanner hanya menyesuaikan strategi/dependency/
      asumsi plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from agent_ai.planning.models import (
    PlanRevision,
    PlanStep,
    StepStatus,
    TaskPlan,
)


class ReplanTrigger(str, Enum):
    """Kondisi yang memicu replanning."""

    STEP_SUCCEEDED = "step_succeeded"          # step berhasil -> lanjut
    STEP_FAILED_RECOVERABLE = "step_failed_recoverable"  # gagal, bisa dipulihkan
    DEPENDENCY_CHANGED = "dependency_changed"  # dependency berubah
    WORKSPACE_CHANGED = "workspace_changed"    # workspace tidak sesuai expected
    ASSUMPTION_INVALID = "assumption_invalid"  # asumsi awal salah
    TASK_IRRELEVANT = "task_irrelevant"        # task tidak lagi relevan
    NONE = "none"                              # tidak perlu replan


@dataclass
class ReplanDecision:
    """Keputusan replanning (struktural, bukan chain-of-thought).

    Attributes:
        needed: True bila plan perlu direvisi.
        trigger: kondisi pemicu.
        reason: alasan singkat.
        metadata: info tambahan bebas.
    """

    needed: bool = False
    trigger: ReplanTrigger = ReplanTrigger.NONE
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "needed": self.needed,
            "trigger": self.trigger.value,
            "reason": self.reason,
            "metadata": self.metadata,
        }


@dataclass
class Observation:
    """Observation/result dari eksekusi satu step (input replanner).

    Attributes:
        step_id: id step yang dieksekusi.
        success: apakah step berhasil.
        outcome: ringkasan hasil (deskriptif).
        error: pesan error (bila gagal).
        recoverable: apakah kegagalan dapat dipulihkan.
        changed_paths: path yang berubah (dari Change Tracking).
        expected_state_ok: apakah workspace sesuai expected state.
        assumption_valid: apakah asumsi plan masih valid.
        task_relevant: apakah task masih relevan.
        metadata: info tambahan bebas.
    """

    step_id: str
    success: bool = True
    outcome: str = ""
    error: Optional[str] = None
    recoverable: bool = False
    changed_paths: List[str] = field(default_factory=list)
    expected_state_ok: bool = True
    assumption_valid: bool = True
    task_relevant: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "success": self.success,
            "outcome": self.outcome,
            "error": self.error,
            "recoverable": self.recoverable,
            "changed_paths": self.changed_paths,
            "expected_state_ok": self.expected_state_ok,
            "assumption_valid": self.assumption_valid,
            "task_relevant": self.task_relevant,
            "metadata": self.metadata,
        }


class Replanner:
    """Mengevaluasi & merevisi plan (bukan executor).

    Args:
        max_replans: batas jumlah replan (default dari config bila None).
        max_plan_depth: batas kedalaman dependency plan (default dari config).
    """

    def __init__(
        self,
        max_replans: Optional[int] = None,
        max_plan_depth: Optional[int] = None,
    ) -> None:
        if max_replans is None or max_plan_depth is None:
            try:
                from agent_ai.config.settings import settings

                cfg = settings.planning
                max_replans = cfg.max_replans if max_replans is None else max_replans
                max_plan_depth = cfg.max_plan_depth if max_plan_depth is None else max_plan_depth
            except Exception:  # noqa: BLE001 - config error tidak boleh crash
                max_replans = 3 if max_replans is None else max_replans
                max_plan_depth = 3 if max_plan_depth is None else max_plan_depth
        self.max_replans = max(int(max_replans), 0)
        self.max_plan_depth = max(int(max_plan_depth), 1)

    # ------------------------------------------------------------------ #
    # Evaluation
    # ------------------------------------------------------------------ #
    def evaluate(self, plan: TaskPlan, observation: Observation) -> ReplanDecision:
        """Evaluasi apakah plan perlu direvisi berdasarkan observation.

        Deterministik. Prioritas trigger:
            1. task tidak relevan -> TASK_IRRELEVANT
            2. asumsi tidak valid -> ASSUMPTION_INVALID
            3. workspace tidak sesuai expected -> WORKSPACE_CHANGED
            4. dependency berubah -> DEPENDENCY_CHANGED
            5. step gagal recoverable -> STEP_FAILED_RECOVERABLE
            6. step berhasil -> STEP_SUCCEEDED (tidak perlu replan)
        """
        if not observation.task_relevant:
            return ReplanDecision(
                needed=True,
                trigger=ReplanTrigger.TASK_IRRELEVANT,
                reason="Task tidak lagi relevan.",
            )
        if not observation.assumption_valid:
            return ReplanDecision(
                needed=True,
                trigger=ReplanTrigger.ASSUMPTION_INVALID,
                reason="Asumsi awal plan tidak valid.",
            )
        if not observation.expected_state_ok:
            return ReplanDecision(
                needed=True,
                trigger=ReplanTrigger.WORKSPACE_CHANGED,
                reason="Workspace tidak sesuai expected state.",
                metadata={"changed_paths": observation.changed_paths},
            )
        if observation.changed_paths:
            return ReplanDecision(
                needed=True,
                trigger=ReplanTrigger.DEPENDENCY_CHANGED,
                reason="Dependency/workspace berubah.",
                metadata={"changed_paths": observation.changed_paths},
            )
        if not observation.success and observation.recoverable:
            return ReplanDecision(
                needed=True,
                trigger=ReplanTrigger.STEP_FAILED_RECOVERABLE,
                reason="Step gagal namun dapat dipulihkan.",
                metadata={"error": observation.error},
            )
        return ReplanDecision(
            needed=False,
            trigger=ReplanTrigger.STEP_SUCCEEDED if observation.success else ReplanTrigger.NONE,
            reason="Plan masih valid.",
        )

    # ------------------------------------------------------------------ #
    # Replanning
    # ------------------------------------------------------------------ #
    def replan(
        self,
        plan: TaskPlan,
        observation: Observation,
        decision: Optional[ReplanDecision] = None,
    ) -> TaskPlan:
        """Hasilkan revisi plan baru (in-place pada plan yang sama).

        Mempertahankan step COMPLETED, menandai step tidak relevan, menambah
        step baru, dan mengubah dependency step yang belum berjalan.

        Args:
            plan: plan saat ini.
            observation: observation/result terbaru.
            decision: keputusan replan (dihitung bila None).

        Returns:
            TaskPlan yang sama, dengan revision bertambah (bila replan terjadi).
        """
        decision = decision or self.evaluate(plan, observation)

        changed: List[Dict[str, Any]] = []

        # 1) Terapkan observation ke step terkait (selalu, agar status step
        #    akurat: step sukses -> COMPLETED, gagal -> retry/failed).
        self._apply_observation(plan, observation, changed)

        # Bila plan masih valid, tidak perlu revisi (status step tetap update).
        if not decision.needed:
            return plan

        # Batas replan: bila tercapai, jangan revisi lagi.
        if plan.revision - 1 >= self.max_replans:
            plan.metadata["replan_exhausted"] = True
            return plan

        # 2) Aksi sesuai trigger.
        if decision.trigger == ReplanTrigger.TASK_IRRELEVANT:
            self._mark_remaining_skipped(plan, "task tidak lagi relevan", changed)
        elif decision.trigger == ReplanTrigger.STEP_FAILED_RECOVERABLE:
            self._handle_recoverable_failure(plan, observation, changed)
        elif decision.trigger in (
            ReplanTrigger.DEPENDENCY_CHANGED,
            ReplanTrigger.WORKSPACE_CHANGED,
            ReplanTrigger.ASSUMPTION_INVALID,
        ):
            self._handle_context_change(plan, observation, decision, changed)

        # 3) Catat revisi (versioned ringan).
        self._record_revision(plan, decision, changed)
        return plan

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _apply_observation(
        self,
        plan: TaskPlan,
        observation: Observation,
        changed: List[Dict[str, Any]],
    ) -> None:
        """Terapkan hasil observation ke step terkait (bila ada)."""
        try:
            step = plan.get_step(observation.step_id)
        except KeyError:
            return
        step.actual_outcome = observation.outcome or step.actual_outcome
        if observation.success:
            if step.status != StepStatus.COMPLETED:
                plan.complete_step(step, actual_outcome=observation.outcome)
                changed.append({"step_id": step.id, "change": "completed"})
        else:
            step.retry_count += 1
            if observation.recoverable:
                # Kembalikan ke READY agar bisa dicoba ulang (bukan executor).
                step.status = StepStatus.READY
                step.replan_reason = "retry setelah kegagalan recoverable"
                changed.append({"step_id": step.id, "change": "retry"})
            else:
                plan.fail_step(step, error=observation.error)
                changed.append({"step_id": step.id, "change": "failed"})

    def _handle_recoverable_failure(
        self,
        plan: TaskPlan,
        observation: Observation,
        changed: List[Dict[str, Any]],
    ) -> None:
        """Tambah step pemulihan sebelum step yang gagal (bila belum ada)."""
        try:
            failed = plan.get_step(observation.step_id)
        except KeyError:
            return
        # Hindari duplikasi step pemulihan.
        if any(s.metadata.get("recovery_for") == failed.id for s in plan.steps):
            return
        recovery = PlanStep(
            title=f"recover: {failed.title}",
            description="Pemulihan sebelum mencoba ulang step yang gagal.",
            objective="Pulihkan kondisi agar step dapat dijalankan ulang.",
            expected_outcome="Kondisi siap untuk retry.",
            metadata={"recovery_for": failed.id, "replan": True},
        )
        # Sisipkan sebelum step yang gagal.
        idx = plan.steps.index(failed)
        plan.steps.insert(idx, recovery)
        # Step gagal bergantung pada recovery.
        if recovery.id not in failed.dependencies:
            failed.dependencies.append(recovery.id)
        changed.append({"step_id": recovery.id, "change": "added", "title": recovery.title})

    def _handle_context_change(
        self,
        plan: TaskPlan,
        observation: Observation,
        decision: ReplanDecision,
        changed: List[Dict[str, Any]],
    ) -> None:
        """Sesuaikan plan saat dependency/workspace/asumsi berubah.

        - Tandai step belum berjalan yang menyentuh path berubah sebagai
          perlu ditinjau (metadata), tanpa menghapusnya.
        - Tambah step verifikasi bila belum ada.
        """
        changed_paths = set(observation.changed_paths or [])
        for step in plan.steps:
            if step.status in (StepStatus.COMPLETED, StepStatus.RUNNING):
                continue
            targets = set(step.metadata.get("targets", []) or [])
            if changed_paths and targets & changed_paths:
                step.metadata["needs_review"] = True
                step.replan_reason = decision.reason
                changed.append({"step_id": step.id, "change": "needs_review"})

        # Tambah step verifikasi (sekali saja).
        if not any(s.metadata.get("replan_verify") for s in plan.steps):
            verify = PlanStep(
                title="verify after change",
                description="Verifikasi ulang setelah perubahan dependency/workspace.",
                objective="Pastikan plan masih sesuai kondisi terbaru.",
                expected_outcome="Plan tervalidasi terhadap kondisi terbaru.",
                metadata={"replan_verify": True, "replan": True},
            )
            plan.steps.append(verify)
            changed.append({"step_id": verify.id, "change": "added", "title": verify.title})

    def _mark_remaining_skipped(
        self,
        plan: TaskPlan,
        reason: str,
        changed: List[Dict[str, Any]],
    ) -> None:
        """Tandai step yang belum berjalan sebagai SKIPPED (task tidak relevan)."""
        for step in plan.steps:
            if step.status in (StepStatus.COMPLETED, StepStatus.SKIPPED):
                continue
            plan.skip_step(step, reason=reason)
            changed.append({"step_id": step.id, "change": "skipped"})

    def _record_revision(
        self,
        plan: TaskPlan,
        decision: ReplanDecision,
        changed: List[Dict[str, Any]],
    ) -> None:
        """Catat revisi baru (versioned ringan, tanpa persistence)."""
        previous = plan.revision
        plan.revision = previous + 1
        plan.previous_revision = previous
        plan.revision_reason = decision.reason
        plan.revisions.append(
            PlanRevision(
                revision=plan.revision,
                previous_revision=previous,
                reason=decision.reason,
                changed_steps=changed,
                metadata={"trigger": decision.trigger.value},
            )
        )
        plan.refresh_readiness()
