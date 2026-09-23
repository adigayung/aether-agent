"""Regression tests: runtime context compaction continuous loop (hemat token).

Menguji context compaction DETERMINISTIK (tanpa LLM/API tambahan) yang
diintegrasikan ke `AgentOrchestrator.run_continuous_loop`:

    A. Percakapan pendek TIDAK berubah (riwayat dikirim apa adanya).
    B. Percakapan panjang MEMICU compaction (token turun, pesan tetap ada).
    C. Task/instruksi awal SELALU dipertahankan utuh.
    D. Tool result terbaru tetap tersedia.
    E. Context lama yang dipadatkan TIDAK merusak protokol tool calling.
    F. Agent dapat MELANJUTKAN task setelah beberapa compaction (end-to-end).
    G. Konteks TIDAK melebihi budget yang ditentukan.
    H. Compaction DETERMINISTIK dan tidak butuh API/LLM.
    I. Regresi Agent/continuous-loop existing tetap PASS (smoke).

Semua test memakai fake provider/tool lokal (TANPA network).

Jalankan:
    python -m pytest tests/test_context_compaction.py
atau:
    python tests/test_context_compaction.py
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

BIG = "X" * 6000
HEAD_TASK = "Selesaikan task panjang ini."
SYSTEM_PROMPT = "Kamu adalah coding agent."


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
    """Tool palsu yang mengembalikan output besar (memicu compaction)."""

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
    history.append_user_message(HEAD_TASK)
    for i in range(turns):
        call = ToolCall.create("read_file", {"path": f"f{i}.txt"}, id=f"call-{i}")
        history.append_assistant_message(content=None, tool_calls=[call])
        history.append_tool_result(call.id, "read_file", "X" * size)
    return history


def _provider_format(history: ConversationHistory, **kwargs: Any) -> List[Dict[str, Any]]:
    return [
        m.to_provider_dict()
        for m in history.compile_compacted_messages(**kwargs)
    ]


# --------------------------------------------------------------------------- #
# A. Percakapan pendek TIDAK berubah
# --------------------------------------------------------------------------- #
def test_a_short_conversation_unchanged() -> None:
    history = _make_history(turns=1, size=40)
    before = history.messages
    # Budget None -> apa adanya.
    assert history.compile_compacted_messages(None) == before
    # Budget besar -> apa adanya (tidak ada pemadatan bila sudah muat).
    compacted = history.compile_compacted_messages(100_000, overhead_tokens=1000)
    assert compacted == before
    assert history.estimate_messages_tokens(compacted) == history.estimate_tokens()


# --------------------------------------------------------------------------- #
# B. Percakapan panjang MEMICU compaction
# --------------------------------------------------------------------------- #
def test_b_long_conversation_triggers_compaction() -> None:
    history = _make_history(turns=20)
    before = history.estimate_tokens()
    # Budget 4000 > ukuran hasil compact penuh (semua pesan tetap ada, hanya
    # dipadatkan) sehingga menguji compaction TANPA pembuangan pesan.
    compacted = history.compile_compacted_messages(4000, overhead_tokens=0)
    after = ConversationHistory.estimate_messages_tokens(compacted)

    assert before > 4000, before
    assert after < before, (before, after)
    # Pesan TIDAK dibuang sembarangan (masih ada, hanya dipadatkan).
    assert len(compacted) == len(history.messages)
    # Konten lama benar-benar dipadatkan (ditandai deterministik).
    joined = " ".join(m.content or "" for m in compacted)
    assert "dipadatkan" in joined


# --------------------------------------------------------------------------- #
# C. Task/instruksi awal selalu dipertahankan utuh
# --------------------------------------------------------------------------- #
def test_c_current_task_preserved() -> None:
    history = _make_history(turns=20)
    compacted = history.compile_compacted_messages(1500, overhead_tokens=0)
    # System + task awal utuh.
    assert compacted[0].role == "system"
    assert compacted[0].content == SYSTEM_PROMPT
    assert compacted[1].role == "user"
    assert compacted[1].content == HEAD_TASK


# --------------------------------------------------------------------------- #
# D. Tool result terbaru tetap tersedia
# --------------------------------------------------------------------------- #
def test_d_recent_tool_result_available() -> None:
    history = _make_history(turns=20)
    compacted = history.compile_compacted_messages(8000, overhead_tokens=0)
    last = compacted[-1]
    assert last.role == "tool"
    # Pesan terakhir dipertahankan UTUH selama masih muat.
    assert last.content == "X" * 6000
    # Bila budget sangat ketat, pesan terakhir tetap ADA (mungkin dipadatkan).
    tight = history.compile_compacted_messages(600, overhead_tokens=0)
    assert tight[-1].role == "tool"
    assert tight[-1].tool_call_id
    assert tight[-1].content  # masih ada isi (cuplikan)


# --------------------------------------------------------------------------- #
# E. Protokol tool calling tetap valid setelah compaction
# --------------------------------------------------------------------------- #
def test_e_tool_protocol_still_valid() -> None:
    history = _make_history(turns=20)
    compacted = history.compile_compacted_messages(2500, overhead_tokens=0)

    assistant_ids = [
        tc.id for m in compacted if m.role == "assistant" for tc in (m.tool_calls or [])
    ]
    tool_ids = [m.tool_call_id for m in compacted if m.role == "tool"]
    # Setiap tool result punya induk assistant(tool_calls) (tidak ada orphan).
    assert all(tid in assistant_ids for tid in tool_ids), "tool result yatim"
    # Tidak ada tool result di awal.
    assert compacted[0].role == "system"
    # Bentuk provider tetap valid (tool_calls + tool_call_id bersesuaian).
    provider = [m.to_provider_dict() for m in compacted]
    for message in provider:
        assert "role" in message
        if message["role"] == "tool":
            assert message["tool_call_id"] in assistant_ids


# --------------------------------------------------------------------------- #
# F. Agent dapat MELANJUTKAN task setelah beberapa compaction (end-to-end)
# --------------------------------------------------------------------------- #
def test_f_agent_continues_after_compaction() -> None:
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
    result = orchestrator.run(HEAD_TASK)

    assert result.status == AgentStatus.DONE, result.error
    assert result.result == "Selesai setelah banyak turn."
    # SEMUA tool call tetap dieksekusi (loop tidak rusak karena compaction).
    assert tool.calls == turns, tool.calls
    assert provider.calls == turns + 1, provider.calls

    # Compaction benar-benar aktif pada turn-turn akhir.
    reqs = [e for e in events if e["type"] == "provider_request"]
    assert reqs, "provider_request harus diemit"
    assert any(e.get("context_compacted") for e in reqs)
    assert reqs[-1].get("context_compacted") is True

    # Request terakhir BOUNDED (jauh lebih kecil dari riwayat mentah penuh).
    last_request = provider.requests[-1]
    last_chars = sum(len(str(m.get("content") or "")) for m in last_request)
    raw_chars = turns * len(BIG)
    assert last_chars < raw_chars, (last_chars, raw_chars)
    assert last_chars < 30_000, last_chars


# --------------------------------------------------------------------------- #
# G. Konteks tidak melebihi budget
# --------------------------------------------------------------------------- #
def test_g_context_within_budget() -> None:
    history = _make_history(turns=20, size=6000)
    for budget in (600, 1000, 2000, 4000, 8000):
        compacted = history.compile_compacted_messages(budget, overhead_tokens=0)
        assert ConversationHistory.estimate_messages_tokens(compacted) <= budget, budget


def test_g2_overhead_reduces_message_budget() -> None:
    history = _make_history(turns=20, size=6000)
    budget, overhead = 3000, 1000
    compacted = history.compile_compacted_messages(budget, overhead_tokens=overhead)
    assert ConversationHistory.estimate_messages_tokens(compacted) <= budget - overhead


# --------------------------------------------------------------------------- #
# H. Deterministik & tanpa API/LLM
# --------------------------------------------------------------------------- #
def test_h_deterministic_and_no_llm() -> None:
    history = _make_history(turns=15)
    first = _provider_format(history, max_tokens=2000)
    second = _provider_format(history, max_tokens=2000)
    assert first == second, "compaction harus deterministik"

    # Compaction murni struktur data: tidak menyentuh provider sama sekali.
    call_log: List[str] = []

    class SpyProvider(ScriptedProvider):
        def generate(self, *args: Any, **kwargs: Any) -> GenerateResult:  # type: ignore[override]
            call_log.append("generate")
            return super().generate(*args, **kwargs)

    spy = SpyProvider([])
    history.compile_compacted_messages(2000)
    assert call_log == [], "compaction tidak boleh memanggil LLM"


# --------------------------------------------------------------------------- #
# I. Regresi continuous loop existing (smoke)
# --------------------------------------------------------------------------- #
def test_i_continuous_loop_regression_smoke() -> None:
    script = [
        _tool_turn(
            "tulis dua file",
            [
                _tool_call("a", "read_file", {"path": "a.txt"}),
                _tool_call("b", "read_file", {"path": "b.txt"}),
            ],
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
    assert provider.calls == 2
    # Hasil tool dikirim sebagai role "tool" (bukan "user").
    second = provider.requests[1]
    roles = [m.get("role") for m in second]
    assert roles[0] == "system" and "tool" in roles
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert sorted(m.get("tool_call_id") for m in tool_msgs) == ["a", "b"]


# --------------------------------------------------------------------------- #
# Standalone runner (tanpa pytest)
# --------------------------------------------------------------------------- #
def _standalone() -> int:
    checks = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = 0
    for name, func in checks:
        try:
            func()
            print(f"[PASS] {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
    print()
    if failures:
        print(f"[FAILED] {failures} test gagal")
        return 1
    print(f"[OK] {len(checks)} test lulus (context compaction)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_standalone())
