"""Verifikasi Project Policy / Permission UX AETHER (per project).

Membuktikan:
    1. Policy disimpan project-local di `<root>/.aether/permissions.json`.
    2. Mode existing dipertahankan: ALLOW / ASK (require_approval) / DENY.
    3. Scope existing dipertahankan: workspace (inside) / outside.
    4. Policy per project INDEPENDEN (project A TIDAK memengaruhi project B).
    5. GET mengembalikan nilai AKTUAL project tersebut.
    6. Save melalui API menulis kembali ke `.aether/permissions.json`.
    7. Enforcement: policy di-enforce PermissionManager EXISTING (bukan sistem
       permission kedua).
    8. Backward compatible: tanpa policy -> executor memakai default.

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


def setup_fixture() -> None:
    shutil.rmtree(FIX_ROOT, ignore_errors=True)
    for name in ("proj_a", "proj_b"):
        target = FIX_ROOT / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "keep.txt").write_text("jangan dihapus\n", encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIX_ROOT, ignore_errors=True)


def _read_permissions(root: Path) -> dict:
    path = root / ".aether" / "permissions.json"
    assert path.is_file(), f"permissions.json harus ada di {path}"
    return json.loads(path.read_text(encoding="utf-8"))


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
    from agent_ai.projects.permissions import (
        MODE_OPTIONS,
        SCOPE_OPTIONS,
        ProjectPermissionStore,
        ProjectPolicy,
    )
    from agent_ai.projects.registry import ProjectRegistry
    from agent_ai.permission import PermissionConfig, PolicyMode

    # --- [1] Model policy: mode + scope existing dipertahankan -------------
    assert {o["value"] for o in MODE_OPTIONS} == {
        "allow",
        "require_approval",
        "deny",
    }, MODE_OPTIONS
    assert {o["label"] for o in MODE_OPTIONS} == {"ALLOW", "ASK", "DENY"}, MODE_OPTIONS
    assert {o["value"] for o in SCOPE_OPTIONS} == {"workspace", "outside"}, SCOPE_OPTIONS
    assert {o["label"] for o in SCOPE_OPTIONS} == {
        "Inside workspace",
        "Outside workspace",
    }, SCOPE_OPTIONS
    # Alias "ask" (UI) == PolicyMode.REQUIRE_APPROVAL.
    assert ProjectPolicy(mode="ask").mode == PolicyMode.REQUIRE_APPROVAL.value
    assert ProjectPolicy(mode="ask").mode_enum() == PolicyMode.REQUIRE_APPROVAL
    print("[1] mode ALLOW/ASK/DENY + scope existing dipertahankan OK")

    # --- [2] Store project-local: `<root>/.aether/permissions.json` --------
    root_a = FIX_ROOT / "proj_a"
    store_a = ProjectPermissionStore(root=root_a)
    assert not store_a.exists()
    default = store_a.load()
    assert default.mode == PolicyMode.ALLOW.value and default.scope == "workspace"
    store_a.save(ProjectPolicy(mode="deny", scope="outside"))
    raw = _read_permissions(root_a)
    assert raw == {"mode": "deny", "scope": "outside"}, raw
    print("[2] policy tersimpan project-local di .aether/permissions.json OK")

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

    # --- [3] GET mengembalikan nilai AKTUAL project -----------------------
    resp = client.get(f"/api/projects/{pid_a}/policy")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    got = resp.json()
    assert got["mode"] == "deny" and got["scope"] == "outside", got
    assert got["mode_label"] == "DENY", got
    assert got["scope_label"] == "Outside workspace", got
    assert got["options"]["mode"] and got["options"]["scope"], got
    # Project B belum punya policy -> default (TIDAK sama dengan A).
    resp_b = client.get(f"/api/projects/{pid_b}/policy")
    got_b = resp_b.json()
    assert got_b["mode"] == "allow" and got_b["scope"] == "workspace", got_b
    print("[3] GET mengembalikan nilai policy AKTUAL per project OK")

    # --- [4] Save via API -> menulis `.aether/permissions.json` -----------
    resp = client.post(
        f"/api/projects/{pid_b}/policy",
        data=json.dumps({"mode": "ask", "scope": "outside"}),
        content_type="application/json",
    )
    assert resp.status_code == 200, (resp.status_code, resp.content)
    saved = resp.json()
    assert saved["mode"] == "require_approval" and saved["mode_label"] == "ASK", saved
    assert saved["scope"] == "outside", saved
    raw_b = _read_permissions(FIX_ROOT / "proj_b")
    assert raw_b == {"mode": "require_approval", "scope": "outside"}, raw_b
    # Nilai project A TIDAK berubah.
    raw_a = _read_permissions(root_a)
    assert raw_a == {"mode": "deny", "scope": "outside"}, raw_a
    print("[4] Save via API menulis .aether/permissions.json (per project) OK")

    # --- [5] Policy INDEPENDEN per project ---------------------------------
    resp = client.post(
        f"/api/projects/{pid_a}/policy",
        data=json.dumps({"mode": "allow", "scope": "workspace"}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    # B tidak terpengaruh oleh perubahan A.
    got_b2 = client.get(f"/api/projects/{pid_b}/policy").json()
    assert got_b2["mode"] == "require_approval" and got_b2["scope"] == "outside", got_b2
    assert _read_permissions(FIX_ROOT / "proj_b") == {
        "mode": "require_approval",
        "scope": "outside",
    }
    print("[5] policy per project INDEPENDEN (A tidak memengaruhi B) OK")

    # --- [6] Validasi input tidak valid ditolak (400) ---------------------
    bad = client.post(
        f"/api/projects/{pid_a}/policy",
        data=json.dumps({"mode": "ngawur"}),
        content_type="application/json",
    )
    assert bad.status_code == 400, (bad.status_code, bad.content)
    assert bad.json()["error"]["code"] == "validation_error", bad.content
    bad2 = client.post(
        f"/api/projects/{pid_a}/policy",
        data=json.dumps({"scope": "kemana-mana"}),
        content_type="application/json",
    )
    assert bad2.status_code == 400, bad2.content
    # Project tidak ada -> 404.
    nf = client.get("/api/projects/tidak_ada/policy")
    assert nf.status_code == 404, (nf.status_code, nf.content)
    print("[6] validasi input policy (400) + project tidak ada (404) OK")

    # --- [7] Enforcement via PermissionManager EXISTING --------------------
    cfg_deny = service.project_permission_config(pid_a)
    assert isinstance(cfg_deny, PermissionConfig), cfg_deny
    cfg_deny_policy = ProjectPolicy.from_dict(_read_permissions(root_a))
    # Set A ke DENY untuk verifikasi enforcement, lalu baca config efektif.
    client.post(
        f"/api/projects/{pid_a}/policy",
        data=json.dumps({"mode": "deny", "scope": "workspace"}),
        content_type="application/json",
    )
    cfg_deny = service.project_permission_config(pid_a)
    assert cfg_deny is not None
    assert cfg_deny.workspace_write == PolicyMode.DENY, cfg_deny
    assert cfg_deny.delete_move == PolicyMode.DENY, cfg_deny
    assert cfg_deny.read_only == PolicyMode.ALLOW, cfg_deny
    # cfg_deny_policy tidak dipakai lebih lanjut (dibuat untuk tipe-check).
    assert cfg_deny_policy is not None

    from agent_ai.permission import PermissionManager, PermissionPolicy

    pm = PermissionManager(policy=PermissionPolicy(config=cfg_deny))
    dec = pm.check("write_file", {"path": "x.txt", "content": "y"})
    assert dec.allowed is False and dec.mode == PolicyMode.DENY, dec
    dec_read = pm.check("read_file", {"path": "x.txt"})
    assert dec_read.allowed is True, dec_read
    print("[7] enforcement via PermissionManager EXISTING OK (deny/allow)")

    # --- [8] Backward compatible: tanpa policy -> default ----------------
    # Project tidak dikenal -> None (tidak ada policy project).
    assert service.project_permission_config("tidak_ada") is None
    # Tanpa active project & tanpa project_id -> None.
    service.project_store.clear_active_project()
    assert service.project_permission_config(None) is None
    # Project tanpa file policy -> tetap mengembalikan config default (ALLOW).
    empty_project = service.create_project(
        name="PolicyEmpty", path=str(FIX_ROOT / "proj_a")
    )
    # Hapus permissions.json untuk mensimulasikan project tanpa policy.
    (root_a / ".aether" / "permissions.json").unlink()
    cfg_default = service.project_permission_config(empty_project["id"])
    assert cfg_default is not None and cfg_default.workspace_write == PolicyMode.ALLOW
    print("[8] backward compatible: tanpa policy -> config default OK")

    # --- [9] Default Project Policy: project BARU diinisialisasi otomatis ---
    # Saat project baru dibuat, `<root>/.aether/permissions.json` dibuat dari
    # Default Project Policy (baseline) yang berlaku, pada project yang BARU
    # dibuat (bukan project lain).
    from agent_ai.projects.permissions import (
        DEFAULT_PROJECT_POLICY_MODE,
        DEFAULT_PROJECT_POLICY_SCOPE,
    )

    assert DEFAULT_PROJECT_POLICY_MODE == "allow", DEFAULT_PROJECT_POLICY_MODE
    assert DEFAULT_PROJECT_POLICY_SCOPE == "workspace", DEFAULT_PROJECT_POLICY_SCOPE

    root_new = FIX_ROOT / "proj_new"
    root_new2 = FIX_ROOT / "proj_new2"
    root_new.mkdir(parents=True, exist_ok=True)
    root_new2.mkdir(parents=True, exist_ok=True)
    proj_new = service.create_project(name="PolicyNew", path=str(root_new))
    # File dibuat otomatis pada project yang baru dibuat.
    raw_new = _read_permissions(root_new)
    assert raw_new == {"mode": "allow", "scope": "workspace"}, raw_new
    # Project LAIN belum tentu dibuat -> file hanya ada di project baru ini.
    assert not (root_new2 / ".aether" / "permissions.json").exists()
    print("[9] project baru otomatis punya .aether/permissions.json (dari Default Policy) OK")

    # --- [10] Perubahan policy TIDAK mengubah default ----------------------
    # Ubah policy project baru ke deny/outside lewat API.
    resp = client.post(
        f"/api/projects/{proj_new['id']}/policy",
        data=json.dumps({"mode": "deny", "scope": "outside"}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert _read_permissions(root_new) == {"mode": "deny", "scope": "outside"}
    # Project baru BERIKUTNYA tetap memakai Default Project Policy (allow/workspace).
    proj_new2 = service.create_project(name="PolicyNew2", path=str(root_new2))
    assert _read_permissions(root_new2) == {
        "mode": "allow",
        "scope": "workspace",
    }, _read_permissions(root_new2)
    # Default policy di modul TIDAK berubah oleh perubahan project.
    assert ProjectPolicy.default().mode == "allow"
    assert ProjectPolicy.default().scope == "workspace"
    print("[10] perubahan policy tidak mengubah Default Policy project berikutnya OK")

    # --- [11] Isolasi: setiap project punya permissions.json sendiri --------
    # File policy terpisah per project (project-local), nilai masing-masing
    # independen.
    assert (root_new / ".aether" / "permissions.json").is_file()
    assert (root_new2 / ".aether" / "permissions.json").is_file()
    assert (root_new / ".aether" / "permissions.json") != (
        root_new2 / ".aether" / "permissions.json"
    )
    # Save ke proj_new2 TIDAK mengubah proj_new.
    client.post(
        f"/api/projects/{proj_new2['id']}/policy",
        data=json.dumps({"mode": "deny", "scope": "workspace"}),
        content_type="application/json",
    )
    assert _read_permissions(root_new2) == {"mode": "deny", "scope": "workspace"}
    assert _read_permissions(root_new) == {"mode": "deny", "scope": "outside"}
    print("[11] tiap project punya permissions.json sendiri (isolasi) OK")

    # --- [12] Boundary frontend: Sidebar -> Projects -> Project Settings ----
    # Policy dikelola dari Projects (per project), bukan Settings global, dan
    # frontend HANYA memanggil HTTP gateway (tanpa policy engine kedua).
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
    # Opsi mode/scope dari backend (TIDAK di-hardcode di frontend).
    assert "options.mode" in panel and "options.scope" in panel, (
        "ProjectPolicyPanel harus membaca opsi dari backend"
    )

    app_vue = (frontend_dir / "App.vue").read_text(encoding="utf-8")
    assert "ProjectPolicyPanel" in app_vue, "App.vue harus memuat ProjectPolicyPanel"
    assert "openProjectPolicy" in app_vue, "App.vue harus membuka Project Settings/Policy"
    print("[12] boundary frontend OK -> Sidebar->Projects->Policy via gateway, tanpa policy kedua")

    # Cleanup.
    shutil.rmtree(workspace, ignore_errors=True)
    shutil.rmtree(tmp_store_dir, ignore_errors=True)

    print()
    print("[OK] Project Policy per project bekerja (project-local, tidak mengubah project lain).")
    return 0


def main() -> int:
    print("=== Verifikasi Project Policy / Permission UX (per project) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


if __name__ == "__main__":
    raise SystemExit(main())
