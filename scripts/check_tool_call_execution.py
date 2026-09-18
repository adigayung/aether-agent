"""Verifikasi ToolExecutor.execute_tool_call() (Task 2: Native Tool Calling).

Menguji alur: ToolCall -> ToolExecutor -> ToolResultPayload, dengan fake tool
in-memory (tanpa menyentuh network, workspace, atau project nyata).

Yang diverifikasi:
    - Success -> ToolResultPayload(status=success) + tool_call_id/tool_name.
    - Tool error (ToolValidationError) -> status=error, content string.
    - Exception runtime -> status=error, content string (loop tidak putus).
    - Permission ditolak -> status=error dengan pesan "Permission Denied: ...".
    - Argumen JSON string di-parse; JSON tidak valid -> status=error.
    - Command gagal (exit_code != 0) -> status=error + detail stdout/stderr.
    - Unknown tool -> status=error.
    - ConversationHistory mengubah payload -> message role="tool".
    - execute_action() lama tetap berjalan (regresi).

Jalankan:
    python scripts/check_tool_call_execution.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core import (  # noqa: E402
    ConversationHistory,
    ToolCall,
    ToolResultPayload,
    ToolResultStatus,
)
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.response import FinishReason, LLMAction, LLMResponse  # noqa: E402
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402


# --------------------------------------------------------------------------- #
# Fake tools (in-memory)
# --------------------------------------------------------------------------- #
class _EchoTool(BaseTool):
    name = "echo"
    input_schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    def execute(self, **arguments):
        return {"echo": arguments.get("text")}


class _FailCommandTool(BaseTool):
    name = "run_command"
    input_schema = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}

    def execute(self, **arguments):
        return {
            "command": arguments.get("command"),
            "stdout": "stdout-line",
            "stderr": "stderr-line",
            "exit_code": 2,
            "success": False,
            "outcome": "command_failure",
        }


class _BoomTool(BaseTool):
    name = "boom"
    input_schema = {"type": "object", "properties": {}}

    def execute(self, **arguments):
        raise RuntimeError("kaboom")


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_EchoTool())
    reg.register(_FailCommandTool())
    reg.register(_BoomTool())
    return reg


def _decision(allowed: bool) -> SimpleNamespace:
    return SimpleNamespace(allowed=allowed, reason="policy-test")


class _DenyManager:
    def check(self, action, arguments):
        return _decision(False)


class _AllowManager:
    def check(self, action, arguments):
        return _decision(True)


def main() -> int:
    print("=== Verifikasi ToolExecutor.execute_tool_call() ===")
    executor = ToolExecutor(registry=_registry())

    # 1) Success.
    call = ToolCall.create("echo", {"text": "halo"})
    payload = executor.execute_tool_call(call)
    print(f"[1] success        -> status={payload.status.value} id={payload.tool_call_id}")
    assert isinstance(payload, ToolResultPayload)
    assert payload.status == ToolResultStatus.SUCCESS and payload.is_success
    assert payload.tool_call_id == call.id, "tool_call_id harus ikut payload"
    assert payload.tool_name == "echo"
    assert '"echo"' in payload.to_content()
    print("OK: success -> payload success + tool_call_id/tool_name tersedia")

    # 2) Tool error (validasi) -> status=error, bukan exception.
    bad = ToolCall.create("echo", {})  # 'text' wajib
    payload_err = executor.execute_tool_call(bad)
    print(f"[2] tool_error     -> status={payload_err.status.value} content={payload_err.to_content()!r}")
    assert payload_err.status == ToolResultStatus.ERROR
    assert isinstance(payload_err.to_content(), str) and payload_err.to_content()
    assert payload_err.tool_call_id == bad.id
    print("OK: ToolError -> status=error (loop tidak putus)")

    # 3) Exception runtime -> status=error.
    boom = ToolCall.create("boom", {})
    payload_boom = executor.execute_tool_call(boom)
    print(f"[3] exception      -> {payload_boom.to_content()!r}")
    assert payload_boom.status == ToolResultStatus.ERROR
    assert "RuntimeError" in payload_boom.to_content()
    print("OK: exception runtime -> status=error dengan info sebagai string")

    # 4) Permission ditolak -> status=error + pesan persis.
    deny_exec = ToolExecutor(registry=_registry(), permission_manager=_DenyManager())
    payload_deny = deny_exec.execute_tool_call(ToolCall.create("echo", {"text": "x"}))
    expected = "Permission Denied: User/Policy rejected execution of tool 'echo'"
    print(f"[4] permission     -> {payload_deny.to_content()!r}")
    assert payload_deny.status == ToolResultStatus.ERROR
    assert payload_deny.to_content() == expected, payload_deny.to_content()
    print("OK: permission denied -> status=error (bukan exception pemutus loop)")

    # 4b) Permission diizinkan -> dieksekusi.
    allow_exec = ToolExecutor(registry=_registry(), permission_manager=_AllowManager())
    payload_allow = allow_exec.execute_tool_call(ToolCall.create("echo", {"text": "x"}))
    assert payload_allow.status == ToolResultStatus.SUCCESS
    print("OK: permission allowed -> tool dijalankan")

    # 5) Argumen berbentuk JSON string (format provider) di-parse.
    as_str = ToolCall(id="call-1", function={"name": "echo", "arguments": '{"text": "js"}'})
    payload_str = executor.execute_tool_call(as_str)
    assert payload_str.status == ToolResultStatus.SUCCESS
    assert payload_str.tool_call_id == "call-1"
    assert "js" in payload_str.to_content()
    print("OK: arguments JSON string di-parse dengan benar")

    # 5b) JSON tidak valid -> status=error (tidak crash).
    bad_json = ToolCall(id="call-2", function={"name": "echo", "arguments": "{bukan json"})
    payload_bad_json = executor.execute_tool_call(bad_json)
    assert payload_bad_json.status == ToolResultStatus.ERROR
    assert "Invalid arguments" in payload_bad_json.to_content()
    print("OK: arguments JSON tidak valid -> status=error")

    # 6) Command gagal (exit_code != 0) -> status=error + detail.
    fail_cmd = executor.execute_tool_call(ToolCall.create("run_command", {"command": "x"}))
    print(f"[6] command_fail   -> {fail_cmd.to_content()!r}")
    assert fail_cmd.status == ToolResultStatus.ERROR
    content = fail_cmd.to_content()
    assert "exit_code=2" in content and "stderr-line" in content and "stdout-line" in content
    print("OK: command gagal -> status=error dengan stdout/stderr/exit_code")

    # 7) Unknown tool -> status=error.
    unknown = executor.execute_tool_call(ToolCall.create("tidak_ada", {}))
    assert unknown.status == ToolResultStatus.ERROR
    print("OK: unknown tool -> status=error")

    # 8) ConversationHistory mengubah payload -> message role="tool".
    history = ConversationHistory()
    history.append_user_message("jalankan echo")
    history.append_assistant_message(content=None, tool_calls=[call])
    history.append_tool_result_payload(payload)
    tool_msg = history.messages[-1]
    print(f"[8] history role   -> {tool_msg.role!r} tool_call_id={tool_msg.tool_call_id}")
    assert tool_msg.role == "tool"
    assert tool_msg.tool_call_id == call.id
    print("OK: ConversationHistory memformat payload jadi role='tool'")

    # 9) Regresi: execute_action() lama tetap berjalan.
    response = LLMResponse(
        actions=[LLMAction(name="echo", arguments={"text": "old"})],
        finish_reason=FinishReason.TOOL_CALLS,
    )
    obs = executor.execute_response(response)[0]
    assert obs.success is True
    print("OK: execute_action()/execute_response() lama tetap berjalan")

    print()
    print("[OK] ToolExecutor.execute_tool_call() -> ToolResultPayload bekerja.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
