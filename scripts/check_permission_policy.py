"""Verifikasi Permission / Safety Policy Layer (#54).

Deterministik, tanpa model/API cloud nyata. Menguji:
    1. policy model dapat dibuat (ActionClass/PolicyMode/Request/Decision/Config)
    2. allow bekerja
    3. deny bekerja
    4. require_approval bekerja
    5. action/tool classification benar
    6. executor tidak menjalankan action yang ditolak
    7. existing architecture tidak rusak (executor tanpa manager tetap jalan)

Jalankan:
    python scripts/check_permission_policy.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core import FinishReason, LLMAction, LLMResponse, ToolExecutor  # noqa: E402
from agent_ai.permission import (  # noqa: E402
    ActionClass,
    ActionClassifier,
    PermissionConfig,
    PermissionDecision,
    PermissionManager,
    PermissionPolicy,
    PermissionRequest,
    PolicyMode,
)
from agent_ai.tools import BaseTool, ToolRegistry  # noqa: E402


class _RecordingTool(BaseTool):
    """Dummy tool yang mencatat apakah ia benar-benar dieksekusi."""

    name = "write_file"
    description = "Dummy write tool untuk verifikasi policy."
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }

    def __init__(self) -> None:
        self.executed = False

    def execute(self, **arguments):
        self.executed = True
        return {"written": True, "path": arguments.get("path")}


def main() -> int:
    print("=== Verifikasi Permission / Safety Policy Layer (#54) ===")
    return _run()


def _run() -> int:
    # 1) policy model dapat dibuat.
    req = PermissionRequest(action="read_file", arguments={"path": "a.txt"})
    assert req.action == "read_file"
    cfg = PermissionConfig()
    assert cfg.enabled is True
    assert cfg.mode_for(ActionClass.READ_ONLY) == PolicyMode.ALLOW
    assert cfg.mode_for(ActionClass.UNKNOWN) == PolicyMode.REQUIRE_APPROVAL
    dec = PermissionDecision(allowed=True)
    assert dec.to_dict()["allowed"] is True
    print("[1] policy model dapat dibuat OK")

    # 2) allow bekerja.
    policy = PermissionPolicy(PermissionConfig(read_only=PolicyMode.ALLOW))
    d = policy.evaluate(PermissionRequest(action="read_file", arguments={"path": "a.txt"}))
    assert d.allowed is True and d.mode == PolicyMode.ALLOW
    assert d.action_class == ActionClass.READ_ONLY
    print(f"[2] allow bekerja OK -> {d.reason}")

    # 3) deny bekerja.
    policy_deny = PermissionPolicy(PermissionConfig(delete_move=PolicyMode.DENY))
    d = policy_deny.evaluate(PermissionRequest(action="delete_file", arguments={"path": "a.txt"}))
    assert d.allowed is False and d.mode == PolicyMode.DENY
    assert d.action_class == ActionClass.DELETE_MOVE
    print(f"[3] deny bekerja OK -> {d.reason}")

    # 4) require_approval bekerja.
    policy_appr = PermissionPolicy(PermissionConfig(command_execution=PolicyMode.REQUIRE_APPROVAL))
    d = policy_appr.evaluate(PermissionRequest(action="run_command", arguments={"command": "ls"}))
    assert d.allowed is False and d.mode == PolicyMode.REQUIRE_APPROVAL
    assert d.requires_approval is True
    print(f"[4] require_approval bekerja OK -> {d.reason}")

    # 5) action/tool classification benar.
    clf = ActionClassifier()
    cases = {
        "read_file": ActionClass.READ_ONLY,
        "list_files": ActionClass.READ_ONLY,
        "search_code": ActionClass.READ_ONLY,
        "write_file": ActionClass.WORKSPACE_WRITE,
        "edit_file": ActionClass.WORKSPACE_WRITE,
        "delete_file": ActionClass.DELETE_MOVE,
        "move_file": ActionClass.DELETE_MOVE,
        "run_command": ActionClass.COMMAND_EXECUTION,
        "http_fetch": ActionClass.EXTERNAL_NETWORK,
        "tool_aneh_tak_dikenal": ActionClass.UNKNOWN,
    }
    for name, expected in cases.items():
        got = clf.classify(name, {})
        assert got == expected, f"klasifikasi '{name}' salah: {got} != {expected}"
    print(f"[5] action/tool classification benar OK -> {len(cases)} kasus")

    # 6) executor tidak menjalankan action yang ditolak.
    registry = ToolRegistry()
    tool = _RecordingTool()
    registry.register(tool)
    manager = PermissionManager(
        policy=PermissionPolicy(PermissionConfig(workspace_write=PolicyMode.DENY))
    )
    executor = ToolExecutor(registry=registry, permission_manager=manager)
    response = LLMResponse(
        actions=[LLMAction(name="write_file", arguments={"path": "a.txt", "content": "x"})],
        finish_reason=FinishReason.TOOL_CALLS,
    )
    obs = executor.execute_response(response)[0]
    assert obs.success is False, "action yang ditolak harus menghasilkan observation gagal"
    assert obs.metadata.get("permission_denied") is True
    assert tool.executed is False, "tool TIDAK boleh dieksekusi saat ditolak"
    print(f"[6] executor tidak menjalankan action yang ditolak OK -> {obs.error}")

    # 6b) executor MENJALANKAN action yang diizinkan (allow path).
    tool2 = _RecordingTool()
    registry2 = ToolRegistry()
    registry2.register(tool2)
    manager_allow = PermissionManager(
        policy=PermissionPolicy(PermissionConfig(workspace_write=PolicyMode.ALLOW))
    )
    executor_allow = ToolExecutor(registry=registry2, permission_manager=manager_allow)
    obs_allow = executor_allow.execute_response(response)[0]
    assert obs_allow.success is True and tool2.executed is True
    print("[6b] executor menjalankan action yang diizinkan OK")

    # 7) existing architecture tidak rusak: executor tanpa manager tetap jalan.
    tool3 = _RecordingTool()
    registry3 = ToolRegistry()
    registry3.register(tool3)
    executor_plain = ToolExecutor(registry=registry3)  # tanpa permission_manager
    obs_plain = executor_plain.execute_response(response)[0]
    assert obs_plain.success is True and tool3.executed is True
    assert executor_plain.permission_manager is None
    print("[7] executor tanpa manager tetap jalan (backward compatible) OK")

    print()
    print("[OK] Permission / Safety Policy Layer bekerja (allow/deny/require_approval + enforced).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
