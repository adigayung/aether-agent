"""Verifikasi Project Permission Matrix AETHER (per project).

Membuktikan:
    1. Policy disimpan project-local di `<root>/.aether/settings/permissions.json`
       sebagai Permission Matrix (aksi x inside/outside).
    2. Mode matrix: ALLOW / ASK (require_approval) / DENY.
    3. Scope matrix: inside / outside workspace.
    4. Policy per project INDEPENDEN (project A TIDAK memengaruhi project B).
    5. GET mengembalikan nilai AKTUAL matrix project tersebut.
    6. Save melalui API menulis kembali ke `.aether/settings/permissions.json`.
    7. Enforcement: matrix di-enforce PermissionManager EXISTING (bukan sistem
       permission kedua) — DENY menahan, ASK menahan + butuh approval.
    8. Backward compatible: project lama tanpa policy -> matrix default.
    9. Project BARU otomatis punya `.aether/settings/permissions.json` (matrix default).
   10. Boundary frontend: policy dikelola per project, tanpa policy engine kedua.

Deterministik, tanpa model/API cloud. Fixture project dibuat di
`dummy_test/project_policy_fixture/...` dan dibersihkan setelah test. Store
SQLite memakai file sementara (tidak menyentuh data produksi).

Jalankan:
    python scripts/check_project_policy.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIX_ROOT = DUMMY_ROOT / "project_policy_fixture"

_MATRIX_ACTIONS = (
    "read_files",
    "modify_files",
    "delete_files",
    "move_files",
    "terminal_read",
    "terminal_mutating",
)

_DENY_ALL_MATRIX = {
    action: {"inside": "deny", "outside": "deny"} for action in _MATRIX_ACTIONS
}


def setup_fixture() -> None:
    shutil.rmtree(FIX_ROOT, ignore_errors=True)
    for name in ("proj_a", "proj_b"):
        target = FIX_ROOT / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "keep.txt").write_text("jangan dihapus\n", encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIX_ROOT, ignore_errors=True)


def _read_permissions(root: Path) -> dict:
    path = root / ".aether" / "settings" / "permissions.json"
    assert path.is_file(), f"permissions.json harus ada di {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _policy_file(root: Path) -> Path:
    return root / ".aether" / "settings" / "permissions.json"


def _run() -> int:
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
    import django

    django.setup()

    # Django test client mengirim Host: testserver. Hardening #57 membatasi
    # host; tambahkan hanya untuk verifier (bukan produksi).
    from django.conf import settings as _dj_settings

    if "testserver" not in _dj_settings.ALLOWED_HOSTS:
        _dj_settings.ALLOWED_HOSTS = ["testserver", *list(_dj_settings.ALLOWED_HOSTS)]

    from django.test import Client

    import api.services as services_mod
    from api.project_store import ProjectStore
    from agent_ai.permission.models import ActionScope, MatrixAction, PolicyMode
    from agent_ai.projects.permissions import (
        MATRIX_MODE_VALUES_SET,
        ProjectPermissionStore,
        ProjectPolicy,
    )
    from agent_ai.projects.registry import ProjectRegistry
    from agent_ai.permission import (
        PermissionConfig,
        PermissionManager,
        PermissionMatrix,
        PermissionPolicy,
    )

    # --- [1] Model matrix: aksi + scope + mode existing dipertahankan -------
    assert set(MATRIX_MODE_VALUES_SET) == {"allow", "ask", "deny"}, MATRIX_MODE_VALUES_SET
    matrix = PermissionMatrix.default()
    assert matrix.mode_for(MatrixAction.READ_FILES, ActionScope.INSIDE) == PolicyMode.ALLOW
    assert matrix.mode_for(MatrixAction.MODIFY_FILES, ActionScope.OUTSIDE) == PolicyMode.DENY
    assert (
        matrix.mode_for(MatrixAction.TERMINAL_MUTATING, ActionScope.INSIDE)
        == PolicyMode.REQUIRE_APPROVAL
    )
    print("[1] model Permission Matrix (allow/ask/deny x inside/outside) OK")

    # --- [2] Store project-local: `<root>/.aether/settings/permissions.json` --------
    root_a = FIX_ROOT / "proj_a"
    store_a = ProjectPermissionStore(root=root_a)
    assert not store_a.exists()
    default = store_a.load()
    assert default.to_dict() == PermissionMatrix.default().to_dict()
    store_a.save(ProjectPolicy.from_dict(_DENY_ALL_MATRIX))
    raw = _read_permissions(root_a)
    assert raw == _DENY_ALL_MATRIX, raw
    print("[2] matrix tersimpan project-local di .aether/settings/permissions.json OK")

    # --- Setup gateway (isolasi registry + store SQLite) -------------------
    workspace = DUMMY_ROOT / "project_policy_registry"
    shutil.rmtree(workspace, ignore_errors=True)
    registry = ProjectRegistry(workspace=workspace)
    tmp_store_dir = tempfile.mkdtemp(prefix="policy_store_")
    store = ProjectStore(db_path=Path(tmp_store_dir) / "t.db")
    service = services_mod.GatewayService(
        project_registry=registry, project_store=store, auto_execute=False
    )
    services_mod._default_service = service
    client = Client()

    proj_a = service.create_project(name="PolicyA", path=str(root_a))
    proj_b = service.create_project(name="PolicyB", path=str(FIX_ROOT / "proj_b"))
    pid_a, pid_b = proj_a["id"], proj_b["id"]

    # --- [3] GET mengembalikan matrix AKTUAL project -----------------------
    resp = client.get(f"/api/projects/{pid_a}/policy")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    got = resp.json()
    assert got["matrix"] == _DENY_ALL_MATRIX, got
    assert got["options"]["actions"] and got["options"]["modes"], got
    assert {m["value"] for m in got["options"]["modes"]} == {"allow", "ask", "deny"}, got
    # Project B belum punya policy -> matrix default (TIDAK sama dengan A).
    resp_b = client.get(f"/api/projects/{pid_b}/policy")
    got_b = resp_b.json()
    assert got_b["matrix"] == PermissionMatrix.default().to_dict(), got_b
    print("[3] GET mengembalikan matrix policy AKTUAL per project OK")

    # --- [4] Save via API -> menulis `.aether/settings/permissions.json` -----------
    ask_matrix = {
        action: {"inside": "ask", "outside": "deny"} for action in _MATRIX_ACTIONS
    }
    resp = client.post(
        f"/api/projects/{pid_b}/policy",
        data=json.dumps(ask_matrix),
        content_type="application/json",
    )
    assert resp.status_code == 200, (resp.status_code, resp.content)
    raw_b = _read_permissions(FIX_ROOT / "proj_b")
    assert raw_b == ask_matrix, raw_b
    # Nilai project A TIDAK berubah.
    raw_a = _read_permissions(root_a)
    assert raw_a == _DENY_ALL_MATRIX, raw_a
    print("[4] Save via API menulis matrix ke .aether/settings/permissions.json OK")

    # --- [5] Policy INDEPENDEN per project ---------------------------------
    allow_matrix = {
        action: {"inside": "allow", "outside": "allow"} for action in _MATRIX_ACTIONS
    }
    resp = client.post(
        f"/api/projects/{pid_a}/policy",
        data=json.dumps(allow_matrix),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    # B tidak terpengaruh oleh perubahan A.
    got_b2 = client.get(f"/api/projects/{pid_b}/policy").json()
    assert got_b2["matrix"] == ask_matrix, got_b2
    assert _read_permissions(FIX_ROOT / "proj_b") == ask_matrix
    print("[5] policy per project INDEPENDEN (A tidak memengaruhi B) OK")

    # --- [6] Validasi input matrix tidak valid ditolak (400) --------------
    bad = client.post(
        f"/api/projects/{pid_a}/policy",
        data=json.dumps({"read_files": {"inside": "ngawur"}}),
        content_type="application/json",
    )
    assert bad.status_code == 400, (bad.status_code, bad.content)
    assert bad.json()["error"]["code"] == "validation_error", bad.content
    bad2 = client.post(
        f"/api/projects/{pid_a}/policy",
        data=json.dumps({"aksi_tidak_ada": {"inside": "allow"}}),
        content_type="application/json",
    )
    assert bad2.status_code == 400, bad2.content
    bad3 = client.post(
        f"/api/projects/{pid_a}/policy",
        data=json.dumps({"read_files": {"kemana-mana": "allow"}}),
        content_type="application/json",
    )
    assert bad3.status_code == 400, bad3.content
    # Project tidak ada -> 404.
    nf = client.get("/api/projects/tidak_ada/policy")
    assert nf.status_code == 404, (nf.status_code, nf.content)
    print("[6] validasi matrix (400) + project tidak ada (404) OK")

    # --- [7] Enforcement via PermissionManager EXISTING --------------------
    # Set A ke DENY total untuk verifikasi enforcement.
    client.post(
        f"/api/projects/{pid_a}/policy",
        data=json.dumps(_DENY_ALL_MATRIX),
        content_type="application/json",
    )
    proj_matrix = service.project_permission_matrix(pid_a)
    assert isinstance(proj_matrix, PermissionMatrix), proj_matrix
    cfg_project = service.project_permission_config(pid_a)
    assert isinstance(cfg_project, PermissionConfig), cfg_project

    pm = PermissionManager(
        policy=PermissionPolicy(config=cfg_project, matrix=proj_matrix)
    )
    ws_root = str(root_a)
    # DENY (modify outside) -> tidak dijalankan, tanpa approval.
    dec = pm.check(
        "write_file",
        {"path": str(FIX_ROOT / "outside.txt"), "content": "x"},
        workspace_root=ws_root,
    )
    assert dec.allowed is False and dec.mode == PolicyMode.DENY, dec
    # READ di luar workspace (read_files.outside = allow pada default, tapi
    # project A = deny all) -> deny.
    dec_read = pm.check("read_file", {"path": "keep.txt"}, workspace_root=ws_root)
    assert dec_read.allowed is False, dec_read
    print("[7] enforcement matrix via PermissionManager EXISTING OK (deny)")

    # ASK: baca tetap allow, tulis di LUAR workspace -> butuh approval.
    default_matrix = PermissionMatrix.default()
    pm_ask = PermissionManager(
        policy=PermissionPolicy(config=cfg_project, matrix=default_matrix)
    )
    dec_inside = pm_ask.check(
        "write_file", {"path": "a.txt", "content": "x"}, workspace_root=ws_root
    )
    assert dec_inside.allowed is True, dec_inside
    dec_outside = pm_ask.check(
        "write_file",
        {"path": str(FIX_ROOT / "outside.txt"), "content": "x"},
        workspace_root=ws_root,
    )
    assert dec_outside.allowed is False and dec_outside.mode == PolicyMode.DENY, dec_outside
    # Terminal mutating DI DALAM workspace via matrix default = ASK.
    dec_tm = pm_ask.check(
        "run_command", {"command": "python -m pytest -q"}, workspace_root=ws_root
    )
    assert dec_tm.allowed is False, dec_tm
    assert dec_tm.requires_approval is True, dec_tm
    # Terminal read-only di dalam workspace = allow.
    dec_tr = pm_ask.check("run_command", {"command": "git status"}, workspace_root=ws_root)
    assert dec_tr.allowed is True, dec_tr
    # Read-only file di luar workspace (default) = allow.
    dec_read_out = pm_ask.check(
        "read_file", {"path": str(FIX_ROOT / "outside.txt")}, workspace_root=ws_root
    )
    assert dec_read_out.allowed is True, dec_read_out
    print("[7b] enforcement matrix OK (allow / ask / deny + inside/outside)")

    # --- [8] Backward compatible: projekt tanpa policy -> matrix default ---
    assert service.project_permission_matrix("tidak_ada") is None
    service.project_store.clear_active_project()
    assert service.project_permission_matrix(None) is None
    # Project tanpa file policy -> tetap mengembalikan matrix default.
    empty_project = service.create_project(
        name="PolicyEmpty", path=str(FIX_ROOT / "proj_a")
    )
    (root_a / ".aether" / "settings" / "permissions.json").unlink()
    m_default = service.project_permission_matrix(empty_project["id"])
    assert m_default is not None
    assert (
        m_default.mode_for(MatrixAction.MODIFY_FILES, ActionScope.INSIDE)
        == PolicyMode.ALLOW
    ), m_default
    print("[8] backward compatible: tanpa policy -> matrix default OK")

    # --- [9] Project BARU diinisialisasi otomatis --------------------------
    root_new = FIX_ROOT / "proj_new"
    root_new2 = FIX_ROOT / "proj_new2"
    root_new.mkdir(parents=True, exist_ok=True)
    root_new2.mkdir(parents=True, exist_ok=True)
    service.create_project(name="PolicyNew", path=str(root_new))
    raw_new = _read_permissions(root_new)
    assert raw_new == PermissionMatrix.default().to_dict(), raw_new
    # Project LAIN belum dibuat -> file hanya ada di project baru ini.
    assert not _policy_file(root_new2).exists()
    print("[9] project baru otomatis punya .aether/settings/permissions.json (matrix default) OK")

    # --- [10] Perubahan policy TIDAK mengubah default ----------------------
    proj_new = service.create_project(name="PolicyNewB", path=str(root_new))
    resp = client.post(
        f"/api/projects/{proj_new['id']}/policy",
        data=json.dumps(_DENY_ALL_MATRIX),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert _read_permissions(root_new) == _DENY_ALL_MATRIX
    # Project baru BERIKUTNYA tetap memakai matrix default.
    service.create_project(name="PolicyNew2", path=str(root_new2))
    assert _read_permissions(root_new2) == PermissionMatrix.default().to_dict()
    assert PermissionMatrix.default().to_dict() == PermissionMatrix().to_dict()
    print("[10] perubahan policy tidak mengubah default project berikutnya OK")

    # --- [11] Isolasi: setiap project punya permissions.json sendiri --------
    assert _policy_file(root_new).is_file()
    assert _policy_file(root_new2).is_file()
    assert _policy_file(root_new) != _policy_file(root_new2)
    print("[11] tiap project punya permissions.json sendiri (isolasi) OK")

    # --- [12] Boundary frontend: Sidebar -> Projects -> Project Settings ---
    frontend_dir = PROJECT_ROOT / "web" / "frontend" / "src"
    api_js = (frontend_dir / "api.js").read_text(encoding="utf-8")
    for fn in ("getProjectPolicy", "saveProjectPolicy"):
        assert fn in api_js, f"api.js harus mengekspor {fn}"
    assert "/policy" in api_js, "api.js harus memanggil endpoint /policy"

    panel = (frontend_dir / "components" / "ProjectPolicyPanel.vue").read_text(
        encoding="utf-8"
    )
    for fn in ("getProjectPolicy", "saveProjectPolicy"):
        assert fn in panel, f"ProjectPolicyPanel harus memakai {fn}"
    # TIDAK ada logic agent/runtime/policy engine di komponen.
    for bad in ("AgentRuntime", "AgentLoop", "orchestrator", "Replanner"):
        assert bad not in panel, f"ProjectPolicyPanel tidak boleh memuat '{bad}'"

    # App.vue = composition root; handler policy ada di ./composables/*.
    app_vue = (frontend_dir / "App.vue").read_text(encoding="utf-8") + "\n" + "\n".join(
        p.read_text(encoding="utf-8") for p in sorted((frontend_dir / "composables").glob("*.js"))
    )
    assert "ProjectPolicyPanel" in app_vue, "App.vue harus memuat ProjectPolicyPanel"
    assert "openProjectPolicy" in app_vue, "App.vue harus membuka Project Settings/Policy"
    print("[12] boundary frontend OK -> Sidebar->Projects->Policy via gateway")

    # Cleanup.
    shutil.rmtree(workspace, ignore_errors=True)
    shutil.rmtree(tmp_store_dir, ignore_errors=True)

    print()
    print("[OK] Project Permission Matrix bekerja (project-local, enforce allow/ask/deny).")
    return 0


def main() -> int:
    print("=== Verifikasi Project Permission Matrix AETHER (per project) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


if __name__ == "__main__":
    raise SystemExit(main())
