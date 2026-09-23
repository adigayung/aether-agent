"""Regression tests: bound retrieval Project Map untuk AETHER Consultant.

Menguji SAFETY/CONTROL LAYER Consultant (bukan Agent):

    A. QUICK   : 3 query Atlas berbeda boleh jalan; query ke-4 dibatasi;
                 setelah bound Consultant tetap menghasilkan final answer.
    B. Repeated: "safeguard" == "SAFEGUARD" (exact-normalized) -> query kedua
                 diblokir (tidak dieksekusi).
    C. Zero    : beberapa query berbeda yang semuanya 0 result TIDAK
                 menghasilkan 40 tool calls.
    D. Investigate: bound lebih tinggi daripada QUICK.
    E. Agent   : budget/perilaku Agent TIDAK berubah (tanpa bound Consultant;
                 refresh_project_map tetap ada; max_steps generik tetap 1000;
                 safeguard `max_steps=40` Consultant tetap ada).
    F. Regresi : pertanyaan kecil QUICK + zero-result map queries (sinonim)
                 TIDAK runaway sampai 40 steps dan menghasilkan final answer;
                 hook provider (num_ctx/is_available) tetap didelegasikan.
    G. Investigate: source-level tools (search_code/read_file) tetap bisa
                 dipakai berkali-kali; bound map lebih longgar dari QUICK.
    H. Paralel : batch READ paralel tetap menghormati bound (reserve atomik).

Semua test memakai fake tool/provider — TIDAK ada API provider eksternal.

Jalankan:
    python -m pytest tests/test_consultant_retrieval_bound.py
atau:
    python tests/test_consultant_retrieval_bound.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.consultant.guard import (  # noqa: E402
    ConsultantBoundProvider,
    ConsultantRetrievalGuard,
    normalize_map_query,
)
from agent_ai.consultant.models import MODE_INVESTIGATE, MODE_QUICK  # noqa: E402
from agent_ai.consultant.policy import (  # noqa: E402
    INVESTIGATION_RETRIEVAL_BUDGET,
    QUICK_RETRIEVAL_BUDGET,
    retrieval_budget_for_mode,
)
from agent_ai.consultant.service import ConsultantService  # noqa: E402
from agent_ai.consultant.tools import (  # noqa: E402
    ConsultantBoundedMapTool,
    build_consultant_registry,
)
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.orchestrator import (  # noqa: E402
    _CONTINUOUS_SAFETY_MAX_STEPS,
    AgentOrchestrator,
)
from agent_ai.core.response import FinishReason, LLMAction, LLMResponse  # noqa: E402
from agent_ai.providers.base import (  # noqa: E402
    BaseProvider,
    GenerateOptions,
    GenerateResult,
    ToolDefinition,
)
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402


# --------------------------------------------------------------------------- #
# Fake tool/provider (deterministik, tanpa network)
# --------------------------------------------------------------------------- #
class FakeMapTool(BaseTool):
    """Tool map palsu: mencatat query yang BENAR-BENAR dieksekusi."""

    def __init__(self, name: str, result_factory=None) -> None:
        self.name = name
        self.description = f"fake {name}"
        self.input_schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.calls: List[str] = []
        self._result_factory = result_factory or (
            lambda q: {"query": q, "total": 1, "returned": 1, "results": [{"name": q}]}
        )

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        query = arguments.get("query")
        self.calls.append(query)
        return self._result_factory(query)


class MapLoopProvider(BaseProvider):
    """Provider palsu yang terus memanggil atlas_query selama tool itu ada.

    Bila `atlas_query` TIDAK lagi ditawarkan (dilepas oleh ConsultantBoundProvider
    setelah bound), provider mengembalikan jawaban final.
    """

    name = "map-loop"

    def __init__(self) -> None:
        self.calls = 0
        self._tools_seen: set = set()
        self.tools_history: List[set] = []

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[Any] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[Any] = None,
        tool_choice: Optional[Any] = None,
    ) -> GenerateResult:
        self._tools_seen = {getattr(t, "name", "") for t in (tools or [])}
        self.tools_history.append(self._tools_seen)
        return GenerateResult(text="", model="fake", provider=self.name)

    def normalize_response(self, result: GenerateResult) -> LLMResponse:
        self.calls += 1
        if "atlas_query" in self._tools_seen:
            return LLMResponse(
                actions=[
                    LLMAction(name="atlas_query", arguments={"query": f"q{self.calls}"})
                ],
                finish_reason=FinishReason.TOOL_CALLS,
            )
        return LLMResponse(
            text="Jawaban final berdasarkan evidence yang sudah ada.",
            finish_reason=FinishReason.STOP,
        )


class ScriptedProvider(BaseProvider):
    """Provider palsu dengan skrip LLMResponse berurutan (untuk jalur Agent)."""

    name = "scripted"

    def __init__(self, script: List[LLMResponse]) -> None:
        self.script = list(script)
        self.calls = 0

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[Any] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[Any] = None,
        tool_choice: Optional[Any] = None,
    ) -> GenerateResult:
        return GenerateResult(text="", model="fake", provider=self.name)

    def normalize_response(self, result: GenerateResult) -> LLMResponse:
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[idx]


def _atlas_call(query: str) -> LLMResponse:
    return LLMResponse(
        actions=[LLMAction(name="atlas_query", arguments={"query": query})],
        finish_reason=FinishReason.TOOL_CALLS,
    )


def _final(text: str) -> LLMResponse:
    return LLMResponse(text=text, finish_reason=FinishReason.STOP)


class SynonymZeroResultProvider(BaseProvider):
    """Provider palsu: meniru LLM yang mengejar banyak SINONIM (semua 0 hasil).

    Selama `atlas_query` masih ditawarkan, provider terus memanggilnya dengan
    query SINONIM yang BERBEDA (bukan query identik), sehingga yang menghentikan
    runaway adalah bound zero-result Consultant — bukan aturan repeated-query.
    Begitu tool map dilepas (bound tercapai), provider menyusun jawaban final.
    """

    name = "synonym-zero-result"

    #: Sinonim berbeda (>= bound) untuk membuktikan pencarian dihentikan oleh
    #: policy/bound, bukan oleh habisnya daftar kata.
    SYNONYMS = (
        "safeguard",
        "max_tool_calls",
        "tool_call_limit",
        "max_iterations",
        "step limit",
        "loop guard",
        "runaway protection",
        "retrieval budget",
        "tool budget",
        "step cap",
        "iteration cap",
        "tool call guard",
    )

    def __init__(self) -> None:
        self.atlas_attempts = 0
        self.tools_history: List[set] = []
        self._offered: set = set()

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[Any] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[Any] = None,
        tool_choice: Optional[Any] = None,
    ) -> GenerateResult:
        self._offered = {getattr(t, "name", "") for t in (tools or [])}
        self.tools_history.append(set(self._offered))
        return GenerateResult(text="", model="fake", provider=self.name)

    def normalize_response(self, result: GenerateResult) -> LLMResponse:
        if "atlas_query" in self._offered:
            query = self.SYNONYMS[self.atlas_attempts % len(self.SYNONYMS)]
            self.atlas_attempts += 1
            return _atlas_call(query)
        return LLMResponse(
            text="Jawaban final: pertanyaan kecil cukup dijawab dari evidence yang ada.",
            finish_reason=FinishReason.STOP,
        )


def _consultant_registry_with_fakes(guard: ConsultantRetrievalGuard, atlas: FakeMapTool) -> ToolRegistry:
    """Registry ala Consultant dengan tool map PALSU (atlas dibungkus guard)."""
    registry = ToolRegistry()
    for tool in (
        atlas,
        FakeMapTool("rig_query"),
        FakeMapTool("project_map_status"),
    ):
        if guard.is_map_query_tool(tool.name):
            registry.register(ConsultantBoundedMapTool(tool, guard))
        else:
            registry.register(tool)
    return registry


# --------------------------------------------------------------------------- #
# A. QUICK: 3 query berbeda boleh; ke-4 dibatasi; final answer tetap dihasilkan
# --------------------------------------------------------------------------- #
def test_a_quick_three_queries_allowed_fourth_blocked() -> None:
    guard = ConsultantRetrievalGuard(mode=MODE_QUICK)
    inner = FakeMapTool("atlas_query")
    wrapped = ConsultantBoundedMapTool(inner, guard)

    for i in range(3):
        assert wrapped.execute(query=f"alpha{i}")["total"] == 1
    assert len(inner.calls) == 3, inner.calls
    assert guard.stopped is False

    bound = wrapped.execute(query="alpha4")
    assert bound["consultant_retrieval_bound"] is True, bound
    assert bound["status"] == "retrieval_bound", bound
    assert bound["executed"] is False, bound
    assert "BATAS" in bound["message"].upper(), bound["message"]
    # Query ke-4 TIDAK dieksekusi.
    assert len(inner.calls) == 3, inner.calls
    assert guard.stopped is True


def test_a2_consultant_produces_final_answer_after_bound() -> None:
    """Setelah bound, Consultant (loop) tetap selesai DONE dengan jawaban final."""
    guard = ConsultantRetrievalGuard(mode=MODE_QUICK)
    inner = FakeMapTool("atlas_query")
    registry = _consultant_registry_with_fakes(guard, inner)

    provider = MapLoopProvider()
    orchestrator = AgentOrchestrator(
        provider=ConsultantBoundProvider(provider, guard),
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="fake"),
        system_prompt="Consultant",
        use_continuous_loop=True,
    )
    result = orchestrator.run_continuous_loop("kenapa Consultant berhenti di max_steps=40?", max_steps=40)

    assert result.status == AgentStatus.DONE, result.error
    assert result.result and "Jawaban final" in result.result, result.result
    # Hanya 3 query atlas yang BENAR-BENAR dieksekusi (ke-4 diblokir).
    assert len(inner.calls) == 3, inner.calls
    # Jauh di bawah safeguard generik.
    assert result.iterations < 40, result.iterations
    # Map tool dilepas setelah bound (buktikan atlas hilang di salah satu turn).
    assert any("atlas_query" not in seen for seen in provider.tools_history[-2:])


# --------------------------------------------------------------------------- #
# B. Repeated query (exact-normalized)
# --------------------------------------------------------------------------- #
def test_b_repeated_query_normalized_is_blocked() -> None:
    assert normalize_map_query("  SaFeGuard ") == "safeguard"
    assert normalize_map_query("max   tool\ncalls") == "max tool calls"

    guard = ConsultantRetrievalGuard(mode=MODE_QUICK)
    inner = FakeMapTool("atlas_query")
    wrapped = ConsultantBoundedMapTool(inner, guard)

    assert wrapped.execute(query="safeguard")["total"] == 1
    bound = wrapped.execute(query="  SAFEGUARD ")
    assert bound["consultant_retrieval_bound"] is True, bound
    assert bound["reason"] == "repeated_query", bound
    # Query kedua TIDAK dieksekusi (tetap 1).
    assert len(inner.calls) == 1, inner.calls


# --------------------------------------------------------------------------- #
# C. Zero-result runaway dihentikan (bukan 40 tool calls)
# --------------------------------------------------------------------------- #
def test_c_zero_result_streak_stops_map_search() -> None:
    guard = ConsultantRetrievalGuard(mode=MODE_QUICK)
    zero = FakeMapTool("atlas_query", result_factory=lambda q: {"query": q, "total": 0, "returned": 0})

    for query in ("safeguard", "max_tool_calls", "tool_call_limit"):
        assert guard.reserve("atlas_query", {"query": query}) is None
        guard.record("atlas_query", {"query": query}, zero.execute(query=query))

    assert guard.stopped is True, "pola zero-result beruntun harus menghentikan map search"
    assert guard.counts()["atlas_query"] == 3

    # Query berbeda berikutnya TIDAK dieksekusi.
    assert guard.reserve("atlas_query", {"query": "max_iterations"}) is not None


def test_c2_zero_result_end_to_end_not_40_tool_calls() -> None:
    guard = ConsultantRetrievalGuard(mode=MODE_QUICK)
    inner = FakeMapTool(
        "atlas_query", result_factory=lambda q: {"query": q, "total": 0, "returned": 0}
    )
    registry = _consultant_registry_with_fakes(guard, inner)

    provider = MapLoopProvider()
    orchestrator = AgentOrchestrator(
        provider=ConsultantBoundProvider(provider, guard),
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="fake"),
        use_continuous_loop=True,
    )
    result = orchestrator.run_continuous_loop("cari konsep safeguard", max_steps=40)

    assert result.status == AgentStatus.DONE, result.error
    # Zero-result streak (3) menghentikan pencarian jauh sebelum 40.
    assert len(inner.calls) == 3, inner.calls
    assert result.iterations < 40, result.iterations


# --------------------------------------------------------------------------- #
# F. REGRESI KASUS ASLI: "Kenapa Consultant berhenti setelah max_steps=40?"
#    Pertanyaan kecil + zero-result map queries (sinonim) TIDAK boleh runaway
#    sampai 40 steps; Consultant HARUS selesai DONE dengan jawaban final.
# --------------------------------------------------------------------------- #
def test_f_original_case_not_runaway_and_final_answer(monkeypatch) -> None:
    """End-to-end lewat `ConsultantService.consult` (QUICK), tanpa provider nyata.

    Meniru kasus asli: pertanyaan kecil memicu LLM mengejar banyak sinonim
    `atlas_query` yang semuanya 0 hasil. Sebelum perbaikan, pola ini berujung
    `atlas_query` x 40 -> safeguard generik `max_steps=40` -> FAILED. Setelah
    perbaikan: bound retrieval Consultant menghentikan pencarian, tool map
    dilepas, dan Consultant menyusun jawaban final (DONE).
    """
    import agent_ai.tools.project_map as project_map_mod

    # 4) Agent TIDAK terkena policy Consultant: tool map registry Agent tetap
    #    TIDAK dibungkus bound Consultant. Dicek SEBELUM monkeypatch agar
    #    memakai builder registry Agent yang asli.
    from agent_ai.tools.registry import build_registry

    agent_atlas = build_registry(root=None).get("atlas_query")
    assert type(agent_atlas).__name__ == "AtlasQueryTool", type(agent_atlas).__name__
    assert not isinstance(agent_atlas, ConsultantBoundedMapTool)

    zero_atlas = FakeMapTool(
        "atlas_query",
        result_factory=lambda q: {"query": q, "total": 0, "returned": 0},
    )
    zero_rig = FakeMapTool(
        "rig_query",
        result_factory=lambda q: {"query": q, "total": 0, "returned": 0},
    )

    def _fake_project_map_tools(root=None, include_refresh=False):
        return [zero_atlas, zero_rig, FakeMapTool("project_map_status")]

    monkeypatch.setattr(
        project_map_mod, "build_project_map_tools", _fake_project_map_tools
    )

    provider = SynonymZeroResultProvider()
    service = ConsultantService()
    result = service.consult(
        "Kenapa Consultant berhenti setelah max_steps=40?",
        provider=provider,
        root=None,
        mode="quick",
    )

    # 1) Selesai NORMAL dengan jawaban final (bukan FAILED karena safeguard).
    assert result.status == "done", (result.status, result.error)
    assert result.reply.strip(), result.reply
    assert result.error is None, result.error
    assert "max_steps" not in (result.error or "")

    # 2) Tidak runaway: jumlah query atlas yang BENAR-BENAR dieksekusi kecil.
    assert len(zero_atlas.calls) <= QUICK_RETRIEVAL_BUDGET.max_atlas_queries, (
        zero_atlas.calls
    )
    assert result.iterations < 40, result.iterations
    # 3) Map tool awalnya ditawarkan, lalu DILEPAS setelah bound -> LLM berhenti
    #    mencari map dan menyusun jawaban final.
    assert "atlas_query" in provider.tools_history[0], provider.tools_history[0]
    assert "atlas_query" not in provider.tools_history[-1], provider.tools_history[-1]


def test_f2_consultant_service_provider_proxy_delegates_hooks() -> None:
    """Proxy provider Consultant TIDAK menyembunyikan hook provider asli.

    `knowledge_budget_tokens` (parity Ollama `num_ctx`) dan `is_available`
    harus tetap didelegasikan saat provider dibungkus `ConsultantBoundProvider`.
    """
    from agent_ai.consultant.guard import ConsultantBoundProvider

    class _HookProvider(BaseProvider):
        name = "hook"

        def knowledge_budget_tokens(self):  # noqa: D102 - hook provider
            return 12288

        def is_available(self) -> bool:  # noqa: D102
            return False

        def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):  # noqa: D102
            return GenerateResult(text="", model="hook", provider=self.name)

    guard = ConsultantRetrievalGuard(mode=MODE_QUICK)
    proxy = ConsultantBoundProvider(_HookProvider(), guard)
    assert proxy.knowledge_budget_tokens() == 12288
    assert proxy.is_available() is False


# --------------------------------------------------------------------------- #
# G. INVESTIGATION tetap mampu investigasi source-level (tidak over-limited)
# --------------------------------------------------------------------------- #
def test_g_investigation_still_allows_source_level_work(monkeypatch) -> None:
    """INVESTIGATION: beberapa search_code/read_file + map query > batas QUICK.

    Membuktikan perbaikan runaway TIDAK membuat Investigate terlalu agresif
    berhenti: tool source-level (search_code/read_file) tetap bisa dipakai
    beberapa kali, dan bound map-nya lebih longgar daripada QUICK.
    """
    import agent_ai.tools.filesystem as fs_mod
    import agent_ai.tools.project_map as pm_mod

    class _FakeSourceTool(BaseTool):
        def __init__(self, name: str, result: Dict[str, Any]) -> None:
            self.name = name
            self.description = name
            self.input_schema = {"type": "object", "properties": {}}
            self.calls = 0
            self._result = result

        def execute(self, **arguments: Any) -> Dict[str, Any]:
            self.calls += 1
            return dict(self._result)

    search = _FakeSourceTool("search_code", {"matches": [{"file": "x.py"}], "total": 1})
    read = _FakeSourceTool("read_file", {"path": "x.py", "total": 1})
    listf = _FakeSourceTool("list_files", {"entries": [], "total": 1})
    atlas = FakeMapTool("atlas_query")  # hasil non-zero (bukan zero-result streak)
    rig = FakeMapTool("rig_query")


    monkeypatch.setattr(
        pm_mod,
        "build_project_map_tools",
        lambda root=None, include_refresh=False: [
            atlas,
            rig,
            FakeMapTool("project_map_status"),
        ],
    )
    monkeypatch.setattr(
        fs_mod, "SearchCodeTool", lambda root=None, read_cache=None: search
    )
    monkeypatch.setattr(
        fs_mod, "ReadFileTool", lambda root=None, read_cache=None: read
    )
    monkeypatch.setattr(fs_mod, "ListFilesTool", lambda root=None: listf)

    guard = ConsultantRetrievalGuard(mode=MODE_INVESTIGATE)
    registry = build_consultant_registry(
        root=None, mode=MODE_INVESTIGATE, guard=guard
    )

    def _call(name: str, args: Dict[str, Any]) -> LLMResponse:
        return LLMResponse(
            actions=[LLMAction(name=name, arguments=args)],
            finish_reason=FinishReason.TOOL_CALLS,
        )

    # 3 search_code + 2 read_file + 4 atlas_query (melebihi bound QUICK=3).
    script = (
        [_call("search_code", {"query": f"q{i}"}) for i in range(3)]
        + [_call("read_file", {"path": "x.py"}) for _ in range(2)]
        + [_call("atlas_query", {"query": f"Sym{i}"}) for i in range(4)]
        + [_final("Jawaban final hasil investigasi source-level.")]
    )
    provider = ScriptedProvider(script)
    orchestrator = AgentOrchestrator(
        provider=ConsultantBoundProvider(provider, guard),
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="fake"),
        system_prompt="Consultant",
        use_continuous_loop=True,
    )
    result = orchestrator.run_continuous_loop(
        "Di mana fungsi X diimplementasikan dan apa yang dilakukannya?", max_steps=40
    )

    assert result.status == AgentStatus.DONE, result.error
    assert result.result and "investigasi" in result.result, result.result
    # Source-level tools benar-benar dipakai berkali-kali (bukan 1-2 call saja).
    assert search.calls == 3, search.calls
    assert read.calls == 2, read.calls
    # Investigate boleh query map melebihi bound QUICK, tetapi tetap dibatasi.
    assert len(atlas.calls) == 4, atlas.calls
    assert len(atlas.calls) > QUICK_RETRIEVAL_BUDGET.max_atlas_queries
    assert len(atlas.calls) <= INVESTIGATION_RETRIEVAL_BUDGET.max_atlas_queries
    assert result.iterations < 40, result.iterations


# --------------------------------------------------------------------------- #
# H. Batch PARALEL (READ) tetap menghormati bound (reserve atomik + dedup)
# --------------------------------------------------------------------------- #
def test_h_parallel_reserve_is_atomic_and_deduped() -> None:
    """Dua query identik yang di-reserve bersamaan tidak dieksekusi dua kali.

    Tool Execution Coordinator boleh menjalankan tool READ (atlas_query/rig_query)
    PARALEL dalam satu batch. Tanpa reservasi atomik, dua query identik bisa lolos
    bersamaan (dedup gagal). Guard harus menandai query saat `reserve`.
    """
    guard = ConsultantRetrievalGuard(mode=MODE_QUICK)

    first = guard.reserve("atlas_query", {"query": "safeguard"})
    second = guard.reserve("atlas_query", {"query": "  SAFEGUARD "})
    assert first is None, first
    assert second is not None and second["reason"] == "repeated_query", second
    assert second["executed"] is False, second


# --------------------------------------------------------------------------- #
# Provider proxy: passthrough + hanya melepas tool map (non-map tetap utuh)
# --------------------------------------------------------------------------- #
def test_provider_proxy_passthrough_and_delegation() -> None:
    class DummyProvider(BaseProvider):
        name = "dummy"

        def __init__(self) -> None:
            self.config = SimpleNamespace(model="dummy-model")
            self.seen: List[Any] = []

        def generate(
            self,
            prompt: Optional[str] = None,
            messages: Optional[Any] = None,
            options: Optional[GenerateOptions] = None,
            tools: Optional[Any] = None,
            tool_choice: Optional[Any] = None,
        ) -> GenerateResult:
            self.seen.append(tools)
            return GenerateResult(text="ok", model="dummy-model", provider=self.name)

    guard = ConsultantRetrievalGuard(mode=MODE_QUICK)
    inner = DummyProvider()
    proxy = ConsultantBoundProvider(inner, guard)

    # Delegasi atribut transparan (config dipakai logging orchestrator).
    assert proxy.name == "dummy"
    assert proxy.config.model == "dummy-model"

    tools = [
        ToolDefinition(name="atlas_query"),
        ToolDefinition(name="rig_query"),
        ToolDefinition(name="read_file"),
        ToolDefinition(name="search_code"),
    ]
    # Belum stopped -> SEMUA tool diteruskan apa adanya (non-map tidak dibatasi).
    proxy.generate(tools=tools, tool_choice=None)
    assert [t.name for t in inner.seen[-1]] == [
        "atlas_query",
        "rig_query",
        "read_file",
        "search_code",
    ]

    # Dorong bound: 3 query atlas + 1 kelebihan.
    for i in range(QUICK_RETRIEVAL_BUDGET.max_atlas_queries):
        assert guard.reserve("atlas_query", {"query": f"q{i}"}) is None
        guard.record("atlas_query", {"query": f"q{i}"}, {"total": 1})
    assert guard.reserve("atlas_query", {"query": "overflow"}) is not None
    assert guard.stopped is True

    # Setelah stopped -> hanya tool map pencarian yang dilepas; non-map tetap ada.
    proxy.generate(tools=tools, tool_choice=None)
    assert [t.name for t in inner.seen[-1]] == ["read_file", "search_code"]


# --------------------------------------------------------------------------- #
# D. INVESTIGATION bound lebih tinggi daripada QUICK
# --------------------------------------------------------------------------- #
def test_d_investigation_bound_higher_than_quick() -> None:
    quick = retrieval_budget_for_mode(MODE_QUICK)
    inv = retrieval_budget_for_mode(MODE_INVESTIGATE)
    assert inv.max_atlas_queries > quick.max_atlas_queries
    assert inv.max_rig_queries > quick.max_rig_queries
    assert quick == QUICK_RETRIEVAL_BUDGET
    assert inv == INVESTIGATION_RETRIEVAL_BUDGET

    # Behavior: investigation mengizinkan lebih banyak query sebelum dibatasi.
    g_inv = ConsultantRetrievalGuard(mode=MODE_INVESTIGATE)
    for i in range(INVESTIGATION_RETRIEVAL_BUDGET.max_atlas_queries):
        assert g_inv.reserve("atlas_query", {"query": f"q{i}"}) is None
        g_inv.record("atlas_query", {"query": f"q{i}"}, {"total": 1})
    assert g_inv.reserve("atlas_query", {"query": "beyond"}) is not None


# --------------------------------------------------------------------------- #
# E. Agent TIDAK berubah
# --------------------------------------------------------------------------- #
def test_e_agent_budget_and_behavior_unchanged() -> None:
    # Safeguard generik continuous loop + safeguard Consultant tetap utuh.
    assert _CONTINUOUS_SAFETY_MAX_STEPS == 1000
    assert ConsultantService().max_steps == 40

    # Registry Agent TIDAK menerapkan bound Consultant (tool map tidak dibungkus).
    from agent_ai.tools.registry import build_registry

    agent_registry = build_registry(root=None)
    assert agent_registry.has("refresh_project_map"), "Agent harus tetap punya refresh"
    assert agent_registry.has("atlas_query")
    atlas = agent_registry.get("atlas_query")
    assert type(atlas).__name__ == "AtlasQueryTool", type(atlas).__name__
    assert not isinstance(atlas, ConsultantBoundedMapTool)


def test_e2_agent_loop_has_no_consultant_bound() -> None:
    """Agent (tanpa guard) boleh memanggil atlas_query > bound Consultant."""
    fake = FakeMapTool("atlas_query")
    registry = ToolRegistry()
    registry.register(fake)

    # 5 query berbeda (melebihi bound QUICK=3) lalu final.
    script = [_atlas_call(f"agent-q{i}") for i in range(5)] + [_final("Agent selesai.")]
    provider = ScriptedProvider(script)
    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="fake"),
        use_continuous_loop=True,
    )
    result = orchestrator.run_continuous_loop("agent task", max_steps=_CONTINUOUS_SAFETY_MAX_STEPS)

    assert result.status == AgentStatus.DONE, result.error
    # SEMUA 5 query dieksekusi: tidak ada bound Consultant pada jalur Agent.
    assert len(fake.calls) == 5, fake.calls
    assert result.result == "Agent selesai.", result.result


# --------------------------------------------------------------------------- #
# Integrasi ConsultantService (wiring guard + provider proxy) tanpa API eksternal
# --------------------------------------------------------------------------- #
def test_consultant_service_wires_bound(monkeypatch) -> None:
    """`ConsultantService.consult` benar-benar memakai guard (bound berlaku)."""
    import agent_ai.tools.project_map as project_map_mod

    fake_atlas = FakeMapTool("atlas_query")

    def _fake_build_project_map_tools(root=None, include_refresh=False):
        return [
            fake_atlas,
            FakeMapTool("rig_query"),
            FakeMapTool("project_map_status"),
        ]

    monkeypatch.setattr(
        project_map_mod, "build_project_map_tools", _fake_build_project_map_tools
    )

    service = ConsultantService()
    provider = MapLoopProvider()
    result = service.consult(
        "kenapa Consultant berhenti setelah max_steps=40?",
        provider=provider,
        root=None,
        mode="quick",
    )

    assert result.status == "done", (result.status, result.error)
    assert "Jawaban final" in result.reply, result.reply
    # Bound dihormati end-to-end lewat ConsultantService.
    assert len(fake_atlas.calls) == 3, fake_atlas.calls
    assert result.iterations < 40, result.iterations
    assert result.mode == "quick"


# --------------------------------------------------------------------------- #
# Runner manual (opsional)
# --------------------------------------------------------------------------- #
def main() -> int:
    print("=== Verifikasi bound retrieval Consultant (safety/control layer) ===")
    tests = [
        ("A. QUICK 3 query boleh / ke-4 dibatasi", test_a_quick_three_queries_allowed_fourth_blocked),
        ("A2. final answer setelah bound", test_a2_consultant_produces_final_answer_after_bound),
        ("B. repeated query normalized diblokir", test_b_repeated_query_normalized_is_blocked),
        ("C. zero-result streak menghentikan map search", test_c_zero_result_streak_stops_map_search),
        ("C2. zero-result e2e bukan 40 tool calls", test_c2_zero_result_end_to_end_not_40_tool_calls),
        ("F2. proxy provider mendelegasikan hook (num_ctx/avail)", test_f2_consultant_service_provider_proxy_delegates_hooks),
        ("Proxy provider passthrough + non-map utuh", test_provider_proxy_passthrough_and_delegation),
        ("D. investigation bound > quick", test_d_investigation_bound_higher_than_quick),
        ("E. Agent budget/behavior tidak berubah", test_e_agent_budget_and_behavior_unchanged),
        ("E2. Agent loop tanpa bound Consultant", test_e2_agent_loop_has_no_consultant_bound),
        ("H. batch paralel reserve atomik + dedup", test_h_parallel_reserve_is_atomic_and_deduped),
    ]
    for label, fn in tests:
        fn()
        print(f"[OK] {label}")

    # Wiring ConsultantService (butuh monkeypatch -> manual di runner).
    from pytest import MonkeyPatch

    mp = MonkeyPatch()
    try:
        test_consultant_service_wires_bound(mp)
    finally:
        mp.undo()
    print("[OK] ConsultantService memakai bound end-to-end")

    # Regresi kasus asli (butuh monkeypatch -> manual di runner).
    mp2 = MonkeyPatch()
    try:
        test_f_original_case_not_runaway_and_final_answer(mp2)
    finally:
        mp2.undo()
    print("[OK] F. kasus asli (zero-result sinonim) tidak runaway -> final answer")

    # Investigation source-level (butuh monkeypatch -> manual di runner).
    mp3 = MonkeyPatch()
    try:
        test_g_investigation_still_allows_source_level_work(mp3)
    finally:
        mp3.undo()
    print("[OK] G. INVESTIGATION tetap bisa investigasi source-level")

    print()
    print("[OK] Bound retrieval Consultant bekerja tanpa mengubah Agent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
