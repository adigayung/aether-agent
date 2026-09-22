"""Verifikasi Parallel Tool Execution (Tool Execution Coordinator).

Menguji bahwa beberapa ToolCall dari SATU response LLM dieksekusi sebagai batch:
read-only paralel, write independen paralel, konflik diserialisasi, dependency
tidak dilanggar, hasil tetap terurut deterministik, error terisolasi, workspace
boundary tetap berlaku, dan continuous loop memakai coordinator yang sama.

Deterministik, tanpa network/API: memakai fake tool in-memory + provider skrip.

Jalankan:
    python scripts/check_parallel_tools.py
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from collections import defaultdict
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
from agent_ai.core.tool_coordinator import (  # noqa: E402
    ToolCategory,
    build_plan,
    plan_groups,
)
from agent_ai.core.types import ToolCall, ToolResultStatus  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.tools.base import BaseTool, ToolExecutionError  # noqa: E402
from agent_ai.tools.registry import ToolRegistry, build_registry  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "parallel_tools_fixture"


# --------------------------------------------------------------------------- #
# Probe concurrency + fake tools
# --------------------------------------------------------------------------- #
class ConcurrencyProbe:
    """Pelacak jumlah eksekusi tool yang berjalan bersamaan (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.started: List[str] = []
        self.finished: List[str] = []
        self.counts: Dict[str, int] = defaultdict(int)

    def begin(self, tag: str) -> None:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.append(tag)
            self.counts[tag] += 1

    def end(self, tag: str) -> None:
        with self._lock:
            self.active -= 1
            self.finished.append(tag)


class _ProbeTool(BaseTool):
    """Fake tool: mencatat concurrency, delay opsional, gagal bila diminta."""

    def __init__(
        self,
        probe: ConcurrencyProbe,
        *,
        delays: Optional[Dict[str, float]] = None,
        fail_tags: Optional[List[str]] = None,
        key: str = "path",
    ) -> None:
        self.probe = probe
        self.delays = delays or {}
        self.fail_tags = set(fail_tags or [])
        self.key = key

    def _tag(self, arguments: Dict[str, Any]) -> str:
        return str(arguments.get(self.key) or arguments.get("command") or "?")

    def execute(self, **arguments: Any) -> Any:
        tag = self._tag(arguments)
        self.probe.begin(tag)
        try:
            time.sleep(self.delays.get(tag, 0.02))
            if tag in self.fail_tags:
                raise ToolExecutionError(f"probe failure: {tag}")
            return self._result(tag, arguments)
        finally:
            self.probe.end(tag)

    def _result(self, tag: str, arguments: Dict[str, Any]) -> Any:  # pragma: no cover
        raise NotImplementedError


class ProbeReadTool(_ProbeTool):
    name = "read_file"
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def _result(self, tag: str, arguments: Dict[str, Any]) -> Any:
        return {"path": tag, "content": f"content-of-{tag}"}


class ProbeWriteTool(_ProbeTool):
    name = "write_file"
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }

    def _result(self, tag: str, arguments: Dict[str, Any]) -> Any:
        return {"path": tag, "written": True, "content": arguments.get("content")}


class ProbeCommandTool(_ProbeTool):
    name = "run_command"
    input_schema = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }

    def _result(self, tag: str, arguments: Dict[str, Any]) -> Any:
        return {"command": tag, "outcome": "success", "success": True, "exit_code": 0}


def _probe_registry(probe: ConcurrencyProbe, **kwargs: Any) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ProbeReadTool(probe, **kwargs))
    reg.register(ProbeWriteTool(probe, **kwargs))
    reg.register(ProbeCommandTool(probe, **kwargs))
    return reg


def _call(name: str, arguments: Dict[str, Any], call_id: str) -> ToolCall:
    return ToolCall.create(name, arguments, id=call_id)


def _groups_of(calls: List[ToolCall]) -> List[List[int]]:
    return plan_groups(build_plan(calls))


def _categories(calls: List[ToolCall]) -> List[str]:
    return [plan.category.value for plan in build_plan(calls)]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_01_parallel_reads() -> None:
    probe = ConcurrencyProbe()
    executor = ToolExecutor(registry=_probe_registry(probe))
    calls = [
        _call("read_file", {"path": "a.py"}, "id-a"),
        _call("read_file", {"path": "b.py"}, "id-b"),
        _call("read_file", {"path": "c.py"}, "id-c"),
    ]
    assert _groups_of(calls) == [[0, 1, 2]], _groups_of(calls)
    assert _categories(calls) == ["read", "read", "read"]

    batch = executor.execute_tool_calls(calls)
    assert [p.status for p in batch.payloads] == [ToolResultStatus.SUCCESS] * 3
    assert probe.max_active >= 2, f"read tidak paralel (max_active={probe.max_active})"
    assert [p.tool_call_id for p in batch.payloads] == ["id-a", "id-b", "id-c"]
    print(f"[1] parallel reads OK (max_active={probe.max_active}, 1 group)")


def test_02_parallel_independent_writes() -> None:
    probe = ConcurrencyProbe()
    executor = ToolExecutor(registry=_probe_registry(probe))
    calls = [
        _call("write_file", {"path": "src/a.py", "content": "A"}, "w-a"),
        _call("write_file", {"path": "src/b.py", "content": "B"}, "w-b"),
        _call("write_file", {"path": "tests/t.py", "content": "T"}, "w-t"),
    ]
    assert _groups_of(calls) == [[0, 1, 2]]
    assert _categories(calls) == ["write", "write", "write"]

    batch = executor.execute_tool_calls(calls)
    assert all(p.is_success for p in batch.payloads)
    assert probe.max_active >= 2, f"write independen tidak paralel ({probe.max_active})"
    print(f"[2] parallel independent writes OK (max_active={probe.max_active})")


def test_03_same_file_writes_sequential() -> None:
    probe = ConcurrencyProbe()
    executor = ToolExecutor(registry=_probe_registry(probe))
    calls = [
        _call("write_file", {"path": "same.py", "content": "1"}, "s-1"),
        _call("write_file", {"path": "same.py", "content": "2"}, "s-2"),
    ]
    assert _groups_of(calls) == [[0], [1]], _groups_of(calls)

    batch = executor.execute_tool_calls(calls)
    assert all(p.is_success for p in batch.payloads)
    assert probe.max_active == 1, f"write file sama harus sequential ({probe.max_active})"
    assert probe.finished == ["same.py", "same.py"]
    print("[3] same-file writes sequential OK (max_active=1, urutan terjaga)")


def test_04_read_and_read() -> None:
    calls = [
        _call("read_file", {"path": "x.py"}, "r-x"),
        _call("read_file", {"path": "y.py"}, "r-y"),
        _call("read_file", {"path": "x.py"}, "r-x2"),
    ]
    # read vs read (termasuk file yang sama) TIDAK berkonflik -> satu group.
    assert _groups_of(calls) == [[0, 1, 2]], _groups_of(calls)
    print("[4] read + read parallel OK (read tidak saling berkonflik)")


def test_05_write_then_dependent_execution() -> None:
    calls = [
        _call("write_file", {"path": "package.json", "content": "{}"}, "w-pkg"),
        _call("run_command", {"command": "npm install"}, "c-install"),
        _call("run_command", {"command": "npm run build"}, "c-build"),
    ]
    assert _groups_of(calls) == [[0], [1], [2]], _groups_of(calls)
    assert _categories(calls) == ["write", "exclusive", "exclusive"]

    probe = ConcurrencyProbe()
    executor = ToolExecutor(registry=_probe_registry(probe))
    batch = executor.execute_tool_calls(calls)
    assert all(p.is_success for p in batch.payloads)
    assert probe.max_active == 1, probe.max_active
    # Dependency tidak dilanggar: write sebelum command, urutan input terjaga.
    assert probe.started == ["package.json", "npm install", "npm run build"]
    print("[5] write -> execution sequential OK (dependency tidak dilanggar)")


def test_06_independent_execution_conservative() -> None:
    calls = [
        _call("run_command", {"command": "python script_a.py"}, "c-a"),
        _call("run_command", {"command": "python script_b.py"}, "c-b"),
    ]
    # AETHER tidak menebak dependency antar command -> conservative sequential.
    assert _groups_of(calls) == [[0], [1]], _groups_of(calls)

    probe = ConcurrencyProbe()
    executor = ToolExecutor(registry=_probe_registry(probe))
    batch = executor.execute_tool_calls(calls)
    assert all(p.is_success for p in batch.payloads)
    assert probe.max_active == 1, "command harus conservative (sequential)"
    assert probe.counts["python script_a.py"] == 1
    assert probe.counts["python script_b.py"] == 1
    print("[6] independent execution OK (conservative: tetap terserialisasi)")


def test_07_failure_isolation() -> None:
    probe = ConcurrencyProbe()
    executor = ToolExecutor(registry=_probe_registry(probe, fail_tags=["boom.txt"]))
    calls = [
        _call("read_file", {"path": "boom.txt"}, "f-boom"),
        _call("read_file", {"path": "ok1.txt"}, "f-ok1"),
        _call("read_file", {"path": "ok2.txt"}, "f-ok2"),
    ]
    batch = executor.execute_tool_calls(calls)
    statuses = [p.status for p in batch.payloads]
    assert statuses[0] == ToolResultStatus.ERROR, statuses
    assert statuses[1] == ToolResultStatus.SUCCESS and statuses[2] == ToolResultStatus.SUCCESS
    # Independent tools tetap selesai walau satu gagal.
    assert probe.counts["ok1.txt"] == 1 and probe.counts["ok2.txt"] == 1
    assert [p.tool_call_id for p in batch.payloads] == ["f-boom", "f-ok1", "f-ok2"]
    print("[7] failure isolation OK (1 gagal, 2 independen tetap sukses)")


def test_08_dependency_failure_blocks_dependent() -> None:
    probe = ConcurrencyProbe()
    executor = ToolExecutor(registry=_probe_registry(probe, fail_tags=["package.json"]))
    calls = [
        _call("write_file", {"path": "package.json", "content": "{}"}, "d-w"),
        _call("run_command", {"command": "npm install"}, "d-c"),
    ]
    assert _groups_of(calls) == [[0], [1]]
    batch = executor.execute_tool_calls(calls)
    assert batch.payloads[0].status == ToolResultStatus.ERROR
    assert batch.payloads[1].status == ToolResultStatus.ERROR
    assert "Blocked" in str(batch.payloads[1].to_content())
    # Dependent TIDAK dijalankan.
    assert probe.counts["npm install"] == 0, probe.counts
    print("[8] dependency failure OK (parent gagal -> dependent BLOCKED)")


def test_09_result_ordering() -> None:
    probe = ConcurrencyProbe()
    # Path pertama paling lambat -> completion order != input order.
    executor = ToolExecutor(
        registry=_probe_registry(probe, delays={"slow.txt": 0.20, "mid.txt": 0.06, "fast.txt": 0.01})
    )
    calls = [
        _call("read_file", {"path": "slow.txt"}, "o-1"),
        _call("read_file", {"path": "mid.txt"}, "o-2"),
        _call("read_file", {"path": "fast.txt"}, "o-3"),
    ]
    batch = executor.execute_tool_calls(calls)
    assert probe.max_active >= 2
    # Mapping tetap benar walau urutan selesai berbeda.
    assert [p.tool_call_id for p in batch.payloads] == ["o-1", "o-2", "o-3"]
    assert [p.output["path"] for p in batch.payloads] == ["slow.txt", "mid.txt", "fast.txt"]
    assert probe.finished != ["slow.txt", "mid.txt", "fast.txt"], (
        f"completion order tidak berbeda dari input (delay tidak efektif): {probe.finished}"
    )
    print(f"[9] result ordering OK (completion={probe.finished} -> mapping urut input)")


def test_10_workspace_boundary() -> None:
    ok_file = FIXTURE / "ok.txt"
    ok_file.write_text("hello", encoding="utf-8")
    escape = FIXTURE.parent / "escape_parallel.txt"
    if escape.exists():
        escape.unlink()

    executor = ToolExecutor(build_registry(root=FIXTURE))
    calls = [
        _call("read_file", {"path": "ok.txt"}, "b-ok"),
        _call("read_file", {"path": "../escape_parallel.txt"}, "b-read-escape"),
        _call("write_file", {"path": "../escape_parallel.txt", "content": "x"}, "b-write-escape"),
    ]
    batch = executor.execute_tool_calls(calls)
    assert batch.payloads[0].is_success, batch.payloads[0].to_content()
    assert batch.payloads[1].status == ToolResultStatus.ERROR, batch.payloads[1].to_content()
    assert batch.payloads[2].status == ToolResultStatus.ERROR, batch.payloads[2].to_content()
    assert not escape.exists(), "workspace boundary bypassed!"
    print("[10] workspace boundary OK (path escape ditolak di dalam batch paralel)")


def test_11_single_tool_call_unchanged() -> None:
    probe = ConcurrencyProbe()
    registry = _probe_registry(probe)
    executor = ToolExecutor(registry=registry)
    call = _call("read_file", {"path": "single.txt"}, "single-1")

    direct = executor.execute_tool_call(call)
    batch = executor.execute_tool_calls([call])
    assert len(batch.payloads) == 1
    payload = batch.payloads[0]
    assert payload.status == direct.status == ToolResultStatus.SUCCESS
    assert payload.tool_call_id == direct.tool_call_id == "single-1"
    assert payload.tool_name == direct.tool_name == "read_file"
    assert payload.output == direct.output
    assert probe.max_active == 1
    print("[11] single tool call OK (hasil identik dengan execute_tool_call)")


def test_12_max_parallel_limit() -> None:
    probe = ConcurrencyProbe()
    executor = ToolExecutor(registry=_probe_registry(probe))
    calls = [_call("read_file", {"path": f"f{i}.py"}, f"lim-{i}") for i in range(6)]
    batch = executor.execute_tool_calls(calls, max_parallel=2)
    assert all(p.is_success for p in batch.payloads)
    assert probe.max_active <= 2, f"concurrency limit dilanggar: {probe.max_active}"
    assert len(batch.payloads) == 6
    print(f"[12] concurrency limit OK (max_parallel=2 -> max_active={probe.max_active})")


def test_13_cancellation_propagates() -> None:
    probe = ConcurrencyProbe()
    executor = ToolExecutor(registry=_probe_registry(probe))
    calls = [
        _call("run_command", {"command": "first"}, "cx-1"),
        _call("run_command", {"command": "second"}, "cx-2"),
    ]
    state = {"cancelled": False}

    def on_complete(_call_obj: ToolCall, _payload: Any) -> None:
        state["cancelled"] = True

    batch = executor.execute_tool_calls(
        calls, on_complete=on_complete, cancel_check=lambda: state["cancelled"]
    )
    assert batch.cancelled is True
    assert probe.counts["first"] == 1
    assert probe.counts["second"] == 0, "tool kedua dijalankan setelah cancel"
    assert batch.payloads[0] is not None
    assert batch.payloads[1] is None
    print("[13] cancellation OK (tool berikutnya tidak dijalankan)")


# --------------------------------------------------------------------------- #
# Continuous loop
# --------------------------------------------------------------------------- #
def _tool_call_payload(call_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _tool_turn(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "choices": [
            {"message": {"content": "", "tool_calls": calls}, "finish_reason": "tool_calls"}
        ]
    }


def _final_turn(text: str) -> Dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


def _msg_to_dict(message: Any) -> Dict[str, Any]:
    if isinstance(message, dict):
        return dict(message)
    return {"role": getattr(message, "role", ""), "content": getattr(message, "content", "")}


class ScriptedProvider(OpenAICompatibleProvider):
    name = "scripted-parallel"

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
        self.requests.append([_msg_to_dict(m) for m in (messages or [])])
        idx = self.calls
        self.calls += 1
        raw = self.script[idx] if idx < len(self.script) else _final_turn("(default)")
        return GenerateResult(text="", model="scripted-model", provider=self.name, raw=raw)


def test_14_continuous_loop_parallel() -> None:
    probe = ConcurrencyProbe()
    executor = ToolExecutor(registry=_probe_registry(probe, delays={"p1.txt": 0.12}))
    provider = ScriptedProvider(
        [
            _tool_turn(
                [
                    _tool_call_payload("p-1", "read_file", {"path": "p1.txt"}),
                    _tool_call_payload("p-2", "read_file", {"path": "p2.txt"}),
                    _tool_call_payload("p-3", "read_file", {"path": "p3.txt"}),
                ]
            ),
            _final_turn("Selesai membaca 3 file."),
        ]
    )
    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=executor,
        options=GenerateOptions(model="scripted-model"),
        use_continuous_loop=True,
    )
    result = orchestrator.run("baca p1, p2, p3")

    assert result.status == AgentStatus.DONE, result.error
    assert provider.calls == 2, provider.calls
    assert probe.max_active >= 2, f"continuous loop tidak paralel ({probe.max_active})"
    assert result.iterations == 3, result.iterations

    second = provider.requests[1]
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert [m.get("tool_call_id") for m in tool_msgs] == ["p-1", "p-2", "p-3"], tool_msgs
    # Hasil tool TIDAK dijejalkan sebagai pesan user.
    joined_user = " ".join(str(m.get("content")) for m in second if m.get("role") == "user")
    assert "content-of-p1.txt" not in joined_user
    print(
        f"[14] continuous loop OK (max_active={probe.max_active}, "
        "3 hasil role=tool urut sesuai input)"
    )


def test_15_read_write_same_path_serialized() -> None:
    probe = ConcurrencyProbe()
    executor = ToolExecutor(registry=_probe_registry(probe))
    calls = [
        _call("write_file", {"path": "auth.py", "content": "x"}, "rw-1"),
        _call("read_file", {"path": "auth.py"}, "rw-2"),
    ]
    # write lalu read pada file SAMA -> konflik -> group terpisah (sequential),
    # urutan input terjaga (write dulu, baru read).
    assert _groups_of(calls) == [[0], [1]], _groups_of(calls)
    batch = executor.execute_tool_calls(calls)
    assert all(p.is_success for p in batch.payloads)
    assert probe.max_active == 1, probe.max_active
    assert probe.finished == ["auth.py", "auth.py"]
    print("[15] read+write same path OK (sequential, urutan input terjaga)")


def test_16_mixed_batch_grouping() -> None:
    calls = [
        _call("read_file", {"path": "A.py"}, "m-1"),
        _call("read_file", {"path": "B.py"}, "m-2"),
        _call("write_file", {"path": "C.py", "content": "x"}, "m-3"),
        _call("read_file", {"path": "D.py"}, "m-4"),
        _call("run_command", {"command": "pytest"}, "m-5"),
    ]
    assert _categories(calls) == ["read", "read", "write", "read", "exclusive"]
    # read A/B/D + write C (target berbeda) -> satu group; command -> group sendiri.
    assert _groups_of(calls) == [[0, 1, 2, 3], [4]], _groups_of(calls)
    print("[16] mixed batch grouping OK (read+read+write+read paralel, command barrier)")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)
    try:
        if DUMMY_ROOT.exists() and not any(DUMMY_ROOT.iterdir()):
            DUMMY_ROOT.rmdir()
    except OSError:
        pass


def main() -> int:
    print("=== Verifikasi Parallel Tool Execution (Tool Execution Coordinator) ===")
    setup_fixture()
    try:
        test_01_parallel_reads()
        test_02_parallel_independent_writes()
        test_03_same_file_writes_sequential()
        test_04_read_and_read()
        test_05_write_then_dependent_execution()
        test_06_independent_execution_conservative()
        test_07_failure_isolation()
        test_08_dependency_failure_blocks_dependent()
        test_09_result_ordering()
        test_10_workspace_boundary()
        test_11_single_tool_call_unchanged()
        test_12_max_parallel_limit()
        test_13_cancellation_propagates()
        test_14_continuous_loop_parallel()
        test_15_read_write_same_path_serialized()
        test_16_mixed_batch_grouping()
    finally:
        teardown_fixture()
    print()
    print("[OK] Parallel tool execution bekerja sesuai kontrak (16/16).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
