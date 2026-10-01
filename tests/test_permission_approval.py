"""Tests: Approval (ASK) coordination + gate pada ToolExecutor.

Membuktikan UX permission policy AETHER memakai permission system EXISTING:

- Mode matrix ASK (require_approval) MENAHAN eksekusi sebelum action dijalankan.
- `approval_gate` yang mengembalikan True -> action DILANJUTKAN.
- `approval_gate` yang mengembalikan False -> action DIBATALKAN, hasil penolakan
  dikembalikan ke Agent (payload error "Approval Required").
- Tanpa gate -> perilaku existing (ASK tidak dijalankan) tetap kompatibel.
- `ApprovalCoordinator` memancarkan event approval_requested/approval_resolved
  (memakai event system existing; tanpa channel kedua) dan tertaut ke task_id.
- Timeout -> EXPIRED (tidak pernah auto-ALLOW).
- Approval TIDAK tertukar antar task (task_id per request; resolve by request_id).
- Konteks approval memuat tool/target/action_class/matrix_action/scope.

Isolasi: memakai `tmp_path` (tidak menyentuh project produksi).
"""

from __future__ import annotations

import threading
import time

import pytest

from agent_ai.core.executor import ToolExecutor
from agent_ai.core.types import ToolCall
from agent_ai.permission import (
    PermissionConfig,
    PermissionManager,
    PermissionMatrix,
    PermissionPolicy,
)
from agent_ai.permission.approval import (
    ApprovalCoordinator,
    ApprovalStatus,
    make_approval_gate,
)
from agent_ai.permission.matrix import MATRIX_ACTIONS
from agent_ai.tools.base import BaseTool
from agent_ai.tools.registry import ToolRegistry


class _RecordingTool(BaseTool):
    """Dummy tool yang mencatat apakah benar-benar dieksekusi."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"dummy {name}"
        self.input_schema = {"type": "object", "properties": {}}
        self.executed = False

    def execute(self, **arguments):
        self.executed = True
        return {"ok": True, "tool": self.name, "arguments": arguments}


def _ask_all_matrix() -> PermissionMatrix:
    return PermissionMatrix.from_dict(
        {action: {"inside": "ask", "outside": "ask"} for action in MATRIX_ACTIONS}
    )


def _executor(tmp_path, gate, tool_name: str = "run_command"):
    reg = ToolRegistry()
    tool = _RecordingTool(tool_name)
    reg.register(tool)
    pm = PermissionManager(
        policy=PermissionPolicy(config=PermissionConfig(), matrix=_ask_all_matrix())
    )
    ex = ToolExecutor(
        registry=reg,
        permission_manager=pm,
        workspace_root=str(tmp_path),
        approval_gate=gate,
    )
    return ex, tool


# --------------------------------------------------------------------------- #
# 1. Gate pada ToolExecutor
# --------------------------------------------------------------------------- #
def test_ask_with_allow_gate_executes(tmp_path):
    contexts = []
    ex, tool = _executor(tmp_path, lambda ctx: (contexts.append(ctx), True)[1])
    res = ex.execute_tool_call(
        ToolCall.create("run_command", {"command": "python -m pytest -q"})
    )
    assert res.is_success is True
    assert tool.executed is True
    assert contexts and contexts[0]["tool"] == "run_command"
    assert contexts[0]["target"] == "python -m pytest -q"
    assert contexts[0]["matrix_action"] == "terminal_mutating"
    assert contexts[0]["scope"] == "inside"


def test_ask_with_deny_gate_blocks(tmp_path):
    ex, tool = _executor(tmp_path, lambda ctx: False)
    res = ex.execute_tool_call(
        ToolCall.create("run_command", {"command": "python -m pytest -q"})
    )
    assert res.is_success is False
    assert tool.executed is False
    assert "Approval Required" in str(res.output)


def test_ask_without_gate_is_backward_compatible(tmp_path):
    ex, tool = _executor(tmp_path, None)
    res = ex.execute_tool_call(
        ToolCall.create("run_command", {"command": "python -m pytest -q"})
    )
    assert res.is_success is False
    assert tool.executed is False


def test_ask_gate_error_is_safe(tmp_path):
    def _boom(ctx):
        raise RuntimeError("gate failure")

    ex, tool = _executor(tmp_path, _boom)
    res = ex.execute_tool_call(
        ToolCall.create("run_command", {"command": "python -m pytest -q"})
    )
    # Kegagalan gate -> tidak dieksekusi (tidak pernah auto-allow).
    assert res.is_success is False
    assert tool.executed is False


# --------------------------------------------------------------------------- #
# 2. ApprovalCoordinator (event + linkage + timeout)
# --------------------------------------------------------------------------- #
def test_coordinator_emits_events_and_links_task():
    events = []

    def _sink(session_id, event_type, payload):
        events.append((event_type, payload))

    coord = ApprovalCoordinator(sink=_sink, timeout=2.0)
    gate = make_approval_gate(coord, task_id="task-1", session_id="sess-1")
    holder = {}

    def _run():
        holder["allowed"] = gate({"tool": "write_file", "target": "a.txt"})

    t = threading.Thread(target=_run)
    t.start()

    deadline = time.time() + 2.0
    while not coord.pending("task-1") and time.time() < deadline:
        time.sleep(0.01)
    pending = coord.pending()
    assert len(pending) == 1, pending
    req = pending[0]
    assert req.task_id == "task-1" and req.session_id == "sess-1"
    assert events and events[0][0] == "approval_requested"
    assert events[0][1]["task_id"] == "task-1"

    coord.resolve(req.request_id, True)
    t.join(timeout=2.0)
    assert holder["allowed"] is True
    assert any(e[0] == "approval_resolved" for e in events)
    assert coord.status(req.request_id) is ApprovalStatus.ALLOWED


def test_coordinator_timeout_expires_never_allows():
    coord = ApprovalCoordinator(timeout=0.05)
    req = coord.request(tool="write_file", task_id="task-2")
    status = coord.wait(req.request_id)
    assert status is ApprovalStatus.EXPIRED


def test_coordinator_resolve_unknown_returns_none():
    coord = ApprovalCoordinator(timeout=1.0)
    assert coord.resolve("tidak-ada", True) is None


def test_coordinator_cancel_task_denies_pending():
    coord = ApprovalCoordinator(timeout=5.0)
    coord.request(tool="write_file", task_id="task-x")
    coord.request(tool="run_command", task_id="task-y")
    assert len(coord.pending("task-x")) == 1
    denied = coord.cancel_task("task-x")
    assert denied == 1
    assert coord.pending("task-x") == []
    # task lain tidak terpengaruh.
    assert len(coord.pending("task-y")) == 1
