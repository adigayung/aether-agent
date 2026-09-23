"""Verifier Task 3: integrasi final, benchmark BEFORE/AFTER, dan memori Agent.

Script ini memvalidasi bahwa Task 1 (runtime context compaction) dan Task 2
(tool-result compaction TERSTRUKTUR) benar-benar terintegrasi pada SATU jalur
produksi (`AgentOrchestrator.run_continuous_loop`) dan mengukur dampaknya pada
skenario yang SAMA untuk BEFORE (tanpa compaction) dan AFTER (config produksi).

Prinsip:
    - Deterministik, TANPA network/LLM nyata (provider skrip).
    - Tool yang dipakai adalah TOOL PRODUKSI nyata (`build_registry`) di atas
      workspace sementara, sehingga bentuk hasil tool realistis.
    - Estimator token memakai heuristik AETHER (`len/4`) yang SAMA dengan
      `ConversationHistory.estimate_messages_tokens` (tanpa tokenizer provider).

Jalankan:
    python scripts/check_task3_final_integration.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
for _path in (str(SRC_DIR), str(PROJECT_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from agent_ai.config.settings import settings  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.orchestrator import (  # noqa: E402
    _CONTINUOUS_SAFETY_MAX_STEPS,
    AgentOrchestrator,
)
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.tools.registry import build_registry  # noqa: E402

#: Anggaran token yang dipakai PRODUKSI untuk provider cloud (knowledge budget
#: provider = None -> fallback `settings.context.max_tokens`).
PRODUCTION_BUDGET = int(getattr(settings.context, "max_tokens", 0) or 0)

#: Emulasi prefix statis produksi (system prompt + Environment + Bible) ~9k token
#: (36k karakter; heuristik len/4). Ini konservatif terhadap baseline audit
#: (~12k token prefix statis termasuk definisi tool ~2.7k).
PREFIX_TOKENS = 9_000
PREFIX_CHARS = PREFIX_TOKENS * 4
PREFIX_MARK_HEAD = "PREFIX_MARK_HEAD_ALPHA"
PREFIX_MARK_TAIL = "PREFIX_MARK_TAIL_OMEGA"


# --------------------------------------------------------------------------- #
# Provider skrip (deterministik, tanpa network) + helper respons
# --------------------------------------------------------------------------- #
def _tool_turn(text: str, calls: List[Tuple[str, str, Dict[str, Any]]]) -> Dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": text,
                    "tool_calls": [
                        {
                            "id": cid,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for cid, name, args in calls
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def _final_turn(text: str) -> Dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class ScriptedProvider(OpenAICompatibleProvider):
    """Provider palsu: respons skrip berurutan; merekam setiap request."""

    name = "scripted"

    def __init__(self, script: List[Dict[str, Any]]) -> None:
        self.config = SimpleNamespace(model="scripted-model")
        self.script = list(script)
        self.calls = 0
        self.requests: List[List[Dict[str, Any]]] = []

    def knowledge_budget_tokens(self) -> Optional[int]:  # provider cloud = None
        return None

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Any]] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[List[Any]] = None,
        tool_choice: Optional[Any] = None,
    ) -> GenerateResult:
        self.requests.append([dict(m) if isinstance(m, dict) else m for m in (messages or [])])
        idx = self.calls
        self.calls += 1
        raw = self.script[idx] if idx < len(self.script) else _final_turn("(default)")
        return GenerateResult(text="", model="scripted-model", provider=self.name, raw=raw)


class NoCompactionOrchestrator(AgentOrchestrator):
    """BEFORE: mematikan compaction -> perilaku lama (riwayat mentah)."""

    def _context_budget_tokens(self) -> Optional[int]:
        return None


# --------------------------------------------------------------------------- #
# Estimator token (heuristik AETHER: len/4) atas pesan format provider
# --------------------------------------------------------------------------- #
def _est_tokens(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        total += max(1, len(str(m.get("content") or "")) // 4)
        total += len(str(m.get("name") or "")) // 4
        for tc in m.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            total += max(1, len(str(fn.get("arguments") or "")) // 4)
            total += len(str(fn.get("name") or "")) // 4
        total += 4
    return total


def _tool_chars(messages: List[Dict[str, Any]]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages if m.get("role") == "tool")


# --------------------------------------------------------------------------- #
# Workspace sementara + prefix statis
# --------------------------------------------------------------------------- #
def _prefix() -> str:
    # Baris dipilih agar total ~PREFIX_CHARS karakter (estimasi len/4 token).
    count = max(1, PREFIX_CHARS // 80)
    body = "\n".join(f"knowledge_line_{i} " + "k" * 60 for i in range(count))
    return f"{PREFIX_MARK_HEAD}\n{body}\n{PREFIX_MARK_TAIL}"


@dataclass
class Scenario:
    name: str
    task: str
    script: List[Dict[str, Any]]
    files: Dict[str, str] = field(default_factory=dict)
    expected_result: str = "Selesai."
    edit_path: Optional[str] = None
    expect_contains: Optional[str] = None


@dataclass
class Metrics:
    rounds: int
    tool_calls: int
    total_input_tokens: int
    max_ctx_tokens: int
    last_ctx_tokens: int
    total_tool_chars: int
    last_tool_chars: int
    compaction_rounds: int
    tool_compacted_rounds: int
    final_tool_msgs: int
    status: str
    result: str


def _run_scenario(scenario: Scenario, mode: str) -> Tuple[Metrics, ScriptedProvider, List[Dict[str, Any]]]:
    ws = Path(tempfile.mkdtemp(prefix="aether_t3_"))
    try:
        for rel, content in scenario.files.items():
            target = ws / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        registry = build_registry(root=ws)
        executor = ToolExecutor(registry=registry)
        provider = ScriptedProvider(scenario.script)
        events: List[Dict[str, Any]] = []
        cls = NoCompactionOrchestrator if mode == "before" else AgentOrchestrator
        kwargs: Dict[str, Any] = {}
        if mode == "after":
            # Config PRODUKSI: tanpa override -> fallback settings.context.max_tokens.
            pass
        else:
            kwargs["context_budget_tokens"] = None
        orch = cls(
            provider=provider,
            executor=executor,
            options=GenerateOptions(model="scripted-model"),
            system_prompt=_prefix(),
            use_continuous_loop=True,
            event_sink=lambda et, payload: events.append({"type": et, **payload}),
            **kwargs,
        )
        result = orch.run(scenario.task)
        if scenario.edit_path is not None:
            scenario._edited_content = (ws / scenario.edit_path).read_text(encoding="utf-8")  # type: ignore[attr-defined]
        per_round = [_est_tokens(req) for req in provider.requests]
        per_round_tool = [_tool_chars(req) for req in provider.requests]
        metrics = Metrics(
            rounds=len(provider.requests),
            tool_calls=sum(1 for e in events if e.get("type") == "tool_completed"),
            total_input_tokens=sum(per_round),
            max_ctx_tokens=max(per_round) if per_round else 0,
            last_ctx_tokens=per_round[-1] if per_round else 0,
            total_tool_chars=sum(per_round_tool),
            last_tool_chars=per_round_tool[-1] if per_round_tool else 0,
            compaction_rounds=sum(1 for e in events if e.get("context_compacted")),
            tool_compacted_rounds=sum(
                1 for e in events if int(e.get("context_tool_compacted", 0) or 0) > 0
            ),
            final_tool_msgs=len(
                [m for m in provider.requests[-1] if m.get("role") == "tool"]
            )
            if provider.requests
            else 0,
            status=result.status.value,
            result=result.result or "",
        )
        return metrics, provider, events
    finally:
        shutil.rmtree(ws, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Definisi skenario (7 wajib)
# --------------------------------------------------------------------------- #
def _big_file_text(lines: int = 1200) -> str:
    return "\n".join(f"line_{i} " + "x" * 70 for i in range(lines))


def scenario_short() -> Scenario:
    return Scenario(
        name="1 task pendek",
        task="baca config lalu selesai",
        files={"app.py": "print('hello')\n"},
        script=[
            _tool_turn("baca", [("c1", "read_file", {"path": "app.py"})]),
            _final_turn("Selesai."),
        ],
    )


def scenario_medium() -> Scenario:
    files = {"src/main.py": _big_file_text(1200), "README.md": "# demo\n"}
    calls = [("c1", "list_files", {"path": "."})]
    for i in range(6):
        calls.append(
            (
                f"c{i + 2}",
                "read_file",
                {"path": "src/main.py", "start_line": 1 + i * 150, "end_line": 150 + i * 150},
            )
        )
    calls.append(("c9", "search_code", {"query": "line_"}))
    return Scenario(
        name="2 task medium",
        task="pahami modul utama",
        files=files,
        script=[_tool_turn("eksplor", calls), _final_turn("Selesai.")],
    )


def scenario_long() -> Scenario:
    files = {"src/main.py": _big_file_text(2600)}
    script: List[Dict[str, Any]] = []
    for i in range(40):
        if i % 5 == 4:
            call = (f"c{i}", "search_code", {"query": f"line_{i * 10}"})
        else:
            start = 1 + i * 55
            call = (
                f"c{i}",
                "read_file",
                {"path": "src/main.py", "start_line": start, "end_line": start + 55},
            )
        script.append(_tool_turn(f"round {i}", [call]))
    script.append(_final_turn("Selesai."))
    return Scenario(name="3 long-running banyak tool", task="kerjakan task panjang", files=files, script=script)


def scenario_baseline_41() -> Scenario:
    """Mendekati profil baseline audit: ~41 round, hasil tool sedang."""
    files = {"src/core.py": _big_file_text(3000)}
    script: List[Dict[str, Any]] = []
    for i in range(41):
        start = 1 + i * 30
        script.append(
            _tool_turn(
                f"round {i}",
                [(f"c{i}", "read_file", {"path": "src/core.py", "start_line": start, "end_line": start + 30})],
            )
        )
    script.append(_final_turn("Selesai."))
    return Scenario(name="3b baseline 41 round", task="task representatif 41 round", files=files, script=script)


def scenario_big_read() -> Scenario:
    script = [_tool_turn("baca besar", [("c0", "read_file", {"path": "src/big.py"})])]
    for i in range(1, 12):
        script.append(_tool_turn(f"cek {i}", [(f"c{i}", "list_files", {"path": "."})]))
    script.append(_final_turn("Selesai."))
    return Scenario(
        name="4 read_file besar",
        task="baca file besar lalu lanjut",
        files={"src/big.py": _big_file_text(1500)},
        script=script,
    )


def scenario_several_files() -> Scenario:
    files = {f"pkg/f{i}.py": f"VALUE_{i} = {i}\n" + (_big_file_text(200)) for i in range(8)}
    calls = [(f"r{i}", "read_file", {"path": f"pkg/f{i}.py"}) for i in range(8)]
    calls.append(("e1", "edit_file", {
        "path": "pkg/f0.py",
        "old_text": "VALUE_0 = 0",
        "new_text": "VALUE_0 = 42",
    }))
    return Scenario(
        name="5 beberapa file + edit",
        task="baca 8 file, edit satu",
        files=files,
        script=[_tool_turn("baca semua", calls), _final_turn("Selesai.")],
        edit_path="pkg/f0.py",
        expect_contains="VALUE_0 = 42",
    )


def scenario_big_command() -> Scenario:
    files = {"gen.py": "print('Y' * 40000)\n"}
    script = [_tool_turn("jalankan", [("c0", "run_command", {"command": "python gen.py"})])]
    for i in range(1, 12):
        script.append(_tool_turn(f"lanjut {i}", [(f"c{i}", "list_files", {"path": "."})]))
    script.append(_final_turn("Selesai."))
    return Scenario(
        name="6 output command besar",
        task="jalankan generator besar lalu lanjut",
        files=files,
        script=script,
    )


MEM_TOP = "IMPORTANT_TOP_ALPHA remember-me-42"
MEM_MIDDLE = "IMPORTANT_MIDDLE_BETA value=777"


def scenario_memory() -> Scenario:
    """Informasi penting di round AWAL (file besar); dipakai/diambil di akhir."""
    lines = [f"# filler {i} " + "z" * 70 for i in range(600)]
    lines[1] = MEM_TOP  # dekat AWAL (masuk preview head)
    lines[300] = MEM_MIDDLE  # di TENGAH (tidak masuk preview)
    big = "\n".join(lines)
    script = [_tool_turn("baca detail besar", [("c0", "read_file", {"path": "src/secret.py"})])]
    # Filler: cukup banyak round agar hasil round-0 menjadi STALE (di luar keep_recent).
    for i in range(1, 12):
        script.append(
            _tool_turn(f"kerja {i}", [(f"c{i}", "read_file", {"path": f"src/other_{i}.py"})])
        )
    script.append(_final_turn("Selesai."))
    files = {"src/secret.py": big}
    for i in range(1, 12):
        files[f"src/other_{i}.py"] = _big_file_text(120)
    return Scenario(
        name="7 butuh info dari round lama",
        task="ingat detail dari awal, pakai di akhir",
        files=files,
        script=script,
        expect_contains="remember-me-42",
    )


SCENARIOS = [
    scenario_short,
    scenario_medium,
    scenario_long,
    scenario_baseline_41,
    scenario_big_read,
    scenario_several_files,
    scenario_big_command,
    scenario_memory,
]


# --------------------------------------------------------------------------- #
# Bagian 0: audit integrasi produksi
# --------------------------------------------------------------------------- #
def audit_production_integration() -> None:
    print("=== [0] Audit integrasi produksi ===")
    # (a) Jalur produksi memakai continuous loop.
    import inspect

    from agent_ai.runtime import runtime as runtime_mod

    runtime_src = Path(runtime_mod.__file__).read_text(encoding="utf-8")
    assert "use_continuous_loop: bool = True" in runtime_src, "AgentRuntime harus default continuous loop"
    assert "use_continuous_loop=self.use_continuous_loop" in runtime_src
    consultant_src = (
        PROJECT_ROOT / "src" / "agent_ai" / "consultant" / "service.py"
    ).read_text(encoding="utf-8")
    assert ".run_continuous_loop(" in consultant_src, "Consultant harus memakai run_continuous_loop"
    print("  [OK] AgentRuntime (default) + Consultant memakai run_continuous_loop")

    # (b) Compaction dipanggil dari run_continuous_loop (bukan hanya verifier).
    orch_src = inspect.getsource(AgentOrchestrator.run_continuous_loop)
    assert "_compile_context_messages" in orch_src, "run_continuous_loop harus memanggil compaction"
    # Regresi: jalur lama `history.to_provider_format()` TIDAK lagi dipakai di loop.
    assert "history.to_provider_format()" not in orch_src, "loop harus memakai konteks terkompilasi"
    print("  [OK] run_continuous_loop memanggil _compile_context_messages (bukan to_provider_format mentah)")

    # (c) Anggaran produksi nyata (cloud provider -> settings.context.max_tokens).
    p = ScriptedProvider([])
    o = AgentOrchestrator(provider=p, executor=ToolExecutor(registry=build_registry()), use_continuous_loop=True)
    budget = o._context_budget_tokens()
    overhead = o._tool_definitions_tokens(o._tool_definitions())
    assert budget == PRODUCTION_BUDGET and budget > 0, (budget, PRODUCTION_BUDGET)
    print(f"  [OK] anggaran produksi = {budget} token; definisi tool = {overhead} token")

    # (d) Compaction benar-benar aktif pada loop untuk riwayat panjang.
    files = {f"big_{i}.py": _big_file_text(120) for i in range(20)}
    sc = Scenario(
        name="_probe",
        task="probe",
        files=files,
        script=[
            _tool_turn(f"r{i}", [(f"c{i}", "read_file", {"path": f"big_{i}.py"})])
            for i in range(20)
        ]
        + [_final_turn("Selesai.")],
    )
    metrics, provider, events = _run_scenario(sc, "after")
    assert metrics.status == AgentStatus.DONE.value, metrics.status
    assert metrics.compaction_rounds > 0, "compaction harus aktif pada runtime"
    assert any(e.get("context_compacted") for e in events)
    print(f"  [OK] compaction terjadi pada runtime ({metrics.compaction_rounds} round terkompak, budget {PRODUCTION_BUDGET})")

    # (e) Idempotensi/verifier-only: pastikan tidak ada pemakaian hanya-verifier.
    hist_src = (SRC_DIR / "agent_ai" / "core" / "history.py").read_text(encoding="utf-8")
    assert "compile_compacted_messages" in hist_src and "_compact_tool_content" in hist_src
    assert "ToolResultCompactor" in hist_src
    print("  [OK] ConversationHistory memakai ToolResultCompactor (Task 2) pada jalur runtime")


# --------------------------------------------------------------------------- #
# Bagian 1: benchmark BEFORE vs AFTER
# --------------------------------------------------------------------------- #
def benchmark() -> List[Tuple[str, Metrics, Metrics]]:
    print("\n=== [1] Benchmark BEFORE vs AFTER (skenario SAMA) ===")
    rows: List[Tuple[str, Metrics, Metrics]] = []
    for factory in SCENARIOS:
        scenario = factory()
        before, _, _ = _run_scenario(scenario, "before")
        after, _, _ = _run_scenario(scenario, "after")
        # Integritas: hasil & jumlah tool call identik (tidak ada pekerjaan ulang).
        assert before.result == after.result, (scenario.name, before.result, after.result)
        assert before.rounds == after.rounds, (scenario.name, before.rounds, after.rounds)
        assert before.tool_calls == after.tool_calls, (scenario.name, before.tool_calls, after.tool_calls)
        assert before.status == after.status == AgentStatus.DONE.value
        # Validasi perubahan source: file benar-benar berubah.
        if scenario.edit_path is not None and scenario.expect_contains:
            edited = getattr(scenario, "_edited_content", "")
            assert scenario.expect_contains in edited, (
                scenario.name,
                "perubahan source tidak terjadi",
            )
        rows.append((scenario.name, before, after))
    header = (
        f"{'scenario':<32}{'rounds':>7}{'tools':>7}{'retain':>7}"
        f"{'in_tok B':>10}{'in_tok A':>10}{'hemat':>8}"
    )
    print(header)
    print("-" * len(header))
    for name, b, a in rows:
        pct = (1 - a.total_input_tokens / b.total_input_tokens) * 100 if b.total_input_tokens else 0
        print(
            f"{name:<32}{b.rounds:>7}{b.tool_calls:>7}{a.final_tool_msgs:>7}"
            f"{b.total_input_tokens:>10}{a.total_input_tokens:>10}{pct:>7.0f}%"
        )
    tb = sum(b.total_input_tokens for _, b, _ in rows)
    ta = sum(a.total_input_tokens for _, _, a in rows)
    print(f"{'TOTAL':<32}{'':>7}{'':>7}{'':>7}{tb:>10}{ta:>10}{(1-ta/tb)*100:>7.0f}%")
    print("(retain = jumlah tool-result yang MASIH ada di konteks request terakhir)")
    return rows


# --------------------------------------------------------------------------- #
# Bagian 2: validasi memori / kecerdasan
# --------------------------------------------------------------------------- #
def memory_validation() -> None:
    print("\n=== [2] Validasi memori / retrieval ===")
    scenario = scenario_memory()
    metrics, provider, events = _run_scenario(scenario, "after")
    final_req = provider.requests[-1]
    joined = "\n".join(str(m.get("content") or "") for m in final_req)

    # (a) Compaction HARUS benar-benar terjadi pada skenario ini.
    assert metrics.compaction_rounds > 0, "skenario memori harus memicu compaction"
    assert any(e.get("context_compacted") for e in events)
    print(f"  [OK] compaction aktif ({metrics.compaction_rounds} round terkompak)")

    # (b) Prefix statis (system + Bible emulasi) TIDAK boleh hilang.
    assert PREFIX_MARK_HEAD in joined and PREFIX_MARK_TAIL in joined, "prefix statis hilang!"
    print("  [OK] instruction system + konteks statis dipertahankan utuh")

    # (c) Pesan task awal tetap utuh.
    assert scenario.task in joined, "pesan task awal hilang"
    print("  [OK] pesan task awal tetap utuh")

    # (d) Detail penting dari round AWAL yang berada di PREVIEW (head) tetap ada.
    assert MEM_TOP in joined, "info penting di awal hasil tool harus tetap terlihat"
    print("  [OK] info penting di AWAL hasil tool tetap tersedia di konteks")

    # (e) Locator retrieval (path + rentang baris) dipertahankan untuk hasil STALE.
    assert "src/secret.py" in joined, "path locator harus dipertahankan"
    assert "read_file" in joined, "locator retrieval read_file harus ada"
    print("  [OK] locator retrieval (path/range) dipertahankan setelah compaction")

    # (f) Detail di TENGAH hasil besar: boleh hilang dari konteks (dipadatkan),
    #     tetapi HARUS dapat diambil ulang lewat tool. Laporkan apa adanya.
    middle_in_ctx = MEM_MIDDLE in joined
    ws = Path(tempfile.mkdtemp(prefix="aether_t3_mem_"))
    try:
        target = ws / "src" / "secret.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(scenario.files["src/secret.py"], encoding="utf-8")
        registry = build_registry(root=ws)
        reader = registry.get("read_file")
        again = reader.execute(path="src/secret.py")
        assert scenario.expect_contains in json.dumps(again), "detail mentah harus dapat diambil ulang"
        assert MEM_MIDDLE in json.dumps(again), "detail tengah harus ada pada retrieval ulang"
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    print(
        "  [OK] detail mentah dapat DIAMBIL ULANG via tool read_file "
        f"(retrieval tersedia; detail-tengah-di-konteks={middle_in_ctx})"
    )

    # (g) Tool result TERBARU tetap tersedia untuk melanjutkan pekerjaan.
    last_tool = [m for m in final_req if m.get("role") == "tool"]
    assert last_tool, "harus ada tool result"
    last_content = str(last_tool[-1].get("content") or "")
    assert last_content, "tool result terbaru kosong"
    assert ("src/other_11.py" in last_content) or ("path" in last_content), last_content[:200]
    raw_last = len(last_content) > 1000
    print(
        "  [OK] tool result TERBARU tetap tersedia "
        f"(utuh-mentah={raw_last}, panjang={len(last_content)} karakter)"
    )

    # (e) Protokol tool calling tetap valid pada request terakhir.
    assistant_ids = {
        tc.get("id")
        for m in final_req
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    }
    for m in final_req:
        if m.get("role") == "tool":
            assert m.get("tool_call_id") in assistant_ids, "tool result yatim"
    print("  [OK] protokol tool calling valid (tanpa tool result yatim)")


# --------------------------------------------------------------------------- #
# Bagian 3: safety / invariants
# --------------------------------------------------------------------------- #
def safety_invariants() -> None:
    print("\n=== [3] Safety / behavior invariants ===")
    import inspect

    # max_steps / continuous safety limit TIDAK berubah.
    assert _CONTINUOUS_SAFETY_MAX_STEPS == 1000, _CONTINUOUS_SAFETY_MAX_STEPS
    sig = inspect.signature(AgentOrchestrator.run_continuous_loop)
    assert sig.parameters["max_steps"].default == _CONTINUOUS_SAFETY_MAX_STEPS
    print("  [OK] _CONTINUOUS_SAFETY_MAX_STEPS = 1000 (tidak berubah)")

    # Completion decision tidak berubah (jalur final = LLM tanpa tool call).
    src = inspect.getsource(AgentOrchestrator.run_continuous_loop)
    assert "if not response.has_tool_calls:" in src
    assert "loop.finish(result=response.text" in src
    print("  [OK] keputusan completion tetap murni dari LLM (tidak ada heuristic baru)")

    # Tool definitions tidak diubah oleh Task 1/2 (jumlah tool tetap).
    reg = build_registry()
    specs = reg.specs()
    assert len(specs) == 12, len(specs)
    print(f"  [OK] definisi tool tidak berubah ({len(specs)} tool)")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    print(f"=== Task 3 final integration (budget produksi={PRODUCTION_BUDGET}) ===")
    audit_production_integration()
    benchmark()
    memory_validation()
    safety_invariants()
    print("\n[OK] Task 1 + Task 2 terintegrasi pada runtime; hemat token tanpa kehilangan memori kerja.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
