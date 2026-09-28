"""Safety/control layer Consultant: bound retrieval Project Map + investigasi.

Tujuan modul ini SEMPIT dan hanya boundary (bukan "otak kedua"):

    - mencegah query Project Map berulang (identik setelah normalisasi);
    - mencegah eksplorasi map runaway (batas jumlah query per giliran);
    - mencegah eksplorasi investigasi runaway (batas jumlah read_file/search_code/
      run_command/list_files per giliran);
    - menghentikan pola zero-result beruntun pada map;
    - memaksa model berhenti mencari setelah budget retrieval habis dan
      menyusun jawaban final dari evidence yang sudah ada.

Policy TIDAK menentukan jawaban: ia hanya membatasi & mengarahkan. Keputusan
akhir tetap milik LLM.

Desain (tanpa menyentuh loop generik Agent/AgentOrchestrator):

    1. `ConsultantRetrievalGuard`  = state machine per giliran konsultasi
       (counter, dedup query ternormalisasi, zero-result streak, stopped).
    2. Wrapper tool (`ConsultantBoundedMapTool`) memanggil
       `guard.reserve()` SEBELUM eksekusi dan `guard.record()` setelahnya.
       Tool yang melewati bound TIDAK dieksekusi; ia dikembalikan sebagai
       ToolResult "bound" yang jelas untuk LLM.
    3. `ConsultantBoundProvider` = proxy provider yang, begitu guard `stopped`,
       MELEPAS tool yang di-bound dari daftar tool sehingga LLM tidak bisa
       lagi memanggilnya -> LLM wajib menyusun jawaban final.

`max_steps` (safeguard generik, mis. 40 di ConsultantService) TETAP menjadi
ultimate safety guard; bound ini hanya menghentikan runaway jauh lebih awal dan
secara graceful (bukan FAILED).
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional, Set

from agent_ai.consultant.policy import (
    CONSULTANT_INVESTIGATION_TOOLS,
    CONSULTANT_MAP_QUERY_TOOLS,
    ConsultantRetrievalBudget,
    retrieval_budget_for_mode,
)

#: Semua tool yang di-bound Consultant = map query + investigasi.
_BOUND_TOOLS = CONSULTANT_MAP_QUERY_TOOLS + CONSULTANT_INVESTIGATION_TOOLS
from agent_ai.core.response import LLMResponse
from agent_ai.providers.base import (
    BaseProvider,
    GenerateOptions,
    GenerateResult,
    Message,
    ToolChoice,
    ToolDefinition,
)

#: Normalisasi whitespace berlebih (spasi/tab/newline) menjadi satu spasi.
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_map_query(value: Any) -> str:
    """Normalisasi query map untuk perbandingan EXACT-normalized.

    Aturan (sederhana, tanpa fuzzy matching):
        - ubah ke string & trim;
        - lowercase (case-insensitive);
        - perlakukan whitespace berlebih secara sama (kolaps ke satu spasi).

    Contoh: "  SaFeGuard " == "safeguard" -> True.
    """
    return _WHITESPACE_RE.sub(" ", str(value or "").strip()).lower()


def _is_zero_result(result: Any) -> bool:
    """True bila hasil query map menandakan TIDAK ada match.

    atlas_query / rig_query mengembalikan dict dengan `total` (jumlah match
    sebelum limit). Defensif terhadap bentuk lain tanpa melempar.
    """
    if not isinstance(result, dict):
        return False
    total = result.get("total")
    if total is None:
        total = result.get("returned", result.get("count"))
    if total is None:
        return False
    try:
        return int(total) == 0
    except (TypeError, ValueError):
        return False


class ConsultantRetrievalGuard:
    """State machine bound retrieval Project Map + investigasi untuk SATU giliran konsultasi.

    Dibuat sekali per panggilan `ConsultantService.consult()`, sehingga batas
    dihitung per pertanyaan (bukan lintas sesi). Mutasi state dijaga
    `threading.Lock`: Tool Execution Coordinator dapat menjalankan beberapa tool
    READ (mis. atlas_query + rig_query) PARALEL dalam satu batch, sehingga
    `reserve`/`record` bisa dipanggil bersamaan.

    Mencakup:
        - Tool map query (atlas_query, rig_query) — dedup + bound jumlah query.
        - Tool investigasi (read_file, search_code, run_command, list_files) —
          bound jumlah pemanggilan.

    Args:
        budget: preset budget. Bila None, diambil dari `mode`.
        mode: mode Consultant ("quick" | "investigate"); dipakai bila `budget`
            tidak diberikan.
    """

    def __init__(
        self,
        budget: Optional[ConsultantRetrievalBudget] = None,
        *,
        mode: Optional[str] = None,
    ) -> None:
        self.budget = budget or retrieval_budget_for_mode(mode)
        self._counts: Dict[str, int] = {name: 0 for name in _BOUND_TOOLS}
        self._seen: Dict[str, Set[str]] = {
            name: set() for name in _BOUND_TOOLS
        }
        self._zero_result_streak = 0
        self._stopped = False
        self._stop_reason: Optional[str] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #
    @property
    def stopped(self) -> bool:
        """True bila pencarian map sudah dihentikan (bound tercapai)."""
        return self._stopped

    @property
    def stop_reason(self) -> Optional[str]:
        """Alasan pencarian map dihentikan (None bila belum)."""
        return self._stop_reason

    @property
    def blocked_tool_names(self) -> Set[str]:
        """Nama tool yang harus dilepas dari penawaran saat `stopped`."""
        if not self._stopped:
            return set()
        return set(_BOUND_TOOLS)

    def is_map_query_tool(self, name: Any) -> bool:
        """True bila `name` adalah tool yang di-bound oleh guard Consultant."""
        return name in self._counts

    def counts(self) -> Dict[str, int]:
        """Salinan jumlah query yang BENAR-BENAR dijalankan per tool."""
        return dict(self._counts)

    # ------------------------------------------------------------------ #
    # Hook: sebelum & sesudah eksekusi tool
    # ------------------------------------------------------------------ #
    def _is_investigation_tool(self, tool_name: str) -> bool:
        """True bila tool adalah investigasi (bukan map query)."""
        return tool_name in CONSULTANT_INVESTIGATION_TOOLS

    def reserve(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Putuskan apakah tool boleh dieksekusi (dipanggil SEBELUM eksekusi).

        Untuk map query (atlas_query/rig_query): dedup by query + count limit.
        Untuk investigasi (read_file/search_code/run_command/list_files): count
        limit SAJA (dedup sudah ditangani ToolReadCache/read_file sendiri).

        Returns:
            None bila tool boleh dieksekusi; atau dict ToolResult "bound" yang
            jelas untuk LLM bila tool harus DIBLOKIR (tidak dieksekusi).
        """
        if tool_name not in self._counts:
            return None

        query = (arguments or {}).get("query")
        normalized = normalize_map_query(query)

        # Lock: batch tool READ dapat dijalankan PARALEL, jadi reserve/record
        # bisa dipanggil bersamaan. Lock menjaga counter/dedup/streak konsisten.
        with self._lock:
            # Sudah stopped: TIDAK ada tool yang dieksekusi lagi.
            if self._stopped:
                return self._bound_result(
                    tool_name, query, self._stop_reason or "search_stopped"
                )

            # Dedup query identik (ternormalisasi) HANYA untuk map query tools.
            # Investigation tools punya dedup sendiri (ToolReadCache -
            # already_available/already_searched) sehingga tidak perlu dedup di sini.
            if not self._is_investigation_tool(tool_name):
                if normalized in self._seen[tool_name]:
                    return self._stop(tool_name, query, "repeated_query")
                # Reservasi optimistik untuk map: tandai query sebagai 'sudah
                # dipakai' SEBELUM eksekusi, sehingga dua query identik yang datang
                # PARALEL pada batch yang sama tidak dieksekusi dua kali.
                self._seen[tool_name].add(normalized)

            # Batas jumlah pemanggilan per tool -> blokir.
            limit = self.budget.max_queries_for(tool_name)
            if limit is not None and self._counts[tool_name] >= limit:
                return self._stop(tool_name, query, "query_limit_reached")

            return None

    def record(
        self, tool_name: str, arguments: Dict[str, Any], result: Any
    ) -> None:
        """Catat hasil eksekusi tool yang BENAR-BENAR dijalankan (setelah eksekusi)."""
        if tool_name not in self._counts:
            return

        normalized = normalize_map_query((arguments or {}).get("query"))
        with self._lock:
            # Catat query HANYA untuk map query tools (investigasi tidak pakai dedup).
            if not self._is_investigation_tool(tool_name):
                self._seen[tool_name].add(normalized)
            self._counts[tool_name] += 1

            if _is_zero_result(result):
                self._zero_result_streak += 1
                if self._zero_result_streak >= self.budget.max_zero_result_queries:
                    self._mark_stopped("zero_result_streak")
            else:
                self._zero_result_streak = 0

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _stop(
        self, tool_name: str, query: Any, reason: str
    ) -> Dict[str, Any]:
        self._mark_stopped(reason)
        return self._bound_result(tool_name, query, reason)

    def _mark_stopped(self, reason: str) -> None:
        if not self._stopped:
            self._stopped = True
            self._stop_reason = reason

    def _bound_result(
        self, tool_name: str, query: Any, reason: str
    ) -> Dict[str, Any]:
        """ToolResult yang jelas: batas pencarian tercapai -> susun jawaban."""
        guidance = (
            "BATAS PENCARIAN SUDAH TERCAPAI "
            f"(alasan: {reason}). Tool '{tool_name}' TIDAK dijalankan. "
            "JANGAN memanggil tool yang sudah dibatasi lagi. "
            "Hentikan pencarian dan susun jawaban final SEKARANG "
            "berdasarkan evidence yang sudah ada (Project Bible/konteks + "
            "hasil pencarian sebelumnya). Sampaikan keterbatasan bila ada dan "
            "jangan mengarang fakta."
        )
        return {
            "consultant_retrieval_bound": True,
            "status": "retrieval_bound",
            "reason": reason,
            "tool": tool_name,
            "query": query,
            "executed": False,
            "counts": self.counts(),
            "limits": {
                "atlas_query": self.budget.max_atlas_queries,
                "rig_query": self.budget.max_rig_queries,
                "read_file": self.budget.max_read_file,
                "search_code": self.budget.max_search_code,
                "run_command": self.budget.max_run_command,
                "list_files": self.budget.max_list_files,
            },
            "message": guidance,
            "guidance": "susun jawaban final dari evidence yang sudah ada",
        }


class ConsultantBoundProvider(BaseProvider):
    """Proxy provider Consultant: melepas tool setelah bound tercapai.

    Membungkus provider nyata (tanpa mengubahnya) dan hanya menyaring daftar
    tool pada pemanggilan `generate`. Selama bound belum tercapai, provider
    menerima SEMUA tool apa adanya (perilaku tidak berubah). Begitu guard
    `stopped`, tool yang di-bound (map query + investigasi) dilepas dari
    daftar tool untuk satu (atau beberapa) pemanggilan berikutnya, sehingga
    LLM tidak lagi punya tool tersebut dan harus menghasilkan jawaban final ->
    konsultasi selesai normal (bukan FAILED karena menyentuh max_steps).

    Tool non-bound (`project_map_status`, `update_project_bible`) TIDAK
    dilepas: bound ini khusus map query + tool investigasi.
    """

    def __init__(
        self, provider: BaseProvider, guard: ConsultantRetrievalGuard
    ) -> None:
        self._provider = provider
        self._guard = guard

    @property
    def name(self) -> str:  # type: ignore[override]
        return getattr(self._provider, "name", "base")

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Message]] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[List[ToolDefinition]] = None,
        tool_choice: Optional[ToolChoice] = None,
    ) -> GenerateResult:
        effective_tools = tools
        effective_choice = tool_choice

        if self._guard.stopped and tools:
            blocked = self._guard.blocked_tool_names
            filtered = [
                tool for tool in tools if getattr(tool, "name", None) not in blocked
            ]
            if len(filtered) != len(tools):
                effective_tools = filtered or None
                # tool_choice mungkin menunjuk tool yang baru dilepas -> jangan
                # paksa; biarkan provider memilih (mode auto).
                effective_choice = None

        return self._provider.generate(
            prompt=prompt,
            messages=messages,
            options=options,
            tools=effective_tools,
            tool_choice=effective_choice,
        )

    def normalize_response(self, result: GenerateResult) -> LLMResponse:
        return self._provider.normalize_response(result)

    # `is_available` dan `knowledge_budget_tokens` SUDAH ada di BaseProvider,
    # sehingga tanpa override eksplisit lookup normal akan memakai implementasi
    # base dan hook provider asli HILANG saat provider dibungkus proxy ini.
    # `knowledge_budget_tokens` khususnya penting: Ollama memakainya untuk
    # memotong konteks pengetahuan (Bible) sesuai `num_ctx`. Delegasi eksplisit
    # menjaga PARITY provider (mis. Ollama) pada jalur Consultant — provider
    # asli tetap satu-satunya sumber kebijakan ini.
    def is_available(self) -> bool:  # type: ignore[override]
        return bool(self._provider.is_available())

    def knowledge_budget_tokens(self) -> Optional[int]:  # type: ignore[override]
        getter = getattr(self._provider, "knowledge_budget_tokens", None)
        if getter is None:
            return None
        try:
            return getter()
        except Exception:  # noqa: BLE001 - hint provider tidak boleh menggagalkan konsultasi
            return None

    def __getattr__(self, item: str) -> Any:
        # Delegasi transparan untuk atribut lain (config, is_available,
        # knowledge_budget_tokens, dll.) agar proxy tidak menyembunyikan
        # kapabilitas provider. `_provider`/`_guard` dikecualikan agar tidak
        # terjadi rekursi saat atribut internal belum ada.
        if item in {"_provider", "_guard"}:
            raise AttributeError(item)
        return getattr(self._provider, item)
