"""Regression tests: respons terpotong (`finish_reason="length"`) TANPA tool-call
yang dibuang tidak boleh dianggap final.

Bug yang dikunci:
`OpenAICompatibleProvider.normalize_response()` sebelumnya menetapkan
`truncated=True` HANYA bila `finish_reason="length"` DAN ada tool-call yang
dibuang (`dropped > 0`). Akibatnya respons yang berhenti karena batas token
tetapi hanya berisi TEKS (tidak ada tool-call tak lengkap) mendapat
`truncated=False`, lalu diperlakukan sebagai jawaban FINAL oleh continuous loop
-> task berstatus DONE padahal output belum lengkap.

Kontrak yang diuji (mock provider, TANPA network):
    1. Respons teks `finish_reason="length"` tanpa tool-call yang dibuang
       dikenali sebagai TIDAK lengkap (`truncated=True`).
    2. Respons terpotong tersebut TIDAK langsung menghasilkan DONE.
    3. Provider yang SELALU mengembalikan respons terpotong berhenti pada batas
       recovery yang sudah ada (BUG-3) dan berakhir FAILED dengan alasan jelas
       (BUKAN DONE, BUKAN loop tanpa batas).
    4. Respons teks normal `finish_reason="stop"` tetap menghasilkan DONE.
    5. Tool-call LENGKAP pada respons terpotong tetap dieksekusi (progres tidak
       hilang), tanpa eksekusi berulang.
    6. Truncation berupa tool-call JSON tidak lengkap tetap memakai mekanisme
       BUG-3 (tool-call dibuang).
    7. Variasi bentuk respons/finish_reason dari provider didukung: `length`
       tanpa choices/`message`, `length` dengan tool_calls hilang, dsb.
    8. Event observability & metadata response tetap konsisten.

Jalankan:
    python -m pytest tests/test_truncation_length_final.py
atau:
    python tests/test_truncation_length_final.py
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
from agent_ai.core.response import FinishReason  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
)
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402

SYSTEM_PROMPT = "Kamu adalah coding agent."


# --------------------------------------------------------------------------- #
# Helper respons mentah (bentuk OpenAI-compatible)
# --------------------------------------------------------------------------- #
def _tool_call(call_id: str, name: str, arguments: Any) -> Dict[str, Any]:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _text_turn(text: str, finish_reason: str) -> Dict[str, Any]:
    return {
        "choices": [
            {"message": {"content": text}, "finish_reason": finish_reason}
        ]
    }


def _tool_turn(
    calls: List[Dict[str, Any]], finish_reason: str = "tool_calls"
) -> Dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"content": "", "tool_calls": calls},
                "finish_reason": finish_reason,
            }
        ]
    }


def _normalize(raw: Any) -> Any:
    provider = OpenAICompatibleProvider()
    return provider.normalize_response(
        GenerateResult(text="", model="m", raw=raw)
    )


# --------------------------------------------------------------------------- #
# Mock provider + tool (lokal, tanpa network)
# --------------------------------------------------------------------------- #
class ScriptedProvider(OpenAICompatibleProvider):
    """Provider palsu: tiap call mengembalikan turn skrip; skrip habis ->
    item TERAKHIR diulang (mensimulasikan provider yang selalu memotong)."""

    name = "length-scripted"

    def __init__(self, script: List[Any]) -> None:
        self.config = SimpleNamespace(model="length-model")
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
            item = _text_turn("(default)", "stop")
        elif idx < len(self.script):
            item = self.script[idx]
        else:
            item = self.script[-1]
        return GenerateResult(
            text="", model="length-model", provider=self.name, raw=item
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
        options=GenerateOptions(model="length-model"),
        system_prompt=SYSTEM_PROMPT,
        use_continuous_loop=True,
        event_sink=sink,
    )


# --------------------------------------------------------------------------- #
# 1. Normalisasi: `length` tanpa tool-call dibuang -> truncated (tidak lengkap)
# --------------------------------------------------------------------------- #
def test_length_text_only_is_marked_truncated() -> None:
    resp = _normalize(_text_turn("Jawaban yang tiba-tiba terpo", "length"))
    assert resp.truncated is True
    assert resp.finish_reason == FinishReason.LENGTH
    assert resp.has_tool_calls is False
    assert resp.incomplete_tool_calls == 0


def test_length_without_choices_is_marked_truncated() -> None:
    # Bentuk respons provider yang tidak menyertakan choices/message (defensif).
    resp = _normalize({"choices": [], "finish_reason": "length"})
    assert resp.truncated is True
    assert resp.finish_reason == FinishReason.LENGTH


def test_stop_text_is_not_truncated() -> None:
    resp = _normalize(_text_turn("Jawaban lengkap.", "stop"))
    assert resp.truncated is False
    assert resp.finish_reason == FinishReason.STOP


def test_normal_tool_calls_not_truncated() -> None:
    resp = _normalize(
        _tool_turn([_tool_call("c1", "echo", {"value": "hi"})])
    )
    assert resp.truncated is False
    assert len(resp.tool_calls()) == 1


def test_length_with_incomplete_json_still_drops_and_truncated() -> None:
    # Perilaku BUG-3 dipertahankan: JSON terpotong dibuang, ditandai truncated.
    resp = _normalize(
        _tool_turn(
            [_tool_call("c1", "echo", '{"value": "terpoto')],
            finish_reason="length",
        )
    )
    assert resp.truncated is True
    assert resp.incomplete_tool_calls == 1
    assert resp.has_tool_calls is False


def test_length_with_complete_tool_calls_keeps_them() -> None:
    # Tool-call LENGKAP pada respons `length` TIDAK dibuang.
    resp = _normalize(
        _tool_turn(
            [
                _tool_call("c1", "echo", {"value": "v"}),
                _tool_call("c2", "echo", '{"value": "terpoto'),
            ],
            finish_reason="length",
        )
    )
    assert resp.truncated is True
    assert [a.name for a in resp.tool_calls()] == ["echo"]
    assert resp.tool_calls()[0].arguments["value"] == "v"
    assert resp.incomplete_tool_calls == 1


# --------------------------------------------------------------------------- #
# 2. Orchestrator: `length` teks-tanpa-tool TIDAK langsung DONE
# --------------------------------------------------------------------------- #
def test_length_text_then_final_does_not_finish_on_truncated_turn() -> None:
    events: List[Dict[str, Any]] = []
    provider = ScriptedProvider(
        [
            _text_turn("Jawaban terpo", "length"),  # turn 1: terpotong
            _text_turn("Jawaban lengkap.", "stop"),  # turn 2: valid
        ]
    )
    orchestrator = _make_orchestrator(provider, events=events)

    result = orchestrator.run("Kerjakan task.")

    assert result.status == AgentStatus.DONE, result.error
    assert result.result == "Jawaban lengkap."
    # Turn terpotong TIDAK menyelesaikan task -> butuh request kedua.
    assert provider.calls == 2, provider.calls
    truncated_events = [
        e for e in events if e["type"] == "provider_response_truncated"
    ]
    assert len(truncated_events) == 1, truncated_events
    assert truncated_events[0]["finish_reason"] == "length"
    assert truncated_events[0]["recovery"] == 1
    assert not [e for e in events if e["type"] == "loop_truncation_abort"]


def test_always_length_text_aborts_failed_within_bound() -> None:
    events: List[Dict[str, Any]] = []
    provider = ScriptedProvider([_text_turn("terus terpo", "length")])
    orchestrator = _make_orchestrator(provider, events=events)

    result = orchestrator.run("Kerjakan task.")

    # Berhenti deterministik: FAILED dengan alasan jelas (BUKAN DONE).
    assert result.status == AgentStatus.FAILED, (result.status, result.result)
    assert result.status != AgentStatus.DONE
    assert result.error is not None
    assert "memotong output" in result.error.lower()
    assert "finish_reason=length" in result.error
    # Jumlah request dibatasi persis oleh batas recovery yang sudah ada.
    assert provider.calls == _MAX_TRUNCATION_RECOVERIES, provider.calls
    truncated_events = [
        e for e in events if e["type"] == "provider_response_truncated"
    ]
    assert len(truncated_events) == _MAX_TRUNCATION_RECOVERIES
    assert [e["recovery"] for e in truncated_events] == list(
        range(1, _MAX_TRUNCATION_RECOVERIES + 1)
    )
    abort_events = [e for e in events if e["type"] == "loop_truncation_abort"]
    assert len(abort_events) == 1, abort_events
    assert result.evidence.get("truncation_abort") is True
    assert (
        result.evidence.get("truncation_recoveries")
        == _MAX_TRUNCATION_RECOVERIES
    )


def test_normal_stop_response_still_done() -> None:
    events: List[Dict[str, Any]] = []
    provider = ScriptedProvider([_text_turn("jawaban final", "stop")])
    orchestrator = _make_orchestrator(provider, events=events)

    result = orchestrator.run("task sederhana")

    assert result.status == AgentStatus.DONE
    assert result.result == "jawaban final"
    assert provider.calls == 1
    assert not [
        e for e in events if e["type"] == "provider_response_truncated"
    ]


# --------------------------------------------------------------------------- #
# 3. Tool-call lengkap pada respons terpotong tetap dieksekusi (tanpa duplikasi)
# --------------------------------------------------------------------------- #
def test_length_with_complete_tool_call_executes_once() -> None:
    tool = CountingEchoTool()
    events: List[Dict[str, Any]] = []
    provider = ScriptedProvider(
        [
            _tool_turn(
                [_tool_call("c1", "echo", {"value": "v"})],
                finish_reason="length",
            ),
            _text_turn("selesai", "stop"),
        ]
    )
    orchestrator = _make_orchestrator(provider, tool, events=events)

    result = orchestrator.run("task")

    assert result.status == AgentStatus.DONE, result.error
    # Tool call lengkap dieksekusi TEPAT sekali (tidak diulang).
    assert tool.calls == 1, tool.calls
    truncated_events = [
        e for e in events if e["type"] == "provider_response_truncated"
    ]
    assert len(truncated_events) == 1, truncated_events
    assert truncated_events[0]["phase"] == "tool_calls"
    assert truncated_events[0]["completed_tool_calls"] == 1
    # Truncation dengan tool call lengkap BUKAN recovery (counter tetap 0).
    assert truncated_events[0]["recovery"] == 0
    assert not [e for e in events if e["type"] == "loop_truncation_abort"]


# --------------------------------------------------------------------------- #
# 4. Metadata respons tetap konsisten (record log)
# --------------------------------------------------------------------------- #
def test_response_record_metadata_consistent() -> None:
    provider = ScriptedProvider([])
    orchestrator = _make_orchestrator(provider)

    resp = _normalize(_text_turn("terpo", "length"))
    record = orchestrator._response_record_success(
        round_index=1, attempt=1, response=resp
    )
    assert record["finish_reason"] == "length"
    assert record["truncated"] is True
    assert record["incomplete_tool_calls"] == 0

    ok = _normalize(_text_turn("lengkap", "stop"))
    record_ok = orchestrator._response_record_success(
        round_index=2, attempt=1, response=ok
    )
    assert record_ok["finish_reason"] == "stop"
    assert record_ok["truncated"] is False


if __name__ == "__main__":  # pragma: no cover - entry manual
    import traceback

    tests = [
        test_length_text_only_is_marked_truncated,
        test_length_without_choices_is_marked_truncated,
        test_stop_text_is_not_truncated,
        test_normal_tool_calls_not_truncated,
        test_length_with_incomplete_json_still_drops_and_truncated,
        test_length_with_complete_tool_calls_keeps_them,
        test_length_text_then_final_does_not_finish_on_truncated_turn,
        test_always_length_text_aborts_failed_within_bound,
        test_normal_stop_response_still_done,
        test_length_with_complete_tool_call_executes_once,
        test_response_record_metadata_consistent,
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
