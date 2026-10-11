"""Verifikasi UX Permission Policy AETHER: modal + ASK approval + enforcement.

Membuktikan (deterministik, tanpa model/API cloud nyata):
    1. Mode matrix ASK (require_approval) MENAHAN eksekusi; dengan approval_gate
       yang mengembalikan True -> action DILANJUTKAN; False/timeout -> DIBATALKAN
       dan hasil penolakan dikembalikan ke Agent.
    2. Gate MENERIMA konteks (tool/target/path/action_class/matrix_action/scope)
       yang diperlukan UI approval.
    3. ApprovalCoordinator memancarkan event approval_requested/approval_resolved
       (memakai event system existing/SessionStore) dan tertaut ke task_id.
    4. HTTP endpoint: GET /api/tasks/approvals (pending) + POST
       /api/tasks/approvals/resolve (Allow/Deny) -> action tertahan dilanjutkan.
    5. Approval TIDAK tertukar antar task (task_id per request).
    6. Project BARU -> `.aether/settings/permissions.json` ada (matrix default) dan GET
       policy mengembalikan matrix AKTUAL (sumber policy project baru).
    7. Boundary frontend: policy dibuka sebagai MODAL (ProjectPolicyPanel),
       approv()/resolveApproval ada di api.js, TIDAK ada permission engine kedua.

Isolasi: fixture project di `dummy_test/policy_ux_fixture` (dibersihkan).
Jalankan:
    python scripts/check_permission_ux.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
FRONTEND_DIR = PROJECT_ROOT / "web" / "frontend" / "src"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIX_ROOT = DUMMY_ROOT / "policy_ux_fixture"


def setup_fixture() -> None:
    shutil.rmtree(FIX_ROOT, ignore_errors=True)
    (FIX_ROOT / "proj").mkdir(parents=True, exist_ok=True)


def teardown_fixture() -> None:
    shutil.rmtree(FIX_ROOT, ignore_errors=True)


def _run() -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
    import django

    django.setup()

    from django.conf import settings as _dj_settings

    if "testserver" not in _dj_settings.ALLOWED_HOSTS:
        _dj_settings.ALLOWED_HOSTS = ["testserver", *list(_dj_settings.ALLOWED_HOSTS)]

    from django.test import Client

    from agent_ai.core.executor import ToolExecutor
    from agent_ai.core.types import ToolCall
    from agent_ai.permission import (
        PermissionConfig,
        PermissionManager,
        PermissionMatrix,
        PermissionPolicy,
    )
    from agent_ai.permission.approval import ApprovalCoordinator, ApprovalStatus
    from agent_ai.permission.matrix import MATRIX_ACTIONS
    from agent_ai.tools.base import BaseTool
    from agent_ai.tools.registry import ToolRegistry

    # --- Fake tool (mencatat apakah benar-benar dieksekusi) -----------------
    class _RecordingTool(BaseTool):
        def __init__(self, name: str) -> None:
            self.name = name
            self.description = f"dummy {name}"
            self.input_schema = {"type": "object", "properties": {}}
            self.executed = False

        def execute(self, **arguments):
            self.executed = True
            return {"ok": True, "tool": self.name, "arguments": arguments}

    ws = str(FIX_ROOT / "proj")
    ask_matrix = {
        action: {"inside": "ask", "outside": "ask"} for action in MATRIX_ACTIONS
    }
    matrix = PermissionMatrix.from_dict(ask_matrix)

    def _build(gate):
        reg = ToolRegistry()
        tool = _RecordingTool("run_command")
        reg.register(tool)
        pm = PermissionManager(
            policy=PermissionPolicy(config=PermissionConfig(), matrix=matrix)
        )
        ex = ToolExecutor(
            registry=reg,
            permission_manager=pm,
            workspace_root=ws,
            approval_gate=gate,
        )
        return ex, tool

    # --- [1] ASK + gate ALLOW -> action dilanjutkan -------------------------
    contexts = []
    ex_allow, tool_allow = _build(lambda ctx: (contexts.append(ctx), True)[1])
    res = ex_allow.execute_tool_call(
        ToolCall.create("run_command", {"command": "python -m pytest -q"})
    )
    assert res.is_success is True, res
    assert tool_allow.executed is True, "ASK + Allow -> tool HARUS dieksekusi"
    assert contexts and contexts[0]["tool"] == "run_command", contexts
    assert contexts[0]["target"] == "python -m pytest -q", contexts
    assert contexts[0]["matrix_action"] == "terminal_mutating", contexts
    assert contexts[0]["scope"] == "inside", contexts
    print("[1] ASK + gate ALLOW -> action dilanjutkan (konteks target terkirim) OK")

    # --- [2] ASK + gate DENY -> action dibatalkan, hasil ke Agent -----------
    ex_deny, tool_deny = _build(lambda ctx: False)
    res_deny = ex_deny.execute_tool_call(
        ToolCall.create("run_command", {"command": "python -m pytest -q"})
    )
    assert res_deny.is_success is False, res_deny
    assert tool_deny.executed is False, "ASK + Deny -> tool TIDAK boleh dieksekusi"
    assert "Approval Required" in str(res_deny.output), res_deny.output
    print("[2] ASK + gate DENY -> action dibatalkan + hasil penolakan ke Agent OK")

    # --- [2b] ASK tanpa gate -> perilaku existing (tidak dijalankan) --------
    ex_nogate, tool_nogate = _build(None)
    res_nogate = ex_nogate.execute_tool_call(
        ToolCall.create("run_command", {"command": "python -m pytest -q"})
    )
    assert res_nogate.is_success is False and tool_nogate.executed is False
    print("[2b] ASK tanpa gate -> tidak dieksekusi (backward compatible) OK")

    # --- [3] Event approval + linkage ke task ------------------------------
    events = []

    def _sink(session_id, event_type, payload):
        events.append((event_type, payload))

    coord = ApprovalCoordinator(sink=_sink, timeout=2.0)
    gate = _make_gate(coord, task_id="task-1", session_id="sess-1")
    import threading

    holder = {}

    def _run_gate():
        holder["allowed"] = gate(
            {
                "tool": "write_file",
                "target": "a.txt",
                "action_class": "workspace_write",
                "matrix_action": "modify_files",
                "scope": "inside",
            }
        )

    t = threading.Thread(target=_run_gate)
    t.start()
    # Tunggu approval_requested muncul.
    import time

    deadline = time.time() + 2.0
    while not coord.pending("task-1") and time.time() < deadline:
        time.sleep(0.01)
    pending = coord.pending()
    assert len(pending) == 1, pending
    req = pending[0]
    assert req.task_id == "task-1" and req.session_id == "sess-1", req.to_dict()
    assert events and events[0][0] == "approval_requested", events
    assert events[0][1]["task_id"] == "task-1", events[0]
    # Allow -> gate lanjut, event resolved tercatat.
    coord.resolve(req.request_id, True)
    t.join(timeout=2.0)
    assert holder["allowed"] is True, holder
    assert any(e[0] == "approval_resolved" for e in events), events
    assert coord.status(req.request_id) is ApprovalStatus.ALLOWED
    print("[3] approval_requested/resolved + linkage task_id OK")

    # --- [3b] Timeout -> EXPIRED (tidak pernah auto-ALLOW) ------------------
    coord2 = ApprovalCoordinator(timeout=0.05)
    req2 = coord2.request(tool="write_file", task_id="task-2")
    status = coord2.wait(req2.request_id)
    assert status is ApprovalStatus.EXPIRED, status
    print("[3b] timeout approval -> EXPIRED (tidak auto-allow) OK")

    # --- [4]/[5] HTTP endpoint + tidak tertukar antar task ------------------
    import api.services as services_mod
    from api.project_store import ProjectStore

    tmp = tempfile.mkdtemp(prefix="policy_ux_store_")
    store = ProjectStore(db_path=Path(tmp) / "t.db")
    service = services_mod.GatewayService(project_store=store, auto_execute=False)
    services_mod._default_service = service
    client = Client()

    gate_a = service.approval_gate_for("taskA", "sessA")
    gate_b = service.approval_gate_for("taskB", "sessB")
    holder_a = {}
    holder_b = {}

    def _run_a():
        holder_a["ok"] = gate_a({"tool": "run_command", "target": "cmd A"})

    def _run_b():
        holder_b["ok"] = gate_b({"tool": "write_file", "target": "file B"})

    ta = threading.Thread(target=_run_a)
    tb = threading.Thread(target=_run_b)
    ta.start()
    tb.start()

    deadline = time.time() + 3.0
    while len(service.list_approvals()) < 2 and time.time() < deadline:
        time.sleep(0.01)
    pending_all = service.list_approvals()
    assert len(pending_all) == 2, pending_all
    # GET endpoint (pending) + filter task_id.
    resp = client.get("/api/tasks/approvals")
    assert resp.status_code == 200, resp.content
    assert len(resp.json()["approvals"]) == 2, resp.json()
    only_a = client.get("/api/tasks/approvals?task_id=taskA").json()["approvals"]
    assert len(only_a) == 1 and only_a[0]["task_id"] == "taskA", only_a
    # Resolve taskA (Allow) -> hanya A lanjut; B tetap menunggu.
    rid_a = only_a[0]["request_id"]
    r = client.post(
        "/api/tasks/approvals/resolve",
        data=json.dumps({"request_id": rid_a, "allow": True}),
        content_type="application/json",
    )
    assert r.status_code == 200, r.content
    ta.join(timeout=2.0)
    assert holder_a.get("ok") is True, holder_a
    # B belum keeputusan.
    remaining = service.list_approvals()
    assert len(remaining) == 1 and remaining[0]["task_id"] == "taskB", remaining
    rid_b = remaining[0]["request_id"]
    client.post(
        "/api/tasks/approvals/resolve",
        data=json.dumps({"request_id": rid_b, "allow": False}),
        content_type="application/json",
    )
    tb.join(timeout=2.0)
    assert holder_b.get("ok") is False, holder_b
    # resolve tidak dikenal -> 404.
    nf = client.post(
        "/api/tasks/approvals/resolve",
        data=json.dumps({"request_id": "tidak-ada", "allow": True}),
        content_type="application/json",
    )
    assert nf.status_code == 404, (nf.status_code, nf.content)
    print("[4] HTTP approvals endpoints (Allow/Deny) OK")
    print("[5] approval TIDAK tertukar antar task (+ 404 unknown) OK")

    # --- [6] Project baru -> permissions.json + policy AKTUAL --------------
    root_new = FIX_ROOT / "proj_new"
    root_new.mkdir(parents=True, exist_ok=True)
    proj = service.create_project(name="PolicyUXNew", path=str(root_new))
    pfile = root_new / ".aether" / "settings" / "permissions.json"
    assert pfile.is_file(), f"permissions.json harus dibuat di {pfile}"
    assert json.loads(pfile.read_text(encoding="utf-8")) == PermissionMatrix.default().to_dict()
    got = client.get(f"/api/projects/{proj['id']}/policy").json()
    assert got["matrix"] == PermissionMatrix.default().to_dict(), got
    assert got["exists"] is True, got
    print("[6] project baru -> .aether/settings/permissions.json + GET policy aktual OK")

    # --- [7] Boundary frontend (modal + api + tanpa engine kedua) ----------
    api_js = (FRONTEND_DIR / "api.js").read_text(encoding="utf-8")
    for fn in ("getProjectPolicy", "saveProjectPolicy", "resolveApproval", "listApprovals"):
        assert fn in api_js, f"api.js harus mengekspor {fn}"
    assert "/tasks/approvals" in api_js, "api.js harus memanggil endpoint approvals"
    panel = (FRONTEND_DIR / "components" / "ProjectPolicyPanel.vue").read_text(
        encoding="utf-8"
    )
    # Panel HANYA memakai API policy + dirender sebagai modal (modal-backdrop).
    assert "getProjectPolicy" in panel and "saveProjectPolicy" in panel
    assert "modal-backdrop" in panel, "policy harus berupa MODAL, bukan panel inline"
    for bad in ("AgentRuntime", "AgentLoop", "orchestrator", "Replanner"):
        assert bad not in panel, f"ProjectPolicyPanel tidak boleh memuat '{bad}'"
    # App.vue = composition root; handler policy/approval ada di ./composables/*.
    app_vue = (FRONTEND_DIR / "App.vue").read_text(encoding="utf-8") + "\n" + "\n".join(
        p.read_text(encoding="utf-8") for p in sorted((FRONTEND_DIR / "composables").glob("*.js"))
    )
    assert "ProjectPolicyPanel" in app_vue and "openProjectPolicy" in app_vue
    assert "decideApproval" in app_vue and "approvals" in app_vue, (
        "App.vue harus menangani approval (Allow/Deny)"
    )
    print("[7] boundary frontend OK -> modal policy + approval UI via gateway")

    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("[OK] Permission Policy UX bekerja (modal + ASK approval + enforcement).")
    return 0


def _make_gate(coordinator, *, task_id: str, session_id: str):
    from agent_ai.permission.approval import make_approval_gate

    return make_approval_gate(coordinator, task_id=task_id, session_id=session_id)


def main() -> int:
    print("=== Verifikasi Permission Policy UX AETHER (modal + approval) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


if __name__ == "__main__":
    raise SystemExit(main())
