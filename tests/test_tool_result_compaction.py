"""Regression tests: TOOL RESULT COMPACTION tanpa kehilangan informasi penting.

Menguji pemadatan hasil tool yang STALE di conversation history menjadi
representasi ringkas TERSTRUKTUR (bukan pemotongan buta), yang:

    - mempertahankan informasi penting per tool (path, range/symbol, query,
      command/exit_code, error, dst);
    - menyertakan locator sehingga detail dapat DIAMBIL ULANG lewat tool yang
      sudah ada (read_file/search_code/run_command/...);
    - tetap bekerja bersama runtime context compaction dari Task 1;
    - tidak merusak protokol tool calling.

Cakupan (A-L):
    A. hasil read_file besar.
    B. beberapa hasil read_file berurutan.
    C. hasil search_code.
    D. hasil atlas_query / rig_query.
    E. output run_command besar.
    F. command gagal dengan stderr penting.
    G. hasil edit_file.
    H. beberapa round dengan tool result yang terus bertambah (end-to-end).
    I. Agent masih dapat mengambil ulang detail yang sudah dikompak.
    J. protokol tool tetap valid.
    K. Task 1 context compaction tetap PASS.
    L. existing Agent/continuous-loop regression tetap PASS.

Semua test memakai fake provider/tool lokal (TANPA network).

Jalankan:
    python -m pytest tests/test_tool_result_compaction.py
atau:
    python tests/test_tool_result_compaction.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
for _path in (str(SRC_DIR), str(PROJECT_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from agent_ai.contextbudget import ToolResultCompactor  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.history import ConversationHistory  # noqa: E402
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.core.types import ToolCall  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.filesystem import ReadFileTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402

MARKER = "[dipadatkan]"
COMPACTOR = ToolResultCompactor()
SYSTEM_PROMPT = "Kamu adalah coding agent."


# --------------------------------------------------------------------------- #
# Helpers: payload tool realistis
# --------------------------------------------------------------------------- #
def _read_file_payload(
    path: str = "src/big.py", lines: int = 240, start: int = 1, end: Optional[int] = None
) -> Dict[str, Any]:
    end = end if end is not None else lines
    content = "\n".join(f"{i}: " + "x" * 80 for i in range(1, lines + 1))
    return {
        "path": path,
        "start_line": start,
        "end_line": end,
        "total_lines": lines,
        "symbol": {
            "name": "big",
            "kind": "function",
            "qualified_name": "Cls.big",
            "start_line": start,
            "end_line": end,
        },
        "content": content,
    }


def _search_payload(query: str = "def run", matches: int = 30) -> Dict[str, Any]:
    rows = [
        {
            "file": f"src/mod{i}.py",
            "line": 10 + i,
            "text": f"    {query}(self): " + "y" * 60,
            "symbol": f"Cls{i}.run",
        }
        for i in range(matches)
    ]
    return {"query": query, "path": ".", "count": matches, "truncated": True, "matches": rows}


def _run_command_payload(command: str, out_lines: int = 400, failed: bool = False) -> Dict[str, Any]:
    stdout = "\n".join(f"out line {i}" for i in range(out_lines))
    if failed:
        return {
            "command": command,
            "stdout": stdout,
            "stderr": "E   AssertionError: kwarg_marker_zzz at tests/test_x.py:42",
            "exit_code": 1,
            "success": False,
            "timed_out": False,
            "outcome": "command_failure",
            "duration": 0.5,
        }
    return {
        "command": command,
        "stdout": stdout,
        "stderr": "",
        "exit_code": 0,
        "success": True,
        "timed_out": False,
        "outcome": "success",
        "duration": 1.0,
    }


def _edit_payload(path: str = "src/big.py", noise: int = 0) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "path": path,
        "replaced": 1,
        "edited": True,
        "changed_start_line": 30,
        "changed_end_line": 34,
        "total_lines": 240,
    }
    if noise:
        payload["note"] = "z" * noise
    return payload


def _atlas_payload(results: int = 12) -> Dict[str, Any]:
    rows = [
        {
            "kind": "class" if i % 2 == 0 else "function",
            "name": f"Entity{i}",
            "qualified": f"Mod{i}.Entity{i}",
            "file": f"src/mod{i}.py",
            "line_start": 1 + i,
            "line_end": 100 + i,
            "related": [{"edge": "calls", "name": f"callee{i}", "file": f"src/mod{i}.py"}],
        }
        for i in range(results)
    ]
    return {
        "query": "Entity",
        "kind": "any",
        "total": results,
        "returned": results,
        "truncated": False,
        "map_status": "fresh",
        "results": rows,
    }


def _rig_payload(results: int = 10) -> Dict[str, Any]:
    rows = [
        {
            "kind": "function",
            "name": f"fn{i}",
            "file": f"src/m{i}.py",
            "line_start": i + 1,
            "line_end": i + 20,
        }
        for i in range(results)
    ]
    payload = {
        "query": "fn",
        "relation": "callers",
        "kind": "any",
        "total": results,
        "returned": results,
        "truncated": False,
        "map_status": "stale",
        "results": rows,
    }
    payload["hint"] = "map mungkin belum merepresentasikan source"
    return payload


def _append_round(
    history: ConversationHistory,
    index: int,
    tool: str,
    arguments: Dict[str, Any],
    content: str,
) -> ToolCall:
    call = ToolCall.create(tool, arguments, id=f"call-{index}")
    history.append_assistant_message(content=None, tool_calls=[call])
    history.append_tool_result(call.id, tool, content)
    return call


def _tool_messages(history: ConversationHistory) -> List[Any]:
    return [m for m in history.messages if m.role == "tool"]


def _assert_protocol_valid(messages: List[Any]) -> None:
    assistant_ids = [
        tc.id for m in messages if m.role == "assistant" for tc in (m.tool_calls or [])
    ]
    tool_ids = [m.tool_call_id for m in messages if m.role == "tool"]
    assert all(tid in assistant_ids for tid in tool_ids), "tool result yatim"
    for message in (m.to_provider_dict() for m in messages):
        assert "role" in message
        if message["role"] == "tool":
            assert message["tool_call_id"] in assistant_ids


# --------------------------------------------------------------------------- #
# A. Hasil read_file besar
# --------------------------------------------------------------------------- #
def test_a_large_read_file_result() -> None:
    raw = json.dumps(_read_file_payload())
    result = COMPACTOR.compact_result("read_file", raw)

    assert result.compact_chars < result.raw_chars, (result.raw_chars, result.compact_chars)
    assert MARKER in result.text
    assert "src/big.py" in result.text
    assert result.locators["path"] == "src/big.py"
    assert result.locators["start_line"] == 1 and result.locators["end_line"] == 240
    assert result.locators["symbol"] == "Cls.big"
    # Locator retrieval tersedia agar Agent bisa read_file ulang.
    assert "read_file(path='src/big.py'" in result.text
    # Idempoten: memadatkan ulang representasi yang sudah padat tidak mengubah.
    assert COMPACTOR.compact_result("read_file", result.text).text == result.text

    # Integrasi: hasil read_file lama dipadatkan di dalam history.
    history = ConversationHistory()
    history.append_system_message(SYSTEM_PROMPT)
    history.append_user_message("baca banyak file")
    raw_by_id: Dict[str, str] = {}
    for i in range(6):
        raw = json.dumps(_read_file_payload(f"src/f{i}.py"))
        call = _append_round(
            history, i, "read_file", {"path": f"src/f{i}.py"}, raw
        )
        raw_by_id[call.id] = raw
    compiled = history.compile_compacted_messages(1500)
    assert ConversationHistory.estimate_messages_tokens(compiled) <= 1500
    # Setiap tool result yang MASIH ADA dan dipadatkan tetap memuat path-nya.
    compacted = 0
    for message in compiled:
        if message.role != "tool":
            continue
        raw = raw_by_id.get(message.tool_call_id)
        assert raw is not None
        if (message.content or "") != raw:
            compacted += 1
            assert "src/f" in (message.content or "")
    assert compacted >= 1
    assert MARKER in " ".join(m.content or "" for m in compiled if m.role == "tool")
    _assert_protocol_valid(compiled)


# --------------------------------------------------------------------------- #
# B. Beberapa hasil read_file berurutan
# --------------------------------------------------------------------------- #
def test_b_multiple_sequential_read_file_results() -> None:
    history = ConversationHistory()
    history.append_system_message(SYSTEM_PROMPT)
    history.append_user_message("baca sepuluh file")
    raw_by_id: Dict[str, str] = {}
    for i in range(10):
        raw = json.dumps(_read_file_payload(f"src/f{i}.py"))
        call = _append_round(history, i, "read_file", {"path": f"src/f{i}.py"}, raw)
        raw_by_id[call.id] = raw
    before = history.estimate_tokens()
    compiled = history.compile_compacted_messages(2000)
    after = ConversationHistory.estimate_messages_tokens(compiled)

    assert after < before, (before, after)
    assert after <= 2000
    # Setiap tool result yang MASIH ADA dan dipadatkan tetap memuat path-nya.
    compacted_count = 0
    for message in compiled:
        if message.role != "tool":
            continue
        raw = raw_by_id.get(message.tool_call_id)
        assert raw is not None
        if (message.content or "") != raw:
            compacted_count += 1
            assert "src/f" in (message.content or "")
    assert compacted_count >= 1, "should compact at least one old read_file result"
    _assert_protocol_valid(compiled)


# --------------------------------------------------------------------------- #
# C. Hasil search_code
# --------------------------------------------------------------------------- #
def test_c_search_code_result() -> None:
    payload = _search_payload(query="def run", matches=30)
    raw = json.dumps(payload)
    result = COMPACTOR.compact_result("search_code", raw)

    assert result.compact_chars < result.raw_chars
    assert result.locators["query"] == "def run"
    assert "def run" in result.text
    # File + line + symbol tiap match dipertahankan (terbatas, deterministik).
    assert "src/mod0.py:10" in result.text
    assert "Cls0.run" in result.text
    assert "src/mod0.py" in result.locators.get("files", [])
    # Query tetap dapat diulang (retrieval).
    assert "search_code(query='def run'" in result.text
    # Jumlah total tetap terlihat walau hanya sebagian didaftar.
    assert "count: 30" in result.text


# --------------------------------------------------------------------------- #
# D. Hasil atlas_query / rig_query
# --------------------------------------------------------------------------- #
def test_d_atlas_rig_result() -> None:
    atlas_raw = json.dumps(_atlas_payload(12))
    atlas = COMPACTOR.compact_result("atlas_query", atlas_raw)
    assert atlas.compact_chars < atlas.raw_chars
    assert atlas.locators["query"] == "Entity"
    assert "src/mod0.py" in atlas.text
    assert "Entity0" in atlas.text
    assert "retrieval: atlas_query(query='Entity'" in atlas.text

    rig_raw = json.dumps(_rig_payload(10))
    rig = COMPACTOR.compact_result("rig_query", rig_raw)
    assert rig.compact_chars < rig.raw_chars
    assert rig.locators["query"] == "fn"
    assert rig.locators.get("relation") == "callers"
    assert "src/m0.py" in rig.text
    assert "stale" in rig.text  # map_status tetap terlihat
    assert "retrieval: rig_query(query='fn'" in rig.text


# --------------------------------------------------------------------------- #
# E. Output run_command besar
# --------------------------------------------------------------------------- #
def test_e_large_run_command_output() -> None:
    payload = _run_command_payload("python -m pytest -q", out_lines=600)
    raw = json.dumps(payload)
    result = COMPACTOR.compact_result("run_command", raw)

    assert result.compact_chars < result.raw_chars
    assert result.locators["command"] == "python -m pytest -q"
    assert result.locators["exit_code"] == 0
    assert "command: python -m pytest -q" in result.text
    assert "exit_code: 0" in result.text
    assert "success: True" in result.text
    assert "stdout" in result.text
    assert MARKER in result.text  # stdout besar dipadatkan, tapi terlihat

    # Integrasi history: output besar lama menyusut drastis.
    history = ConversationHistory()
    history.append_system_message(SYSTEM_PROMPT)
    history.append_user_message("jalankan test")
    for i in range(4):
        _append_round(
            history, i, "run_command", {"command": f"cmd {i}"},
            json.dumps(_run_command_payload(f"cmd {i}", out_lines=500)),
        )
    compiled = history.compile_compacted_messages(1200)
    assert ConversationHistory.estimate_messages_tokens(compiled) <= 1200
    _assert_protocol_valid(compiled)


# --------------------------------------------------------------------------- #
# F. Command gagal dengan stderr penting
# --------------------------------------------------------------------------- #
def test_f_failed_command_stderr_preserved() -> None:
    # Bentuk konten kegagalan command dari executor (string, bukan JSON).
    failure = (
        "Tool execution failed (outcome=command_failure).\n"
        "exit_code=1\n"
        "error=None\n"
        "stdout:\n" + "\n".join(f"noise {i}" for i in range(400)) + "\n"
        "stderr:\n"
        "E   AssertionError: kwarg_marker_zzz at tests/test_x.py:42\n"
        + "\n".join(f"E   extra context {i}" for i in range(200))
    )
    result = COMPACTOR.compact_result("run_command", failure)

    assert result.compact_chars < len(failure)
    assert "exit_code=1" in result.text
    assert "outcome=command_failure" in result.text or "Tool execution failed" in result.text
    # stderr PENTING tidak hilang.
    assert "kwarg_marker_zzz" in result.text
    assert "stderr:" in result.text

    # Integrasi history + protokol tetap valid.
    history = ConversationHistory()
    history.append_system_message(SYSTEM_PROMPT)
    history.append_user_message("jalankan test yang gagal")
    _append_round(history, 0, "run_command", {"command": "pytest"}, failure)
    compiled = history.compile_compacted_messages(600, keep_recent=0)
    compacted_tool = [m for m in compiled if m.role == "tool"]
    assert compacted_tool and "kwarg_marker_zzz" in (compacted_tool[-1].content or "")
    _assert_protocol_valid(compiled)


# --------------------------------------------------------------------------- #
# G. Hasil edit_file
# --------------------------------------------------------------------------- #
def test_g_edit_file_result() -> None:
    # Hasil edit kecil: TIDAK perlu dipadatkan -> tetap utuh (tanpa info hilang).
    small_raw = json.dumps(_edit_payload())
    small = COMPACTOR.compact_result("edit_file", small_raw)
    assert small.text == small_raw
    assert "changed_start_line" in small.text

    # Hasil edit dengan payload besar: dipadatkan struktur, info edit tetap ada.
    big_raw = json.dumps(_edit_payload(noise=3000))
    result = COMPACTOR.compact_result("edit_file", big_raw)
    assert result.compact_chars < result.raw_chars
    assert result.locators["path"] == "src/big.py"
    assert result.locators["changed_start_line"] == 30
    assert result.locators["changed_end_line"] == 34
    assert "path: src/big.py" in result.text
    assert "changed_lines: 30-34" in result.text
    assert "edited: True" in result.text
    assert "retrieval: read_file(path='src/big.py'" in result.text

    # Integrasi history: edit besar lama dipadatkan, protokol valid.
    history = ConversationHistory()
    history.append_system_message(SYSTEM_PROMPT)
    history.append_user_message("edit file")
    for i in range(3):
        _append_round(
            history, i, "edit_file", {"path": f"src/f{i}.py"},
            json.dumps(_edit_payload(f"src/f{i}.py", noise=2000)),
        )
    compiled = history.compile_compacted_messages(900)
    _assert_protocol_valid(compiled)


# --------------------------------------------------------------------------- #
# H. Beberapa round dengan tool result yang terus bertambah (end-to-end)
# --------------------------------------------------------------------------- #
def _tool_call_dict(call_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _tool_turn(text: str, calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "choices": [
            {"message": {"content": text, "tool_calls": calls}, "finish_reason": "tool_calls"}
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
        self.requests.append([dict(m) if isinstance(m, dict) else m for m in (messages or [])])
        idx = self.calls
        self.calls += 1
        raw = self.script[idx] if idx < len(self.script) else _final_turn("(default)")
        return GenerateResult(text="", model="scripted-model", provider=self.name, raw=raw)


class FakeTool(BaseTool):
    """Tool palsu dengan payload yang bertambah besar tiap pemanggilan."""

    def __init__(self, name: str, payload_fn: Callable[[int], Dict[str, Any]]) -> None:
        self.name = name
        self.description = f"fake {name}"
        self.input_schema = {"type": "object", "properties": {}, "required": []}
        self._payload_fn = payload_fn
        self.calls = 0

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        self.calls += 1
        return self._payload_fn(self.calls)


def _build_growing_registry() -> Dict[str, FakeTool]:
    tools = {
        "read_file": FakeTool("read_file", lambda n: _read_file_payload("src/r.py", lines=80 + n * 120)),
        "search_code": FakeTool("search_code", lambda n: _search_payload("q", matches=10 + n * 8)),
        "run_command": FakeTool("run_command", lambda n: _run_command_payload(f"cmd {n}", out_lines=80 + n * 200)),
        "edit_file": FakeTool("edit_file", lambda n: _edit_payload("src/r.py", noise=n * 400)),
    }
    return tools


def test_h_multiple_rounds_growing_results() -> None:
    rounds = 16
    tools = _build_growing_registry()
    order = ["read_file", "search_code", "run_command", "edit_file"]
    script = [
        _tool_turn(f"round {i}", [_tool_call_dict(f"c{i}", order[i % 4], {"path": "src/r.py", "query": "q"})])
        for i in range(rounds)
    ]
    script.append(_final_turn("Selesai setelah banyak round."))

    provider = ScriptedProvider(script)
    registry = ToolRegistry()
    for tool in tools.values():
        registry.register(tool)
    events: List[Dict[str, Any]] = []

    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="scripted-model"),
        system_prompt=SYSTEM_PROMPT,
        use_continuous_loop=True,
        context_budget_tokens=1500,
        event_sink=lambda et, payload: events.append({"type": et, **payload}),
    )
    result = orchestrator.run("kerjakan task panjang dengan banyak tool")

    assert result.status == AgentStatus.DONE, result.error
    assert result.result == "Selesai setelah banyak round."
    # Semua tool call tetap dieksekusi (loop tidak rusak karena compaction).
    assert sum(t.calls for t in tools.values()) == rounds
    assert provider.calls == rounds + 1

    reqs = [e for e in events if e["type"] == "provider_request"]
    assert reqs
    assert any(e.get("context_compacted") for e in reqs)
    # Compaction hasil tool benar-benar terjadi (statistik observability).
    assert any(e.get("context_tool_compacted", 0) > 0 for e in reqs)
    last = reqs[-1]
    assert last.get("context_tool_raw_chars", 0) > last.get("context_tool_compacted_chars", 0)
    # Request terakhir BOUNDED (jauh lebih kecil dari akumulasi mentah).
    last_request = provider.requests[-1]
    last_chars = sum(len(str(m.get("content") or "")) for m in last_request)
    assert last_chars < 60_000, last_chars
    # Protokol tetap valid pada request terakhir.
    assistant_ids = {
        tc.get("id")
        for m in last_request
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    }
    for m in last_request:
        if m.get("role") == "tool":
            assert m.get("tool_call_id") in assistant_ids


# --------------------------------------------------------------------------- #
# I. Agent masih dapat mengambil ulang detail yang sudah dikompak
# --------------------------------------------------------------------------- #
def test_i_details_retrievable_after_compaction() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        lines = [f"def fn_{i}(x):\n    return x + {i}\n" for i in range(400)]
        (root / "big.py").write_text("\n".join(lines), encoding="utf-8")

        reader = ReadFileTool(root=root)
        original = reader.execute(path="big.py", start_line=40, end_line=90)
        raw = json.dumps(original)
        assert len(raw) > 1500

        # Padatkan (seperti hasil yang sudah stale) & ambil locator.
        result = COMPACTOR.compact_result("read_file", raw, head_chars=0, tail_chars=0)
        locator = result.locators
        assert locator["path"] == "big.py"
        assert locator["start_line"] == 40 and locator["end_line"] == 90
        assert "read_file(path='big.py', start_line=40, end_line=90)" in result.text

        # Agent MENGAMBIL ULANG detail memakai locator yang dipertahankan.
        again = reader.execute(path=locator["path"], start_line=locator["start_line"], end_line=locator["end_line"])
        assert again["content"] == original["content"]
        assert again["total_lines"] == original["total_lines"]


# --------------------------------------------------------------------------- #
# J. Protokol tool tetap valid
# --------------------------------------------------------------------------- #
def test_j_tool_protocol_valid() -> None:
    history = ConversationHistory()
    history.append_system_message(SYSTEM_PROMPT)
    history.append_user_message("protokol")
    for i in range(8):
        _append_round(
            history, i, "search_code", {"query": "def run"},
            json.dumps(_search_payload("def run", matches=40)),
        )
    compiled = history.compile_compacted_messages(1500)
    _assert_protocol_valid(compiled)

    # Tiap tool result masih punya pasangan assistant(tool_calls); tidak ada
    # tool result di awal; role valid.
    assert compiled[0].role == "system"
    assistant_ids = {
        tc.id for m in compiled if m.role == "assistant" for tc in (m.tool_calls or [])
    }
    for message in compiled:
        if message.role == "tool":
            assert message.tool_call_id in assistant_ids
            assert message.name  # nama tool dipertahankan untuk retrieval


# --------------------------------------------------------------------------- #
# K. Task 1 context compaction tetap PASS
# --------------------------------------------------------------------------- #
def _run_module_tests(module_name: str) -> List[str]:
    import importlib

    module = importlib.import_module(module_name)
    names = sorted(
        name
        for name in dir(module)
        if name.startswith("test_") and callable(getattr(module, name))
    )
    assert names, f"tidak ada test di {module_name}"
    for name in names:
        getattr(module, name)()
    return names


def test_k_task1_context_compaction_still_passes() -> None:
    names = _run_module_tests("tests.test_context_compaction")
    assert "test_f_agent_continues_after_compaction" in names


# --------------------------------------------------------------------------- #
# L. Existing Agent/continuous-loop regression tetap PASS
# --------------------------------------------------------------------------- #
_EXISTING_VERIFIERS = (
    "scripts/check_conversation_history.py",
    "scripts/check_continuous_loop.py",
    "scripts/check_tool_call_execution.py",
    "scripts/check_parallel_tools.py",
)


def test_l_existing_regression_verifiers_pass() -> None:
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    for rel in _EXISTING_VERIFIERS:
        proc = subprocess.run(
            [sys.executable, rel],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"{rel} gagal:\n{proc.stdout[-800:]}\n{proc.stderr[-800:]}"


# --------------------------------------------------------------------------- #
# Standalone runner (tanpa pytest) + benchmark
# --------------------------------------------------------------------------- #
def _estimate_provider_tokens(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        total += max(1, len(str(message.get("content") or "")) // 4)
        total += len(str(message.get("name") or "")) // 4
        for tc in message.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            total += len(str(fn.get("arguments") or "")) // 4
            total += len(str(fn.get("name") or "")) // 4
        total += 4
    return total


def _benchmark_scenario() -> Dict[str, Any]:
    """Skenario yang SAMA, dijalankan SEBELUM (tanpa compaction) dan SESUDAH."""
    rounds = 20
    order = ["read_file", "search_code", "run_command", "edit_file"]

    def _run(budget: Optional[int]) -> Dict[str, Any]:
        tools = _build_growing_registry()
        script = [
            _tool_turn(
                f"round {i}",
                [_tool_call_dict(f"c{i}", order[i % 4], {"path": "src/r.py", "query": "q"})],
            )
            for i in range(rounds)
        ]
        script.append(_final_turn("Selesai."))
        provider = ScriptedProvider(script)
        registry = ToolRegistry()
        for tool in tools.values():
            registry.register(tool)
        orchestrator = AgentOrchestrator(
            provider=provider,
            executor=ToolExecutor(registry=registry),
            options=GenerateOptions(model="scripted-model"),
            system_prompt=SYSTEM_PROMPT,
            use_continuous_loop=True,
            context_budget_tokens=budget,
        )
        result = orchestrator.run("task benchmark")
        total_input_tokens = sum(_estimate_provider_tokens(req) for req in provider.requests)
        return {
            "status": result.status.value,
            "result": result.result,
            "rounds": provider.calls,
            "tool_calls": sum(t.calls for t in tools.values()),
            "total_input_tokens": total_input_tokens,
        }

    before = _run(None)
    after = _run(1800)
    return {"before": before, "after": after}


def benchmark() -> Dict[str, Any]:
    data = _benchmark_scenario()
    before, after = data["before"], data["after"]
    assert before["result"] == after["result"], (before, after)
    assert before["rounds"] == after["rounds"], (before, after)
    assert before["tool_calls"] == after["tool_calls"], (before, after)
    assert after["total_input_tokens"] < before["total_input_tokens"], (before, after)
    return data


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
    try:
        data = benchmark()
        print(
            "[BENCH] tokens before={before[total_input_tokens]} after={after[total_input_tokens]} "
            "rounds={before[rounds]} tool_calls={before[tool_calls]}".format(**data)
        )
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"[FAIL] benchmark: {type(exc).__name__}: {exc}")
    print()
    if failures:
        print(f"[FAILED] {failures} test gagal")
        return 1
    print(f"[OK] {len(checks)} test lulus (tool result compaction)")
    return 0


def main() -> int:
    """Entry point untuk verifier `scripts/check_tool_result_compaction.py`."""
    return _standalone()


if __name__ == "__main__":
    raise SystemExit(_standalone())
