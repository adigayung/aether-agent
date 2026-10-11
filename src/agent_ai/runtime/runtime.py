"""Agent Runtime: menyatukan layer AETHER menjadi coding agent nyata.

Flow (NORMAL / default, continuous loop):
    PreparedTask
        -> AgentRuntime
             -> AgentOrchestrator.run_continuous_loop
                  -> LLM Provider (Native Tool Calling)
                  -> LLMResponse (tool_calls / final)
                  -> ToolExecutor (SEMUA tool call satu turn)
                  -> hasil tool kembali ke percakapan (role "tool")
                  -> LLM -> ... sampai LLM memberi response final
        -> DONE / FAILED

Prinsip:
    - Provider-agnostic: memakai BaseProvider lewat AgentOrchestrator.
    - SATU task = SATU percakapan kontinu (Native Tool Calling). Task TIDAK
      dipecah menjadi TaskStep yang dieksekusi terpisah.
    - Plan (bila ada) hanya menjadi context ADVISORY opsional untuk LLM;
      ia TIDAK menentukan urutan pemanggilan tool.
    - Tidak memakai heuristic completion lama (plan/gagal-terus/"sepertinya
      selesai"): selesai murni dari response LLM tanpa tool call.
    - Tidak menduplikasi logic AgentLoop/AgentOrchestrator/ToolExecutor.
    - Tool execution tetap melalui ToolExecutor/ToolRegistry.
    - Workspace security tetap dipegang tool yang sudah ada.
    - Tool/command failure dikirim kembali sebagai observation (loop tidak crash).
    - Tidak menyimpan hidden chain-of-thought.
    - Model/status dipisah di models.py; class ini fokus pada eksekusi.

Legacy path (use_continuous_loop=False) dipertahankan untuk kompatibilitas:
per-step execution + recovery/fallback/validation berbasis plan. Jalur ini
TIDAK dipakai oleh jalur normal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

from agent_ai.core.cancel import CancellationToken
from agent_ai.core.executor import ToolExecutor
from agent_ai.core.orchestrator import AgentOrchestrator
from agent_ai.core.models import AgentStatus
from agent_ai.planning.models import PlanStep, StepStatus, TaskPlan
from agent_ai.providers.base import BaseProvider, GenerateOptions
from agent_ai.runtime.models import RuntimeProgress, RuntimeResult, RuntimeStatus
from agent_ai.runtime.policy import (
    ExecutionPolicyResolver,
    ExecutionPolicyState,
    policy_activity_text,
)
from agent_ai.task.models import PreparedTask
from agent_ai.validation.models import (
    ValidationOutcome,
    ValidationRequest,
    ValidationResult,
)
from agent_ai.validation.strategy import (
    VerificationStrategy,
    format_verification_activity,
    strategy_for_mode,
)
from agent_ai.runtime.working_state import (
    WorkingState,
    WorkingStateManager,
    PlanEntry,
    PlanEntryStatus,
)

_FILE_OPERATION_TYPES = {
    "edit_file": "edit",
    "write_file": "write",
    "delete_file": "delete",
    "move_file": "move",
}

if TYPE_CHECKING:  # pragma: no cover - hanya untuk type hint, hindari import cycle
    from agent_ai.changes.task_evidence import TaskChangeEvidence
    from agent_ai.changes.tracker import ChangeTracker
    from agent_ai.fallback.manager import FallbackManager
    from agent_ai.planning.replanner import Replanner
    from agent_ai.recovery.manager import RecoveryManager
    from agent_ai.session.store import SessionStore
    from agent_ai.tasks.lifecycle import TaskLifecycle
    from agent_ai.validation.runner import ValidationRunner


class AgentRuntime:
    """Menjalankan PreparedTask sebagai coding agent multi-step.

    Args:
        provider: instance BaseProvider (abstraction). Wajib.
        executor: ToolExecutor. Default: ToolExecutor() dengan registry global.
        max_iterations: batas iterasi per step. Hanya berlaku untuk jalur
            legacy (`use_continuous_loop=False`); continuous loop memakai
            safety cap-nya sendiri dari orchestrator.
        options: GenerateOptions default untuk setiap pemanggilan LLM.
        system_prompt: prompt sistem opsional.
        use_continuous_loop: jalur eksekusi NORMAL. Bila True (DEFAULT),
            satu task dijalankan sebagai SATU percakapan kontinu Native Tool
            Calling; task tidak dipecah menjadi TaskStep, dan plan (bila ada)
            hanya menjadi context advisory opsional. Bila False, jalur legacy
            (eksekusi per step plan + recovery/fallback/validation) dipakai.
        policy_resolver: ExecutionPolicyResolver opsional (fast/balanced/deep).
            Default: resolver standar. Policy hanya INFORMASI/STRATEGI kerja —
            bukan hard limit dan tidak mengubah keputusan loop LLM.
        requested_mode: mode policy yang diminta user/metadata (opsional).
            Bila kosong, dibaca dari metadata PreparedTask
            (`agent_mode`/`policy_mode`/`mode`); bila tetap kosong, policy
            tidak diaktifkan (perilaku lama tidak berubah).
    """

    def __init__(
        self,
        provider: BaseProvider,
        executor: Optional[ToolExecutor] = None,
        max_iterations: int = 10,
        options: Optional[GenerateOptions] = None,
        system_prompt: Optional[str] = None,
        validation_runner: Optional["ValidationRunner"] = None,
        validation_request: Optional[ValidationRequest] = None,
        replanner: Optional["Replanner"] = None,
        session_store: Optional["SessionStore"] = None,
        session_id: Optional[str] = None,
        change_tracker: Optional["ChangeTracker"] = None,
        change_evidence: Optional["TaskChangeEvidence"] = None,
        max_validation_cycles: Optional[int] = None,
        stop_on_validation_failure: Optional[bool] = None,
        recovery_manager: Optional["RecoveryManager"] = None,
        fallback_manager: Optional["FallbackManager"] = None,
        provider_factory: Optional[Any] = None,
        project_root: Optional[str] = None,
        project_brain: bool = True,
        use_continuous_loop: bool = True,
        cancel_token: Optional[CancellationToken] = None,
        policy_resolver: Optional[ExecutionPolicyResolver] = None,
        requested_mode: Optional[str] = None,
    ) -> None:
        if provider is None:
            raise ValueError("AgentRuntime butuh provider (BaseProvider).")
        self.provider = provider
        # Cooperative cancellation (opsional). Diteruskan ke AgentOrchestrator
        # agar loop berhenti di safe boundary saat user menekan Stop. Bukan
        # sistem cancellation kedua: token tunggal milik gateway per task.
        self.cancel_token = cancel_token
        # Project-local storage (Task 5): root project target. Bila diisi,
        # runtime menulis `.aether/log/<task_id>.log` dan memakai AI Project
        # Bible project-local (`.aether/bible`) lewat ProjectBrain.
        self.project_root = project_root
        # Executor: bila pemanggil memberi executor (mis. TaskExecutor yang
        # sudah merakit registry ber-root workspace), pakai apa adanya. Bila
        # TIDAK, dan `project_root` diketahui, rakit executor ber-root project
        # tersebut sehingga tool filesystem/workspace benar-benar beroperasi di
        # project target (bukan root AETHER). Tanpa `project_root`, perilaku
        # lama dipertahankan (registry global).
        if executor is not None:
            self.executor = executor
        elif project_root:
            try:
                from pathlib import Path as _Path

                from agent_ai.tools.registry import build_registry

                self.executor = ToolExecutor(
                    registry=build_registry(
                        root=_Path(project_root), cancel_token=cancel_token
                    ),
                    workspace_root=project_root,
                )
            except Exception:  # noqa: BLE001 - fallback ke registry default
                self.executor = ToolExecutor()
        else:
            self.executor = ToolExecutor()
        self.max_iterations = max_iterations
        self.options = options
        self.system_prompt = system_prompt
        # Jalur eksekusi NORMAL (default True): continuous loop Native Tool
        # Calling -> satu percakapan kontinu per task, tanpa pemecahan TaskStep.
        # Set False untuk memakai jalur legacy (per step plan + recovery).
        self.use_continuous_loop = use_continuous_loop

        self.project_brain_enabled = project_brain
        self._task_log: Optional[Any] = None
        # Log response API LLM project-local (`.aether/log/response/<task_id>.json`).
        # Dibuat per task HANYA bila `data/settings.json` -> `write_log_response_api`
        # aktif. Default None = tidak ada logging (perilaku sekarang).
        self._response_log: Optional[Any] = None
        self._brain: Optional[Any] = None
        # Environment Context project-local (`<project_root>/.aether/ENVIRONMENT.md`).
        # Dibuat/dimuat SEKALI per session (instance runtime); hasilnya di-cache
        # di `_environment_text` dan hanya disuntikkan pada task pertama.
        self._environment_text: Optional[str] = None
        self._environment_injected: bool = False
        # Environment Context untuk task berjalan (diteruskan ke orchestrator
        # continuous loop). None pada jalur legacy / non-continuous.
        self._current_environment_context: Optional[str] = None

        # Activity Phase (UI-facing): aktivitas NYATA Agent yang sekarang
        # berjalan (planning/inspecting/editing/running/validating). Ini
        # TERPISAH dari `TaskPhase` internal runtime; nilainya dikirim ke
        # frontend lewat event `phase_changed`. Di-reset per task di `run()`.
        # `None` = belum ada aktivitas yang diklasifikasi untuk task ini.
        self._activity_phase: Optional[str] = None

        # Agent Execution Policy (fast/balanced/deep) — PREferensi STRATEGI kerja,
        # BUKAN hard limit dan BUKAN penggerak loop. `requested_mode` = mode yang
        # diminta user/metadata; `effective_mode` = mode yang benar-benar dipakai
        # (dapat naik lewat escalation yang diputuskan Agent/LLM). Resolver murni
        # deterministik (tanpa heuristic keyword/scoring). Tidak ada state global:
        # policy hidup per-run dan di-reset di `run()`. `None` = policy tidak
        # aktif (runtime lama/uji) -> perilaku persis seperti sebelumnya.
        self.policy_resolver = policy_resolver or ExecutionPolicyResolver()
        self.requested_mode = requested_mode
        self.policy: Optional[ExecutionPolicyState] = None
        # Verification strategy (advisory) yang mengikuti effective_mode.
        # Di-resolve dari policy bila policy aktif; None bila tidak ada mode.
        # TIDAK membuat pipeline validation baru: hanya preferensi/check list
        # yang diberikan ke validation layer (metadata) dan ke Agent (advisory).
        self.verification_strategy: Optional[VerificationStrategy] = None

        # Validation <-> Runtime Integration (#42), semuanya OPSIONAL.
        # Bila validation_runner/validation_request tidak diberikan, runtime
        # berperilaku persis seperti sebelumnya (backward compatible).
        self.validation_runner = validation_runner
        self.validation_request = validation_request
        self.replanner = replanner
        self.session_store = session_store
        self.session_id = session_id
        self.change_tracker = change_tracker
        # Change EVIDENCE per task (OPSIONAL): adapter tipis di atas
        # ChangeTracker EXISTING yang menyediakan daftar perubahan file nyata
        # (created/modified/deleted) untuk continuous loop + review. Runtime
        # TIDAK membuat tracker kedua: bila `change_tracker` diberikan tanpa
        # `change_evidence`, adapter dibangun di atas tracker yang sama.
        #
        # Bila keduanya TIDAK diberikan tetapi `project_root` diketahui,
        # runtime membuat SATU ChangeTracker untuk root itu (satu change index,
        # bukan dua) + adapter di atasnya. Dengan begitu runtime yang dipakai
        # langsung (mis. verifier/gateway) tetap memiliki change evidence,
        # tanpa perlu pemanggil merakitnya. Bila `project_root` juga tidak ada,
        # tidak ada deteksi perubahan sama sekali (perilaku lama).
        self.change_evidence: Optional["TaskChangeEvidence"] = change_evidence
        if self.change_evidence is None and self.change_tracker is None and project_root:
            try:
                from agent_ai.changes.tracker import ChangeTracker

                self.change_tracker = ChangeTracker(root=Path(project_root))
            except Exception:  # noqa: BLE001 - tracking tidak boleh crash runtime
                self.change_tracker = None
        if self.change_evidence is None and self.change_tracker is not None:
            try:
                from agent_ai.changes.task_evidence import TaskChangeEvidence

                self.change_evidence = TaskChangeEvidence(self.change_tracker)
            except Exception:  # noqa: BLE001 - evidence tidak boleh crash runtime
                self.change_evidence = None

        # Advanced Recovery (#43), OPSIONAL. Bila None, runtime berperilaku
        # seperti sebelumnya (step gagal -> FAILED tanpa recovery).
        self.recovery_manager = recovery_manager

        # Provider Fallback (#45), OPSIONAL. Bila None, runtime berperilaku
        # seperti sebelumnya (provider error -> FAILED tanpa fallback).
        # provider_factory: callable `(provider_name) -> BaseProvider` untuk
        # membangun provider alternatif. Bila None, fallback tidak dapat
        # berpindah provider (keputusan tetap dihitung, tapi tidak diterapkan).
        self.fallback_manager = fallback_manager
        self.provider_factory = provider_factory

        # Policy dari config (tidak di-hardcode), dapat di-override per-instance.
        cfg = self._validation_config()
        self.max_validation_cycles = (
            max_validation_cycles if max_validation_cycles is not None else cfg.max_replan_cycles
        )
        self.stop_on_validation_failure = (
            stop_on_validation_failure
            if stop_on_validation_failure is not None
            else cfg.stop_on_failure
        )

        # Working State manager (di-reset per task di run(), bukan di sini).
        # Inisialisasi awal sebagai None; akan dibuat saat task dimulai.
        self._working_state_manager: Optional[WorkingStateManager] = None

    @staticmethod
    def _validation_config() -> Any:
        """Ambil ValidationConfig dari settings (fallback aman bila gagal)."""
        try:
            from agent_ai.config.settings import settings

            return settings.validation
        except Exception:  # noqa: BLE001 - config error tidak boleh crash
            from agent_ai.config.settings import ValidationConfig

            return ValidationConfig()

    @property
    def validation_enabled(self) -> bool:
        """True bila validation diaktifkan untuk runtime ini.

        Validation hanya aktif bila runner DAN request diberikan secara
        eksplisit. Ini menjaga backward compatibility Runtime(prepared).
        """
        return self.validation_runner is not None and self.validation_request is not None

    # ------------------------------------------------------------------ #
    # Working State access
    # ------------------------------------------------------------------ #
    @property
    def working_state(self) -> Optional[WorkingState]:
        """Snapshot Working State task saat ini (None bila tidak ada task)."""
        if self._working_state_manager is None:
            return None
        return self._working_state_manager.snapshot()

    @property
    def working_state_text(self) -> str:
        """Render Working State sebagai teks untuk konteks LLM (kosong = tidak ada)."""
        if self._working_state_manager is None:
            return ""
        state = self._working_state_manager.snapshot()
        return state.to_text() if not state.is_empty else ""

    def update_working_state(self, **kwargs: Any) -> WorkingState:
        """Update Working State (merge) dan kembalikan snapshot baru.

        Kwargs diteruskan ke WorkingStateManager.update(). Bisa dipakai
        backend/internal untuk merekam progres (file inspected/changed, dst).
        """
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.update(**kwargs)

    def set_working_state(self, **kwargs: Any) -> WorkingState:
        """Set Working State fields (replace list, bukan merge)."""
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.set(**kwargs)

    def record_files_inspected(self, *files: str) -> WorkingState:
        """Catat file yang sudah dibaca/dipahami (append + dedup)."""
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.record_files_inspected(*files)

    def record_files_changed(self, *files: str) -> WorkingState:
        """Catat file yang sudah ditulis/diubah/dihapus (append + dedup)."""
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.record_files_changed(*files)

    def record_decision(self, *decisions: str) -> WorkingState:
        """Catat keputusan yang diambil Agent (append + dedup)."""
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.record_decision(*decisions)

    def record_hypothesis(self, *hypotheses: str) -> WorkingState:
        """Catat hipotesis kerja Agent (append + dedup)."""
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.record_hypothesis(*hypotheses)

    def record_open_question(self, *questions: str) -> WorkingState:
        """Catat pertanyaan terbuka (append + dedup)."""
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.record_open_question(*questions)

    def record_completed_step(self, *steps: str) -> WorkingState:
        """Catat langkah yang sudah selesai (append + dedup)."""
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.record_completed_step(*steps)

    def set_current_focus(self, focus: str) -> WorkingState:
        """Set fokus kerja saat ini."""
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.set_current_focus(focus)

    def set_goal(self, goal: str) -> WorkingState:
        """Set goal task."""
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.set_goal(goal)

    # ------------------------------------------------------------------ #
    # Living Plan access (plan = state di Working State, bukan engine)
    # ------------------------------------------------------------------ #
    @property
    def plan(self) -> List[PlanEntry]:
        """Snapshot living plan task saat ini (list kosong bila belum ada)."""
        if self._working_state_manager is None:
            return []
        state = self._working_state_manager.snapshot()
        return list(state.plan)

    def plan_progress(self) -> Dict[str, Any]:
        """Ringkasan progress living plan (read-only, deterministik)."""
        if self._working_state_manager is None:
            return {
                "total": 0,
                "completed": 0,
                "skipped": 0,
                "failed": 0,
                "pending": 0,
                "running": 0,
                "blocked": 0,
                "percent": 0.0,
                "current_step": None,
            }
        return self._working_state_manager.snapshot().plan_progress()

    def create_plan_entry(self, *, title: str, description: str = "",
                          order: Optional[int] = None,
                          metadata: Optional[Dict[str, Any]] = None) -> WorkingState:
        """Tambah langkah baru ke living plan (source of truth: Working State)."""
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.create_plan_entry(
            title=title, description=description, order=order, metadata=metadata
        )

    def add_plan_entry(self, *, title: str, description: str = "",
                       after_id: Optional[str] = None,
                       before_id: Optional[str] = None,
                       metadata: Optional[Dict[str, Any]] = None) -> WorkingState:
        """Tambah langkah plan relatif terhadap langkah lain (after/before)."""
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.add_plan_entry(
            title=title, description=description,
            after_id=after_id, before_id=before_id, metadata=metadata,
        )

    def update_plan_entry(self, entry_id: str, *,
                          title: Optional[str] = None,
                          description: Optional[str] = None,
                          status: Optional[PlanEntryStatus] = None,
                          metadata: Optional[Dict[str, Any]] = None,
                          notes: Optional[str] = None) -> WorkingState:
        """Ubah isi/status langkah plan yang sudah ada (replanning)."""
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.update_plan_entry(
            entry_id, title=title, description=description,
            status=status, metadata=metadata, notes=notes,
        )

    def remove_plan_entry(self, entry_id: str) -> WorkingState:
        """Hapus langkah dari living plan (lalu urutan dirapikan)."""
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.remove_plan_entry(entry_id)

    def reorder_plan_entry(self, entry_id: str, new_order: int) -> WorkingState:
        """Pindahkan langkah ke posisi urutan baru (reorder plan)."""
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.reorder_plan_entry(entry_id, new_order)

    def skip_plan_entry(self, entry_id: str, reason: str = "") -> WorkingState:
        """Lewati langkah (LLM memutuskan, AETHER hanya mencatat)."""
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.skip_plan_entry(entry_id, reason)

    def complete_plan_entry(self, entry_id: str, outcome: str = "") -> WorkingState:
        """Tandai langkah plan selesai (dengan outcome opsional)."""
        if self._working_state_manager is None:
            self._working_state_manager = WorkingStateManager()
        return self._working_state_manager.complete_plan_entry(entry_id, outcome)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def run(
        self,
        prepared: PreparedTask,
        lifecycle: Optional["TaskLifecycle"] = None,
        *,
        user_parts: Optional[List[Dict[str, Any]]] = None,
    ) -> RuntimeResult:
        """Jalankan PreparedTask dan kembalikan RuntimeResult.

        Args:
            prepared: PreparedTask (task + context + plan).
            lifecycle: TaskLifecycle opsional. Bila diisi, runtime akan
                menggerakkan transition status (RUNNING -> VALIDATING ->
                COMPLETED/FAILED) tanpa mengubah perilaku eksekusi. Bila None,
                perilaku sama seperti sebelumnya.
            user_parts: content blocks opsional untuk pesan user awal (mis.
                image, format internal AETHER provider-agnostic). Diteruskan ke
                AgentOrchestrator. Kosong (default) = text-only tidak berubah.

        Returns:
            RuntimeResult (status, result/error, progress, steps).

        Raises:
            ValueError: bila prepared tidak valid.
        """
        if prepared is None or not getattr(prepared, "task", ""):
            raise ValueError("AgentRuntime butuh PreparedTask dengan task.")

        progress = RuntimeProgress()
        plan: Optional[TaskPlan] = prepared.plan
        self._current_prepared = prepared

        # task_id untuk event emission (dari prepared atau lifecycle).
        self._current_task_id = getattr(prepared, "task_id", None) or (
            lifecycle.task_id if lifecycle is not None else None
        )

        # Project-local storage (Task 5): Task Log + AI Project Bible.
        # Best-effort: kegagalan di sini TIDAK boleh menggagalkan eksekusi.
        self._setup_project_storage(prepared)

        # Change evidence per task: ikat ke task_id AKTUAL yang sudah
        # dikonsolidasikan `_setup_project_storage` (task_id dari prepared /
        # lifecycle). Snapshot awal TIDAK dilakukan di sini (tidak ada
        # full-project hashing saat task mulai); kandidat perubahan didaftarkan
        # dari aktivitas tool nyata. Karena itu evidence dibangun SETELAH
        # task_id ditetapkan.
        self._begin_change_evidence()

        # Agent Execution Policy: resolve requested_mode -> effective_mode.
        # Murni informasi/strategi (bukan hard limit, bukan penggerak loop):
        # effective_mode SELALU dimulai sama dengan requested_mode; kenaikan
        # hanya lewat escalation eksplisit oleh Agent/LLM. Di-reset per task.
        self._resolve_policy(prepared)

        # Observability (#55): catat task dimulai (bila session store tersedia).
        self._emit_event("task_started", {"task": prepared.task})

        # Activity Phase (UI): task baru mulai -> `planning` (sebelum aktivitas
        # inspection/editing/running). Di-reset per task agar task pada instance
        # runtime yang sama tidak mewarisi phase task sebelumnya.
        self._activity_phase = None
        self._set_activity_phase("planning")

        # Working State: state internal per task yang menjaga pemahaman kerja
        # (goal/requirements/decisions/files/hypotheses/open_questions/...).
        # Di-reset per task; BUKAN activity/history (tetap telemetry UI).
        # LLM tetap pengambil keputusan; ini hanya tempat state + persistence.
        self._working_state_manager = WorkingStateManager()
        self._working_state_manager.set_goal(prepared.task)

        # Initialize living plan from TaskPlan (advisory-only, non-binding)
        if plan and plan.steps:
            for step in plan.steps:
                self._working_state_manager.create_plan_entry(
                    title=step.title,
                    description=step.description or "",
                    metadata={
                        "objective": step.objective,
                        "expected_outcome": step.expected_outcome,
                        "dependencies": list(step.dependencies),
                        "prerequisites": list(step.prerequisites),
                    },
                )

        # Lifecycle (opsional): tandai eksekusi dimulai.
        if lifecycle is not None:
            self._lifecycle_to_running(lifecycle)

        # Jalur NORMAL (default): SATU percakapan kontinu (continuous loop
        # Native Tool Calling). Task TIDAK dipecah menjadi TaskStep; plan
        # (bila ada) hanya context advisory. Tidak ada pemanggilan per-step.
        if self.use_continuous_loop:
            self._current_environment_context = self._session_environment_context()
            result = self._run_continuous(prepared, progress, user_parts=user_parts)
            result = self._maybe_validate(prepared, progress, result, lifecycle)
            self._finish_change_evidence()
            self._lifecycle_finalize(lifecycle, result)
            return result

        # --- Jalur legacy (use_continuous_loop=False) ---
        # Tanpa plan: jalankan task sebagai satu langkah tunggal.
        if plan is None or not plan.steps:
            result = self._run_single(prepared, progress, user_parts=user_parts)
            result = self._maybe_validate(prepared, progress, result, lifecycle)
            self._finish_change_evidence()
            self._lifecycle_finalize(lifecycle, result)
            return result

        # Multi-step: jalankan tiap step plan secara berurutan.
        # Recovery (opsional) mengorkestrasi tindakan saat step gagal.
        result = self._run_plan_with_recovery(prepared, plan, progress)

        # Validation (opsional): hanya bila execution sukses & validation aktif.
        result = self._maybe_validate(prepared, progress, result, lifecycle)

        self._finish_change_evidence()
        self._lifecycle_finalize(lifecycle, result)
        return result

    # ------------------------------------------------------------------ #
    # Recovery integration (#43)
    # ------------------------------------------------------------------ #
    def _run_plan_with_recovery(
        self,
        prepared: PreparedTask,
        plan: TaskPlan,
        progress: RuntimeProgress,
    ) -> RuntimeResult:
        """Jalankan plan; bila step gagal, konsultasikan RecoveryManager.

        RecoveryManager hanya memutuskan tindakan (retry/recover/replan/stop/
        fail) dan memicu replanner; runtime tetap satu-satunya executor.
        Bila recovery tidak aktif, perilaku sama seperti `_run_plan`.
        """
        if self.recovery_manager is None or not self.recovery_manager.enabled:
            return self._run_plan(prepared, plan, progress)

        while True:
            result = self._run_plan(prepared, plan, progress)
            if result.status == RuntimeStatus.COMPLETED:
                return result

            # Step gagal -> bangun sinyal terstruktur & minta keputusan recovery.
            signal = self._build_failure_signal(prepared, plan, result)
            observation = self._recovery_observation(plan, result)
            decision = self.recovery_manager.recover(signal, plan=plan, observation=observation)

            action = decision.action.value
            if action == "retry":
                # Ulangi plan (step gagal sudah di-READY oleh replanner/plan).
                self._reset_failed_step(plan)
                continue
            if action == "recover":
                # Ubah pendekatan: reset step gagal agar dicoba ulang.
                self._reset_failed_step(plan)
                continue
            if action == "replan":
                # Replanner sudah dipicu oleh RecoveryManager; jalankan ulang.
                self._reset_failed_step(plan)
                continue
            if action == "stop":
                result.error = f"Recovery STOP: {decision.reason}"
                return result
            # fail
            result.error = result.error or f"Recovery FAIL: {decision.reason}"
            return result

    def _build_failure_signal(
        self,
        prepared: PreparedTask,
        plan: TaskPlan,
        result: RuntimeResult,
    ) -> Any:
        """Bangun FailureSignal terstruktur dari hasil step yang gagal.

        Sinyal berasal dari subsystem yang sudah ada (bukan string exception):
        outcome step, flag command/tool, event reliability, perubahan workspace.
        """
        from agent_ai.recovery.models import FailureSignal

        # Step yang gagal (untuk menentukan command/tool & outcome).
        failed_step = None
        for step in plan.steps:
            if step.status == StepStatus.FAILED:
                failed_step = step
                break

        outcome = "execution_error"
        is_command = False
        is_tool = False
        if failed_step is not None:
            meta = failed_step.metadata or {}
            outcome = meta.get("outcome") or outcome
            is_command = bool(meta.get("is_command"))
            is_tool = bool(meta.get("is_tool"))

        # Event reliability (bila reliability tersedia di recovery manager).
        reliability_events: List[str] = []
        rm = self.recovery_manager
        if rm is not None and rm.reliability is not None:
            try:
                events = rm.reliability.latest_events() or rm.reliability.events
                reliability_events = [e.type.value for e in events]
            except Exception:  # noqa: BLE001
                reliability_events = []

        return FailureSignal(
            source="runtime",
            outcome=outcome,
            recoverable=True,
            is_command=is_command,
            is_tool=is_tool,
            reliability_events=reliability_events,
            attempts=rm.attempts if rm is not None else 0,
            metadata={
                "task_id": getattr(prepared, "task_id", None),
                "step_id": failed_step.id if failed_step is not None else None,
            },
        )

    def _recovery_observation(self, plan: TaskPlan, result: RuntimeResult) -> Any:
        """Bangun Observation replanner dari step yang gagal (untuk replan)."""
        from agent_ai.planning.replanner import Observation

        failed_step = None
        for step in plan.steps:
            if step.status == StepStatus.FAILED:
                failed_step = step
                break
        step_id = failed_step.id if failed_step is not None else (plan.steps[-1].id if plan.steps else "")
        return Observation(
            step_id=step_id,
            success=False,
            outcome="step_failed",
            error=result.error,
            recoverable=True,
        )

    @staticmethod
    def _reset_failed_step(plan: TaskPlan) -> None:
        """Kembalikan step FAILED ke READY agar dapat dijalankan ulang.

        Tidak mengeksekusi apa pun; hanya menyesuaikan status plan.
        """
        for step in plan.steps:
            if step.status == StepStatus.FAILED:
                step.status = StepStatus.READY

    def _run_plan(
        self,
        prepared: PreparedTask,
        plan: TaskPlan,
        progress: RuntimeProgress,
    ) -> RuntimeResult:
        """Jalankan seluruh step plan secara berurutan (tanpa validation)."""
        for step in plan.steps:
            plan.start_step(step)
            progress.current_step = step.title
            progress.current_step_id = step.id

            step_result = self._run_step(prepared, step, progress)

            if step_result.success:
                plan.complete_step(step)
                progress.completed_steps.append(step.title)
                progress.current_step = None
                progress.current_step_id = None
                continue

            # Step gagal -> tandai plan FAILED dan hentikan runtime.
            # Simpan outcome terstruktur pada metadata step (untuk recovery).
            self._record_step_failure(step, step_result)
            plan.fail_step(step, error=step_result.error)
            progress.failed_step = step.title
            return RuntimeResult(
                status=RuntimeStatus.FAILED,
                result=None,
                error=step_result.error or f"Step '{step.title}' gagal.",
                progress=progress,
                steps=plan.to_dict()["steps"],
                iterations=progress.iteration,
            )

        # Semua step selesai.
        return RuntimeResult(
            status=RuntimeStatus.COMPLETED,
            result=self._final_result(prepared, plan),
            error=None,
            progress=progress,
            steps=plan.to_dict()["steps"],
            iterations=progress.iteration,
        )

    # ------------------------------------------------------------------ #
    # Validation integration (#42)
    # ------------------------------------------------------------------ #
    def _maybe_validate(
        self,
        prepared: PreparedTask,
        progress: RuntimeProgress,
        result: RuntimeResult,
        lifecycle: Optional["TaskLifecycle"],
    ) -> RuntimeResult:
        """Jalankan validation bila aktif dan execution sukses.

        Bila validation tidak aktif (runner/request tidak diberikan), kembalikan
        result apa adanya (backward compatible). Bila execution sudah FAILED,
        validation tidak dijalankan (tidak ada yang divalidasi).
        """
        if not self.validation_enabled:
            return result
        if result.status != RuntimeStatus.COMPLETED:
            return result

        return self._validate_with_replan(prepared, progress, result, lifecycle)

    def _validate_with_replan(
        self,
        prepared: PreparedTask,
        progress: RuntimeProgress,
        result: RuntimeResult,
        lifecycle: Optional["TaskLifecycle"],
    ) -> RuntimeResult:
        """Jalankan validation, dan replan bila gagal (bounded).

        Alur:
            execution selesai -> VALIDATING -> jalankan validator
                SUCCESS -> COMPLETED
                FAILURE -> (bila replanner ada & masih bisa) REPLAN -> RUNNING
                           -> execution ulang -> VALIDATING -> ...
                TIMEOUT/EXECUTION_ERROR -> recovery/replan sesuai boundary
                           (bounded oleh max_validation_cycles)

        Replanner hanya membuat/memperbarui plan; runtime tetap yang menjalankan.
        Reliability tidak diduplikasi di sini.
        """
        plan: Optional[TaskPlan] = prepared.plan
        cycles = 0
        last_validation: Optional[ValidationResult] = None

        while True:
            # Masuk phase VALIDATING (lifecycle + event).
            self._lifecycle_to_validating(lifecycle)
            self._emit_event("validation_started", {"cycle": cycles})
            # Activity Phase (UI): validation existing => `validating`.
            # Memakai mekanisme validation yang SUDAH ADA (bukan deteksi baru).
            self._set_activity_phase("validating")

            last_validation = self._run_validation()
            cycles += 1

            self._emit_event(
                "validation_completed",
                {
                    "cycle": cycles,
                    "success": last_validation.success,
                    "outcome": last_validation.outcome.value,
                    "exit_code": last_validation.exit_code,
                },
            )

            if last_validation.success:
                # SUCCESS -> COMPLETED.
                result.validation = last_validation.to_dict()
                result.validation_cycles = cycles
                return result

            # Validation gagal. Tentukan apakah bisa replan.
            can_replan = (
                self.replanner is not None
                and plan is not None
                and plan.steps
                and cycles < self.max_validation_cycles
                and not self.stop_on_validation_failure
            )
            if not can_replan:
                # FAILURE / TIMEOUT / EXECUTION_ERROR tanpa replan -> FAILED.
                result.status = RuntimeStatus.FAILED
                result.error = self._validation_error_message(last_validation)
                result.validation = last_validation.to_dict()
                result.validation_cycles = cycles
                return result

            # REPLAN: bangun observation dari hasil validation, minta replanner
            # memperbarui plan, lalu jalankan ulang plan (runtime tetap executor).
            observation = self._validation_observation(plan, last_validation)
            self._emit_event(
                "phase_changed",
                {"phase": "replan", "reason": observation.outcome},
            )
            self.replanner.replan(plan, observation)

            # Jalankan ulang plan yang sudah diperbarui.
            rerun = self._run_plan(prepared, plan, progress)
            if rerun.status != RuntimeStatus.COMPLETED:
                # Execution ulang gagal -> FAILED (bukan validation failure).
                rerun.validation = last_validation.to_dict()
                rerun.validation_cycles = cycles
                return rerun
            result = rerun
            # Loop kembali ke VALIDATING untuk memvalidasi hasil baru.

    def _run_validation(self) -> ValidationResult:
        """Jalankan validator yang ditentukan (memakai ValidationRunner).

        Error menjalankan validator (validator sendiri gagal) TIDAK disamakan
        dengan validation failure: dikembalikan sebagai EXECUTION_ERROR.
        """
        request = self.validation_request
        assert request is not None  # dijaga oleh validation_enabled
        if self.verification_strategy is not None:
            meta = dict(request.metadata)
            meta["verification_strategy"] = self.verification_strategy.to_dict()
            meta["effective_mode"] = self.verification_strategy.mode
            request = ValidationRequest(
                target=request.target,
                command=request.command,
                timeout=request.timeout,
                metadata=meta,
            )
        try:
            return self.validation_runner.run(request)
        except Exception as exc:  # noqa: BLE001 - validator error -> execution_error
            return ValidationResult(
                success=False,
                outcome=ValidationOutcome.EXECUTION_ERROR,
                exit_code=None,
                stdout="",
                stderr=str(exc),
                duration=0.0,
                validator="runtime",
                metadata={"error": f"{type(exc).__name__}: {exc}"},
            )

    @staticmethod
    def _validation_error_message(validation: ValidationResult) -> str:
        """Pesan error runtime dari hasil validation (bedakan tiap outcome)."""
        if validation.outcome == ValidationOutcome.TIMEOUT:
            return f"Validation timeout: {validation.stderr or 'validator melewati batas waktu'}"
        if validation.outcome == ValidationOutcome.EXECUTION_ERROR:
            return f"Validation execution error: {validation.stderr or 'validator gagal dijalankan'}"
        return (
            f"Validation failed ({validation.outcome.value}): "
            f"{validation.stderr or validation.stdout or 'pekerjaan tidak memenuhi criteria'}"
        )

    def _validation_observation(self, plan: TaskPlan, validation: ValidationResult) -> Any:
        """Bangun Observation (model replanner) dari hasil validation.

        Validation failure dianggap recoverable (masih bisa diperbaiki) agar
        replanner dapat menyesuaikan strategi. TIMEOUT/EXECUTION_ERROR juga
        recoverable selama masih ada siklus tersisa.
        """
        from agent_ai.planning.replanner import Observation

        # Step terakhir yang dieksekusi (untuk dikaitkan ke observation).
        step_id = ""
        for step in reversed(plan.steps):
            if step.status in (StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.RUNNING):
                step_id = step.id
                break
        if not step_id and plan.steps:
            step_id = plan.steps[-1].id

        return Observation(
            step_id=step_id,
            success=False,
            outcome=f"validation:{validation.outcome.value}",
            error=validation.stderr or validation.stdout or None,
            recoverable=True,
            metadata={
                "validation_outcome": validation.outcome.value,
                "exit_code": validation.exit_code,
            },
        )

    # ------------------------------------------------------------------ #
    # Lifecycle helpers (opsional, tidak mengubah eksekusi)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _lifecycle_to_running(lifecycle: "TaskLifecycle") -> None:
        """Bawa lifecycle ke RUNNING bila memungkinkan (tanpa memaksa)."""
        from agent_ai.tasks.models import TaskStatus

        # PREPARING/PLANNING -> RUNNING, atau CREATED -> RUNNING bila belum.
        if lifecycle.status == TaskStatus.CREATED:
            if lifecycle.can_transition(TaskStatus.PREPARING):
                lifecycle.transition(TaskStatus.PREPARING)
        if lifecycle.can_transition(TaskStatus.RUNNING):
            lifecycle.transition(TaskStatus.RUNNING)

    @staticmethod
    def _lifecycle_to_validating(lifecycle: Optional["TaskLifecycle"]) -> None:
        """Bawa lifecycle ke VALIDATING bila memungkinkan (tanpa memaksa)."""
        if lifecycle is None or lifecycle.is_terminal:
            return
        from agent_ai.tasks.models import TaskStatus

        if lifecycle.can_transition(TaskStatus.VALIDATING):
            lifecycle.transition(TaskStatus.VALIDATING)

    # ------------------------------------------------------------------ #
    # Project-local storage (Task 5): Task Log + AI Project Bible
    # ------------------------------------------------------------------ #
    def _setup_project_storage(self, prepared: PreparedTask) -> None:
        """Siapkan Task Log + Project Brain (Bible) project-local (best-effort).

        Kegagalan apa pun di sini TIDAK boleh menggagalkan eksekusi; runtime
        tetap berjalan tanpa logging/knowledge. Bila execution masuk tanpa
        task_id, Task Log membuat task_id baru lebih dulu.
        """
        self._task_log = None
        self._brain = None
        self._response_log = None
        if not self.project_root:
            return
        try:
            from agent_ai.projects.aether_store import TaskLog

            self._task_log = TaskLog(self.project_root, task_id=self._current_task_id)
            # Task Log membuat task_id bila execution masuk tanpa task_id.
            self._current_task_id = self._task_log.task_id
        except Exception:  # noqa: BLE001 - logging tidak boleh menggagalkan task
            self._task_log = None
        # Log response API LLM (opt-in via `data/settings.json`).
        # Best-effort: kegagalan menyiapkan logger TIDAK menggagalkan task.
        try:
            from agent_ai.config.settings import write_log_response_api

            if write_log_response_api():
                from agent_ai.projects.aether_store import ResponseLog

                self._response_log = ResponseLog(
                    self.project_root, task_id=self._current_task_id
                )
        except Exception:  # noqa: BLE001 - logging tidak boleh menggagalkan task
            self._response_log = None
        self._log(
            "task_requested",
            {"prompt": prepared.task, "task_id": getattr(prepared, "task_id", None)},
        )
        if not self.project_brain_enabled:
            return
        try:
            from agent_ai.projects.brain import ProjectBrain

            self._brain = ProjectBrain.for_project(
                self.project_root, provider=self.provider, options=self.options
            )
        except Exception:  # noqa: BLE001 - knowledge tidak boleh menggagalkan task
            self._brain = None

    def _log(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Tulis satu event ke Task Log project-local (best-effort)."""
        log = self._task_log
        if log is None:
            return
        try:
            log.append(event_type, dict(payload or {}))
        except Exception:  # noqa: BLE001 - log tidak boleh crash
            return

    def _make_orchestrator(self, provider: BaseProvider) -> AgentOrchestrator:
        """Bangun AgentOrchestrator dengan Project Brain (context-only).

        Brain dipakai untuk context injection. Learning TIDAK dilakukan per
        run; runtime menanganinya sekali per task (__record_project_learning).
        Environment Context project-local (bila ada) diteruskan sebagai system
        message pada awal session continuous loop.
        """
        # System prompt default Agent: bila pemanggil TIDAK memberi system
        # prompt, pakai System Prompt Agent per-project
        # (`<project_root>/.aether/settings/agent.json` -> `system_prompt`).
        # Bila project TIDAK mengoverride, jatuh ke System Prompt Agent Global
        # Settings (`data/settings.json` -> `agent.system_prompt`, dikelola
        # lewat Sidebar -> Settings -> Agent). Bila user belum mengaturnya,
        # loader mengembalikan isi System Prompt Agent existing (default)
        # sehingga behavior AETHER tetap sama. Prompt ini adalah INSTRUCTION
        # DASAR Agent; context dinamis (Environment, Project Bible, Skill,
        # tool) tetap disisipkan seperti sebelumnya oleh orchestrator. Bila
        # pemanggil memberi system prompt sendiri (mis. Consultant), prompt itu
        # yang dipakai.
        system_prompt = self.system_prompt
        if system_prompt is None:
            from agent_ai.projects.project_settings import project_agent_system_prompt

            system_prompt = project_agent_system_prompt(self.project_root)
        if system_prompt is None:
            from agent_ai.config.settings import agent_system_prompt

            system_prompt = agent_system_prompt()
        return AgentOrchestrator(
            provider=provider,
            executor=self.executor,
            max_iterations=self.max_iterations,
            options=self.options,
            system_prompt=system_prompt,
            event_sink=self._event_sink,
            brain=self._brain,
            brain_learning=False,
            use_continuous_loop=self.use_continuous_loop,
            environment_context=self._current_environment_context,
            cancel_token=self.cancel_token,
            response_log=self._response_log,
            # Agent Execution Policy (fast/balanced/deep): metadata/strategi
            # (bukan hard limit). Diteruskan agar orchestrator TAHU preferensi
            # kerja aktif dan dapat membagikan policy terbaru ke pemanggil
            # (escalation in-place). Tidak mengubah keputusan loop LLM.
            execution_policy=self._policy_for_orchestrator(),
            policy_escalator=self.escalate_policy,
            # Working State (internal): provider teks per round agar LLM
            # mendapat pemahaman kerja terbaru pada setiap putaran.
            working_state_provider=lambda: self.working_state_text,
            # Provider Fallback pada jalur AKTIF (continuous loop): resolver
            # disuntikkan agar orchestrator dapat meminta provider alternatif
            # saat provider utama gagal (setelah retry habis). Orchestrator
            # tetap provider-agnostic (tidak mengimpor subsistem fallback).
            provider_fallback_resolver=self._provider_fallback_resolver,
            # Change evidence per task (OPSIONAL): daftar perubahan file yang
            # BENAR-BENAR terdeteksi (created/modified/deleted) dipakai sebagai
            # evidence untuk verification nudge existing. Tidak ada evaluator
            # completion baru: nudge tetap memutuskan lewat aturan existing.
            change_evidence=self.change_evidence,
        )

    def _session_environment_context(self) -> Optional[str]:
        """Environment Context project-local, dimuat SEKALI per session.

        Session = satu instance AgentRuntime. Pada task PERTAMA session, file
        `<project_root>/.aether/ENVIRONMENT.md` dibuat bila belum ada (atau
        dimuat bila sudah ada) dan dikembalikan untuk dijadikan system message.
        Task berikutnya pada session yang sama TIDAK membaca/menyusun ulang
        (mengembalikan None). Instance runtime baru = session baru -> deteksi
        ulang. Best-effort: kegagalan tidak boleh menggagalkan eksekusi task.
        """
        if self._environment_injected:
            return None
        self._environment_injected = True
        if not self.project_root:
            return None
        if self._environment_text is None:
            try:
                from agent_ai.projects.environment import build_or_load_environment

                self._environment_text = build_or_load_environment(self.project_root)
            except Exception:  # noqa: BLE001 - context tidak boleh menggagalkan task
                self._environment_text = ""
        text = (self._environment_text or "").strip()
        return text or None

    def _record_project_learning(self, result: RuntimeResult) -> None:
        """Update AI Project Bible dari hasil task (best-effort, sekali/task)."""
        if self._brain is None:
            return
        try:
            observations = self._build_learning_observations(result)
            if not observations:
                return
            learned = self._brain.learn(observations)
            summary = learned.to_dict() if hasattr(learned, "to_dict") else None
            self._log("bible_update", {"summary": summary})
        except Exception:  # noqa: BLE001 - update Bible tidak boleh menggagalkan task
            self._log("bible_update_failed", {})

    def _build_learning_observations(self, result: RuntimeResult) -> List[str]:
        """Bangun observations ringkas dari hasil task (bounded)."""
        prepared = getattr(self, "_current_prepared", None)
        task = getattr(prepared, "task", "") or ""
        observations: List[str] = [f"Task: {task}"]
        if result.result:
            observations.append(f"Result: {result.result}")
        if result.error:
            observations.append(f"Error: {result.error}")
        for step in (result.steps or [])[:20]:
            observation = step.get("observation") if isinstance(step, dict) else None
            if isinstance(observation, dict) and observation.get("success") and observation.get("content"):
                observations.append(f"Tool result: {observation['content']}")
        # Catat metadata perubahan file yang berhasil (compact, bounded).
        # Hanya mencatat tool_name + path, bukan old_text/new_text/content.
        for step in (result.steps or [])[:20]:
            observation = step.get("observation") if isinstance(step, dict) else None
            if not isinstance(observation, dict) or not observation.get("success"):
                continue
            action = step.get("action") if isinstance(step, dict) else None
            if not isinstance(action, dict):
                continue
            tool_name = action.get("name")
            if not tool_name:
                continue
            args = action.get("arguments") if isinstance(action.get("arguments"), dict) else {}
            operation = _FILE_OPERATION_TYPES.get(tool_name)
            if not operation:
                continue
            if tool_name == "move_file":
                source = args.get("source")
                destination = args.get("destination")
                path = f"{source} -> {destination}" if source and destination else None
            else:
                path = args.get("path")
            if path:
                observations.append(f"File change: operation={operation} tool={tool_name} path={path} status=success")
        return observations

    def _emit_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Emit execution event (opsional) memakai model event yang sudah ada.

        Tidak membuat event bus kedua: hanya append ke SessionStore yang
        diberikan. Bila store/session tidak ada, tidak melakukan apa-apa.
        Payload disanitasi (tanpa secret) sebelum dicatat. Event juga
        ditee ke Task Log project-local (bila ada).
        """
        self._log(event_type, payload)
        if self.session_store is None or not self.session_id:
            return
        try:
            from agent_ai.core.observability import sanitize_event_payload
            from agent_ai.session.events import EventType, make_event

            try:
                et = EventType(event_type)
            except ValueError:
                et = EventType.PHASE_CHANGED
            event = make_event(
                session_id=self.session_id,
                event_type=et,
                task_id=getattr(self, "_current_task_id", None),
                payload=sanitize_event_payload(event_type, payload),
            )
            self.session_store.append_event(event)
        except Exception:  # noqa: BLE001 - event emission tidak boleh crash
            return

    def _event_sink(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Adapter sink untuk AgentOrchestrator (#55).

        Meneruskan event dari orchestrator (tool/provider) ke SessionStore
        existing lewat `_emit_event`. Bila session store tidak ada, no-op.

        Titik TERPUSAT activity phase: setiap tool call Agent (event
        `tool_called`, dari orchestrator) diklasifikasi di sini menjadi
        activity phase dan memancarkan `phase_changed` SEBELUM tool
        dieksekusi. Dengan begitu tidak perlu menambahkan logic yang sama di
        tiap tempat pemanggilan tool.

        Juga memperbarui Working State dari observation secara DETERMINISTIK
        (bukan inferensi/LLM): mencatat file yang dibaca/diperiksa/diperbarui
        sehingga state internal tetap konsisten antar-round.
        """
        if event_type == "tool_called":
            # Registrasi kandidat perubahan SEGERA sebelum tool dieksekusi:
            # snapshot kondisi "sebelum" diambil deterministik dari tool call
            # LLM (bukan dari angka/tebakan), sehingga perubahan yang terjadi
            # dapat dibedakan created vs modified.
            self._register_tool_change_target(payload)
            self._emit_activity_phase_for_tool(payload)
        elif event_type == "agent_observation":
            self._update_working_state_from_observation(payload)
        elif event_type == "tool_result":
            # Efek filesystem NYATA (hanya setelah tool selesai): deteksi
            # perubahan dibatasi pada path yang memang disentuh task.
            self._mark_tool_effects(payload)
        self._emit_event(event_type, payload)

    def _register_tool_change_target(self, payload: Dict[str, Any]) -> None:
        """Catat path target tool mutasi sebagai kandidat perubahan (bounded).

        Dipanggil dari event `tool_called` (sebelum eksekusi) memakai argumen
        tool yang tersedia; bukan inferensi dari hasil.
        """
        tool = str(payload.get("tool") or "").strip().lower()
        if tool not in _FILE_OPERATION_TYPES:
            return
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            return
        if tool == "move_file":
            paths = (arguments.get("source"), arguments.get("destination"))
        else:
            paths = (arguments.get("path"),)
        self._register_change_target(*paths)

    def _update_working_state_from_observation(
        self, payload: Dict[str, Any]
    ) -> None:
        """Update Working State dari Agent Observation (deterministic).

        Ini BUKAN inferensi keputusan LLM — hanya pencatatan fakta struktural
        (file yang dibaca/ditulis) agar Working State tetap konsisten. LLM
        tetap satu-satunya pengambil keputusan; state ini hanya menyimpan
        jejak progres agar tersedia pada round berikutnya.

        Best-effort: kegagalan di sini TIDAK boleh menggagalkan task.
        """
        try:
            if self._working_state_manager is None:
                return
            tool = payload.get("tool") or payload.get("metadata", {}).get("tool")
            content = payload.get("content")
            if not tool or content is None:
                return
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except (ValueError, TypeError):
                    return
            if not isinstance(content, dict):
                return
            path = content.get("path")
            if tool in ("read_file", "view_image", "atlas_query", "rig_query"):
                if path:
                    self._working_state_manager.record_files_inspected(str(path))
            elif tool in ("write_file", "edit_file", "delete_file", "move_file",
                          "create_skill", "delete_skill", "update_skill"):
                if path and tool in _FILE_OPERATION_TYPES:
                    # HANYA tool filesystem: `path` argumennya benar-benar path
                    # file. Tool Skill memakai `path` sebagai identifier skill,
                    # bukan path filesystem (tidak boleh masuk daftar perubahan).
                    self._working_state_manager.record_files_changed(str(path))
        except Exception:  # noqa: BLE001 - state update tidak boleh crash
            return

    # ------------------------------------------------------------------ #
    # Change evidence per task (ChangeTracker existing, bounded detection)
    # ------------------------------------------------------------------ #
    def _begin_change_evidence(self) -> None:
        """Ikat change evidence ke task_id AKTUAL task yang sedang berjalan.

        Idempoten: `run()` dapat dipanggil ulang pada instance runtime yang
        sama (mis. verifier/gateway memakai satu runtime untuk beberapa task);
        `begin()` selalu mengembalikan state bersih untuk task_id tersebut.
        Best-effort: kegagalan TIDAK boleh menggagalkan eksekusi task.
        """
        evidence = self.change_evidence
        if evidence is None:
            return
        try:
            evidence.begin(self._current_task_id)
        except Exception:  # noqa: BLE001 - evidence tidak boleh crash task
            return

    def _register_change_target(self, *paths: Any) -> None:
        """Catat path mutasi sebagai kandidat + snapshot kondisi "sebelum".

        Dipanggil SEGERA sebelum tool mutasi dijalankan (deterministik dari
        tool call LLM), sehingga perbandingan hash/size berikutnya dapat
        membedakan file yang DIBUAT vs DIUBAH.

        Read-only & bounded: hanya file yang benar-benar disentuh task.
        """
        evidence = self.change_evidence
        if evidence is None:
            return
        for path in paths:
            try:
                evidence.register_path(path, task_id=self._current_task_id)
            except Exception:  # noqa: BLE001 - snapshot tidak boleh crash task
                continue

    def _mark_tool_effects(self, payload: Dict[str, Any]) -> None:
        """Tandai efek filesystem NYATA dari satu tool call yang selesai.

        Sumber: event `tool_result` (payload eksekusi apa adanya) dari
        orchestrator — bukan interpretasi tambahan.

        * Mutasi berhasil (status=success) -> deteksi perubahan dibatasi pada
          path yang dilaporkan tool (bounded, bukan full-project hashing).
        * Mutasi GAGAL -> path dihapus dari kandidat agar tidak ada perubahan
          semu yang terdeteksi untuk file yang tidak tersentuh.
        * Command berhasil (`run_command`) -> deteksi ulang pada kandidat yang
          sudah terdaftar, untuk menangkap file yang ditulis lewat command.
        """
        evidence = self.change_evidence
        if evidence is None:
            return
        tool = str(payload.get("tool") or "").strip().lower()
        if tool not in _FILE_OPERATION_TYPES and tool != "run_command":
            return
        try:
            if tool == "run_command":
                if payload.get("success"):
                    # `run_command` tidak melaporkan path: file baru yang
                    # ditulis command hanya dapat ditangkap dengan memindai
                    # SCOPE kandidat yang sudah terdaftar (bounded, bukan
                    # seluruh project). Bila task belum punya kandidat, tidak
                    # ada yang dipindai -> tidak ada perubahan yang dikarang.
                    discover = getattr(evidence, "discover_from_candidates", None)
                    if callable(discover):
                        discover(task_id=self._current_task_id)
                    evidence.detect(task_id=self._current_task_id)
                return
            output = payload.get("output")
            path = None
            old_path = None
            if isinstance(output, dict):
                path = output.get("path")
                old_path = output.get("old_path")
            elif isinstance(output, str):
                try:
                    parsed = json.loads(output)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    path = parsed.get("path")
                    old_path = parsed.get("old_path")
            if old_path:
                # Move: source berhenti ada di lokasi lama. Snapshot source
                # SUDAH tercatat sebelum tool berjalan; menghentikan tracking
                # di sana mencegah laporan 'deleted' semu.
                evidence.unregister_path(old_path, task_id=self._current_task_id)
            if not path:
                return
            if not payload.get("success"):
                evidence.unregister_path(path, task_id=self._current_task_id)
                return
            evidence.detect(task_id=self._current_task_id)
        except Exception:  # noqa: BLE001 - evidence tidak boleh crash task
            return

    def _finish_change_evidence(self) -> None:
        """Tutup change evidence task dan lampirkan ringkasannya ke Working State.

        Ringkasan berisi fakta yang benar-benar terdeteksi (path + kind);
        tidak mengarang jenis perubahan atau hasil validasi.
        """
        evidence = self.change_evidence
        if evidence is None:
            return
        try:
            evidence.finish(self._current_task_id)
            summary = evidence.summary(self._current_task_id)
        except Exception:  # noqa: BLE001 - evidence tidak boleh crash task
            return
        if not summary:
            return
        try:
            manager = self._working_state_manager
            if manager is None:
                return
            for item in summary:
                path = item.get("path")
                kind = item.get("kind")
                if not path:
                    continue
                manager.record_files_changed(f"{path} ({kind})")
        except Exception:  # noqa: BLE001 - state update tidak boleh crash
            return

    def _emit_activity_phase_for_tool(self, payload: Dict[str, Any]) -> None:
        """Klasifikasi payload `tool_called` -> activity phase (terpusat)."""
        try:
            from agent_ai.runtime.activity import classify_tool_activity

            phase = classify_tool_activity(
                payload.get("tool"), payload.get("arguments")
            )
        except Exception:  # noqa: BLE001 - klasifikasi tidak boleh menggagalkan task
            return
        if phase is None:
            return
        self._set_activity_phase(phase.value)

    def _set_activity_phase(self, phase: str) -> None:
        """Set + emit activity phase (dedup: hanya bila phase berubah).

        Tidak mengubah `TaskPhase` internal maupun status task; hanya
        memancarkan event `phase_changed` (model event yang sudah ada) agar
        frontend dapat mengikuti aktivitas Agent.
        """
        if phase == self._activity_phase:
            return
        self._activity_phase = phase
        self._emit_event("phase_changed", {"phase": phase})

    # ------------------------------------------------------------------ #
    # Agent Execution Policy (fast/balanced/deep) — informasi/strategi
    # ------------------------------------------------------------------ #
    def _resolve_policy(self, prepared: PreparedTask) -> Optional[ExecutionPolicyState]:
        """Resolve requested_mode -> effective_mode untuk task ini (per-run).

        Mode dibaca dari argumen runtime (`requested_mode`) atau metadata
        PreparedTask (kunci `agent_mode`/`policy_mode`/`mode`), lalu
        dinormalisasi oleh `ExecutionPolicyResolver` (default: balanced).
        Bila tidak ada sumber mode apa pun, policy TIDAK diaktifkan (None) agar
        perilaku runtime lama persis seperti sebelumnya (backward compatible).

        Policy BUKAN hard limit dan tidak menggerakkan keputusan loop: ia hanya
        metadata/strategi. Escalation terjadi lewat `_escalate_policy` yang
        dipanggil dari keputusan Agent/LLM (bukan heuristic).
        """
        if self.policy_resolver is None:
            self.policy = None
            return None
        metadata: Dict[str, Any] = {}
        prepared_metadata = getattr(prepared, "metadata", None)
        if isinstance(prepared_metadata, dict):
            metadata = prepared_metadata
        requested = self.requested_mode
        if (requested is None or not str(requested).strip()) and not any(
            isinstance(metadata.get(key), str) and metadata.get(key).strip()
            for key in ("agent_mode", "policy_mode", "mode")
        ):
            # Tidak ada mode dari mana pun -> policy tidak aktif (behavior lama).
            self.policy = None
            return None
        state = self.policy_resolver.resolve(requested, metadata=metadata)
        state = self._apply_policy_escalation(state, metadata)
        self.policy = state
        self.verification_strategy = strategy_for_mode(state.effective_mode)
        self._emit_verification_strategy(self.verification_strategy)
        self._emit_policy_applied(state)
        return state

    def _apply_policy_escalation(
        self,
        state: ExecutionPolicyState,
        metadata: Mapping[str, Any],
    ) -> ExecutionPolicyState:
        """Terapkan escalation DEKLARATIF dari metadata, bila ada.

        Metadata `escalate_to` (+ `escalate_reason`) adalah MEKANISME yang
        dipakai pemanggil/Agent untuk MENYATAKAN permintaan escalation. Ini
        bukan rule/heuristic: tidak ada pembacaan keyword task di sini.

        Best-effort: permintaan tidak valid TIDAK menggagalkan task; hanya
        diabaikan (task tetap berjalan dengan mode sebelumnya).
        """
        target = metadata.get("escalate_to") if isinstance(metadata, Mapping) else None
        if not (isinstance(target, str) and target.strip()):
            return state
        reason = metadata.get("escalate_reason")
        if not (isinstance(reason, str) and reason.strip()):
            reason = "Escalation diminta oleh task metadata."
        try:
            self.policy_resolver.escalate(state, str(reason), target_mode=target)
        except Exception:  # noqa: BLE001 - permintaan tidak valid tidak boleh crash
            return state
        return state

    def _emit_policy_applied(self, state: ExecutionPolicyState) -> None:
        """Emit event `policy_applied` (event system existing; best-effort).

        Payload memuat ringkasan policy + `activity` (blok teks siap baca pada
        activity/log, termasuk blok escalation bila ada).
        """
        try:
            payload = state.to_dict()
            payload["activity"] = policy_activity_text(state)
            self._emit_event("policy_applied", payload)
        except Exception:  # noqa: BLE001 - observability tidak boleh crash
            return

    def escalate_policy(
        self,
        reason: str,
        target_mode: Optional[str] = None,
    ) -> Optional[ExecutionPolicyState]:
        """Mekanisme escalation policy: naikkan effective_mode + emit event.

        Dipanggil oleh Agent/LLM (keputusan), BUKAN oleh rule otomatis AETHER:
        runtime hanya MENYEDIAKAN mekanismenya, termasuk memperbarui policy yang
        dipakai orchestrator pada putaran berikutnya (state hidup di satu
        instance `ExecutionPolicyState` yang dibagikan runtime <-> orchestrator).
        De-escalation TIDAK didukung (bukan escalation).

        Args:
            reason: alasan escalation (wajib non-kosong).
            target_mode: mode tujuan opsional (default: satu tingkat di atas).

        Returns:
            State policy yang sudah diperbarui (untuk traceability), atau None
            bila policy tidak aktif.

        Raises:
            PolicyEscalationError: alasan kosong / arah tidak maju.
        """
        state = self.policy
        if state is None:
            return None
        previous = state.effective_mode
        state = self.policy_resolver.escalate(state, reason, target_mode=target_mode)
        self.policy = state
        self.verification_strategy = strategy_for_mode(state.effective_mode)
        # Metadata escalation untuk observability/UI (tanpa menyentuh loop).
        payload = state.to_dict()
        payload.update(
            {
                "from_mode": previous,
                "to_mode": state.effective_mode,
                "reason": state.reason,
                "activity": policy_activity_text(state, previous_mode=previous),
            }
        )
        self._emit_event("policy_escalated", payload)
        self._emit_verification_strategy(self.verification_strategy, previous_mode=previous)
        return state

    def _emit_verification_strategy(
        self,
        strategy: VerificationStrategy,
        previous_mode: Optional[Any] = None,
    ) -> None:
        """Emit telemetry [VERIFY] untuk strategi mode yang sedang aktif."""
        try:
            payload = strategy.to_dict()
            payload["activity"] = format_verification_activity(
                strategy, previous_mode=previous_mode
            )
            self._emit_event("verification_strategy_applied", payload)
        except Exception:  # noqa: BLE001 - telemetry tidak boleh menggagalkan task
            return

    def _policy_for_orchestrator(self) -> Optional[Dict[str, Any]]:
        """Policy yang diteruskan ke orchestrator (dict ringkas) atau None."""
        state = self.policy
        if state is None:
            return None
        return state.to_dict()

    def _lifecycle_finalize(self, lifecycle: Optional["TaskLifecycle"], result: RuntimeResult) -> None:
        """Sinkronkan status akhir runtime ke lifecycle + emit event terminal.

        Bila lifecycle diisi, status akhir disinkronkan (COMPLETED/FAILED).
        Event terminal (task_completed/task_failed) diemit memakai model event
        yang sudah ada (bila session store tersedia).
        """
        cancelled = result.status == RuntimeStatus.CANCELLED

        # Execution Policy (fast/balanced/deep): lampirkan ringkasan policy
        # (requested vs effective) ke hasil runtime. Metadata-only; tidak
        # mengubah status/loop. None bila policy tidak aktif.
        try:
            if hasattr(result, "policy"):
                result.policy = self._policy_for_orchestrator()
        except Exception:  # noqa: BLE001 - metadata tidak boleh menggagalkan task
            pass

        # Final Agent Report = output final LLM. Ini SATU-SATUNYA field yang
        # dikirim verbatim (tanpa pemotongan sanitasi): report yang ditampilkan
        # ke user harus utuh dari LLM -> log/SSE -> frontend. Batas payload
        # activity log / tool output internal TIDAK diubah.
        from agent_ai.core.observability import verbatim

        report_text = verbatim(result.result) if result.result is not None else None

        # Update AI Project Bible (sekali per task) lalu catat status akhir.
        # Task yang di-CANCEL TIDAK meng-update Bible: hasilnya parsial dan
        # tidak boleh menjadi knowledge project (hindari retry/learning).
        if not cancelled:
            self._record_project_learning(result)
        self._log(
            "task_finished",
            {
                "status": result.status.value,
                "result": report_text,
                "error": result.error,
            },
        )

        if result.status == RuntimeStatus.COMPLETED:
            self._emit_event("task_completed", {"result": report_text})
        elif cancelled:
            # Event terminal AETHER existing (task_cancelled) supaya Task
            # History/Agent Activity mengetahui task dihentikan, bukan selesai.
            self._emit_event("task_cancelled", {"reason": result.error})
        else:
            self._emit_event("task_failed", {"error": result.error})

        if lifecycle is None or lifecycle.is_terminal:
            return
        from agent_ai.tasks.models import TaskStatus

        if result.status == RuntimeStatus.COMPLETED:
            if lifecycle.can_transition(TaskStatus.COMPLETED):
                lifecycle.complete(result=result.result)
        elif cancelled:
            if lifecycle.can_transition(TaskStatus.CANCELLED):
                lifecycle.cancel(reason=result.error)
        else:
            if lifecycle.can_transition(TaskStatus.FAILED):
                lifecycle.fail(result.error or "Runtime gagal.")

    # ------------------------------------------------------------------ #
    # Step execution
    # ------------------------------------------------------------------ #
    def _run_step(
        self,
        prepared: PreparedTask,
        step: PlanStep,
        progress: RuntimeProgress,
    ) -> Any:
        """Jalankan satu step plan lewat AgentOrchestrator.

        Bila Provider Fallback aktif dan step gagal karena provider error,
        runtime berpindah ke provider alternatif dan menjalankan ulang step
        (bounded). Runtime tetap satu-satunya executor.

        Mengembalikan objek dengan atribut `.success`, `.result`, `.error`,
        `.iterations` (yaitu OrchestratorResult).
        """
        step_task = self._build_step_task(prepared, step)
        provider = self.provider

        while True:
            orchestrator = self._make_orchestrator(provider)
            result = orchestrator.run(step_task)
            progress.iteration += max(1, getattr(result, "iterations", 0))

            # Fallback hanya untuk kegagalan provider (bukan tool/command).
            if getattr(result, "success", False) or not getattr(result, "provider_error", False):
                return result
            if self.fallback_manager is None or not self.fallback_manager.enabled:
                return result

            new_provider = self._try_provider_fallback(prepared, step, result)
            if new_provider is None:
                return result
            provider = new_provider
            # Persist provider alternatif agar step berikutnya memakainya
            # (task state tetap dilanjutkan, bukan direset).
            self.provider = new_provider

    def _try_provider_fallback(
        self,
        prepared: PreparedTask,
        step: PlanStep,
        result: Any,
    ) -> Optional[BaseProvider]:
        """Konsultasikan FallbackManager saat provider error.

        Returns:
            Provider alternatif (BaseProvider) bila fallback diputuskan dan
            dapat dibangun; None bila tidak (runtime tetap pakai provider lama).
        """
        from agent_ai.fallback.models import FallbackAction, FallbackRequest

        fm = self.fallback_manager
        reason = fm.classify_error_from_message(result.error or "")
        request = FallbackRequest(
            current_provider=getattr(self.provider, "name", ""),
            current_model=self._current_model_name(),
            reason=reason,
            required_capabilities=self._required_capabilities(),
            context_tokens=self._context_token_requirement(),
            transient=fm.policy.is_transient(reason),
            attempts=fm.attempts,
            metadata={"task_id": getattr(prepared, "task_id", None), "step_id": step.id},
        )
        decision = fm.fallback(request)
        self._emit_event(
            "phase_changed",
            {"phase": "provider_fallback", "action": decision.action.value, "reason": decision.reason.value},
        )
        if decision.action != FallbackAction.FALLBACK or decision.selected is None:
            return None
        if self.provider_factory is None:
            return None
        try:
            return self.provider_factory(decision.selected.provider)
        except Exception:  # noqa: BLE001 - gagal membangun provider -> tidak fallback
            return None

    def _current_model_name(self) -> str:
        """Nama model provider aktif (dari options bila ada)."""
        if self.options is not None and getattr(self.options, "model", None):
            return self.options.model
        return ""

    def _provider_fallback_resolver(self, error: BaseException) -> Optional[BaseProvider]:
        """Resolver fallback untuk JALUR AKTIF (continuous loop).

        Dipanggil AgentOrchestrator (via callback yang disuntikkan) HANYA
        setelah seluruh retry provider AKTIF habis pada satu pemanggilan LLM.
        Mengembalikan provider alternatif BILA:

            1. fallback aktif (`self.fallback_manager` ada & enabled),
            2. `FallbackManager` memutuskan FALLBACK (kegagalan provider, ada
               kandidat eligible, kuota fallback belum habis),
            3. provider alternatif berhasil dibangun (`self.provider_factory`).

        Provider alternatif juga di-persist ke `self.provider` agar request
        berikutnya pada task ini memakai provider yang sama (task state tetap
        dilanjutkan, riwayat percakapan TIDAK direset).

        Catatan boundary: runtime TIDAK membuat runtime/executor kedua; ia hanya
        mengorkestrasi (keputusan tetap milik FallbackManager). Bila tidak ada
        fallback, mengembalikan None (orchestrator menutup lifecycle sebagai
        FAILED seperti perilaku lama).
        """
        fm = self.fallback_manager
        if fm is None or not fm.enabled:
            return None
        if self.provider_factory is None:
            return None

        from agent_ai.fallback.models import FallbackAction, FallbackRequest

        reason = fm.classify_error(error)
        request = FallbackRequest(
            current_provider=getattr(self.provider, "name", ""),
            current_model=self._current_model_name(),
            reason=reason,
            required_capabilities=self._required_capabilities(),
            context_tokens=self._context_token_requirement(),
            transient=fm.policy.is_transient(reason),
            attempts=fm.attempts,
            metadata={"task_id": getattr(self, "_current_task_id", None)},
        )
        try:
            decision = fm.fallback(request)
        except Exception:  # noqa: BLE001 - keputusan gagal -> tidak fallback
            return None

        self._emit_event(
            "phase_changed",
            {
                "phase": "provider_fallback",
                "action": decision.action.value,
                "reason": decision.reason.value,
            },
        )
        if decision.action != FallbackAction.FALLBACK or decision.selected is None:
            return None
        try:
            new_provider = self.provider_factory(decision.selected.provider)
        except Exception:  # noqa: BLE001 - gagal membangun provider -> tidak fallback
            return None
        if new_provider is None:
            return None
        # Persist agar request berikutnya pada task ini memakai provider yang
        # sama; task state (riwayat/loop) tetap dilanjutkan, bukan direset.
        self.provider = new_provider
        return new_provider

    def _required_capabilities(self) -> Any:
        """Capability wajib untuk fallback (dari prepared.metadata bila ada)."""
        try:
            meta = getattr(self._current_prepared, "metadata", None) or {}
            return frozenset(meta.get("required_capabilities", []))
        except Exception:  # noqa: BLE001
            return frozenset()

    def _context_token_requirement(self) -> Optional[int]:
        """Kebutuhan context token untuk fallback (dari prepared.metadata bila ada)."""
        try:
            meta = getattr(self._current_prepared, "metadata", None) or {}
            return meta.get("context_tokens")
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _record_step_failure(step: PlanStep, step_result: Any) -> None:
        """Catat outcome kegagalan terstruktur pada metadata step.

        Recovery mengklasifikasi berdasarkan outcome terstruktur ini (bukan
        string exception). Sumber: steps OrchestratorResult (observation).
        """
        outcome = "execution_error"
        is_command = False
        is_tool = False
        steps = getattr(step_result, "steps", None) or []
        for s in reversed(steps):
            obs = s.get("observation") if isinstance(s, dict) else None
            if not obs:
                continue
            meta = obs.get("metadata") or {}
            if meta.get("command_failure"):
                outcome = "command_failure"
                is_command = True
            elif not obs.get("success", True):
                outcome = "execution_error"
                is_tool = True
            break
        step.metadata["outcome"] = outcome
        step.metadata["is_command"] = is_command
        step.metadata["is_tool"] = is_tool

    def _run_continuous(
        self,
        prepared: PreparedTask,
        progress: RuntimeProgress,
        *,
        user_parts: Optional[List[Dict[str, Any]]] = None,
    ) -> RuntimeResult:
        """Jalankan task sebagai SATU percakapan kontinu (Native Tool Calling).

        Ini jalur normal AgentRuntime: task TIDAK dipecah menjadi TaskStep, dan
        TIDAK memanggil planner/loop per-step. AgentOrchestrator menjalankan
        continuous loop satu percakapan; ia meminta tool, mengeksekusinya via
        ToolExecutor, mengembalikan hasil sebagai role "tool", lalu melanjutkan
        sampai LLM memberi response final (DONE) atau terjadi error (FAILED).

        Plan (bila ada) hanya dipakai sebagai context ADVISORY opsional; ia
        tidak menentukan urutan pemanggilan tool.

        user_parts: content blocks opsional (mis. image) untuk pesan user awal,
        diteruskan ke AgentOrchestrator.run. Kosong = text-only tidak berubah.
        """
        progress.current_step = prepared.task
        task_text = self._build_continuous_task(prepared)
        orchestrator = self._make_orchestrator(self.provider)
        # `verification_task` = task ASLI user (bukan teks + plan/advisory) agar
        # kebutuhan verifikasi dinilai dari permintaan user, bukan saran plan.
        result = orchestrator.run(
            task_text,
            user_parts=user_parts,
            verification_task=prepared.task,
        )
        progress.iteration += max(1, getattr(result, "iterations", 0))

        # Cancellation (cooperative): loop berhenti di safe boundary -> task
        # CANCELLED (bukan FAILED/COMPLETED). Tidak ada retry lanjutan.
        if result.status == AgentStatus.CANCELLED:
            progress.current_step = None
            return RuntimeResult(
                status=RuntimeStatus.CANCELLED,
                result=None,
                error=result.error or "Task dibatalkan (user stop).",
                progress=progress,
                steps=[],
                iterations=progress.iteration,
            )

        if result.status == AgentStatus.DONE:
            progress.completed_steps.append(prepared.task)
            progress.current_step = None
            return RuntimeResult(
                status=RuntimeStatus.COMPLETED,
                result=result.result,
                error=None,
                progress=progress,
                steps=[],
                iterations=progress.iteration,
                evidence=getattr(result, "evidence", None),
            )

        progress.failed_step = prepared.task
        progress.current_step = None
        return RuntimeResult(
            status=RuntimeStatus.FAILED,
            result=None,
            error=result.error or "Task gagal.",
            progress=progress,
            steps=[],
            iterations=progress.iteration,
            evidence=getattr(result, "evidence", None),
        )

    def _run_single(
        self,
        prepared: PreparedTask,
        progress: RuntimeProgress,
        *,
        user_parts: Optional[List[Dict[str, Any]]] = None,
    ) -> RuntimeResult:
        """Jalankan task tanpa plan (satu langkah tunggal) - jalur legacy."""
        progress.current_step = prepared.task
        orchestrator = self._make_orchestrator(self.provider)
        result = orchestrator.run(self._build_task(prepared), user_parts=user_parts)
        progress.iteration += max(1, getattr(result, "iterations", 0))

        if result.status == AgentStatus.DONE:
            progress.completed_steps.append(prepared.task)
            return RuntimeResult(
                status=RuntimeStatus.COMPLETED,
                result=result.result,
                error=None,
                progress=progress,
                steps=[],
                iterations=progress.iteration,
            )
        progress.failed_step = prepared.task
        return RuntimeResult(
            status=RuntimeStatus.FAILED,
            result=None,
            error=result.error or "Task gagal.",
            progress=progress,
            steps=[],
            iterations=progress.iteration,
        )

    # ------------------------------------------------------------------ #
    # Task/message building
    # ------------------------------------------------------------------ #
    def _build_task(self, prepared: PreparedTask) -> str:
        """Bangun teks task dari PreparedTask (task + context)."""
        parts: List[str] = [prepared.task]
        context_text = prepared.context_text()
        if context_text:
            parts.append("\n# Context\n" + context_text)
        verification_advisory = self._verification_advisory()
        if verification_advisory:
            parts.append("\n# Strategi verifikasi (advisory, tidak mengikat)\n" + verification_advisory)
        return "\n".join(parts)

    def _build_step_task(self, prepared: PreparedTask, step: PlanStep) -> str:
        """Bangun teks task untuk satu step plan (task + context + step).

        Hanya dipakai jalur legacy (`use_continuous_loop=False`).
        """
        parts: List[str] = [
            f"Task: {prepared.task}",
            f"\nCurrent step: {step.title}",
        ]
        if step.description:
            parts.append(f"Step description: {step.description}")
        context_text = prepared.context_text()
        if context_text:
            parts.append("\n# Context\n" + context_text)
        return "\n".join(parts)

    def _build_continuous_task(self, prepared: PreparedTask) -> str:
        """Bangun teks task untuk continuous loop (task + context + advisory).

        Berbeda dengan jalur legacy, task TIDAK dipecah per step: seluruh
        pekerjaan diberikan sebagai satu permintaan, dengan plan (bila ada)
        hanya menjadi saran pendekatan yang tidak mengikat.
        """
        parts: List[str] = [prepared.task]
        context_text = prepared.context_text()
        if context_text:
            parts.append("\n# Context\n" + context_text)
        advisory = self._advisory_context(prepared)
        if advisory:
            parts.append("\n# Saran pendekatan (advisory, tidak mengikat)\n" + advisory)
        working_state_text = self.working_state_text
        if working_state_text:
            parts.append("\n# Working State (internal)\n" + working_state_text)
        verification_advisory = self._verification_advisory()
        if verification_advisory:
            parts.append(
                "\n# Strategi verifikasi (advisory, tidak mengikat)\n" + verification_advisory
            )
        return "\n".join(parts)

    def _verification_advisory(self) -> str:
        """Saran verifikasi berdasarkan mode efektif (advisory-only).

        Tidak mengubah keputusan loop maupun completion: hanya memberikan
        preferensi check kepada Agent (LLM). None bila policy tidak aktif
        (backward compatible).
        """
        strategy = self.verification_strategy
        if strategy is None:
            return ""
        lines = [
            f"Mode: {strategy.mode.title()}",
            "Preferensi verifikasi (tidak mengikat; LLM tetap menentukan langkah):",
        ]
        for check in strategy.checks:
            lines.append(f"- {check}")
        if strategy.mode == "fast":
            lines.append(
                "Fast tidak menghalangi test tambahan bila memang dibutuhkan; "
                "hindari regression besar secara default kecuali ada alasan kuat."
            )
        elif strategy.mode == "deep":
            lines.append(
                "Untuk perubahan besar, pertimbangkan membuat Git checkpoint "
                "(stash/commit) sebelum mengubah, lihat diff yang lebih luas, "
                "dan review impact area sebelum final."
            )
        return "\n".join(lines)

    @staticmethod
    def _advisory_context(prepared: PreparedTask) -> str:
        """Hasilkan saran pendekatan dari plan (ADVISORY-ONLY, deterministik).

        Plan TIDAK menentukan urutan tool: teks ini hanya dikirim sebagai
        context opsional dan LLM tetap memutuskan langkah & tool sendiri.
        Tidak memanggil planner/LLM apa pun di sini.
        """
        plan = prepared.plan
        if plan is None or not plan.steps:
            return ""
        titles = [step.title for step in plan.steps if step.title]
        if not titles:
            return ""
        return (
            "Rencana berikut hanya SARAN (tidak mengikat) dan TIDAK menentukan "
            "urutan pemanggilan tool. Kamu tetap memutuskan sendiri langkah "
            "dan tool yang dipakai:\n- " + "\n- ".join(titles)
        )

    @staticmethod
    def _final_result(prepared: PreparedTask, plan: TaskPlan) -> str:
        """Hasil akhir ringkas setelah semua step selesai."""
        completed = [s.title for s in plan.steps if s.status == StepStatus.COMPLETED]
        return (
            f"Task selesai: {prepared.task}\n"
            f"Langkah yang diselesaikan: {', '.join(completed) or '(tidak ada)'}"
        )
