"""Tests: Project Permission Matrix enforcement (allow / ask / deny).

Membuktikan matrix project benar-benar BERLAKU saat Agent melakukan action:

- Default matrix AETHER (read/modify/delete/move file + terminal read/mutating
  x inside/outside workspace).
- `allow`, `ask`, `deny` diterapkan sesuai sel matrix.
- Pembedaan path DI DALAM vs DI LUAR workspace project.
- Read, modify, delete, move file.
- Terminal read-only (allow) vs terminal mutating (ask/deny).
- DENY mencegah eksekusi (tool tidak dijalankan).
- ASK menahan eksekusi + menandai butuh approval ke caller.
- Project baru otomatis punya `.aether/permissions.json` (default matrix).
- Existing project tanpa file policy memakai default tanpa merusak project.

Isolasi: memakai `tmp_path` (tidak menyentuh project produksi).
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_ai.core.executor import ToolExecutor
from agent_ai.core.types import ToolCall
from agent_ai.permission import (
    ActionScope,
    MatrixAction,
    PermissionConfig,
    PermissionManager,
    PermissionMatrix,
    PermissionPolicy,
    PolicyMode,
)
from agent_ai.permission.matrix import (
    DEFAULT_MATRIX_RULES,
    classify_terminal_command,
)
from agent_ai.projects.permissions import (
    PERMISSIONS_FILE_NAME,
    ProjectPermissionStore,
)
from agent_ai.projects.registry import ProjectRegistry
from agent_ai.tools.base import BaseTool
from agent_ai.tools.registry import ToolRegistry


# --------------------------------------------------------------------------- #
# Fake tools (mencatat apakah benar-benar dieksekusi)
# --------------------------------------------------------------------------- #
class _RecordingTool(BaseTool):
    """Tool dummy yang mencatat eksekusi (tidak menyentuh filesystem)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"dummy {name}"
        self.input_schema = {"type": "object", "properties": {}}
        self.executed = False

    def execute(self, **arguments):
        self.executed = True
        return {"ok": True, "tool": self.name, "arguments": arguments}


def _registry(*names: str) -> tuple:
    reg = ToolRegistry()
    tools = {}
    for name in names:
        tool = _RecordingTool(name)
        reg.register(tool)
        tools[name] = tool
    return reg, tools


def _manager(matrix: PermissionMatrix, workspace_root: str) -> PermissionManager:
    """PermissionManager dengan matrix project + workspace root eksplisit."""
    return PermissionManager(
        policy=PermissionPolicy(
            config=PermissionConfig(), matrix=matrix
        )
    )


def _executor(matrix, workspace_root, *tool_names):
    reg, tools = _registry(*tool_names)
    pm = _manager(matrix, workspace_root)
    executor = ToolExecutor(
        registry=reg, permission_manager=pm, workspace_root=workspace_root
    )
    return executor, tools


# --------------------------------------------------------------------------- #
# 1. Default matrix
# --------------------------------------------------------------------------- #
def test_default_matrix_matches_spec():
    matrix = PermissionMatrix.default()
    assert matrix.to_dict() == DEFAULT_MATRIX_RULES
    assert matrix.to_dict() == {
        "read_files": {"inside": "allow", "outside": "allow"},
        "modify_files": {"inside": "allow", "outside": "deny"},
        "delete_files": {"inside": "allow", "outside": "deny"},
        "move_files": {"inside": "allow", "outside": "deny"},
        "terminal_read": {"inside": "allow", "outside": "allow"},
        "terminal_mutating": {"inside": "ask", "outside": "deny"},
    }


# --------------------------------------------------------------------------- #
# 2. Resolusi aksi + scope
# --------------------------------------------------------------------------- #
def test_action_resolution_and_scope(tmp_path):
    ws = str(tmp_path)
    pm = _manager(PermissionMatrix.default(), ws)

    # Read inside -> allow.
    dec = pm.check("read_file", {"path": "a.txt"}, workspace_root=ws)
    assert dec.allowed is True and dec.scope == ActionScope.INSIDE

    # Modify inside -> allow; modify outside -> deny.
    dec_in = pm.check(
        "write_file", {"path": "a.txt", "content": "x"}, workspace_root=ws
    )
    assert dec_in.allowed is True and dec_in.matrix_action == MatrixAction.MODIFY_FILES
    outside = str(tmp_path.parent / "outside.txt")
    dec_out = pm.check(
        "write_file", {"path": outside, "content": "x"}, workspace_root=ws
    )
    assert dec_out.allowed is False and dec_out.scope == ActionScope.OUTSIDE
    assert dec_out.mode == PolicyMode.DENY

    # Delete/move inside allow; di luar deny.
    assert pm.check("delete_file", {"path": "a.txt"}, workspace_root=ws).allowed is True
    assert (
        pm.check("delete_file", {"path": outside}, workspace_root=ws).mode
        == PolicyMode.DENY
    )
    dec_move = pm.check(
        "move_file", {"source": "a.txt", "destination": "b.txt"}, workspace_root=ws
    )
    assert dec_move.allowed is True
    assert dec_move.matrix_action == MatrixAction.MOVE_FILES
    assert (
        pm.check(
            "move_file",
            {"source": "a.txt", "destination": outside},
            workspace_root=ws,
        ).mode
        == PolicyMode.DENY
    )


# --------------------------------------------------------------------------- #
# 3. Terminal: read-only vs mutating
# --------------------------------------------------------------------------- #
def test_terminal_classification():
    assert classify_terminal_command("git status") == "read"
    assert classify_terminal_command("dir") == "read"
    assert classify_terminal_command("python --version") == "read"
    assert classify_terminal_command("python -m pytest -q") == "mutate"
    assert classify_terminal_command("rm -rf build") == "mutate"
    assert classify_terminal_command("echo hi > out.txt") == "mutate"
    assert classify_terminal_command("git commit -m x") == "mutate"
    assert classify_terminal_command("") == "mutate"


def test_terminal_matrix_enforcement(tmp_path):
    ws = str(tmp_path)
    pm = _manager(PermissionMatrix.default(), ws)

    # Terminal read-only inside -> allow.
    dec = pm.check("run_command", {"command": "git status"}, workspace_root=ws)
    assert dec.allowed is True and dec.matrix_action == MatrixAction.TERMINAL_READ

    # Terminal mutating inside -> ask (butuh approval).
    dec_mut = pm.check("run_command", {"command": "python -m pytest -q"}, workspace_root=ws)
    assert dec_mut.allowed is False
    assert dec_mut.requires_approval is True
    assert dec_mut.matrix_action == MatrixAction.TERMINAL_MUTATING

    # Terminal mutating di LUAR workspace (cwd luar) -> deny.
    outside_dir = str(tmp_path.parent)
    dec_out = pm.check(
        "run_command",
        {"command": "python -m pytest -q", "cwd": outside_dir},
        workspace_root=ws,
    )
    assert dec_out.allowed is False
    assert dec_out.mode == PolicyMode.DENY
    assert dec_out.scope == ActionScope.OUTSIDE


# --------------------------------------------------------------------------- #
# 4. DENY mencegah eksekusi lewat ToolExecutor
# --------------------------------------------------------------------------- #
def test_deny_prevents_execution(tmp_path):
    ws = str(tmp_path)
    outside = str(tmp_path.parent / "outside.txt")
    executor, tools = _executor(PermissionMatrix.default(), ws, "write_file")

    result = executor.execute_tool_call(
        ToolCall.create("write_file", {"path": outside, "content": "x"})
    )
    assert result.is_success is False
    assert tools["write_file"].executed is False, "tool TIDAK boleh dieksekusi saat DENY"
    assert "Permission Denied" in str(result.output)


# --------------------------------------------------------------------------- #
# 5. ASK menahan eksekusi + menandai kebutuhan approval
# --------------------------------------------------------------------------- #
def test_ask_holds_execution_and_reports_approval(tmp_path):
    ws = str(tmp_path)
    executor, tools = _executor(PermissionMatrix.default(), ws, "run_command")

    result = executor.execute_tool_call(
        ToolCall.create("run_command", {"command": "python -m pytest -q"})
    )
    assert result.is_success is False
    assert tools["run_command"].executed is False, "tool TIDAK boleh dieksekusi saat ASK"
    assert "Approval Required" in str(result.output)


# --------------------------------------------------------------------------- #
# 6. ALLOW mengeksekusi (inside workspace)
# --------------------------------------------------------------------------- #
def test_allow_executes(tmp_path):
    ws = str(tmp_path)
    executor, tools = _executor(PermissionMatrix.default(), ws, "write_file", "run_command")

    write = executor.execute_tool_call(
        ToolCall.create("write_file", {"path": "a.txt", "content": "x"})
    )
    assert write.is_success is True and tools["write_file"].executed is True

    # Terminal read-only inside -> dijalankan.
    read_cmd = executor.execute_tool_call(
        ToolCall.create("run_command", {"command": "git status"})
    )
    assert read_cmd.is_success is True and tools["run_command"].executed is True


# --------------------------------------------------------------------------- #
# 7. Custom matrix (mis. semua deny) benar-benar berlaku
# --------------------------------------------------------------------------- #
def test_custom_matrix_is_honored(tmp_path):
    ws = str(tmp_path)
    deny_all = {
        action: {"inside": "deny", "outside": "deny"} for action in DEFAULT_MATRIX_RULES
    }
    matrix = PermissionMatrix.from_dict(deny_all)
    pm = _manager(matrix, ws)
    assert pm.check("read_file", {"path": "a.txt"}, workspace_root=ws).allowed is False
    assert (
        pm.check("write_file", {"path": "a.txt", "content": "x"}, workspace_root=ws).allowed
        is False
    )
    assert pm.check("run_command", {"command": "git status"}, workspace_root=ws).allowed is False


# --------------------------------------------------------------------------- #
# 8. Project baru / existing project
# --------------------------------------------------------------------------- #
def test_new_project_gets_default_matrix_file(tmp_path):
    root = tmp_path / "new"
    root.mkdir()
    registry = ProjectRegistry(workspace=tmp_path / "ws")
    registry.register(name="New", root=str(root))

    path = root / ".aether" / PERMISSIONS_FILE_NAME
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8")) == DEFAULT_MATRIX_RULES


def test_existing_project_without_file_uses_default(tmp_path):
    root = tmp_path / "existing"
    root.mkdir()
    (root / "keep.txt").write_text("data\n", encoding="utf-8")
    store = ProjectPermissionStore(root=root)
    assert not store.exists()

    policy = store.load()
    assert policy.to_dict() == DEFAULT_MATRIX_RULES
    # Project TIDAK berubah: file tetap belum dibuat oleh load().
    assert not store.exists()
    assert (root / "keep.txt").read_text(encoding="utf-8") == "data\n"


def test_backward_compatible_legacy_mode_scope(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    path = root / ".aether" / PERMISSIONS_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mode": "deny", "scope": "outside"}), encoding="utf-8")

    policy = ProjectPermissionStore(root=root).load()
    assert (
        policy.mode_for(MatrixAction.MODIFY_FILES, ActionScope.OUTSIDE) == PolicyMode.DENY
    )
    assert policy.mode_for(MatrixAction.READ_FILES, ActionScope.INSIDE) == PolicyMode.ALLOW


# --------------------------------------------------------------------------- #
# 9. Backward compatible: tanpa matrix/workspace -> perilaku existing
# --------------------------------------------------------------------------- #
def test_manager_without_matrix_is_backward_compatible():
    pm = PermissionManager(policy=PermissionPolicy(config=PermissionConfig()))
    # Tidak ada matrix -> pakai PermissionConfig (allow untuk write/delete/command).
    assert pm.check("write_file", {"path": "a.txt", "content": "x"}).allowed is True
    assert pm.check("read_file", {"path": "a.txt"}).allowed is True


# --------------------------------------------------------------------------- #
# 10. End-to-end wiring: TaskExecutor (gateway) menerapkan matrix project
# --------------------------------------------------------------------------- #
def _ensure_django() -> None:
    import os
    import sys

    project_root = Path(__file__).resolve().parents[1]
    for rel in ("src", "web/django_app"):
        path = str(project_root / rel)
        if path not in sys.path:
            sys.path.insert(0, path)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
    import django

    try:
        django.setup()
    except RuntimeError:
        pass


def _fake_provider(tool_name: str, tool_args: dict):
    from agent_ai.core.response import ActionType, FinishReason, LLMAction, LLMResponse
    from agent_ai.providers.base import BaseProvider, GenerateResult

    class _Fake(BaseProvider):
        name = "fake-matrix"

        def __init__(self) -> None:
            self.calls = 0

        def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
            return GenerateResult(text="", provider=self.name, model="fake-1")

        def is_available(self) -> bool:
            return True

        def normalize_response(self, result):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    text="",
                    actions=[
                        LLMAction(name=tool_name, arguments=tool_args, type=ActionType.TOOL_CALL)
                    ],
                    finish_reason=FinishReason.TOOL_CALLS,
                    provider=self.name,
                )
            return LLMResponse(
                text="selesai",
                actions=[],
                finish_reason=FinishReason.STOP,
                provider=self.name,
            )

    return _Fake()


def _run_gateway_task(tmp_path, workspace_root: str, tool_name: str, tool_args: dict):
    """Jalankan satu task lewat TaskExecutor dengan matrix project."""
    from agent_ai.session.store import InMemorySessionStore
    from agent_ai.task.models import PreparedTask

    from api.execution import TaskExecutor

    store = InMemorySessionStore()
    session = store.create_session()
    executor = TaskExecutor(
        store,
        provider_factory=lambda: _fake_provider(tool_name, tool_args),
    )
    task_id = "matrix-task"
    result = executor.run(
        PreparedTask(task="matrix enforcement", task_id=task_id),
        session_id=session.session_id,
        task_id=task_id,
        workspace_root=workspace_root,
        project_permission_matrix=PermissionMatrix.default(),
    )
    return store, task_id, result


def test_gateway_execution_denies_write_outside_workspace(tmp_path):
    _ensure_django()
    ws = tmp_path / "project"
    ws.mkdir()
    outside = tmp_path / "outside.txt"

    store, task_id, result = _run_gateway_task(
        tmp_path, str(ws), "write_file", {"path": str(outside), "content": "x"}
    )

    # DENY menahan eksekusi -> file TIDAK dibuat, observation error.
    assert not outside.exists(), "file di luar workspace TIDAK boleh ditulis saat DENY"
    events = store.get_events(task_id=task_id)
    observations = [e for e in events if e.event_type.value == "observation_received"]
    assert observations, [e.event_type.value for e in events]
    assert any(
        "Permission" in json.dumps(e.payload) or "Denied" in json.dumps(e.payload)
        for e in observations
    ), [e.payload for e in observations]


def test_gateway_execution_allows_write_inside_workspace(tmp_path):
    _ensure_django()
    ws = tmp_path / "project"
    ws.mkdir()

    _store, _task_id, _result = _run_gateway_task(
        tmp_path, str(ws), "write_file", {"path": "inside.txt", "content": "hi\n"}
    )

    # ALLOW -> file dibuat DI DALAM workspace.
    assert (ws / "inside.txt").read_text(encoding="utf-8") == "hi\n"


def test_gateway_execution_ask_holds_command(tmp_path):
    _ensure_django()
    ws = tmp_path / "project"
    ws.mkdir()

    store, task_id, _result = _run_gateway_task(
        tmp_path, str(ws), "run_command", {"command": "python -m pytest -q"}
    )
    events = store.get_events(task_id=task_id)
    called = [e for e in events if e.event_type.value == "tool_called"]
    assert called, "tool_called harus tercatat"
    # run_command mutating di dalam workspace -> ASK -> tidak dieksekusi.
    assert not (ws / "should_not_exist.txt").exists()
    observations = [e for e in events if e.event_type.value == "observation_received"]
    assert any("Approval" in json.dumps(e.payload) for e in observations), [
        e.payload for e in observations
    ]

