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

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from agent_ai.core.cancel import CancellationToken
from agent_ai.core.executor import ToolExecutor
from agent_ai.core.orchestrator import AgentOrchestrator
from agent_ai.core.models import AgentStatus
from agent_ai.planning.models import PlanStep, StepStatus, TaskPlan
from agent_ai.providers.base import BaseProvider, GenerateOptions
from agent_ai.runtime.models import RuntimeProgress, RuntimeResult, RuntimeStatus
from agent_ai.task.models import PreparedTask
from agent_ai.validation.models import (
    ValidationOutcome,
    ValidationRequest,
    ValidationResult,
)

if TYPE_CHECKING:  # pragma: no cover - hanya untuk type hint, hindari import cycle
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
        max_validation_cycles: Optional[int] = None,
        stop_on_validation_failure: Optional[bool] = None,
        recovery_manager: Optional["RecoveryManager"] = None,
        fallback_manager: Optional["FallbackManager"] = None,
        provider_factory: Optional[Any] = None,
        project_root: Optional[str] = None,
        project_brain: bool = True,
        use_continuous_loop: bool = True,
        cancel_token: Optional[CancellationToken] = None,
    ) -> None:
        if provider is None:
            raise ValueError("AgentRuntime butuh provider (BaseProvider).")
        self.provider = provider
        # Cooperative cancellation (opsional). Diteruskan ke AgentOrchestrator
        # agar loop berhenti di safe boundary saat user menekan Stop. Bukan
        # sistem cancellation kedua: token tunggal milik gateway per task.
        self.cancel_token = cancel_token
        self.executor = executor or ToolExecutor()
        self.max_iterations = max_iterations
        self.options = options
        self.system_prompt = system_prompt
        # Jalur eksekusi NORMAL (default True): continuous loop Native Tool
        # Calling -> satu percakapan kontinu per task, tanpa pemecahan TaskStep.
        # Set False untuk memakai jalur legacy (per step plan + recovery).
        self.use_continuous_loop = use_continuous_loop

        # Project-local storage (Task 5): root project target. Bila diisi,
        # runtime menulis `.aether/log/<task_id>.log` dan memakai AI Project
        # Bible project-local (`.aether/bible`) lewat ProjectBrain.
        self.project_root = project_root
        self.project_brain_enabled = project_brain
        self._task_log: Optional[Any] = None
        self._brain: Optional[Any] = None
        # Environment Context project-local (`<project_root>/.aether/ENVIRONMENT.md`).
        # Dibuat/dimuat SEKALI per session (instance runtime); hasilnya di-cache
        # di `_environment_text` dan hanya disuntikkan pada task pertama.
        self._environment_text: Optional[str] = None
        self._environment_injected: bool = False
        # Environment Context untuk task berjalan (diteruskan ke orchestrator
        # continuous loop). None pada jalur legacy / non-continuous.
        self._current_environment_context: Optional[str] = None

        # Validation <-> Runtime Integration (#42), semuanya OPSIONAL.
        # Bila validation_runner/validation_request tidak diberikan, runtime
        # berperilaku persis seperti sebelumnya (backward compatible).
        self.validation_runner = validation_runner
        self.validation_request = validation_request
        self.replanner = replanner
        self.session_store = session_store
        self.session_id = session_id
        self.change_tracker = change_tracker

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
    # Public API
    # ------------------------------------------------------------------ #
    def run(
        self,
        prepared: PreparedTask,
        lifecycle: Optional["TaskLifecycle"] = None,
    ) -> RuntimeResult:
        """Jalankan PreparedTask dan kembalikan RuntimeResult.

        Args:
            prepared: PreparedTask (task + context + plan).
            lifecycle: TaskLifecycle opsional. Bila diisi, runtime akan
                menggerakkan transition status (RUNNING -> VALIDATING ->
                COMPLETED/FAILED) tanpa mengubah perilaku eksekusi. Bila None,
                perilaku sama seperti sebelumnya.

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

        # Observability (#55): catat task dimulai (bila session store tersedia).
        self._emit_event("task_started", {"task": prepared.task})

        # Lifecycle (opsional): tandai eksekusi dimulai.
        if lifecycle is not None:
            self._lifecycle_to_running(lifecycle)

        # Jalur NORMAL (default): SATU percakapan kontinu (continuous loop
        # Native Tool Calling). Task TIDAK dipecah menjadi TaskStep; plan
        # (bila ada) hanya context advisory. Tidak ada pemanggilan per-step.
        if self.use_continuous_loop:
            self._current_environment_context = self._session_environment_context()
            result = self._run_continuous(prepared, progress)
            result = self._maybe_validate(prepared, progress, result, lifecycle)
            self._lifecycle_finalize(lifecycle, result)
            return result

        # --- Jalur legacy (use_continuous_loop=False) ---
        # Tanpa plan: jalankan task sebagai satu langkah tunggal.
        if plan is None or not plan.steps:
            result = self._run_single(prepared, progress)
            result = self._maybe_validate(prepared, progress, result, lifecycle)
            self._lifecycle_finalize(lifecycle, result)
            return result

        # Multi-step: jalankan tiap step plan secara berurutan.
        # Recovery (opsional) mengorkestrasi tindakan saat step gagal.
        result = self._run_plan_with_recovery(prepared, plan, progress)

        # Validation (opsional): hanya bila execution sukses & validation aktif.
        result = self._maybe_validate(prepared, progress, result, lifecycle)

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
        if not self.project_root:
            return
        try:
            from agent_ai.projects.aether_store import TaskLog

            self._task_log = TaskLog(self.project_root, task_id=self._current_task_id)
            # Task Log membuat task_id bila execution masuk tanpa task_id.
            self._current_task_id = self._task_log.task_id
        except Exception:  # noqa: BLE001 - logging tidak boleh menggagalkan task
            self._task_log = None
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
        return AgentOrchestrator(
            provider=provider,
            executor=self.executor,
            max_iterations=self.max_iterations,
            options=self.options,
            system_prompt=self.system_prompt,
            event_sink=self._event_sink,
            brain=self._brain,
            brain_learning=False,
            use_continuous_loop=self.use_continuous_loop,
            environment_context=self._current_environment_context,
            cancel_token=self.cancel_token,
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
            from agent_ai.core.observability import sanitize_payload
            from agent_ai.session.events import EventType, make_event

            try:
                et = EventType(event_type)
            except ValueError:
                et = EventType.PHASE_CHANGED
            event = make_event(
                session_id=self.session_id,
                event_type=et,
                task_id=getattr(self, "_current_task_id", None),
                payload=sanitize_payload(dict(payload or {})),
            )
            self.session_store.append_event(event)
        except Exception:  # noqa: BLE001 - event emission tidak boleh crash
            return

    def _event_sink(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Adapter sink untuk AgentOrchestrator (#55).

        Meneruskan event dari orchestrator (tool/provider) ke SessionStore
        existing lewat `_emit_event`. Bila session store tidak ada, no-op.
        """
        self._emit_event(event_type, payload)

    def _lifecycle_finalize(self, lifecycle: Optional["TaskLifecycle"], result: RuntimeResult) -> None:
        """Sinkronkan status akhir runtime ke lifecycle + emit event terminal.

        Bila lifecycle diisi, status akhir disinkronkan (COMPLETED/FAILED).
        Event terminal (task_completed/task_failed) diemit memakai model event
        yang sudah ada (bila session store tersedia).
        """
        cancelled = result.status == RuntimeStatus.CANCELLED

        # Update AI Project Bible (sekali per task) lalu catat status akhir.
        # Task yang di-CANCEL TIDAK meng-update Bible: hasilnya parsial dan
        # tidak boleh menjadi knowledge project (hindari retry/learning).
        if not cancelled:
            self._record_project_learning(result)
        self._log(
            "task_finished",
            {
                "status": result.status.value,
                "result": result.result,
                "error": result.error,
            },
        )

        if result.status == RuntimeStatus.COMPLETED:
            self._emit_event("task_completed", {"result": result.result})
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
    ) -> RuntimeResult:
        """Jalankan task sebagai SATU percakapan kontinu (Native Tool Calling).

        Ini jalur normal AgentRuntime: task TIDAK dipecah menjadi TaskStep, dan
        TIDAK memanggil planner/loop per-step. AgentOrchestrator menjalankan
        continuous loop satu percakapan; ia meminta tool, mengeksekusinya via
        ToolExecutor, mengembalikan hasil sebagai role "tool", lalu melanjutkan
        sampai LLM memberi response final (DONE) atau terjadi error (FAILED).

        Plan (bila ada) hanya dipakai sebagai context ADVISORY opsional; ia
        tidak menentukan urutan pemanggilan tool.
        """
        progress.current_step = prepared.task
        task_text = self._build_continuous_task(prepared)
        orchestrator = self._make_orchestrator(self.provider)
        result = orchestrator.run(task_text)
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
        )

    def _run_single(
        self,
        prepared: PreparedTask,
        progress: RuntimeProgress,
    ) -> RuntimeResult:
        """Jalankan task tanpa plan (satu langkah tunggal) - jalur legacy."""
        progress.current_step = prepared.task
        orchestrator = self._make_orchestrator(self.provider)
        result = orchestrator.run(self._build_task(prepared))
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
        return "\n".join(parts)

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
