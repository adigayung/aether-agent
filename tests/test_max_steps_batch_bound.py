"""Regression tests: BUG-2 — batas `max_steps` terlampaui dalam SATU batch.

Sebelum perbaikan, `AgentOrchestrator.run_continuous_loop` hanya memeriksa
`loop.iteration >= max_steps` di AWAL while loop, sedangkan `loop.record_action()`
dipanggil SETELAH seluruh batch tool call selesai. Akibatnya satu respons LLM
berisi banyak tool call dapat melewati batas (mis. `max_steps=1` + batch 3 tool
call -> 3 langkah tereksekusi).

Kontrak yang diuji (mock provider + tool lokal, TANPA network/efek samping):
    1. `max_steps=1`, batch beberapa tool call -> MAKSIMAL satu tool call
       dieksekusi; status FAILED (bukan DONE) dengan alasan eksplisit.
    2. Sisa anggaran = 2, batch 4 tool call -> tepat 2 yang dijalankan; tool
       berikutnya TIDAK pernah didispatch.
    3. Batch yang masih dalam batas -> berjalan seperti sebelumnya (DONE).
    4. Batch yang menyentuh batas -> tidak ada DONE palsu + alasan terminal tepat.
    5. Jalur truncation dengan tool call LENGKAP tetap tunduk pada batas.
    6. Tidak ada tool call yang dieksekusi dua kali akibat penghentian batch.
    7. Jalur "sisa anggaran sudah habis" (defense-in-depth) tidak men-dispatch
       tool call apa pun.

Jalankan:
    python -m pytest tests/test_max_steps_batch_bound.py
atau:
    python tests/test_max_steps_batch_bound.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
)
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402

SYSTEM_PROMPT = "Kamu adalah coding agent."


# --------------------------------------------------------------------------- #
# Mock provider + tool (lokal, deterministik, tanpa network)
# --------------------------------------------------------------------------- #
def _tool_call(call_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _read_turn(calls: List[Dict[str, Any]], *, truncated: bool = False) -> Dict[str, Any]:
    """Respons dengan tool call LENGKAP (opsional finish_reason=length)."""
    return {
        "choices": [
            {
                "message": {"content": "", "tool_calls": calls},
                "finish_reason": "length" if truncated else "tool_calls",
            }
        ]
    }


def _final_turn(text: str) -> Dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class ScriptedProvider(OpenAICompatibleProvider):
    """Provider palsu: tiap call mengembalikan turn skrip (item terakhir diulang)."""

    name = "maxsteps-scripted"

    def __init__(self, script: List[Any]) -> None:
        self.config = SimpleNamespace(model="maxsteps-model")
        self.script = list(script)
        self.calls = 0

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Any]] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[List[Any]] = None,
        tool_choice: Optional[Any] = None,
    ) -> GenerateResult:
        idx = self.calls
        self.calls += 1
        if not self.script:
            item = _final_turn("(default)")
        elif idx < len(self.script):
            item = self.script[idx]
        else:
            item = self.script[-1]
        return GenerateResult(
            text="", model="maxsteps-model", provider=self.name, raw=item
        )


class CountingReadTool(BaseTool):
    """Tool palsu READ-ONLY: menghitung eksekusi + mencatat urutan argumen.

    READ_ONLY + path berbeda -> semua call aman paralel, sehingga batch benar-
    benar di-dispatch sebagai satu batch oleh coordinator (kasus terburuk
    BUG-2). Tidak ada efek samping (tidak menyentuh filesystem).
    """

    def __init__(self, name: str = "read_file") -> None:
        self.name = name
        self.description = "fake read"
        self.input_schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": [],
        }
        self.calls = 0
        self.seen: List[str] = []

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        self.calls += 1
        path = str(arguments.get("path", ""))
        self.seen.append(path)
        return {"path": path, "content": f"content:{path}"}


def _batch_turn(count: int, prefix: str = "c") -> Dict[str, Any]:
    return _read_turn(
        [_tool_call(f"{prefix}{i}", "read_file", {"path": f"f{i}.txt"}) for i in range(count)]
    )


def _make_orchestrator(
    provider: ScriptedProvider,
    tool: Optional[CountingReadTool] = None,
    *,
    events: Optional[List[Dict[str, Any]]] = None,
) -> AgentOrchestrator:
    registry = ToolRegistry()
    if tool is not None:
        registry.register(tool)
    sink = None
    if events is not None:
        sink = lambda et, payload: events.append({"type": et, **payload})  # noqa: E731
    return AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="maxsteps-model"),
        system_prompt=SYSTEM_PROMPT,
        use_continuous_loop=True,
        event_sink=sink,
    )


# --------------------------------------------------------------------------- #
# 1. max_steps=1 + batch 3 tool call -> hanya SATU yang dieksekusi
# --------------------------------------------------------------------------- #
def test_max_steps_one_executes_at_most_one_tool_call() -> None:
    tool = CountingReadTool()
    events: List[Dict[str, Any]] = []
    provider = ScriptedProvider([_batch_turn(3), _final_turn("selesai")])
    orchestrator = _make_orchestrator(provider, tool, events=events)

    result = orchestrator.run_continuous_loop("task batch", max_steps=1)

    # HANYA satu tool call boleh benar-benar dieksekusi.
    assert tool.calls == 1, tool.calls
    assert tool.seen == ["f0.txt"], tool.seen
    # Status terminal: FAILED (BUKAN DONE palsu) + alasan eksplisit.
    assert result.status == AgentStatus.FAILED, (result.status, result.result)
    assert result.status != AgentStatus.DONE
    assert result.iterations == 1, result.iterations
    assert result.error is not None
    assert "max_steps=1" in result.error
    assert "TIDAK dijalankan" in result.error

    # Event safety abort: jumlah dropped + fase batch konsisten dengan fakta.
    aborts = [e for e in events if e["type"] == "loop_safety_abort"]
    assert len(aborts) == 1, aborts
    assert aborts[0]["max_steps"] == 1
    assert aborts[0]["iteration"] == 1
    assert aborts[0]["dropped_tool_calls"] == 2
    assert aborts[0]["phase"] == "tool_calls"

    # Evidence additive menandai abort batas (bukan truncation).
    assert result.evidence.get("max_steps_abort") is True
    assert result.evidence.get("max_steps") == 1
    assert result.evidence.get("max_steps_dropped_tool_calls") == 2
    assert "truncation_abort" not in result.evidence

    # Tidak ada tool call dieksekusi dua kali (tool menghitung + event per call).
    assert provider.calls == 1, provider.calls


# --------------------------------------------------------------------------- #
# 2. Sisa anggaran 2, batch 4 tool call -> tepat 2 dijalankan
# --------------------------------------------------------------------------- #
def test_remaining_budget_two_of_four_tool_calls_started() -> None:
    tool = CountingReadTool()
    events: List[Dict[str, Any]] = []
    # Turn 1: batch 1 call (menghabiskan 1 dari 3 step anggaran).
    # Turn 2: batch 4 call, sisa anggaran = 2 -> hanya 2 boleh dijalankan.
    provider = ScriptedProvider(
        [
            _read_turn([_tool_call("a0", "read_file", {"path": "a0.txt"})]),
            _batch_turn(4, prefix="b"),
            _final_turn("selesai"),
        ]
    )
    orchestrator = _make_orchestrator(provider, tool, events=events)

    result = orchestrator.run_continuous_loop("task anggaran", max_steps=3)

    # 1 (turn pertama) + 2 (maksimum sisa anggaran) = 3 = max_steps.
    assert tool.calls == 3, tool.calls
    assert tool.seen == ["a0.txt", "f0.txt", "f1.txt"], tool.seen
    assert result.iterations == 3, result.iterations

    # Tool call ke-3 dan ke-4 pada batch TIDAK PERNAH didispatch.
    called_ids = [
        e["tool_call_id"] for e in events if e["type"] == "tool_called"
    ]
    assert called_ids == ["a0", "b0", "b1"], called_ids

    assert result.status == AgentStatus.FAILED
    assert result.error is not None
    assert "max_steps=3" in result.error
    aborts = [e for e in events if e["type"] == "loop_safety_abort"]
    assert len(aborts) == 1, aborts
    assert aborts[0]["dropped_tool_calls"] == 2
    assert result.evidence.get("max_steps_dropped_tool_calls") == 2


# --------------------------------------------------------------------------- #
# 3. Batch yang masih dalam batas -> berjalan seperti sebelumnya (DONE)
# --------------------------------------------------------------------------- #
def test_batch_within_budget_unchanged() -> None:
    tool = CountingReadTool()
    provider = ScriptedProvider(
        [
            _batch_turn(3),
            _final_turn("selesai"),
        ]
    )
    orchestrator = _make_orchestrator(provider, tool)

    result = orchestrator.run_continuous_loop("task normal", max_steps=10)

    assert result.status == AgentStatus.DONE, result.error
    assert result.result == "selesai"
    assert tool.calls == 3, tool.calls
    assert result.iterations == 3, result.iterations
    assert "max_steps_abort" not in result.evidence


def test_batch_exactly_at_budget_is_allowed() -> None:
    """Batch yang PAS menyentuh anggaran dijalankan UTUH (tidak dipotong BUG-2).

    Seluruh tool call pada batch tetap dieksekusi; penghentian setelahnya
    berasal dari guard `max_steps` yang sudah ada di awal while loop (jalur
    pre-existing), BUKAN dari pemotongan batch BUG-2 — dibuktikan dengan
    ketiadaan `max_steps_abort` dan ketiadaan `dropped_tool_calls`.
    """
    tool = CountingReadTool()
    events: List[Dict[str, Any]] = []
    provider = ScriptedProvider([_batch_turn(3), _final_turn("selesai")])
    orchestrator = _make_orchestrator(provider, tool, events=events)

    result = orchestrator.run_continuous_loop("task pas", max_steps=3)

    # Batch dijalankan utuh (3 dari 3) — tidak ada tool call yang dibuang.
    assert tool.calls == 3, tool.calls
    assert result.iterations == 3
    assert "max_steps_abort" not in result.evidence
    aborts = [e for e in events if e["type"] == "loop_safety_abort"]
    assert len(aborts) == 1, aborts
    # Guard awal loop (pre-existing), bukan pemotongan batch.
    assert "dropped_tool_calls" not in aborts[0]


# --------------------------------------------------------------------------- #
# 4. Batch yang menyentuh batas -> status & alasan terminal benar
# --------------------------------------------------------------------------- #
def test_batch_reaching_limit_yields_terminal_reason_not_done() -> None:
    tool = CountingReadTool()
    events: List[Dict[str, Any]] = []
    provider = ScriptedProvider([_batch_turn(5), _final_turn("selesai")])
    orchestrator = _make_orchestrator(provider, tool, events=events)

    result = orchestrator.run_continuous_loop("task batas", max_steps=2)

    assert result.status != AgentStatus.DONE
    assert result.status == AgentStatus.FAILED
    assert result.result is None
    assert result.error is not None
    assert "max_steps=2" in result.error
    assert "Emergency safety limit" in result.error
    # 5 tool call dalam batch -> 2 dijalankan, 3 dibuang.
    assert tool.calls == 2, tool.calls
    assert result.iterations == 2
    aborts = [e for e in events if e["type"] == "loop_safety_abort"]
    assert aborts and aborts[0]["dropped_tool_calls"] == 3


# --------------------------------------------------------------------------- #
# 5. Jalur truncation dengan tool call LENGKAP tetap tunduk pada batas
# --------------------------------------------------------------------------- #
def test_truncated_with_complete_tool_calls_obeys_budget() -> None:
    tool = CountingReadTool()
    events: List[Dict[str, Any]] = []
    # finish_reason=length + tool call LENGKAP -> dieksekusi, tetapi batch
    # melebihi anggaran -> tetap dipotong oleh batas max_steps.
    provider = ScriptedProvider(
        [
            _read_turn(
                [
                    _tool_call(f"t{i}", "read_file", {"path": f"t{i}.txt"})
                    for i in range(4)
                ],
                truncated=True,
            )
        ]
    )
    orchestrator = _make_orchestrator(provider, tool, events=events)

    result = orchestrator.run_continuous_loop("task truncation", max_steps=2)

    assert tool.calls == 2, tool.calls
    assert tool.seen == ["t0.txt", "t1.txt"], tool.seen
    assert result.status == AgentStatus.FAILED
    assert result.error is not None
    assert "max_steps=2" in result.error
    # Batas BUG-2 (bukan batas truncation recovery) yang menghentikan loop.
    assert result.evidence.get("max_steps_abort") is True
    assert "truncation_abort" not in result.evidence


def test_truncated_with_complete_tool_calls_within_budget_still_runs() -> None:
    """Batas truncation recovery (BUG-3) TIDAK ikut berubah oleh BUG-2."""
    tool = CountingReadTool()
    events: List[Dict[str, Any]] = []
    provider = ScriptedProvider(
        [
            _read_turn(
                [_tool_call("t0", "read_file", {"path": "t0.txt"})], truncated=True
            ),
            _final_turn("selesai"),
        ]
    )
    orchestrator = _make_orchestrator(provider, tool, events=events)

    result = orchestrator.run_continuous_loop("task truncation normal", max_steps=10)

    assert result.status == AgentStatus.DONE, result.error
    assert tool.calls == 1, tool.calls
    # Truncation dengan tool call lengkap BUKAN recovery (counter tetap 0),
    # konsisten dengan kontrak BUG-3 yang sudah diperbaiki.
    trunc_events = [e for e in events if e["type"] == "provider_response_truncated"]
    assert len(trunc_events) == 1
    assert trunc_events[0]["phase"] == "tool_calls"
    assert trunc_events[0]["recovery"] == 0


# --------------------------------------------------------------------------- #
# 6. Tidak ada tool call dieksekusi dua kali akibat penghentian batch
# --------------------------------------------------------------------------- #
def test_no_duplicate_execution_after_batch_stop() -> None:
    tool = CountingReadTool()
    events: List[Dict[str, Any]] = []
    provider = ScriptedProvider([_batch_turn(3), _final_turn("selesai")])
    orchestrator = _make_orchestrator(provider, tool, events=events)

    result = orchestrator.run_continuous_loop("task duplikasi", max_steps=1)

    assert tool.calls == 1, tool.calls
    assert len(tool.seen) == len(set(tool.seen)) == 1
    # Tiap tool_call_id punya TEPAT satu tool_called & satu tool_completed.
    called = [e["tool_call_id"] for e in events if e["type"] == "tool_called"]
    completed = [e["tool_call_id"] for e in events if e["type"] == "tool_completed"]
    assert called == ["c0"], called
    assert completed == ["c0"], completed
    # Provider TIDAK dipanggil lagi (loop langsung berhenti).
    assert provider.calls == 1, provider.calls
    assert result.status == AgentStatus.FAILED


# --------------------------------------------------------------------------- #
# 7. Tidak ada jalur alternatif yang melewati batas
# --------------------------------------------------------------------------- #
def test_no_alternative_path_bypasses_limit() -> None:
    """Setelah anggaran habis, round berikutnya TIDAK mendispatch tool call.

    Batch pertama menghabiskan seluruh anggaran; provider lalu mengembalikan
    batch lagi, tetapi loop DIHENTIKAN lebih dulu oleh guard batas sehingga
    tidak ada tool call tambahan yang dijalankan (jumlah tool call total tetap
    sama dengan `max_steps`).
    """
    tool = CountingReadTool()
    events: List[Dict[str, Any]] = []
    # Skrip: batch 2 call berulang (item terakhir diulang) + final.
    provider = ScriptedProvider([_batch_turn(2, prefix="x")])
    orchestrator = _make_orchestrator(provider, tool, events=events)

    result = orchestrator.run_continuous_loop("task habis", max_steps=2)

    # Tepat 2 tool call dieksekusi = max_steps; tidak ada yang lolos.
    assert tool.calls == 2, tool.calls
    assert result.iterations == 2
    assert result.status == AgentStatus.FAILED
    assert result.error is not None
    assert "max_steps=2" in result.error
    aborts = [e for e in events if e["type"] == "loop_safety_abort"]
    assert len(aborts) == 1, aborts
    # Penghentian dari guard awal loop (batch pertama tidak dipotong karena
    # persis sesuai anggaran), sehingga tidak ada `dropped_tool_calls`.
    assert "dropped_tool_calls" not in aborts[0]


def test_bucket_branch_guard_is_in_source() -> None:
    """Defense-in-depth: cabang sisa anggaran <= 0 tetap ada di source.

    Guard di awal while loop menangani kasus ini lebih dulu pada runtime, tetapi
    cabang di titik dispatch tetap dipertahankan agar TIDAK ADA jalur (mis.
    perubahan lintas-round) yang dapat men-dispatch tool call tanpa anggaran.
    """
    source = (SRC_DIR / "agent_ai" / "core" / "orchestrator.py").read_text(
        encoding="utf-8"
    )
    assert "remaining_budget = int(max_steps) - int(loop.iteration)" in source
    assert "if remaining_budget <= 0:" in source


if __name__ == "__main__":  # pragma: no cover - entry manual
    import traceback

    tests = [
        test_max_steps_one_executes_at_most_one_tool_call,
        test_remaining_budget_two_of_four_tool_calls_started,
        test_batch_within_budget_unchanged,
        test_batch_exactly_at_budget_is_allowed,
        test_batch_reaching_limit_yields_terminal_reason_not_done,
        test_truncated_with_complete_tool_calls_obeys_budget,
        test_truncated_with_complete_tool_calls_within_budget_still_runs,
        test_no_duplicate_execution_after_batch_stop,
        test_no_alternative_path_bypasses_limit,
        test_bucket_branch_guard_is_in_source,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  OK  {test.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {test.__name__}")
            traceback.print_exc()
    print(f"\n{'FAILED' if failed else 'OK'}: {len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
