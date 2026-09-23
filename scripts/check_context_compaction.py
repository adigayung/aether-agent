"""Verifier Runtime Context Compaction (continuous loop, hemat token).

Memverifikasi bahwa `AgentOrchestrator.run_continuous_loop` TIDAK lagi mengirim
seluruh riwayat mentah setiap round, melainkan konteks yang BOUNDED dan
dipadatkan secara DETERMINISTIK (tanpa LLM/API tambahan), tanpa kehilangan
kemampuan reasoning/memory kerja/tool calling.

Properti yang diverifikasi:
    A. Percakapan pendek TIDAK berubah (riwayat apa adanya).
    B. Percakapan panjang memicu compaction (token turun).
    C. Instruksi system + task awal selalu dipertahankan utuh.
    D. Tool result terbaru tetap tersedia.
    E. Protokol tool calling tetap valid (tidak ada tool result yatim).
    F. Agent tetap MELANJUTKAN task sampai final setelah beberapa compaction.
    G. Konteks tidak melebihi budget.
    H. Compaction deterministik & tanpa API/LLM.
    I. Jalur continuous loop existing tetap bekerja (regresi smoke).
    J. Pengukuran sebelum/sesudah pada simulasi long-running task.

Jalankan:
    python scripts/check_context_compaction.py
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
from agent_ai.core.history import ConversationHistory  # noqa: E402
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.core.types import ToolCall  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402

SYSTEM_PROMPT = "Kamu adalah coding agent."
TASK = "Kerjakan task panjang."
BIG = "X" * 6000


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


class ScriptedProvider(OpenAICompatibleProvider):
    """Provider palsu: respons skrip berurutan (tanpa network)."""

    name = "scripted"

    def __init__(self, script: List[Dict[str, Any]]) -> None:
        self.config = SimpleNamespace(model="scripted-model")
        self.script = list(script)
        self.calls = 0
        self.requests: List[List[Dict[str, Any]]] = []

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Any]] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[List[Any]] = None,
        tool_choice: Optional[Any] = None,
    ) -> GenerateResult:
        self.requests.append(
            [dict(m) if isinstance(m, dict) else m for m in (messages or [])]
        )
        idx = self.calls
        self.calls += 1
        raw = self.script[idx] if idx < len(self.script) else _final_turn("(default)")
        return GenerateResult(text="", model="scripted-model", provider=self.name, raw=raw)


class BigResultTool(BaseTool):
    """Tool palsu: output besar (memicu compaction)."""

    def __init__(self, name: str = "read_file", size: int = 6000) -> None:
        self.name = name
        self.description = "fake big reader"
        self.input_schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": [],
        }
        self.size = size
        self.calls = 0

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        self.calls += 1
        return {"content": "X" * self.size, "total_lines": 1}


def _make_history(turns: int, size: int = 6000) -> ConversationHistory:
    history = ConversationHistory()
    history.append_system_message(SYSTEM_PROMPT)
    history.append_user_message(TASK)
    for i in range(turns):
        call = ToolCall.create("read_file", {"path": f"f{i}.txt"}, id=f"call-{i}")
        history.append_assistant_message(content=None, tool_calls=[call])
        history.append_tool_result(call.id, "read_file", "X" * size)
    return history


def check_a_short_unchanged() -> None:
    history = _make_history(turns=1, size=40)
    assert history.compile_compacted_messages(None) == history.messages
    assert history.compile_compacted_messages(100_000) == history.messages
    print("[A] percakapan pendek TIDAK berubah (riwayat apa adanya) OK")


def check_b_long_compacted() -> None:
    history = _make_history(turns=20)
    before = history.estimate_tokens()
    compacted = history.compile_compacted_messages(4000)
    after = ConversationHistory.estimate_messages_tokens(compacted)
    assert before > 4000 and after < before, (before, after)
    assert len(compacted) == len(history.messages)
    assert "dipadatkan" in " ".join(m.content or "" for m in compacted)
    print(f"[B] percakapan panjang dipadatkan OK -> {before} -> {after} token")


def check_c_head_preserved() -> None:
    compacted = _make_history(turns=20).compile_compacted_messages(1500)
    assert compacted[0].role == "system" and compacted[0].content == SYSTEM_PROMPT
    assert compacted[1].role == "user" and compacted[1].content == TASK
    print("[C] system + task awal dipertahankan utuh OK")


def check_d_recent_available() -> None:
    history = _make_history(turns=20)
    assert history.compile_compacted_messages(8000)[-1].content == BIG
    tight = history.compile_compacted_messages(600)
    assert tight[-1].role == "tool" and tight[-1].tool_call_id and tight[-1].content
    print("[D] tool result terbaru tetap tersedia OK")


def check_e_protocol_valid() -> None:
    compacted = _make_history(turns=20).compile_compacted_messages(2500)
    assistant_ids = [
        tc.id for m in compacted if m.role == "assistant" for tc in (m.tool_calls or [])
    ]
    tool_ids = [m.tool_call_id for m in compacted if m.role == "tool"]
    assert all(tid in assistant_ids for tid in tool_ids), "tool result yatim"
    assert compacted[0].role == "system"
    for message in (m.to_provider_dict() for m in compacted):
        if message["role"] == "tool":
            assert message["tool_call_id"] in assistant_ids
    print("[E] protokol tool calling tetap valid (tanpa tool result yatim) OK")


def check_f_agent_continues() -> None:
    turns = 25
    script = [
        _tool_turn(f"baca {i}", [_tool_call(f"c{i}", "read_file", {"path": f"f{i}.txt"})])
        for i in range(turns)
    ]
    script.append(_final_turn("Selesai setelah banyak turn."))
    provider = ScriptedProvider(script)
    registry = ToolRegistry()
    tool = BigResultTool("read_file")
    registry.register(tool)
    events: List[Dict[str, Any]] = []
    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="scripted-model"),
        system_prompt=SYSTEM_PROMPT,
        use_continuous_loop=True,
        context_budget_tokens=800,
        event_sink=lambda et, payload: events.append({"type": et, **payload}),
    )
    result = orchestrator.run(TASK)
    assert result.status == AgentStatus.DONE, result.error
    assert tool.calls == turns and provider.calls == turns + 1
    reqs = [e for e in events if e["type"] == "provider_request"]
    assert any(e.get("context_compacted") for e in reqs)
    print(f"[F] Agent melanjutkan sampai final setelah compaction OK -> {provider.calls} LLM calls")


def check_g_budget() -> None:
    history = _make_history(turns=20)
    for budget in (600, 1000, 2000, 4000, 8000):
        compacted = history.compile_compacted_messages(budget)
        assert ConversationHistory.estimate_messages_tokens(compacted) <= budget
    compacted = history.compile_compacted_messages(3000, overhead_tokens=1000)
    assert ConversationHistory.estimate_messages_tokens(compacted) <= 2000
    print("[G] konteks tidak melebihi budget (termasuk overhead tool) OK")


def check_h_deterministic() -> None:
    history = _make_history(turns=15)
    fmt = lambda: [m.to_provider_dict() for m in history.compile_compacted_messages(2000)]
    assert fmt() == fmt(), "compaction harus deterministik"
    print("[H] compaction deterministik (tanpa API/LLM) OK")


def check_i_regression_smoke() -> None:
    script = [
        _tool_turn(
            "baca dua",
            [_tool_call("a", "read_file", {}), _tool_call("b", "read_file", {})],
        ),
        _final_turn("Selesai."),
    ]
    provider = ScriptedProvider(script)
    registry = ToolRegistry()
    registry.register(BigResultTool("read_file", size=20))
    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="scripted-model"),
        system_prompt=SYSTEM_PROMPT,
        use_continuous_loop=True,
    )
    result = orchestrator.run("baca a dan b")
    assert result.status == AgentStatus.DONE, result.error
    roles = [m.get("role") for m in provider.requests[1]]
    assert roles[0] == "system" and "tool" in roles
    print("[I] jalur continuous loop existing tetap bekerja (regresi smoke) OK")


def check_j_measurement() -> None:
    """Ukur sebelum/sesudah pada simulasi long-running task."""
    history = _make_history(turns=40, size=4000)
    before = history.estimate_tokens()
    print("\n[J] Pengukuran simulasi long-running task (40 turn tool besar):")
    print(f"    riwayat mentah (tiap round tanpa compaction) -> {before} token (estimasi)")
    for budget in (2000, 4000, 8000, 12000):
        after = ConversationHistory.estimate_messages_tokens(
            history.compile_compacted_messages(budget)
        )
        pct = (1 - after / before) * 100 if before else 0
        print(f"    budget {budget:>6} -> {after:>6} token  (hemat ~{pct:.0f}%)")


def main() -> int:
    print("=== Verifikasi Runtime Context Compaction (continuous loop) ===")
    check_a_short_unchanged()
    check_b_long_compacted()
    check_c_head_preserved()
    check_d_recent_available()
    check_e_protocol_valid()
    check_f_agent_continues()
    check_g_budget()
    check_h_deterministic()
    check_i_regression_smoke()
    check_j_measurement()
    print()
    print("[OK] Context compaction bekerja: hemat token tanpa kehilangan memori kerja Agent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
