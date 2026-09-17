"""Agent Orchestrator: iterative agent loop.

Menghubungkan Agent/provider, AgentLoop, ToolExecutor, dan LLMResponse menjadi
satu siklus iteratif:

    Task -> LLM -> LLMResponse
        -> FINAL      : selesai (result)
        -> TOOL_CALL  : ToolExecutor -> AgentObservation
                        -> observation dikirim kembali ke LLM -> ulangi
    sampai FINAL atau max_iterations.

Prinsip:
    - Provider hanya dipakai lewat abstraction (BaseProvider.generate +
      normalize_response). Tidak ada format tool-call provider baru.
    - LLMResponse adalah protocol internal.
    - Tool error dikirim kembali sebagai observation (loop tidak crash).
    - Hormati max_iterations via AgentLoop.
    - ProjectBrain (opsional) dipakai untuk membaca context sebelum task dan
      menyimpan learning setelah selesai. Orchestrator tidak tahu detail
      IntelligenceContext/Learner (hanya lewat facade ProjectBrain).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from agent_ai.core.coding import CodingTask
from agent_ai.core.executor import ToolExecutor
from agent_ai.core.loop import AgentLoop, MaxIterationsExceeded
from agent_ai.core.models import AgentObservation, AgentStatus
from agent_ai.core.observability import EventSink, emit as emit_event
from agent_ai.core.response import LLMResponse
from agent_ai.providers.base import (
    BaseProvider,
    GenerateOptions,
    Message,
    ToolChoice,
    ToolDefinition,
)
from agent_ai.reliability.manager import ReliabilityManager
from agent_ai.reliability.models import (
    DecisionAction,
    ProgressSnapshot,
    ReliabilityDecision,
)

if TYPE_CHECKING:  # pragma: no cover - hanya untuk type hint, hindari import cycle
    from agent_ai.projects.brain import ProjectBrain


@dataclass
class OrchestratorResult:
    """Hasil akhir orkestrasi."""

    status: AgentStatus
    result: Optional[str] = None
    error: Optional[str] = None
    iterations: int = 0
    steps: List[Dict[str, Any]] = field(default_factory=list)
    learning: Optional[Dict[str, Any]] = None
    # True bila kegagalan berasal dari provider (bukan tool/command).
    # Dipakai oleh Provider Fallback (#45) untuk memutuskan perpindahan provider.
    provider_error: bool = False

    @property
    def success(self) -> bool:
        return self.status == AgentStatus.DONE


class AgentOrchestrator:
    """Menjalankan iterative agent loop di atas provider + tools.

    Args:
        provider: instance BaseProvider (abstraction).
        executor: ToolExecutor. Default: ToolExecutor() dengan registry global.
        max_iterations: batas iterasi (anti infinite loop).
        options: GenerateOptions default untuk setiap pemanggilan LLM.
        system_prompt: prompt sistem opsional.
        brain: ProjectBrain opsional. Bila diisi, context Project Intelligence
            disisipkan sebelum task dan learning disimpan setelah selesai.
    """

    def __init__(
        self,
        provider: BaseProvider,
        executor: Optional[ToolExecutor] = None,
        max_iterations: int = 10,
        options: Optional[GenerateOptions] = None,
        system_prompt: Optional[str] = None,
        brain: Optional["ProjectBrain"] = None,
        use_tools: bool = True,
        tool_choice: Optional[ToolChoice] = None,
        reliability: Optional[ReliabilityManager] = None,
        event_sink: Optional[EventSink] = None,
    ) -> None:
        self.provider = provider
        self.executor = executor or ToolExecutor()
        self.max_iterations = max_iterations
        self.options = options
        self.system_prompt = system_prompt
        self.brain = brain
        # Native tool calling: kirim definisi tool dari registry ke provider.
        # Default aktif; tool_choice default None (tidak dipaksa).
        self.use_tools = use_tools
        self.tool_choice = tool_choice
        # Reliability Layer (opsional). Bila None, loop berjalan seperti biasa.
        self.reliability = reliability
        # Observability (#55): sink event opsional. Bila None, tidak ada event
        # yang diemit (backward compatible). Sink menerima (event_type, payload)
        # dan payload sudah disanitasi (tanpa secret).
        self.event_sink = event_sink

    # ------------------------------------------------------------------ #
    # Tool definitions
    # ------------------------------------------------------------------ #
    def _tool_definitions(self) -> List[ToolDefinition]:
        """Bangun ToolDefinition (provider-agnostic) dari ToolRegistry.specs().

        Semua tool yang terdaftar tersedia untuk model. Tidak ada nama tool
        yang di-hardcode. Format konversi ke API dilakukan oleh provider.
        """
        if not self.use_tools:
            return []
        specs = self.executor.registry.specs()
        return [ToolDefinition.from_spec(spec) for spec in specs]

    # ------------------------------------------------------------------ #
    # History helpers
    # ------------------------------------------------------------------ #
    def _build_messages(self, task: str, history: List[Message]) -> List[Message]:
        """Bangun daftar pesan untuk LLM: system + task + history."""
        messages: List[Message] = []
        if self.system_prompt:
            messages.append(Message(role="system", content=self.system_prompt))
        messages.append(Message(role="user", content=task))
        messages.extend(history)
        return messages

    def _brain_context_message(self) -> Optional[Message]:
        """Ambil context Project Intelligence sebagai system message (opsional).

        Error dari brain diisolasi: bila gagal, kembalikan None tanpa
        menggagalkan task utama.
        """
        if self.brain is None:
            return None
        try:
            ctx = self.brain.get_context()
        except Exception:  # noqa: BLE001 - context error tidak boleh menggagalkan task
            return None
        text = getattr(ctx, "text", "") or ""
        if not text.strip():
            return None
        return Message(role="system", content=text)

    def _model_name(self) -> str:
        """Nama model aktif (dari options bila ada). Tidak pernah secret."""
        if self.options is not None and getattr(self.options, "model", None):
            return self.options.model
        return ""

    @staticmethod
    def _extract_commentary(response: LLMResponse) -> str:
        """Ambil commentary natural dari response LLM (bila bermakna).

        Commentary = teks penjelasan LLM tentang pekerjaannya. BUKAN log tool
        dan BUKAN payload code/tool. Mengembalikan string kosong bila teks
        tidak bermakna (kosong / hanya whitespace / terlihat seperti dump
        code/tool payload) sehingga UI tidak menampilkan noise.
        """
        text = (response.text or "").strip()
        if not text:
            return ""
        # Abaikan teks yang jelas berupa payload code/tool (bukan commentary).
        lowered = text.lower()
        if lowered.startswith(("```", "tool_call", "function_call", "{")):
            return ""
        # Batasi panjang agar tidak membanjiri UI (commentary ringkas).
        if len(text) > 600:
            text = text[:600].rstrip() + "…"
        return text

    @staticmethod
    def _tool_target(arguments: Dict[str, Any]) -> str:
        """Ekstrak target ringkas tool dari argumen yang memang tersedia.

        Mengambil path/query/command bila ada (tanpa hardcode nama file).
        Mengembalikan string kosong bila tidak ada informasi target.
        """
        if not arguments:
            return ""
        for key in ("path", "file", "filename", "query", "command", "pattern", "target"):
            value = arguments.get(key)
            if value:
                text = str(value).strip()
                if text:
                    return text
        return ""

    @staticmethod
    def _observation_to_message(observation: AgentObservation) -> Message:
        """Ubah AgentObservation menjadi pesan untuk dikirim kembali ke LLM."""
        if observation.success:
            content = f"[tool result] {observation.content}"
        else:
            content = f"[tool error] {observation.error}"
        return Message(role="user", content=content)

    # ------------------------------------------------------------------ #
    # Reliability helpers (provider-agnostic)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _action_signature(action: Any) -> str:
        """Identitas action: nama + argumen (deterministik)."""
        name = getattr(action, "name", "") or ""
        args = getattr(action, "arguments", {}) or {}
        try:
            args_repr = json.dumps(args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            args_repr = str(args)
        return f"{name}:{args_repr}"

    @staticmethod
    def _observation_signature(observation: AgentObservation) -> str:
        """Ringkasan observation untuk deteksi perubahan (bukan identitas penuh)."""
        if observation.success:
            payload = str(observation.content)
        else:
            payload = f"error:{observation.error}"
        digest = hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()[:12]
        return digest

    @staticmethod
    def _observation_outcome(observation: AgentObservation) -> str:
        """Klasifikasi outcome observation (provider-agnostic)."""
        if not observation.success:
            return "execution_error"
        meta = observation.metadata or {}
        if meta.get("command_failure"):
            return "command_failure"
        return "success"

    def _recovery_message(self, decision: ReliabilityDecision) -> Message:
        """Pesan recovery yang disisipkan ke context (bukan loop baru)."""
        events = ", ".join(e.type.value for e in decision.events) or "unknown"
        return Message(
            role="user",
            content=(
                "[reliability] Terdeteksi pola bermasalah: "
                f"{events}. {decision.reason} "
                "Ubah pendekatan: jangan ulangi action yang sama tanpa informasi baru. "
                "Jika task sudah selesai, berikan jawaban final."
            ),
        )

    def _handle_provider_error(
        self, loop: AgentLoop, error: BaseException
    ) -> Optional[Message]:
        """Tangani error provider via reliability.

        Returns:
            Message recovery untuk disisipkan (RETRY/RECOVER), atau None bila
            loop sudah dihentikan (STOP/FAIL).
        """
        if self.reliability is None:
            loop.fail(f"{type(error).__name__}: {error}")
            return None

        event = self.reliability.observe_error(error)
        decision = self.reliability.decide(outcome=event.type.value)

        if decision.action == DecisionAction.RETRY:
            self.reliability.retry.record_attempt()
            if decision.delay > 0:
                time.sleep(decision.delay)
            return Message(
                role="user",
                content=(
                    f"[reliability] Provider error ({event.type.value}); "
                    f"retry attempt {self.reliability.retry.attempts}. {decision.reason}"
                ),
            )
        # Outcome retryable tetapi kuota retry habis -> FAIL (bukan recover).
        if (
            self.reliability.retry.policy.is_retryable(event.type.value)
            and not self.reliability.should_retry(event.type.value)
        ):
            loop.fail(
                f"Gagal oleh reliability: retry habis untuk '{event.type.value}' "
                f"(max_retries={self.reliability.retry.policy.max_retries})."
            )
            return None
        if decision.action == DecisionAction.RECOVER:
            return self._recovery_message(decision)
        if decision.action == DecisionAction.STOP:
            loop.fail(f"Dihentikan oleh reliability: {decision.reason}")
            return None
        # FAIL
        loop.fail(f"Gagal oleh reliability: {decision.reason}")
        return None

    def _record_and_decide(
        self,
        loop: AgentLoop,
        action: Any,
        observation: AgentObservation,
    ) -> Optional[ReliabilityDecision]:
        """Catat progres ke reliability dan kembalikan keputusan (bila ada).

        Returns:
            ReliabilityDecision bila reliability aktif dan ada tindakan yang
            perlu diambil; None bila reliability tidak aktif atau tidak ada
            event (lanjut normal).
        """
        if self.reliability is None:
            return None

        signature = self._action_signature(action)
        obs_sig = self._observation_signature(observation)
        outcome = self._observation_outcome(observation)

        # Progres dianggap terjadi bila observation berbeda dari sebelumnya
        # ATAU outcome bukan kegagalan. Repeated action dengan observation
        # yang berubah tetap dianggap progres (legitimate).
        prev = self.reliability.history[-1] if self.reliability.history else None
        changed_observation = prev is None or prev.observation_signature != obs_sig
        made_progress = changed_observation or outcome == "success"

        snapshot = ProgressSnapshot(
            iteration=loop.iteration,
            action_signature=signature,
            outcome=outcome,
            observation_signature=obs_sig,
            made_progress=made_progress,
        )
        detected = self.reliability.record_progress(snapshot)
        if not detected:
            return None

        decision = self.reliability.decide(outcome=outcome)
        # RETRY pada level action tidak diulang otomatis di sini (tool sudah
        # dieksekusi); retry ditangani pada level provider. Untuk action,
        # RECOVER/STOP/FAIL yang relevan.
        if decision.action == DecisionAction.RETRY:
            return None
        return decision

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    def run(self, task: str) -> OrchestratorResult:
        """Jalankan iterative agent loop untuk sebuah task.

        Returns:
            OrchestratorResult (status DONE/FAILED, result, steps).
        """
        loop = AgentLoop(task=task, max_iterations=self.max_iterations)
        loop.start()

        history: List[Message] = []
        provider_error = False

        # Context Project Intelligence (opsional) disisipkan sebelum task.
        brain_context = self._brain_context_message()
        if brain_context is not None:
            history.append(brain_context)

        while not loop.is_finished:
            # 1) Panggil LLM via abstraction (sertakan definisi tool native).
            messages = self._build_messages(task, history)
            tools = self._tool_definitions()
            emit_event(
                self.event_sink,
                "provider_request",
                {
                    "provider": getattr(self.provider, "name", ""),
                    "model": self._model_name(),
                    "iteration": loop.iteration,
                    "tool_count": len(tools),
                },
            )
            try:
                gen_result = self.provider.generate(
                    messages=messages,
                    options=self.options,
                    tools=tools or None,
                    tool_choice=self.tool_choice,
                )
                response: LLMResponse = self.provider.normalize_response(gen_result)
            except Exception as exc:  # noqa: BLE001 - provider error -> reliability
                provider_error = True
                emit_event(
                    self.event_sink,
                    "provider_response",
                    {
                        "provider": getattr(self.provider, "name", ""),
                        "model": self._model_name(),
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                recovery = self._handle_provider_error(loop, exc)
                if recovery is None:
                    break
                history.append(recovery)
                continue

            emit_event(
                self.event_sink,
                "provider_response",
                {
                    "provider": response.provider or getattr(self.provider, "name", ""),
                    "model": response.model or self._model_name(),
                    "finish_reason": response.finish_reason.value,
                    "tool_calls": len(response.tool_calls()),
                },
            )

            # Commentary natural dari LLM (bukan log tool). Hanya diemit bila
            # response punya teks bermakna (bukan kosong / bukan code/tool
            # payload). Tidak mengarang commentary dari nama tool.
            commentary = self._extract_commentary(response)
            if commentary:
                emit_event(
                    self.event_sink,
                    "agent_commentary",
                    {"text": commentary, "iteration": loop.iteration},
                )

            # 2) FINAL -> selesai (tidak menjalankan tool).
            if response.is_final:
                loop.finish(result=response.text)
                break

            # 3) TOOL_CALL -> eksekusi tiap action, catat step, kirim balik.
            try:
                for action in response.tool_calls():
                    loop.record_action(self.executor.to_agent_action(action))
                    emit_event(
                        self.event_sink,
                        "tool_called",
                        {
                            "tool": action.name,
                            "arguments": dict(action.arguments or {}),
                            # Target ringkas (path/query/command) dari argumen
                            # tool yang memang tersedia. Bukan hardcode nama file.
                            "target": self._tool_target(action.arguments or {}),
                            "iteration": loop.iteration,
                        },
                    )
                    observation = self.executor.execute_action(action)
                    loop.record_observation(observation)
                    emit_event(
                        self.event_sink,
                        "tool_completed",
                        {
                            "tool": action.name,
                            "success": observation.success,
                            "error": observation.error,
                            "target": self._tool_target(action.arguments or {}),
                            "metadata": dict(observation.metadata or {}),
                        },
                    )
                    emit_event(
                        self.event_sink,
                        "observation_received",
                        {
                            "tool": action.name,
                            "success": observation.success,
                            "content": observation.content,
                        },
                    )
                    history.append(self._observation_to_message(observation))

                    # Reliability: catat progres & putuskan tindakan.
                    decision = self._record_and_decide(loop, action, observation)
                    if decision is None:
                        continue
                    if decision.action == DecisionAction.RECOVER:
                        history.append(self._recovery_message(decision))
                    elif decision.action == DecisionAction.STOP:
                        loop.fail(f"Dihentikan oleh reliability: {decision.reason}")
                        break
                    elif decision.action == DecisionAction.FAIL:
                        loop.fail(f"Gagal oleh reliability: {decision.reason}")
                        break
                if loop.is_finished:
                    break
            except MaxIterationsExceeded:
                # loop sudah di-set FAILED oleh AgentLoop.
                break

        # Setelah selesai: simpan learning (opsional, error terisolasi).
        learning = self._learn_from_run(task, loop)

        return OrchestratorResult(
            status=loop.status,
            result=loop.state.result,
            error=loop.state.error,
            iterations=loop.iteration,
            steps=loop.to_dict()["steps"],
            learning=learning,
            provider_error=provider_error,
        )

    def run_coding_task(self, task: str) -> CodingTask:
        """Jalankan task coding dan bungkus hasilnya sebagai CodingTask.

        Thin wrapper di atas run(); tidak mengubah logika loop. AgentLoop tetap
        mengelola iteration/action/observation.

        Returns:
            CodingTask (request, status, iterations, result, error, steps).
        """
        coding = CodingTask(request=task).start()
        result = self.run(task)
        if result.success:
            coding.complete(
                result=result.result,
                iterations=result.iterations,
                steps=result.steps,
            )
        else:
            coding.fail(
                error=result.error or "task gagal",
                iterations=result.iterations,
                steps=result.steps,
            )
        return coding

    # ------------------------------------------------------------------ #
    # Learning (opsional, terisolasi)
    # ------------------------------------------------------------------ #
    def _learn_from_run(self, task: str, loop: AgentLoop) -> Optional[Dict[str, Any]]:
        """Kirim ringkasan pekerjaan ke brain.learn (opsional).

        Learning error TIDAK menggagalkan task utama; kembalikan None bila
        brain tidak tersedia atau learning gagal.
        """
        if self.brain is None:
            return None
        try:
            observations = self._build_observations(task, loop)
            if not observations:
                return None
            result = self.brain.learn(observations)
            return result.to_dict() if hasattr(result, "to_dict") else None
        except Exception:  # noqa: BLE001 - learning error harus terisolasi
            return None

    @staticmethod
    def _build_observations(task: str, loop: AgentLoop) -> List[str]:
        """Bangun observations ringkas dari task + hasil loop (bukan per step)."""
        observations: List[str] = [f"Task: {task}"]
        if loop.state.result:
            observations.append(f"Result: {loop.state.result}")
        if loop.state.error:
            observations.append(f"Error: {loop.state.error}")
        # Ringkas tool results yang relevan (bukan setiap internal step).
        for step in loop.to_dict()["steps"]:
            observation = step.get("observation") or {}
            if observation.get("success") and observation.get("content"):
                observations.append(f"Tool result: {observation['content']}")
        return observations


