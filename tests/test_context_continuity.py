"""Regression tests: KONTINUITAS konteks antar-round pada continuous loop.

Bug yang dilindungi (root cause):

    `ConversationHistory.compile_compacted_messages()` menghitung anggaran ekor
    sebagai `budget - kepala`, dengan kepala = seluruh pesan system + task awal.
    Bila KEPALA (system prompt + konteks pengetahuan/Project Bible + task)
    SENDIRI sudah memenuhi/melampaui anggaran (mis. retrieval Bible mengisi
    hampir seluruh anggaran provider), maka `limit` menjadi 0 dan Tahap 6
    membuang SELURUH ekor percakapan (semua assistant tool_call + hasil tool).

    Akibatnya LLM tidak lagi melihat tool call/hasil sebelumnya, menganggap
    dirinya "baru mulai", lalu mengulang eksplorasi dari awal -> loop puluhan
    round tanpa kemajuan (52 round, 125 tool call, pola list_files berulang).

Yang diuji di sini:
    J1. Kepala yang melebihi anggaran TIDAK membuang state kerja:
        tool call + tool result terbaru tetap terkirim dan protokol valid.
    J2. Sebelum fix hanya tersisa kepala + 1 pesan; setelah fix jauh lebih banyak.
    J3. End-to-end continuous loop dengan kepala besar: SETIAP round lanjutan
        provider menerima tool result round sebelumnya (bukan system+task saja).
    J4. Kondisi NORMAL (kepala kecil) tidak berubah: hasil tetap <= budget.

Semua test memakai fake provider/tool lokal (TANPA network).

Jalankan:
    python -m pytest tests/test_context_continuity.py
atau:
    python tests/test_context_continuity.py
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
from agent_ai.providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
)
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402

HEAD_TASK = "Implementasikan permission system."
SYSTEM_PROMPT = "Kamu adalah coding agent."
#: Konteks pengetahuan/Bible besar: SENDIRI melebihi budget kecil (10.000 token).
BIG_KNOWLEDGE = "K" * 40_000


# --------------------------------------------------------------------------- #
# Fake provider + tool (lokal, deterministik, tanpa network)
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


class ScriptedProvider(OpenAICompatibleProvider):
    """Provider palsu: mengembalikan respons skrip berurutan (tanpa network)."""

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
    """Tool palsu yang mengembalikan output besar (mirip read_file)."""

    def __init__(self, name: str = "read_file", size: int = 200) -> None:
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


def _make_history_with_big_head(turns: int, size: int = 200) -> ConversationHistory:
    """History dengan kepala besar (system prompt + knowledge/Bible) + eksplorasi."""
    history = ConversationHistory()
    history.append_system_message(SYSTEM_PROMPT)
    history.append_system_message("PROJECT BIBLE: " + BIG_KNOWLEDGE)
    history.append_user_message(HEAD_TASK)
    for i in range(turns):
        call = ToolCall.create("list_files", {"path": f"dir{i}"}, id=f"call-{i}")
        history.append_assistant_message(content="explore", tool_calls=[call])
        history.append_tool_result(call.id, "list_files", "L" * size)
    return history


def _tool_call_ids(messages: List[Any]) -> set:
    ids = set()
    for message in messages:
        for call in message.tool_calls or []:
            ids.add(call.id)
    return ids


# --------------------------------------------------------------------------- #
# J1. Kepala melebihi anggaran TIDAK membuang state kerja
# --------------------------------------------------------------------------- #
def test_j1_oversized_head_keeps_recent_working_window() -> None:
    history = _make_history_with_big_head(turns=20)
    head_tokens = ConversationHistory.estimate_messages_tokens(history.messages[:2])
    budget = 4000
    assert head_tokens > budget, "skenario butuh kepala > anggaran"

    compacted = history.compile_compacted_messages(budget, overhead_tokens=0)

    tool_msgs = [m for m in compacted if m.role == "tool"]
    assistant_tool_msgs = [
        m for m in compacted if m.role == "assistant" and m.tool_calls
    ]
    # Tool call + hasil dari round sebelumnya TETAP terkirim.
    assert assistant_tool_msgs, "tool call hilang total (Agent kembali ke awal)"
    assert tool_msgs, "tool result hilang total (Agent kembali ke awal)"
    # Protokol tetap valid: tidak ada tool result yatim.
    call_ids = _tool_call_ids(compacted)
    assert all(m.tool_call_id in call_ids for m in tool_msgs)
    # Hasil tool TERBARU (pesan terakhir) tetap terkirim.
    assert compacted[-1].role == "tool"
    # System prompt + task awal tetap utuh di depan.
    assert compacted[0].role == "system"
    assert compacted[0].content == SYSTEM_PROMPT


def test_j2_oversized_head_keeps_more_than_head_plus_one() -> None:
    history = _make_history_with_big_head(turns=20)
    compacted = history.compile_compacted_messages(4000, overhead_tokens=0)
    # Sebelum fix: hanya tersisa kepala (system+task) + 1 pesan -> state hilang.
    assert len(compacted) > 5, len(compacted)


# --------------------------------------------------------------------------- #
# J3. End-to-end: provider SELALU menerima tool result round sebelumnya
# --------------------------------------------------------------------------- #
def test_j3_continuous_loop_survives_oversized_head() -> None:
    turns = 12
    script = [
        _tool_turn(
            f"explore {i}",
            [_tool_call(f"c{i}", "read_file", {"path": f"f{i}.txt"})],
        )
        for i in range(turns)
    ]
    script.append(_final_turn("selesai"))

    provider = ScriptedProvider(script)
    registry = ToolRegistry()
    registry.register(BigResultTool("read_file", size=200))
    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="scripted-model"),
        system_prompt=SYSTEM_PROMPT,
        use_continuous_loop=True,
        context_budget_tokens=4000,
        environment_context=("KNOWLEDGE " + BIG_KNOWLEDGE),
    )
    result = orchestrator.run(HEAD_TASK)

    assert result.status == AgentStatus.DONE, result.error
    # Setiap round lanjutan HARUS menerima tool result round sebelumnya, bukan
    # hanya system + task (itu gejala "Agent mengulang dari awal").
    for idx, req in enumerate(provider.requests[1:], start=1):
        roles = [m.get("role") for m in req]
        assert "tool" in roles, (idx, roles)


# --------------------------------------------------------------------------- #
# J4. Kondisi NORMAL tidak berubah: hasil tetap <= budget (kepala kecil)
# --------------------------------------------------------------------------- #
def test_j4_normal_head_still_respects_budget() -> None:
    history = ConversationHistory()
    history.append_system_message(SYSTEM_PROMPT)
    history.append_user_message(HEAD_TASK)
    for i in range(20):
        call = ToolCall.create("read_file", {"path": f"f{i}.txt"}, id=f"call-{i}")
        history.append_assistant_message(content=None, tool_calls=[call])
        history.append_tool_result(call.id, "read_file", "X" * 6000)

    for budget in (600, 1000, 2000, 4000, 8000):
        compacted = history.compile_compacted_messages(budget, overhead_tokens=0)
        tokens = ConversationHistory.estimate_messages_tokens(compacted)
        assert tokens <= budget, (budget, tokens)


# --------------------------------------------------------------------------- #
# Standalone runner (tanpa pytest)
# --------------------------------------------------------------------------- #
def _standalone() -> int:
    tests = [
        test_j1_oversized_head_keeps_recent_working_window,
        test_j2_oversized_head_keeps_more_than_head_plus_one,
        test_j3_continuous_loop_survives_oversized_head,
        test_j4_normal_head_still_respects_budget,
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as exc:  # noqa: PERF203
            failures += 1
            print(f"FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_standalone())
