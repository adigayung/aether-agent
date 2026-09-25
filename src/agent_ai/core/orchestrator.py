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
    - Hormati max_iterations via AgentLoop sebagai SAFETY LIMIT, bukan target.
    - Completion detection: sinyal final dari model adalah source of truth;
      loop berhenti saat final muncul. Selain itu, setelah setiap observation
      loop memeriksa bukti langkah (implementasi + requirement task terpenuhi,
      tanpa error aktif) dan berhenti lebih awal bila task terdeteksi selesai.
      Verification hanya diwajibkan bila task menuntutnya; mutasi sukses +
      requirement terpenuhi dapat menjadi completion. Bila iteration limit
      tercapai, bukti langkah yang sama dipakai untuk memutuskan Completed vs
      Failed, sehingga task yang sudah selesai tidak salah ditandai Failed
      hanya karena model terus memanggil tool.
    - Blok heuristic di atas berlaku HANYA untuk LOOP LAMA. Jalur continuous
      (`run_continuous_loop`, `use_continuous_loop=True`) TIDAK memakai
      heuristic completion apa pun: keputusan selesai murni dari response LLM
      yang tidak memiliki tool call (LLM Final -> DONE).
    - ProjectBrain (opsional) dipakai untuk membaca context sebelum task dan
      menyimpan learning setelah selesai. Orchestrator tidak tahu detail
      IntelligenceContext/Learner (hanya lewat facade ProjectBrain).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from agent_ai.core.bible_lifecycle import (
    BibleContextState,
    bible_source_revision,
    fingerprint_text,
    resolve_bible_root,
)
from agent_ai.core.cancel import CancellationToken
from agent_ai.core.coding import CodingTask
from agent_ai.core.executor import ToolExecutor
from agent_ai.core.history import ConversationHistory
from agent_ai.core.loop import AgentLoop, MaxIterationsExceeded
from agent_ai.core.models import AgentObservation, AgentStatus
from agent_ai.core.observability import EventSink, emit as emit_event
from agent_ai.core.response import ActionType, LLMResponse
from agent_ai.core.types import ToolCall, ToolResultPayload
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


# Dipertahankan untuk KOMPATIBILITAS API (default parameter yang diimpor
# sebagian verifier). Nilainya BUKAN lagi digunakan sebagai hard stop:
# continuous loop TIDAK di-FAIL karena jumlah step. Loop berhenti hanya ketika
# LLM memberi response final TANPA tool call, user cancel, atau fatal error
# nyata. Agent task dapat berjalan selama diperlukan.
_CONTINUOUS_SAFETY_MAX_STEPS = 1000

#: Estimasi kasar karakter per token, dipakai untuk mengubah anggaran token
#: provider (mis. context window default provider) menjadi anggaran karakter konteks
#: pengetahuan. Heuristik sederhana tanpa tokenizer eksternal.
_KNOWLEDGE_CHARS_PER_TOKEN = 4


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
        brain_learning: bila False, context brain tetap dipakai tetapi learning
            tidak dijalankan di akhir run (dikelola pemanggil, mis. Runtime).
        use_continuous_loop: bila True, `run()` memakai continuous loop Native
            Tool Calling (satu percakapan kontinu) menggantikan loop lama.
            Default False (perilaku lama tetap dipertahankan).
        environment_context: Environment Context project-local (markdown dari
            `.aether/ENVIRONMENT.md`). Bila diisi, disisipkan sebagai system
            message pada awal session continuous loop. Disiapkan pemanggil.
        context_budget_tokens: anggaran token untuk konteks percakapan
            (system + task + history) pada continuous loop. Bila None, anggaran
            diturunkan dari provider (`knowledge_budget_tokens`) atau config
            context yang ada. Riwayat yang melebihi anggaran dipadatkan secara
            DETERMINISTIK (tanpa LLM); task pendek tidak berubah.
    """

    def __init__(
        self,
        provider: BaseProvider,
        executor: Optional[ToolExecutor] = None,
        max_iterations: int = 10,
        options: Optional[GenerateOptions] = None,
        system_prompt: Optional[str] = None,
        brain: Optional["ProjectBrain"] = None,
        brain_learning: bool = True,
        use_tools: bool = True,
        tool_choice: Optional[ToolChoice] = None,
        reliability: Optional[ReliabilityManager] = None,
        event_sink: Optional[EventSink] = None,
        use_continuous_loop: bool = False,
        environment_context: Optional[str] = None,
        cancel_token: Optional[CancellationToken] = None,
        context_budget_tokens: Optional[int] = None,
    ) -> None:
        self.provider = provider
        self.executor = executor or ToolExecutor()
        self.max_iterations = max_iterations
        self.options = options
        self.system_prompt = system_prompt
        self.brain = brain
        # Environment Context (project-local, opsional). Bila diisi (teks
        # markdown dari `.aether/ENVIRONMENT.md`), disisipkan sebagai system
        # message pada awal session continuous loop. Disiapkan oleh pemanggil
        # (mis. AgentRuntime) agar tidak menulis file di sini.
        self.environment_context = environment_context
        # Bila False, orchestrator tetap membaca context brain tetapi TIDAK
        # melakukan learning di akhir run (learning dikelola pemanggil, mis.
        # sekali per task di Runtime). Default True (perilaku lama).
        self.brain_learning = brain_learning
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
        # Jalur execution baru (Native Tool Calling, satu percakapan kontinu).
        # Default False -> `run()` memakai loop lama (backward compatible).
        self.use_continuous_loop = use_continuous_loop
        # Cooperative cancellation (opsional). Bila diisi, loop memeriksa token
        # ini pada SAFE BOUNDARY (sebelum iteration LLM berikutnya dan sebelum
        # setiap tool call) dan berhenti sebagai CANCELLED tanpa tool call baru.
        # Bukan sistem cancellation kedua: satu token dibagikan lintas layer.
        self.cancel_token = cancel_token
        # Anggaran token untuk konteks percakapan (system + task + history).
        # Bila None, anggaran diturunkan dari provider (context window) atau
        # config context yang sudah ada; lihat `_context_budget_tokens()`.
        # Dipakai runtime compaction continuous loop agar riwayat lama tidak
        # dikirim mentah setiap round. None + fallback None = tanpa batas
        # (perilaku lama).
        self.context_budget_tokens = context_budget_tokens

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
    # Cooperative cancellation (safe boundary)
    # ------------------------------------------------------------------ #
    def _cancel_requested(self) -> bool:
        """True bila pembatalan (user stop) sudah diminta.

        Dibaca pada SAFE BOUNDARY saja (sebelum memanggil LLM lagi / sebelum
        tool call berikutnya). Tidak memaksa menghentikan operasi yang sedang
        blocking; operasi tersebut diselesaikan dulu lalu loop berhenti.
        """
        token = self.cancel_token
        return token is not None and token.is_cancelled()

    def _cancel_reason(self) -> str:
        """Alasan pembatalan (fallback generik) untuk pesan status."""
        token = self.cancel_token
        reason = getattr(token, "reason", None) if token is not None else None
        return reason or "Task dibatalkan (user stop)."

    # ------------------------------------------------------------------ #
    # History helpers
    # ------------------------------------------------------------------ #
    def _build_messages(self, task: str, history: List[Message]) -> List[Message]:
        """Bangun daftar pesan untuk LLM: system + task + history."""
        messages: List[Message] = []
        if self.system_prompt:
            messages.append(Message(role="system", content=self.system_prompt))
        # Environment context (opsional): disisipkan sebelum task.
        environment = self._environment_context_message()
        if environment is not None:
            messages.append(environment)
        messages.append(Message(role="user", content=task))
        messages.extend(history)
        return messages

    def _brain_context_message(self, query: str = "") -> Optional[Message]:
        """Ambil context Project Intelligence sebagai system message (opsional).

        Error dari brain diisolasi: bila gagal, kembalikan None tanpa
        menggagalkan task utama.

        Project Bible TIDAK dikirim UTUH secara default: knowledge dipilih
        berdasarkan relevance terhadap `query` (task/pertanyaan) oleh
        `BibleRetriever`, lalu dibatasi token budget provider sebagai batas
        AKHIR. Bible tetap sumber kebenaran knowledge yang lengkap — retrieval
        hanya memilih subset untuk satu request (read-only).

        Bila retrieval tidak tersedia (mis. brain duck-typed tanpa akses
        terstruktur), jalur lama dipakai: `brain.get_context()` + pemotongan
        pada batas baris sesuai anggaran provider (perilaku backward
        compatible).
        """
        if self.brain is None:
            return None
        text = self._retrieve_knowledge_context(query)
        text = self._fit_knowledge_context(text)
        if not text.strip():
            return None
        return Message(role="system", content=text)

    # ------------------------------------------------------------------
    # Bible Context Lifecycle (Task 4)
    # ------------------------------------------------------------------
    # Tujuan: context Project Bible (hasil retrieval existing) tidak dikirim
    # ULANG ke provider pada round-round berikutnya selama task yang sama dan
    # selama isi Bible belum berubah. State bersifat TASK-SCOPED (instance
    # runtime), bukan cache global lintas task.
    def bible_context_state(self) -> BibleContextState:
        """State Bible context untuk scope task yang sedang berjalan."""
        state = getattr(self, "_bible_context_state", None)
        if state is None:
            state = BibleContextState()
            self._bible_context_state = state
        return state

    def begin_bible_context_scope(self, scope: str = "") -> BibleContextState:
        """Mulai scope task baru: Bible dianggap BELUM pernah dikirim.

        Dipanggil sekali di awal task (Agent continuous loop maupun legacy
        run) sehingga task lain tidak mewarisi state task sebelumnya.
        """
        state = BibleContextState(scope=scope or "")
        self._bible_context_state = state
        return state

    def invalidate_bible_context(self, reason: str = "") -> BibleContextState:
        """Tandai context Bible pada scope ini TIDAK valid lagi.

        Setelahnya retrieval existing boleh mengirim context Bible kembali
        (dipakai bila Bible berubah di tengah task / invalidation eksplisit).
        """
        return self.bible_context_state().invalidate(reason)

    def bible_context_state_dict(self) -> Dict[str, Any]:
        """Ringkasan state (observability/verifikasi), tanpa isi konteks."""
        return self.bible_context_state().to_dict()

    def _bible_sources_revision(self) -> Optional[str]:
        """Revision murah sumber Bible (nama+size+mtime tiap kategori)."""
        try:
            return bible_source_revision(resolve_bible_root(getattr(self, "brain", None)))
        except Exception:
            return None

    def _mark_bible_context_sent(
        self,
        state: BibleContextState,
        text: str,
        revision: Optional[str],
        query: str,
    ) -> bool:
        """Catat context Bible yang dikirim. False = isi identik (jangan kirim)."""
        fingerprint = fingerprint_text(text or "")
        return state.mark_injected(fingerprint, revision, query=query)

    def _bible_context_message_for_task(self, task: str = "") -> Optional[Message]:
        """Retrieval existing + lifecycle gate untuk satu task.

        Return None bila context Bible untuk task ini SUDAH dikirim dan isi
        Bible belum berubah (duplicate prevention). Retrieval tetap memakai
        mekanisme existing (_brain_context_message -> _retrieve_knowledge_context).
        """
        state = self.bible_context_state()
        revision = self._bible_sources_revision()
        if not state.needs_retrieval(revision):
            state.record_skip()
            return None
        message = self._brain_context_message(task)
        if message is None:
            return None
        if not self._mark_bible_context_sent(state, message.content, revision, task):
            # Isi identik dengan yang sudah pernah dikirim pada task ini.
            state.record_skip()
            return None
        return message

    def _refresh_bible_context(self, history: ConversationHistory, task: str = "") -> bool:
        """Cek per round (murah): sisipkan context Bible HANYA bila berubah.

        Return True bila context baru benar-benar disisipkan ke riwayat.
        """
        if history is None:
            return False
        message = self._bible_context_message_for_task(task)
        if message is None:
            return False
        try:
            history.append_system_message(message.content)
        except Exception:
            return False
        return True

    def _knowledge_budget_tokens(self) -> Optional[int]:
        """Anggaran token konteks pengetahuan dari provider (bila dilaporkan)."""
        getter = getattr(self.provider, "knowledge_budget_tokens", None)
        if getter is None:
            return None
        try:
            tokens = getter()
        except Exception:  # noqa: BLE001 - hint provider tidak boleh menggagalkan task
            return None
        if not tokens or int(tokens) <= 0:
            return None
        return int(tokens)

    def _knowledge_budget_chars(self) -> Optional[int]:
        """Anggaran karakter untuk konteks pengetahuan (dari provider).

        Memakai anggaran PROVIDER apa adanya (`_knowledge_budget_tokens`), TANPA
        batas tambahan: jalur ini adalah FALLBACK saat retrieval terstruktur
        tidak tersedia, dan kontraknya adalah "kirim konteks apa adanya bila
        provider tidak melaporkan anggaran".

        Returns:
            Anggaran karakter, atau None bila anggaran tidak diketahui
            (context window tidak diketahui -> tanpa pemotongan).
        """
        tokens = self._knowledge_budget_tokens()
        if tokens is None:
            return None
        return tokens * _KNOWLEDGE_CHARS_PER_TOKEN

    def _retrieve_knowledge_context(self, query: str = "") -> str:
        """Context pengetahuan berbasis RELEVANCE (Bible retrieval).

        Alur: query -> BibleRetriever (relevance + ranking + fallback) ->
        token budget. Fallback ke jalur lama (`brain.get_context()`) bila
        retrieval tidak tersedia, supaya brain tanpa akses terstruktur tetap
        bekerja seperti sebelumnya.

        Observability: metadatanya diemit lewat event sink yang sudah ada
        (`bible_retrieval`) TANPA isi Bible dan tanpa secret.
        """
        try:
            from agent_ai.projects.retrieval import BibleRetriever

            retriever = BibleRetriever(self.brain)
            result = retriever.retrieve(
                query, budget_tokens=self._knowledge_context_budget_tokens()
            )
            if result is not None:
                emit_event(self.event_sink, "bible_retrieval", result.to_metadata())
                return result.text
        except Exception:  # noqa: BLE001 - retrieval error -> fallback jalur lama
            pass
        # Fallback (perilaku lama): seluruh context dari facade, lalu dipotong
        # oleh `_fit_knowledge_context` bila provider melaporkan anggaran.
        try:
            ctx = self.brain.get_context()
        except Exception:  # noqa: BLE001 - context error tidak boleh menggagalkan task
            return ""
        return getattr(ctx, "text", "") or ""

    def _fit_knowledge_context(self, text: str) -> str:
        """Potong konteks pengetahuan agar muat pada context window provider.

        Pemotongan dilakukan pada BATAS BARIS sehingga struktur heading/kategori
        Bible tetap utuh (bagian awal — architecture, ui, dst. — yang paling
        penting dipertahankan), lalu diberi penanda singkat. Bila provider tidak
        melaporkan anggaran, teks dikembalikan APA ADANYA (perilaku lama).
        """
        budget = self._knowledge_budget_chars()
        if budget is None or len(text) <= budget:
            return text
        cut = text.rfind("\n", 0, budget)
        if cut <= 0:
            cut = budget
        return (
            text[:cut].rstrip()
            + "\n- (konteks pengetahuan dipotong otomatis agar muat pada context "
            "window provider)"
        )

    def _environment_context_message(self) -> Optional[Message]:
        """Environment Context (project-local) sebagai system message (opsional).

        Teks berasal dari `<root>/.aether/ENVIRONMENT.md` yang sudah disiapkan
        pemanggil (AgentRuntime). Bila kosong/tidak diisi, kembalikan None tanpa
        efek samping. Tidak menulis file di sini.
        """
        text = (self.environment_context or "").strip()
        if not text:
            return None
        return Message(role="system", content=text)

    # ------------------------------------------------------------------ #
    # Runtime context compaction (hemat token tanpa kehilangan memori)
    # ------------------------------------------------------------------ #
    def _context_budget_decision(self) -> Tuple[Optional[int], str]:
        """Anggaran token pesan + SUMBER keputusan (untuk observability).

        Prioritas (memakai mekanisme yang SUDAH ada, tanpa angka liar):
            1. Override eksplisit `context_budget_tokens` -> source "explicit".
            2. Anggaran yang dilaporkan provider (`knowledge_budget_tokens()`),
               sehingga mengikuti context window provider yang terbatas ->
               source "provider".
            3. Budget konteks yang sudah ada di config
               (`settings.context.max_tokens`) -> source "config".

        Source "none" = anggaran tidak diketahui (tanpa batas -> perilaku lama:
        riwayat dikirim apa adanya).

        Returns:
            (budget, source) -- budget None bila tidak diketahui.
        """
        explicit = self.context_budget_tokens
        if explicit is not None:
            value = int(explicit)
            return (value, "explicit") if value > 0 else (None, "none")
        provider_budget = self._knowledge_budget_tokens()
        if provider_budget is not None:
            return (provider_budget, "provider")
        try:
            from agent_ai.config.settings import settings

            value = int(getattr(settings.context, "max_tokens", 0) or 0)
        except Exception:  # noqa: BLE001 - budget opsional, jangan gagalkan task
            return (None, "none")
        return (value, "config") if value > 0 else (None, "none")

    def _context_budget_tokens(self) -> Optional[int]:
        """Anggaran token untuk daftar pesan (system + task + history).

        Lihat `_context_budget_decision()` untuk prioritas + sumber anggaran.

        Returns:
            Anggaran token pesan, atau None bila tidak diketahui (tanpa batas ->
            perilaku lama: riwayat dikirim apa adanya).
        """
        return self._context_budget_decision()[0]

    def _knowledge_context_budget_tokens(self) -> Optional[int]:
        """Anggaran untuk KONTEKS PENGETAHUAN (Project Bible / retrieval).

        Terpisah dari anggaran PERCAKAPAN: context window provider adalah
        anggaran untuk seluruh percakapan, sehingga konteks pengetahuan harus
        dibatasi pada porsi kecil dari padanya (kalau tidak, Bible dapat
        menghabiskan hampir seluruh anggaran dan jendela kerja Agent kosong).

        Batas diambil dari config yang SUDAH ada
        (`settings.context.knowledge_max_tokens`); bila 0 -> tanpa batas
        tambahan (perilaku lama: memakai anggaran provider/config apa adanya).

        Returns:
            Anggaran token konteks pengetahuan, atau None bila tidak diketahui.
        """
        provider_budget = self._knowledge_budget_tokens()
        try:
            from agent_ai.config.settings import settings

            cap = int(getattr(settings.context, "knowledge_max_tokens", 0) or 0)
        except Exception:  # noqa: BLE001 - cap opsional, jangan gagalkan task
            return provider_budget
        if cap <= 0:
            return provider_budget
        if provider_budget is None:
            return cap
        return min(int(provider_budget), cap)

    def _tool_definitions_tokens(self, tools: List[ToolDefinition]) -> int:
        """Estimasi token definisi tool (dikirim pada request yang sama).

        Definisi tool ikut memakan context window, jadi diperhitungkan sebagai
        overhead saat menentukan kompilasi pesan. Estimasi kasar (len/4),
        konsisten dengan estimator konteks yang sudah ada.
        """
        total = 0
        for tool in tools or []:
            try:
                payload = json.dumps(tool.to_dict(), ensure_ascii=False, default=str)
            except Exception:  # noqa: BLE001 - estimasi tidak boleh gagalkan task
                payload = getattr(tool, "name", "") or ""
            total += max(1, len(payload) // _KNOWLEDGE_CHARS_PER_TOKEN)
        return total

    def _compile_context_messages(
        self,
        history: ConversationHistory,
        tools: List[ToolDefinition],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Kompilasi pesan ke format provider DENGAN runtime context compaction.

        Bila anggaran token diketahui, riwayat yang panjang dipadatkan secara
        DETERMINISTIK (tanpa LLM) sehingga request tidak lagi membawa seluruh
        riwayat mentah setiap round. Task pendek (muat budget) dikembalikan APA
        ADANYA -> perilaku lama tidak berubah. Detail yang dipadatkan dapat
        diambil ulang oleh Agent lewat tool yang sudah ada.

        Returns:
            (messages, stats) -- messages siap kirim; stats ringkas (untuk
            observability/verifikasi, tanpa isi konten) bila anggaran diketahui.
        """
        budget, budget_source = self._context_budget_decision()
        if budget is None:
            return history.to_provider_format(), {}

        overhead = self._tool_definitions_tokens(tools)
        before = history.estimate_tokens()
        original = history.messages
        compiled = history.compile_compacted_messages(
            budget, overhead_tokens=overhead
        )
        # Selaraskan cache retrieval dengan ISI konteks yang benar-benar dikirim
        # ke LLM. Tanpa ini, dedup read/search bisa mengklaim sumber "sudah
        # tersedia" padahal detailnya baru saja dibuang oleh compaction
        # (false positive) sehingga Agent tidak pernah menerima isi yang
        # dibutuhkannya. Ini murni sinkronisasi state: TIDAK mengubah peran tool
        # dedup dan TIDAK menggantikan keputusan LLM.
        self._sync_retrieval_cache(history, compiled)
        after = history.estimate_messages_tokens(compiled)
        tool_stats = ConversationHistory.tool_compaction_stats(original, compiled)
        # Nama key SENGAJA tidak memuat substring kredensial (mis. "token") agar
        # tidak terkena redaksi `sanitize_payload` (pola yang sama dipakai
        # `projects/retrieval.py`), sehingga angka anggaran tetap terbaca di log.
        stats: Dict[str, Any] = {
            "context_budget": int(budget),
            "context_budget_source": str(budget_source),
            "context_overhead": int(overhead),
            "context_before": int(before),
            "context_after": int(after),
            "context_compacted": after < before,
            # Ringkasan compaction hasil tool (tanpa isi konten): berapa tool
            # result yang dipadatkan struktur + karakter yang dihemat.
            "context_tool_results": int(tool_stats["tool_results"]),
            "context_tool_compacted": int(tool_stats["tool_compacted"]),
            "context_tool_raw_chars": int(tool_stats["tool_raw_chars"]),
            "context_tool_compacted_chars": int(tool_stats["tool_compacted_chars"]),
        }
        return [message.to_provider_dict() for message in compiled], stats

    # ------------------------------------------------------------------ #
    # Identitas retrieval (observability: deteksi repeat)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _retrieval_identity(
        tool_name: str, arguments: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Identitas satu retrieval untuk deteksi repeat (OBSERVABILITY SAJA).

        Hanya tool retrieval yang diamati (`read_file`, `search_code`); tool lain
        mengembalikan None. Hasilnya TIDAK dipakai untuk mengubah perilaku
        eksekusi/dedup — murni untuk mengemit event `retrieval_repeat`.

        Args:
            tool_name: nama tool yang dieksekusi.
            arguments: argumen tool call (apa adanya dari LLM).

        Returns:
            Dict berisi `path`, `range`, `mode`, `force` — atau None bila tool
            ini bukan tool retrieval yang diamati / argumen tidak lengkap.
        """
        if tool_name == "read_file":
            path = arguments.get("path")
            if not path:
                return None
            symbol = arguments.get("symbol")
            start = arguments.get("start_line")
            end = arguments.get("end_line")
            if symbol:
                rng = str(symbol)
            elif start is None and end is None:
                rng = "full"
            else:
                rng = "{}-{}".format(
                    "start" if start is None else start,
                    "end" if end is None else end,
                )
            return {
                "path": str(path),
                "range": rng,
                "mode": str(arguments.get("mode") or "default"),
                "force": bool(arguments.get("force", False)),
            }
        if tool_name == "search_code":
            query = arguments.get("query")
            if query is None:
                return None
            return {
                "path": str(arguments.get("path") or "."),
                "range": str(query),
                "mode": "search",
                "force": False,
            }
        return None

    # ------------------------------------------------------------------ #
    # Sinkronisasi cache retrieval <-> konteks (runtime compaction)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _read_result_span(content: Optional[str]) -> Optional[Tuple[str, int, int]]:
        """Ekstrak (path, start, end) dari hasil read_file yang MEMUAT isi.

        Hanya hasil yang benar-benar membawa `content` (read nyata) yang
        dihitung; stub `already_available`, hasil `mode='structure'`, dan error
        tidak membawa isi sehingga dilewati (isinya diwakili hasil read nyata
        yang mendahuluinya). Mengembalikan None bila bukan hasil read berisi.
        """
        if not content:
            return None
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or "content" not in data:
            return None
        path = data.get("path")
        if not path:
            return None
        total = data.get("total_lines")
        start = data.get("start_line")
        end = data.get("end_line")
        if start is None and end is None:
            if isinstance(total, int) and total > 0:
                start, end = 1, total
            else:
                return None
        else:
            start = 1 if start is None else int(start)
            if end is None:
                if isinstance(total, int) and total > 0:
                    end = total
                else:
                    return None
            else:
                end = int(end)
        if start < 1 or end < start:
            return None
        return (str(path), start, end)

    @staticmethod
    def _search_result_key(content: Optional[str]) -> Optional[Tuple[str, str, int]]:
        """Ekstrak (query, path, context_lines) dari hasil search_code berisi.

        Stub `already_searched` (tanpa `matches`) dilewati. Mengembalikan None
        bila bukan hasil pencarian berisi.
        """
        if not content:
            return None
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or "matches" not in data:
            return None
        query = data.get("query")
        if query is None:
            return None
        path = data.get("path") or "."
        try:
            context_lines = int(data.get("context_lines", 0) or 0)
        except (TypeError, ValueError):
            context_lines = 0
        return (str(query), str(path), context_lines)

    def _sync_retrieval_cache(
        self, history: ConversationHistory, compiled: List[Any]
    ) -> None:
        """Selaraskan cache retrieval dengan pesan yang BENAR-BENAR dikirim.

        Bila context compaction membuang detail hasil read_file/search_code dari
        konteks yang dikirim ke LLM, cache dedup HARUS berhenti mengklaim sumber
        itu "sudah tersedia": kalau tidak, LLM akan menerima stub
        `already_available`/`already_searched` untuk isi yang SEBENARNYA tidak
        lagi ada di konteksnya (false positive) dan tidak akan pernah memperoleh
        data yang dibutuhkannya.

        Sebaliknya, begitu isi dikirimkan ulang (read/search baru), tanda
        "tersedia" kembali berlaku sehingga dedup normal bekerja lagi.

        Murni sinkronisasi state cache; TIDAK mengubah peran tool, TIDAK
        menambah bound, dan TIDAK mengambil keputusan untuk LLM.
        """
        registry = getattr(self.executor, "registry", None)
        cache = getattr(registry, "read_cache", None)
        if cache is None:
            return

        originals = {
            message.tool_call_id: message.content
            for message in history.messages
            if message.role == "tool" and message.tool_call_id
        }
        present_ids = {
            message.tool_call_id
            for message in compiled
            if getattr(message, "role", None) == "tool" and message.tool_call_id
        }
        compacted_ids = set()
        for message in compiled:
            if getattr(message, "role", None) != "tool" or not message.tool_call_id:
                continue
            before = originals.get(message.tool_call_id)
            if before is None:
                continue
            if (message.content or "") != (before or ""):
                compacted_ids.add(message.tool_call_id)

        # read_file: rentang yang MASIH terlihat = union span hasil read yang
        # hadir di konteks DAN belum dipadatkan.
        available: Dict[str, List[Tuple[int, int]]] = {}
        seen_paths = set()
        for message in history.messages:
            if message.role != "tool" or (message.name or "") != "read_file":
                continue
            span = self._read_result_span(message.content)
            if span is None:
                continue
            path, start, end = span
            seen_paths.add(path)
            visible = (
                message.tool_call_id in present_ids
                and message.tool_call_id not in compacted_ids
            )
            if visible:
                available.setdefault(path, []).append((start, end))
        cache.sync_from_context(available, seen_paths)

        # search_code: hasil yang sudah TIDAK terlihat harus dapat dicari ulang.
        # Hanya keputusan "terakhir" per (query, path, ctx) yang dipakai, agar
        # pencarian ulang yang hasilnya masih terlihat tidak ikut dilupakan.
        latest_search_gone: Dict[Tuple[str, str, int], bool] = {}
        for message in history.messages:
            if message.role != "tool" or (message.name or "") != "search_code":
                continue
            key = self._search_result_key(message.content)
            if key is None:
                continue
            visible = (
                message.tool_call_id in present_ids
                and message.tool_call_id not in compacted_ids
            )
            latest_search_gone[key] = not visible
        for (query, path, context_lines), gone in latest_search_gone.items():
            if gone:
                cache.forget_search(query, path, context_lines)

    def _model_name(self) -> str:
        """Nama model aktif untuk logging. Tidak pernah secret.

        Prioritas: `options.model` (model eksplisit per-run) -> `provider.config.model`
        (model default provider, mis. config provider). Fallback ke config
        provider penting agar log `provider_request` tidak menampilkan model
        kosong ketika provider memakai model default-nya (bukan bug runtime,
        hanya akurasi logging). Tidak mengubah payload yang dikirim provider.
        """
        if self.options is not None and getattr(self.options, "model", None):
            return self.options.model
        config = getattr(self.provider, "config", None)
        model = getattr(config, "model", None)
        return model or ""

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

    # ------------------------------------------------------------------ #
    # Completion detection — LEGACY LOOP ONLY (use_continuous_loop=False)
    # ------------------------------------------------------------------ #
    # Blok ini HANYA dipakai jalur loop lama. Jalur continuous
    # (`run_continuous_loop`) TIDAK memanggilnya sama sekali: di sana
    # completion murni dari response LLM tanpa tool call. Heuristic di bawah
    # tidak boleh (dan tidak bisa) memutus continuous reasoning loop.
    # Penanda mutasi workspace yang dikembalikan tool tulis/ubah/hapus/pindah
    # (lihat tools/workspace.py). Dipakai generik, bukan hardcode nama tool.
    _MUTATION_MARKERS = ("written", "edited", "deleted", "moved")

    # Dipertahankan untuk kompatibilitas. Batas ini BUKAN lagi mematikan task:
    # response terpotong yang berulang tetap dipulihkan (pesan lanjut dikirim
    # ke LLM) dan TIDAK men-FAIL seluruh task.
    _MAX_TRUNCATION_RECOVERIES = 3

    # ------------------------------------------------------------------ #
    # Requirement dari teks task (deterministik, provider-agnostic).
    # Dipakai HANYA untuk memutuskan apakah bukti verifikasi diwajibkan dan
    # artifact mana yang harus terpenuhi. BUKAN semantic evaluator / planner.
    # ------------------------------------------------------------------ #
    _VALIDATION_KEYWORDS = (
        "test", "tests", "testing", "debug", "validate", "validation",
        "validasi", "verifikasi", "verify", "lulus", "pytest", "unit test",
        "uji", "spec",
    )
    # Frasa yang MENIADAKAN kebutuhan validation (user eksplisit: jangan test).
    _NO_VALIDATION_PHRASES = (
        "jangan test", "jangan debug", "jangan uji", "jangan validasi",
        "tanpa test", "tanpa debug", "tanpa uji", "tanpa verifikasi",
        "tidak perlu test", "tidak perlu debug", "tidak usah test",
        "tidak usah debug", "no test", "no tests", "no debug", "no validation",
        "skip test", "skip tests", "don't test", "dont test",
        "without test", "without tests",
    )
    # Token file/path eksplisit pada teks task (artifact yang diminta task).
    _TARGET_FILE_RE = re.compile(
        r"[A-Za-z0-9_\-./\\]+\.(?:py|js|jsx|ts|tsx|html|htm|css|scss|json|md|"
        r"txt|yml|yaml|toml|ini|cfg|sh|bat|ps1|java|c|h|cpp|hpp|go|rb|php|"
        r"vue|sql|xml|csv|env)"
    )
    # Fitur/aksi yang SECARA JELAS dinyatakan task kompleks (mis. "menggunakan
    # cookie/session", "redirect", "validasi credential"). Deterministik dan
    # HANYA dipakai bila kata tersebut benar-benar tertulis di task. BUKAN
    # semantic evaluator / planner baru.
    _REQUIREMENT_FEATURES = {
        "auth": ("login", "log in", "signin", "sign in", "auth",
                 "authenticate", "authentication", "autentikasi"),
        "session": ("session", "sessions", "cookie", "cookies"),
        "redirect": ("redirect", "alihkan", "arahkan"),
        "credential": ("credential", "credentials", "password", "kata sandi"),
    }
    # Operasi CRUD hanya menjadi requirement bila task memang CRUD (kata "crud"
    # atau menyebut >= 2 operasi), agar kata seperti "tambah/ubah" pada task
    # biasa tidak salah dianggap requirement.
    _CRUD_WORDS = ("create", "read", "update", "delete")
    _CRUD_FEATURES = {
        "create": ("create", "insert", "add", "tambah", "buat"),
        "read": ("read", "get", "list", "fetch", "lihat", "tampil"),
        "update": ("update", "edit", "modify", "ubah", "perbarui"),
        "delete": ("delete", "remove", "destroy", "hapus"),
    }

    @classmethod
    def _task_requires_validation(cls, task: str) -> bool:
        """True bila task eksplisit menuntut test/validasi/debug.

        Bila user meniadakannya (mis. "jangan test/debug", "tidak perlu test"),
        kembalikan False agar test/debug tidak dipaksa. Deterministik dari teks
        task yang sudah tersedia; BUKAN semantic evaluator.
        """
        text = (task or "").lower()
        if any(phrase in text for phrase in cls._NO_VALIDATION_PHRASES):
            return False
        return any(keyword in text for keyword in cls._VALIDATION_KEYWORDS)

    @classmethod
    def _task_target_files(cls, task: str) -> set:
        """Nama file/path yang disebut eksplisit pada task.

        Ini daftar artifact yang benar-benar diminta task (requirement),
        BUKAN hitungan jumlah file/mutasi. Dipakai agar task multi-artifact
        tidak dianggap selesai hanya karena satu mutasi terjadi.
        """
        targets = set()
        for match in cls._TARGET_FILE_RE.findall(task or ""):
            base = match.replace("\\", "/").split("/")[-1].strip().lower()
            if base:
                targets.add(base)
        return targets

    @staticmethod
    def _step_targets(step: Any) -> set:
        """Semua basename path dari argumen action sebuah step (bila ada)."""
        arguments = getattr(getattr(step, "action", None), "arguments", None) or {}
        targets = set()
        for key in ("path", "file", "filename", "target", "source", "destination"):
            value = arguments.get(key)
            if value:
                targets.add(str(value).replace("\\", "/").split("/")[-1].strip().lower())
        targets.discard("")
        return targets

    @staticmethod
    def _contains_word(text: str, word: str) -> bool:
        """True bila `word` muncul sebagai kata utuh pada `text` (lowercase)."""
        return re.search(
            rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text
        ) is not None

    @classmethod
    def _task_requirement_features(cls, task: str) -> Dict[str, tuple]:
        """Fitur/aksi yang SECARA JELAS dinyatakan task (deterministik).

        Mengembalikan mapping {nama_requirement: aliases} untuk fitur yang
        benar-benar tertulis di task. Dipakai sebagai requirement tambahan
        untuk task kompleks (mis. cookie/session, redirect, CRUD). BUKAN
        semantic evaluator: hanya pencocokan kata terbatas dari teks task.
        """
        text = (task or "").lower()
        features: Dict[str, tuple] = {}
        for name, aliases in cls._REQUIREMENT_FEATURES.items():
            if any(cls._contains_word(text, alias) for alias in aliases):
                features[name] = aliases
        crud_words = [word for word in cls._CRUD_WORDS if cls._contains_word(text, word)]
        if cls._contains_word(text, "crud") or len(crud_words) >= 2:
            for word in crud_words:
                features[f"crud_{word}"] = cls._CRUD_FEATURES[word]
        return features

    @staticmethod
    def _step_evidence(step: Any) -> str:
        """Teks bukti dari argumen action step (konten/command yang dihasilkan).

        Memakai argumen action (mis. isi file yang ditulis, command yang
        dijalankan) — bukan nama tool — sehingga bukan sekadar arti tool.
        """
        arguments = getattr(getattr(step, "action", None), "arguments", None) or {}
        parts = []
        for value in arguments.values():
            if isinstance(value, str):
                parts.append(value)
            elif value is not None:
                parts.append(str(value))
        return " ".join(parts)

    @staticmethod
    def _is_change_observation(observation: AgentObservation) -> bool:
        """True bila observation menandakan perubahan nyata (implementasi)."""
        content = observation.content
        return (
            observation.success
            and isinstance(content, dict)
            and any(marker in content for marker in AgentOrchestrator._MUTATION_MARKERS)
        )

    @staticmethod
    def _is_failure_observation(observation: Optional[AgentObservation]) -> bool:
        """True bila observation merepresentasikan error aktif.

        Mencakup tool_error (tool gagal) dan command failure (command jalan
        tetapi exit_code != 0 / timeout / gagal spawn). run_command
        "success=True" TIDAK berarti command berhasil: status sebenarnya dibaca
        dari field `outcome` yang dikembalikan tool terminal.
        """
        if observation is None:
            return False
        meta = observation.metadata or {}
        if not observation.success:
            return True
        if meta.get("tool_error") or meta.get("command_failure"):
            return True
        content = observation.content
        if isinstance(content, dict):
            if content.get("outcome") in ("command_failure", "timeout", "spawn_error"):
                return True
            if content.get("success") is False:
                return True
        return False

    @staticmethod
    def _is_validation_observation(observation: AgentObservation) -> bool:
        """True bila observation adalah eksekusi command yang sukses.

        Dipakai sebagai bukti validation/test untuk task yang menuntut "test
        lulus". Sinyal generik: hasil eksekusi membawa `exit_code` (bentuk
        output run_command) dan tidak sedang gagal. Bukan hardcode nama tool,
        dan read_file/search TIDAK dianggap validation.
        """
        if not observation.success or AgentOrchestrator._is_failure_observation(observation):
            return False
        if (observation.metadata or {}).get("exit_code") is not None:
            return True
        content = observation.content
        return isinstance(content, dict) and content.get("exit_code") is not None

    def _completion_signal(self, response: LLMResponse) -> Optional[str]:
        """Teks final bila response membawa sinyal penyelesaian task.

        Sinyal final = response tanpa tool call (FINAL) ATAU response yang
        memuat action bertipe FINAL. Bila action FINAL datang bersama tool
        call, caller mengeksekusi tool call tersebut lebih dahulu lalu
        MENGHENTIKAN loop (final completion = source of truth; tool call
        tambahan bukan alasan untuk terus loop).

        Returns:
            Teks final (bisa string kosong), atau None bila tidak ada sinyal.
        """
        if response.is_final:
            return response.text
        for action in response.actions:
            if action.type == ActionType.FINAL:
                answer = (action.arguments or {}).get("answer")
                return response.text or (str(answer) if answer is not None else "")
        return None

    def _completion_detected(self, loop: AgentLoop) -> bool:
        """Deteksi penyelesaian task dari bukti langkah + requirement task.

        LEGACY-ONLY: method ini HANYA dipakai jalur loop lama
        (`use_continuous_loop=False`). Jalur continuous (`run_continuous_loop`)
        TIDAK memanggilnya: di sana AETHER tidak boleh menebak task selesai dari
        perubahan file / hasil command / keyword task — completion hanya dari
        response LLM tanpa tool call.

        Requirement yang dinilai (deterministik dari teks task + evidence step):
            1) Implementasi: minimal satu mutasi sukses (write/edit/delete/move).
               Loop read-only TIDAK dianggap selesai.
            2) Tidak ada error aktif: observasi terakhir bukan kegagalan.
            3) Bila task eksplisit menuntut test/validasi (dan TIDAK ditiadakan
               dengan "jangan test/debug"), WAJIB ada bukti eksekusi command
               (test/validation) yang sukses SETELAH perubahan terakhir. Ini
               menjaga task seperti "pastikan test lulus" tetap CONTINUE sampai
               validation benar-benar dijalankan (read_file saja tidak cukup).
            4) Bila task menyebut artifact eksplisit (mis. "a.html dan b.js"),
               SEMUA artifact tersebut harus terpenuhi. Ini mencegah task
               multi-file dianggap selesai hanya karena ada mutasi, tanpa
               memakai hitungan jumlah file/mutasi.
            5) Bila task kompleks menyebut fitur/aksi eksplisit (mis.
               "cookie/session", "redirect", "validasi credential", CRUD),
               setiap fitur tersebut WAJIB punya evidence pada argumen action
               (konten/command yang dihasilkan). Fitur tanpa evidence ->
               CONTINUE. write_file/edit_file TIDAK otomatis memenuhi semua
               requirement.

        Verification TIDAK lagi diwajibkan bila task tidak menuntutnya: mutasi
        sukses dapat menjadi evidence completion untuk task sederhana. Ini BUKAN
        "mutation == complete": tetap wajib ada mutasi sukses, tanpa error aktif,
        dan seluruh requirement task (validation/artifact) terpenuhi.

        Dipakai di dua titik: (1) setelah setiap observation di dalam loop, dan
        (2) saat iteration limit (safety limit) tercapai.
        """
        task = loop.state.task or ""
        needs_validation = self._task_requires_validation(task)
        required_targets = self._task_target_files(task)
        required_features = self._task_requirement_features(task)

        change_applied = False
        validation_after_change = False
        satisfied_targets: set = set()
        evidence_parts: List[str] = []
        last: Optional[AgentObservation] = None

        for step in loop.state.steps:
            observation = step.observation
            if observation is None:
                continue
            last = observation
            if self._is_change_observation(observation):
                change_applied = True
                # Bukti validation harus terjadi SETELAH perubahan terakhir.
                validation_after_change = False
            elif self._is_validation_observation(observation):
                validation_after_change = True
            # Artifact/fitur task dianggap terpenuhi pada step yang sukses.
            if observation.success:
                satisfied_targets.update(self._step_targets(step))
                evidence_parts.append(self._step_evidence(step))

        # 1) Wajib ada implementasi (mutasi sukses); read-only belum selesai.
        if not change_applied:
            return False
        # 2) Observasi terakhir tidak boleh kegagalan aktif (mis. write gagal).
        if last is None or self._is_failure_observation(last):
            return False
        # 3) Task yang menuntut validation wajib punya bukti eksekusi command
        #    (test/validation) setelah perubahan terakhir.
        if needs_validation and not validation_after_change:
            return False
        # 4) Semua artifact yang disebut task harus terpenuhi.
        if required_targets and not required_targets.issubset(satisfied_targets):
            return False
        # 5) Fitur/aksi yang jelas diminta task harus punya evidence pada
        #    argumen action (konten/command). Requirement tanpa evidence ->
        #    CONTINUE (hindari false positive completion task kompleks).
        if required_features:
            evidence = " ".join(evidence_parts).lower()
            for aliases in required_features.values():
                if not any(self._contains_word(evidence, alias) for alias in aliases):
                    return False
        return True

    @staticmethod
    def _completion_result() -> str:
        """Result ringkas saat completion terdeteksi (di loop atau di limit)."""
        return (
            "Task selesai: perubahan diterapkan dan requirement task terpenuhi "
            "(completion terdeteksi sebelum iteration limit)."
        )

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

    @staticmethod
    def _truncation_message() -> Message:
        """Pesan recovery saat response provider terpotong (finish_reason=length).

        Deterministik: meminta model menulis SATU file per turn (satu tool call)
        dan menulis secara bertahap bila satu file sangat besar, sehingga tidak
        membutuhkan satu response raksasa yang melampaui batas token.
        """
        return Message(
            role="user",
            content=(
                "[provider] Respons sebelumnya TERPOTONG (finish_reason=length) "
                "sehingga tool-call tidak lengkap dan DIBATALKAN — tidak ada file "
                "parsial yang ditulis. Jangan menggabungkan beberapa file besar ke "
                "dalam satu respons. Tulis SATU file per turn (satu tool call per "
                "respons). Bila satu file sangat besar, tulis secara bertahap: buat "
                "bagian awal lebih dulu, lalu lanjutkan dengan tool berikutnya pada "
                "turn selanjutnya. Lanjutkan task dari langkah terakhir yang sudah "
                "berhasil."
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

        Bila `use_continuous_loop` aktif, delegasikan ke `run_continuous_loop()`
        (Native Tool Calling, satu percakapan kontinu). Default memakai loop
        lama agar perilaku existing tidak berubah.

        Returns:
            OrchestratorResult (status DONE/FAILED, result, steps).
        """
        if self.use_continuous_loop:
            return self.run_continuous_loop(task)

        loop = AgentLoop(task=task, max_iterations=self.max_iterations)
        loop.start()

        history: List[Message] = []
        provider_error = False
        truncation_recoveries = 0

        # Task isolation: state lifecycle Bible di-reset untuk task ini.
        self.begin_bible_context_scope()

        # Context Project Intelligence (opsional) disisipkan sebelum task.
        # Knowledge dipilih berdasarkan relevance terhadap task (bukan seluruh Bible).
        brain_context = self._bible_context_message_for_task(task)
        if brain_context is not None:
            history.append(brain_context)

        while not loop.is_finished:
            # Cooperative cancellation (safe boundary): jangan memulai
            # iteration/LLM call baru bila task sudah dibatalkan.
            if self._cancel_requested():
                loop.cancel(self._cancel_reason())
                break
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

            # 2) Sinyal penyelesaian dari model adalah source of truth.
            #    FINAL (tanpa tool call) -> selesai. Bila action FINAL datang
            #    bersama tool call, `completion` tidak None tetapi response
            #    bukan is_final; tool call dieksekusi dulu lalu loop berhenti.
            #    Response yang TERPOTONG (finish_reason=length) TIDAK dianggap
            #    final: tool-call tak lengkap sudah DIBUANG oleh provider (tidak
            #    ada file parsial), dan agent diberi kesempatan melanjutkan.
            truncated = bool(getattr(response, "truncated", False))
            if truncated:
                truncation_recoveries += 1
            else:
                truncation_recoveries = 0
            completion = self._completion_signal(response)
            if response.is_final and not truncated:
                loop.finish(result=completion)
                break
            if truncated:
                emit_event(
                    self.event_sink,
                    "provider_response_truncated",
                    {
                        "provider": response.provider or getattr(self.provider, "name", ""),
                        "model": response.model or self._model_name(),
                        "finish_reason": response.finish_reason.value,
                        "completed_tool_calls": len(response.tool_calls()),
                        "incomplete_tool_calls": int(
                            getattr(response, "incomplete_tool_calls", 0)
                        ),
                        "recovery": truncation_recoveries,
                    },
                )
                # Catatan: response terpotong yang BERULANG TIDAK lagi mematikan
                # task; agent tetap diberi kesempatan melanjutkan (recovery di
                # bawah). Hanya hard-termination-nya yang dihilangkan.

            # 3) TOOL_CALL -> eksekusi tiap action, catat step, kirim balik.
            try:
                for action in response.tool_calls():
                    # Cooperative cancellation (safe boundary): jangan eksekusi
                    # tool call baru setelah pembatalan terdeteksi.
                    if self._cancel_requested():
                        loop.cancel(self._cancel_reason())
                        break
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

                    # Completion detection selama loop: setelah SETIAP
                    # observation, periksa apakah requirement task sudah
                    # terpenuhi (implementasi + validasi/artifact bila diminta,
                    # tanpa error aktif). Bila ya, hentikan loop lebih awal
                    # tanpa menunggu max_iterations. Satu tool call (termasuk
                    # write_file) TIDAK otomatis dianggap selesai; keputusan
                    # tetap memakai kriteria _completion_detected(). Bila
                    # requirement masih ada, loop tetap lanjut. max_iterations
                    # tetap menjadi safety fallback.
                    if not loop.is_finished and self._completion_detected(loop):
                        loop.finish(result=self._completion_result())
                        break

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
                # Response terpotong: tool-call yang LENGKAP sudah dieksekusi di
                # atas (progres tidak hilang), lalu minta model melanjutkan
                # dengan penulisan bertahap (satu file per turn). Jangan
                # menandai task final hanya karena actions kosong.
                if truncated:
                    if not loop.is_finished:
                        history.append(self._truncation_message())
                    if loop.is_finished:
                        break
                    continue
                # Bila model sudah memberi sinyal final (mis. action FINAL
                # bersama tool call), jangan terus loop: selesaikan sekarang.
                if completion is not None and not loop.is_finished:
                    loop.finish(result=completion)
                    break
                if loop.is_finished:
                    break
            except MaxIterationsExceeded:
                # Iteration limit = SAFETY LIMIT (loop sudah di-set FAILED oleh
                # AgentLoop). Completion detection: bila bukti langkah
                # menunjukkan pekerjaan sudah selesai (implementasi + requirement
                # task terpenuhi, tanpa error aktif), tutup loop sebagai sukses
                # (Completed), bukan iteration-limit failure. Bila belum
                # selesai -> tetap FAILED dengan alasan iteration limit.
                if self._completion_detected(loop):
                    loop.state.error = None
                    loop.finish(result=self._completion_result())
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

    # ------------------------------------------------------------------ #
    # Continuous loop (Native Tool Calling, satu percakapan kontinu)
    # ------------------------------------------------------------------ #
    def run_continuous_loop(
        self,
        task: str,
        *,
        system_prompt: Optional[str] = None,
        max_steps: int = _CONTINUOUS_SAFETY_MAX_STEPS,
        options: Optional[GenerateOptions] = None,
        user_parts: Optional[List[Dict[str, Any]]] = None,
    ) -> OrchestratorResult:
        """Jalankan SATU percakapan kontinu sampai LLM memberi jawaban final.

        Pola Native Tool Calling (tanpa nested session, tanpa completion
        detection semantik):

            LLM -> tool_calls -> eksekusi SEMUA tool -> hasil role="tool"
                -> LLM -> tool_calls -> ... -> LLM final (tanpa tool call)

        Karakteristik:
            - Satu `ConversationHistory` untuk seluruh task (system + task +
              seluruh turn), BUKAN session LLM baru per langkah.
            - Hasil tool SELALU dikirim sebagai pesan role "tool" dengan
              `tool_call_id` (via ConversationHistory), bukan dijejalkan
              sebagai pesan user.
            - Tidak ada reasoning/observation buatan AETHER di antara iterasi;
              setelah hasil tool, kontrol kembali 100% ke LLM.
            - Completion sepenuhnya ditentukan LLM: loop berhenti saat response
              final (tanpa tool call). Tidak ada semantic evaluator/planner baru.

        Args:
            task: task/permintaan user.
            system_prompt: override system prompt (default: system prompt loop).
            max_steps: emergency safety guard terhadap runaway loop. Nilai
                default TINGGI dan bukan limit behavior agent.
            options: override GenerateOptions (default: options loop).
            user_parts: content blocks opsional untuk pesan user awal (mis.
                image, format internal AETHER provider-agnostic). Kosong
                (default) = perilaku text-only tidak berubah.

        Returns:
            OrchestratorResult (status DONE/FAILED, result, steps).
        """
        effective_options = options if options is not None else self.options
        prompt = system_prompt if system_prompt is not None else self.system_prompt

        loop = AgentLoop(task=task, max_iterations=max(1, int(max_steps)))
        loop.start()

        # Satu percakapan kontinu untuk seluruh task.
        history = ConversationHistory()
        if prompt:
            history.append_system_message(prompt)
        environment_context = self._environment_context_message()
        if environment_context is not None:
            history.append_system_message(environment_context.content)
        # Task isolation: state lifecycle Bible di-reset untuk task ini.
        self.begin_bible_context_scope()
        brain_context = self._bible_context_message_for_task(task)
        if brain_context is not None:
            history.append_system_message(brain_context.content)
        history.append_user_message(task, parts=user_parts)

        tools = self._tool_definitions()
        provider_error = False
        # Safety (infrastruktur, bukan completion): batasi berapa kali response
        # provider yang TERPOTONG boleh dicoba ulang. Bukan keputusan "task
        # selesai"; hanya proteksi runaway saat provider terus memotong output.
        truncation_recoveries = 0
        # Observability-only: hitung berapa kali retrieval terhadap RESOURCE YANG
        # SAMA diminta ulang dalam task ini (mis. read_file dengan path+range+mode
        # identik). Nilai ini TIDAK dipakai untuk mengubah eksekusi, dedup, cache,
        # force, maupun loop — hanya untuk mengemit event `retrieval_repeat`.
        retrieval_seen: Dict[Tuple[str, str, str, str, str], int] = {}

        while not loop.is_finished:
            # Bible context lifecycle (murah & deterministik): sisipkan context
            # Bible HANYA bila isi Bible berubah sejak sisipan terakhir pada
            # task ini. Duplicate context yang tidak berubah tidak dikirim ulang.
            self._refresh_bible_context(history, task)
            # Cooperative cancellation (safe boundary): jangan memulai
            # iteration/LLM call baru bila task sudah dibatalkan.
            if self._cancel_requested():
                loop.cancel(self._cancel_reason())
                break

            # Runtime context compaction: kirim konteks yang BOUNDED, bukan
            # seluruh riwayat mentah. Task pendek dikembalikan apa adanya.
            messages, context_stats = self._compile_context_messages(history, tools)
            emit_event(
                self.event_sink,
                "provider_request",
                {
                    "provider": getattr(self.provider, "name", ""),
                    "model": self._model_name(),
                    "iteration": loop.iteration,
                    "tool_count": len(tools),
                    **context_stats,
                },
            )
            try:
                gen_result = self.provider.generate(
                    messages=messages,
                    options=effective_options,
                    tools=tools or None,
                    tool_choice=self.tool_choice,
                )
                response: LLMResponse = self.provider.normalize_response(gen_result)
            except Exception as exc:  # noqa: BLE001 - provider error -> FAILED jelas
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
                # Error handling existing: hentikan loop dengan pesan jelas,
                # tanpa mengarang keputusan reasoning pengganti LLM.
                loop.fail(f"{type(exc).__name__}: {exc}")
                break

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

            # Cooperative cancellation (safe boundary): pembatalan yang datang
            # saat LLM call berlangsung terdeteksi DI SINI — sebelum tool call
            # apa pun dieksekusi dan sebelum reasoning dilanjutkan.
            if self._cancel_requested():
                loop.cancel(self._cancel_reason())
                break

            # Commentary natural dari LLM (bila ada); bukan reasoning buatan.
            commentary = self._extract_commentary(response)
            if commentary:
                emit_event(
                    self.event_sink,
                    "agent_commentary",
                    {"text": commentary, "iteration": loop.iteration},
                )

            truncated = bool(getattr(response, "truncated", False))
            if not truncated:
                truncation_recoveries = 0

            # LLM TIDAK memanggil tool -> jawaban final (source of truth LLM).
            # Ini SATU-SATUNYA jalur completion continuous loop. TIDAK ada
            # heuristic file/command/keyword/jumlah-step yang boleh
            # menyelesaikan loop lebih awal: keputusan selesai murni dari
            # response LLM tanpa tool call.
            if not response.has_tool_calls:
                if truncated:
                    # Response terpotong -> BUKAN final. Bounded recovery
                    # (infrastruktur, bukan completion): beri model kesempatan
                    # melanjutkan; bila provider terus memotong, berhenti dengan
                    # error JELAS agar tidak runaway.
                    truncation_recoveries += 1
                    emit_event(
                        self.event_sink,
                        "provider_response_truncated",
                        {
                            "provider": response.provider
                            or getattr(self.provider, "name", ""),
                            "model": response.model or self._model_name(),
                            "finish_reason": response.finish_reason.value,
                            "recovery": truncation_recoveries,
                        },
                    )
                    # Catatan: truncation berulang TIDAK lagi mematikan task;
                    # agent terus diberi kesempatan melanjutkan sampai LLM
                    # menghasilkan jawaban final (bukan FAILED karena cap).
                    history.append_user_message(self._truncation_message().content)
                    continue
                history.append_assistant_message(content=response.text or "")
                loop.finish(result=response.text or "")
                break

            # LLM memanggil tool: simpan assistant(tool_calls) penuh lebih dulu,
            # baru eksekusi tool call sebagai SATU BATCH melalui Tool Execution
            # Coordinator (paralel untuk call independen, sequential untuk yang
            # berkonflik), lalu kirim hasilnya (role="tool") pada urutan input.
            model_tool_calls = response.tool_calls()
            tool_calls = [
                ToolCall.create(action.name, action.arguments, id=action.id)
                for action in model_tool_calls
            ]
            history.append_assistant_message(
                content=response.text or None, tool_calls=tool_calls
            )

            # Cooperative cancellation (safe boundary): jangan mulai batch baru
            # bila pembatalan sudah diminta.
            if self._cancel_requested():
                loop.cancel(self._cancel_reason())
                break

            # Argumen per tool call (untuk target event) dipetakan lewat id agar
            # callback event tetap memakai bentuk yang sama seperti sebelumnya.
            actions_by_id = {
                tool_call.id: action
                for tool_call, action in zip(tool_calls, model_tool_calls)
            }

            def _emit_tool_called(tool_call: ToolCall) -> None:
                action = actions_by_id.get(tool_call.id)
                arguments = (
                    dict(getattr(action, "arguments", {}) or {})
                    if action is not None
                    else {}
                )
                emit_event(
                    self.event_sink,
                    "tool_called",
                    {
                        "tool": tool_call.name,
                        "tool_call_id": tool_call.id,
                        "arguments": arguments,
                        "target": self._tool_target(arguments),
                        "iteration": loop.iteration,
                    },
                )

            def _emit_tool_finished(
                tool_call: ToolCall, payload: ToolResultPayload
            ) -> None:
                action = actions_by_id.get(tool_call.id)
                arguments = (
                    dict(getattr(action, "arguments", {}) or {})
                    if action is not None
                    else {}
                )
                emit_event(
                    self.event_sink,
                    "tool_completed",
                    {
                        "tool": payload.tool_name,
                        "tool_call_id": tool_call.id,
                        "success": payload.is_success,
                        "error": None if payload.is_success else payload.to_content(),
                        "target": self._tool_target(arguments),
                        "metadata": {},
                    },
                )
                emit_event(
                    self.event_sink,
                    "observation_received",
                    {
                        "tool": payload.tool_name,
                        "tool_call_id": tool_call.id,
                        "success": payload.is_success,
                        "content": payload.output,
                    },
                )
                # Observability-only: deteksi retrieval berulang atas resource
                # yang sama (path + range/symbol + mode). TIDAK memblokir
                # retrieval, TIDAK mengubah dedup/cache/force behavior, dan
                # TIDAK mengubah loop. Dibungkus aman agar observability tidak
                # pernah menggagalkan eksekusi tool.
                try:
                    identity = self._retrieval_identity(
                        payload.tool_name, arguments
                    )
                    if identity is not None:
                        key = (
                            str(payload.tool_name),
                            str(identity["path"]),
                            str(identity["range"]),
                            str(identity["mode"]),
                            "force" if identity["force"] else "normal",
                        )
                        retrieval_seen[key] = retrieval_seen.get(key, 0) + 1
                        if retrieval_seen[key] > 1:
                            output = payload.output
                            stub = bool(
                                isinstance(output, dict)
                                and (
                                    output.get("already_available")
                                    or output.get("already_read")
                                    or output.get("already_searched")
                                )
                            )
                            emit_event(
                                self.event_sink,
                                "retrieval_repeat",
                                {
                                    "tool": payload.tool_name,
                                    "path": identity["path"],
                                    "range": identity["range"],
                                    "mode": identity["mode"],
                                    "force": bool(identity["force"]),
                                    "stub": stub,
                                    "result": "stub" if stub else "full",
                                    "repeat_count": int(retrieval_seen[key]),
                                    "iteration": loop.iteration,
                                },
                            )
                except Exception:  # noqa: BLE001 - event tidak boleh crash
                    pass

            batch = self.executor.execute_tool_calls(
                tool_calls,
                on_start=_emit_tool_called,
                on_complete=_emit_tool_finished,
                cancel_check=self._cancel_requested,
            )

            # Hasil dipetakan kembali sesuai urutan input (tool_call id),
            # bukan urutan selesai. Payload None = tidak dieksekusi (cancel).
            for action, payload in zip(model_tool_calls, batch.payloads):
                if payload is None:
                    continue
                # Hasil tool SELALU dikirim sebagai pesan role "tool".
                history.append_tool_result(
                    payload.tool_call_id, payload.tool_name, payload.to_content()
                )
                # Catat step untuk observability (bukan keputusan completion).
                # Tidak ada hard limit step: agent berjalan selama diperlukan.
                loop.record_action(self.executor.to_agent_action(action))
                loop.record_observation(self._tool_payload_to_observation(payload))
            if batch.cancelled:
                loop.cancel(self._cancel_reason())
                break

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

    @staticmethod
    def _tool_payload_to_observation(payload: ToolResultPayload) -> AgentObservation:
        """Ubah ToolResultPayload menjadi AgentObservation (bookkeeping step).

        HANYA dipakai untuk mencatat step (observability). Tidak menentukan
        completion dan TIDAK pernah dikirim ke LLM: histori LLM memakai pesan
        role "tool" lewat ConversationHistory.
        """
        if payload.is_success:
            return AgentObservation(
                content=payload.output,
                success=True,
                metadata={"tool": payload.tool_name},
            )
        return AgentObservation(
            content=None,
            success=False,
            error=payload.to_content(),
            metadata={"tool": payload.tool_name, "tool_error": True},
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
        if self.brain is None or not self.brain_learning:
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


