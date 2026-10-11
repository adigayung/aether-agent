"""Regression tests: BUG-3 — unbounded truncation recovery loop.

Mengunci perbaikan bug infinite loop pada `AgentOrchestrator.run_continuous_loop`:
sebelumnya jalur response TERPOTONG (`finish_reason=length`) TANPA tool call
melakukan `continue` tanpa batas dan tanpa memanggil `loop.record_action()`,
sehingga guard `max_steps` (yang dihitung dari jumlah recorded steps) TIDAK
pernah tercapai. Provider yang terus memotong output membuat loop berputar
tanpa terminasi & tanpa batas jumlah request.

Kontrak yang diuji (deterministik, mock provider, TANPA network):
    1. provider SELALU truncated tanpa tool call -> loop berhenti setelah batas
       recovery, status FAILED (BUKAN DONE), dan jumlah request DIBATASI persis
       oleh batas (tidak terus bertambah).
    2. provider truncated beberapa kali lalu memberi respons valid SEBELUM batas
       -> recovery berhasil, loop lanjut normal, DONE.
    3. respons valid tanpa truncation -> perilaku sebelumnya tetap berfungsi.
    4. event truncation (`provider_response_truncated` / `loop_truncation_abort`)
       konsisten dengan hasil aktual (tidak menyesatkan); task sukses TIDAK
       mengemit `loop_truncation_abort`.
    5. truncation dengan tool CALL lengkap tetap dieksekusi (bukan recovery);
       progress tidak hilang dan loop tetap selesai normal.

Jalankan:
    python -m pytest tests/test_truncation_recovery_bound.py
atau:
    python tests/test_truncation_recovery_bound.py
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
from agent_ai.core.orchestrator import (  # noqa: E402
    _MAX_TRUNCATION_RECOVERIES,
    AgentOrchestrator,
)
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


def _tool_turn(text: str, calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"content": text, "tool_calls": calls},
                "finish_reason": "tool_calls",
            }
        ]
    }


def _final_turn(text: str) -> Dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


def _truncated_turn(text: str = "", call_id: str = "trunc") -> Dict[str, Any]:
    """Response TERPOTONG tanpa tool call LENGKAP (jalur rawan runaway BUG-3).

    `finish_reason="length"` + satu tool call dengan argumen JSON TERPOTONG:
    normalisasi provider membuang tool call tak lengkap itu sehingga
    `response.truncated is True` dan `response.has_tool_calls is False` — inilah
    jalur yang sebelumnya `continue` tanpa batas (tanpa `loop.record_action()`).
    """
    return {
        "choices": [
            {
                "message": {
                    "content": text,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": "echo",
                                "arguments": '{"value": "terpoto',
                            },
                        }
                    ],
                },
                "finish_reason": "length",
            }
        ]
    }


class ScriptedProvider(OpenAICompatibleProvider):
    """Provider palsu: tiap call mengembalikan turn skrip.

    Bila skrip habis, item TERAKHIR diulang — sehingga satu `_truncated_turn`
    mensimulasikan provider yang SELALU memotong output.
    """

    name = "trunc-scripted"

    def __init__(self, script: List[Any]) -> None:
        self.config = SimpleNamespace(model="trunc-model")
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
            text="", model="trunc-model", provider=self.name, raw=item
        )


class CountingEchoTool(BaseTool):
    """Tool palsu yang menghitung berapa kali ia dieksekusi."""

    def __init__(self, name: str = "echo") -> None:
        self.name = name
        self.description = "fake echo"
        self.input_schema = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": [],
        }
        self.calls = 0

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        self.calls += 1
        return {"content": f"echo:{arguments.get('value', '')}"}


def _make_orchestrator(
    provider: ScriptedProvider,
    tool: Optional[CountingEchoTool] = None,
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
        options=GenerateOptions(model="trunc-model"),
        system_prompt=SYSTEM_PROMPT,
        use_continuous_loop=True,
        event_sink=sink,
    )


# --------------------------------------------------------------------------- #
# 1. Provider SELALU truncated -> berhenti deterministik, FAILED, request dibatasi
# --------------------------------------------------------------------------- #
def test_always_truncated_terminates_failed_within_bound() -> None:
    events: List[Dict[str, Any]] = []
    provider = ScriptedProvider([_truncated_turn("potongan...")])
    orchestrator = _make_orchestrator(provider, events=events)

    result = orchestrator.run("Kerjakan task besar.")

    # Status terminal: FAILED (BUKAN DONE) dengan alasan jelas.
    assert result.status == AgentStatus.FAILED, (result.status, result.result)
    assert result.status != AgentStatus.DONE
    assert result.error is not None
    assert "memotong output" in result.error.lower()
    assert "finish_reason=length" in result.error
    assert "truncation recovery" in result.error
    assert str(_MAX_TRUNCATION_RECOVERIES) in result.error

    # Jumlah request DIBATASI persis oleh batas recovery (deterministik).
    assert provider.calls == _MAX_TRUNCATION_RECOVERIES, provider.calls

    # Event truncation sesuai fakta: `recovery` naik 1..limit dan diakhiri
    # `loop_truncation_abort` TEPAT sekali; tidak ada event yang menyesatkan.
    truncated_events = [
        e for e in events if e["type"] == "provider_response_truncated"
    ]
    assert len(truncated_events) == _MAX_TRUNCATION_RECOVERIES, len(truncated_events)
    assert [e["recovery"] for e in truncated_events] == list(
        range(1, _MAX_TRUNCATION_RECOVERIES + 1)
    )
    # Response tanpa tool call -> `completed_tool_calls` tidak dipaksakan ada.
    for event in truncated_events:
        assert event["finish_reason"] == "length"
        assert event["limit"] == _MAX_TRUNCATION_RECOVERIES
    abort_events = [e for e in events if e["type"] == "loop_truncation_abort"]
    assert len(abort_events) == 1, abort_events
    assert abort_events[0]["recoveries"] == _MAX_TRUNCATION_RECOVERIES

    # Evidence additive: menandai penghentian karena batas truncation.
    assert result.evidence.get("truncation_abort") is True
    assert result.evidence.get("truncation_recoveries") == _MAX_TRUNCATION_RECOVERIES


def test_bounded_requests_do_not_keep_growing() -> None:
    """Batas TIDAK bergantung pada isi skrip: selalu berhenti pada limit."""
    for extra in (1, 25, 200):
        # Skrip berisi `extra` turn truncated; setelah habis item TERAKHIR
        # (truncated) diulang -> provider terus memotong output tanpa batas.
        provider = ScriptedProvider([_truncated_turn("x") for _ in range(extra)])
        orchestrator = _make_orchestrator(provider)
        result = orchestrator.run("task")
        assert result.status == AgentStatus.FAILED
        # Selalu persis `_MAX_TRUNCATION_RECOVERIES` request — tidak tumbuh.
        assert provider.calls == _MAX_TRUNCATION_RECOVERIES, (extra, provider.calls)


# --------------------------------------------------------------------------- #
# 2. Truncated beberapa kali lalu respons valid SEBELUM batas -> recovery sukses
# --------------------------------------------------------------------------- #
def test_recovery_succeeds_before_bound() -> None:
    events: List[Dict[str, Any]] = []
    recoveries = _MAX_TRUNCATION_RECOVERIES - 1
    provider = ScriptedProvider(
        [_truncated_turn("potongan")] * recoveries + [_final_turn("selesai")]
    )
    orchestrator = _make_orchestrator(provider, events=events)

    result = orchestrator.run("task yang butuh recovery")

    assert result.status == AgentStatus.DONE, result.error
    assert result.result == "selesai"
    # 1 request truncated gagal + N recovery + 1 final.
    assert provider.calls == recoveries + 1, provider.calls
    truncated_events = [
        e for e in events if e["type"] == "provider_response_truncated"
    ]
    assert len(truncated_events) == recoveries
    # Recovery sukses -> TIDAK ada abort.
    assert not [e for e in events if e["type"] == "loop_truncation_abort"]
    assert "truncation_abort" not in result.evidence


# --------------------------------------------------------------------------- #
# 3. Respons valid tanpa truncation -> perilaku sebelumnya tetap berfungsi
# --------------------------------------------------------------------------- #
def test_valid_response_unchanged() -> None:
    events: List[Dict[str, Any]] = []
    provider = ScriptedProvider([_final_turn("jawaban final")])
    orchestrator = _make_orchestrator(provider, events=events)

    result = orchestrator.run("task sederhana")

    assert result.status == AgentStatus.DONE
    assert result.result == "jawaban final"
    assert provider.calls == 1
    assert not [e for e in events if e["type"] == "provider_response_truncated"]
    assert not [e for e in events if e["type"] == "loop_truncation_abort"]


# --------------------------------------------------------------------------- #
# 4. Counter reset setelah respons valid (recovery tidak menumpuk lintas-turun)
# --------------------------------------------------------------------------- #
def test_counter_reset_after_valid_response() -> None:
    tool = CountingEchoTool()
    events: List[Dict[str, Any]] = []
    # Turn 1 truncated (tanpa tool) -> recovery ke-1.
    # Turn 2 tool call valid -> counter RESET, tool dieksekusi.
    # Turn 3 truncated -> recovery kembali ke-1 (bukan 2).
    # Turn 4 final -> DONE.
    provider = ScriptedProvider(
        [
            _truncated_turn("potongan"),
            _tool_turn("", [_tool_call("c1", "echo", {"value": "hi"})]),
            _truncated_turn("potongan lagi"),
            _final_turn("selesai"),
        ]
    )
    orchestrator = _make_orchestrator(provider, tool, events=events)

    result = orchestrator.run("task dengan tool call")

    assert result.status == AgentStatus.DONE, result.error
    assert tool.calls == 1, tool.calls
    truncated_events = [
        e for e in events if e["type"] == "provider_response_truncated"
    ]
    # Dua recovery, masing-masing bernilai 1 (counter reset oleh respons valid).
    assert [e["recovery"] for e in truncated_events] == [1, 1]
    assert not [e for e in events if e["type"] == "loop_truncation_abort"]


# --------------------------------------------------------------------------- #
# 5. Truncation dengan tool CALL lengkap -> bukan recovery, progress tidak hilang
# --------------------------------------------------------------------------- #
def test_truncated_with_complete_tool_call_executes_tool() -> None:
    tool = CountingEchoTool()
    events: List[Dict[str, Any]] = []
    truncated_with_tool = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        _tool_call("c1", "echo", {"value": "v"}),
                        {
                            "id": "c2",
                            "type": "function",
                            "function": {
                                "name": "echo",
                                "arguments": '{"value": "terpoto',
                            },
                        },
                    ],
                },
                "finish_reason": "length",
            }
        ]
    }
    provider = ScriptedProvider([truncated_with_tool, _final_turn("selesai")])
    orchestrator = _make_orchestrator(provider, tool, events=events)

    result = orchestrator.run("task truncation dengan tool lengkap")

    assert result.status == AgentStatus.DONE, result.error
    # Tool call lengkap TETAP dieksekusi (progress tidak hilang).
    assert tool.calls == 1, tool.calls
    # Event observability diemit, tetapi ini BUKAN recovery (counter tetap 0):
    # truncation dengan tool call lengkap tidak menghitung batas recovery.
    truncated_events = [
        e for e in events if e["type"] == "provider_response_truncated"
    ]
    assert len(truncated_events) == 1, truncated_events
    assert truncated_events[0]["phase"] == "tool_calls"
    assert truncated_events[0]["recovery"] == 0
    assert truncated_events[0]["completed_tool_calls"] == 1
    # Tidak ada abort karena batas truncation.
    assert not [e for e in events if e["type"] == "loop_truncation_abort"]


if __name__ == "__main__":  # pragma: no cover - entry manual
    import traceback

    tests = [
        test_always_truncated_terminates_failed_within_bound,
        test_bounded_requests_do_not_keep_growing,
        test_recovery_succeeds_before_bound,
        test_valid_response_unchanged,
        test_counter_reset_after_valid_response,
        test_truncated_with_complete_tool_call_executes_tool,
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
