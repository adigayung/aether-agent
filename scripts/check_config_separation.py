"""Verifikasi PEMISAHAN KONFIGURASI AETHER (Task lanjutan setelah Task 2 & 3).

Tujuan: memastikan DUA surface konfigurasi TIDAK PERNAH tercampur dan tetap
memakai sumber konfigurasi existing masing-masing:

    - Sidebar -> Settings        = GLOBAL AETHER SETTINGS
                                   (`data/settings.json`, loader existing
                                   `agent_ai.config.settings`).
    - Sidebar -> Projects        = PROJECT SETTINGS / POLICY
                                   (`<root>/.aether/permissions.json`,
                                   ProjectPermissionStore existing).

Yang diuji:
    1. Sumber konfigurasi TETAP terpisah (dua file, dua store, tanpa skema baru).
    2. Global Settings MENOLAK key policy/permission project (mode/scope/...),
       file `data/settings.json` TIDAK berubah.
    3. Project Policy MENOLAK key Global Settings (port/compression/...),
       file project `permissions.json` TIDAK berubah.
    4. Perubahan Global Settings TIDAK menyentuh policy project mana pun.
    5. Perubahan Project Policy TIDAK menyentuh `data/settings.json`.
    6. Isolasi antar-project dipertahankan (policy A != policy B) walaupun
       Global Settings berubah.
    7. Boundary frontend: dua panel terpisah, endpoint berbeda, tidak ada
       panel policy yang di-mount di SettingsView / sebaliknya.
    8. Tidak ada subsystem konfigurasi kedua (endpoint existing dipakai ulang).

Deterministik & offline: Django test client dengan `SETTINGS_PATH` diarahkan ke
file SEMENTARA dan registry/store SQLite di fixture `dummy_test` (tidak
menyentuh `data/settings.json`, `data/aether.db`, maupun project produksi).

Jalankan:
    python scripts/check_config_separation.py
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
FIX_ROOT = DUMMY_ROOT / "config_separation_fixture"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _permissions_path(root: Path) -> Path:
    return root / ".aether" / "permissions.json"


def _run() -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"

    import django

    django.setup()
    from django.test import Client

    from agent_ai.config import settings as settings_mod
    from agent_ai.llm_config import LLMConfigService
    from agent_ai.projects.registry import ProjectRegistry

    import api.services as services_mod
    from api.project_store import ProjectStore

    shutil.rmtree(FIX_ROOT, ignore_errors=True)
    root_a = FIX_ROOT / "proj_a"
    root_b = FIX_ROOT / "proj_b"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)

    # --- [1] Sumber konfigurasi TETAP terpisah (dua file, dua store) --------
    assert settings_mod.SETTINGS_PATH == PROJECT_ROOT / "data" / "settings.json", (
        "Global Settings harus tetap memakai data/settings.json (sumber tunggal)"
    )
    from agent_ai.projects.permissions import (
        PERMISSIONS_FILE_NAME,
        ProjectPermissionStore,
    )

    assert PERMISSIONS_FILE_NAME == "permissions.json", PERMISSIONS_FILE_NAME
    store_probe = ProjectPermissionStore(root=root_a)
    # Dua lokasi file yang benar-benar berbeda -> tidak ada file konfigurasi
    # gabungan baru.
    assert store_probe.path != settings_mod.SETTINGS_PATH
    assert store_probe.path.name == "permissions.json"
    assert settings_mod.SETTINGS_PATH.name == "settings.json"
    print("[1] sumber konfigurasi terpisah OK -> settings.json vs permissions.json")

    # --- Setup gateway terisolasi (SETTINGS_PATH + registry + SQLite) ------
    tmp_dir = Path(tempfile.mkdtemp(prefix="config_sep_", dir=str(DUMMY_ROOT)))
    settings_path = tmp_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "port": 8123,
                "compression": {"enabled": False},
                "write_log_response_api": True,
                "api_retry": {"failed_count": 9, "failed_sleep": 2},
            }
        ),
        encoding="utf-8",
    )
    previous_path = settings_mod.SETTINGS_PATH
    settings_mod.SETTINGS_PATH = settings_path

    workspace = FIX_ROOT / "registry"
    registry = ProjectRegistry(workspace=workspace)
    store = ProjectStore(db_path=tmp_dir / "store.db")
    service = services_mod.GatewayService(
        project_registry=registry,
        project_store=store,
        llm_config_service=LLMConfigService(
            db_path=tmp_dir / "llm.db", env_path=tmp_dir / ".env"
        ),
        auto_execute=False,
    )
    services_mod._default_service = service
    client = Client()

    try:
        proj_a = service.create_project(name="SepA", path=str(root_a))
        proj_b = service.create_project(name="SepB", path=str(root_b))
        pid_a, pid_b = proj_a["id"], proj_b["id"]

        def post_settings(payload: dict):
            return client.post(
                "/api/settings/update",
                data=json.dumps(payload),
                content_type="application/json",
            )

        def post_policy(pid: str, payload: dict):
            return client.post(
                f"/api/projects/{pid}/policy",
                data=json.dumps(payload),
                content_type="application/json",
            )

        # Snapshot awal file global.
        settings_before = _read_json(settings_path)

        # --- [2] Global Settings MENOLAK key policy project ----------------
        for bad in (
            {"mode": "deny"},
            {"scope": "outside"},
            {"policy": {}},
            {"permission": "allow"},
            {"mode": "deny", "port": 9000},
        ):
            resp = post_settings(bad)
            assert resp.status_code == 400, (bad, resp.status_code, resp.content)
            assert resp.json()["error"]["code"] == "validation_error", resp.json()
        # File global TIDAK berubah (tidak menyimpan policy project).
        assert _read_json(settings_path) == settings_before, (
            "data/settings.json berubah saat menerima key policy project"
        )
        # Key policy TIDAK pernah tersimpan di file global.
        doc = _read_json(settings_path)
        for key in ("mode", "scope", "policy", "permission"):
            assert key not in doc, f"key project policy '{key}' bocor ke settings.json"
        print("[2] Global Settings menolak key Project Policy OK (file global utuh)")

        # --- [3] Project Policy MENOLAK key Global Settings ----------------
        # Set policy valid dulu sebagai baseline project A (matrix).
        from agent_ai.permission.matrix import DEFAULT_MATRIX_RULES

        allow_matrix = {
            action: {"inside": "allow", "outside": "allow"}
            for action in DEFAULT_MATRIX_RULES
        }
        resp = post_policy(pid_a, allow_matrix)
        assert resp.status_code == 200, (resp.status_code, resp.content)
        policy_a_before = _read_json(_permissions_path(root_a))
        for bad in (
            {"port": 9000},
            {"compression": {"enabled": True}},
            {"write_log_response_api": False},
            {"api_retry": {"failed_count": 1}},
            {"port": 9000, "read_files": {"inside": "allow"}},
        ):
            resp = post_policy(pid_a, bad)
            assert resp.status_code == 400, (bad, resp.status_code, resp.content)
        # File policy project TIDAK berubah.
        assert _read_json(_permissions_path(root_a)) == policy_a_before, (
            "permissions.json berubah saat menerima key Global Settings"
        )
        # Key global TIDAK pernah tersimpan di permissions.json.
        pol = _read_json(_permissions_path(root_a))
        assert set(pol) == set(DEFAULT_MATRIX_RULES), pol
        print("[3] Project Policy menolak key Global Settings OK (file policy utuh)")

        # --- [4] Perubahan Global Settings TIDAK menyentuh policy project ---
        resp = post_settings({"port": 9200, "api_retry": {"failed_count": 4}})
        assert resp.status_code == 200, (resp.status_code, resp.content)
        # Policy project A & B TIDAK berubah oleh perubahan global.
        assert _read_json(_permissions_path(root_a)) == policy_a_before, (
            "policy project A berubah karena Global Settings"
        )
        assert _read_json(_permissions_path(root_b)) == DEFAULT_MATRIX_RULES, _read_json(
            _permissions_path(root_b)
        )
        print("[4] Global Settings tidak menyentuh Project Policy OK")

        # --- [5] Perubahan Project Policy TIDAK menyentuh settings.json -----
        settings_before2 = _read_json(settings_path)
        ask_matrix = {
            action: {"inside": "ask", "outside": "deny"}
            for action in DEFAULT_MATRIX_RULES
        }
        resp = post_policy(pid_b, ask_matrix)
        assert resp.status_code == 200, (resp.status_code, resp.content)
        assert _read_json(_permissions_path(root_b)) == ask_matrix, _read_json(
            _permissions_path(root_b)
        )
        assert _read_json(settings_path) == settings_before2, (
            "data/settings.json berubah karena Project Policy"
        )
        print("[5] Project Policy tidak menyentuh Global Settings OK")

        # --- [6] Isolasi antar-project dipertahankan ------------------------
        # A allow-all; B ask/deny; keduanya berbeda file.
        assert _read_json(_permissions_path(root_a)) != _read_json(
            _permissions_path(root_b)
        ), "policy A == policy B (isolasi rusak)"
        assert _permissions_path(root_a) != _permissions_path(root_b)
        # Nilai Global Settings tetap terbaca konsisten setelah semua perubahan.
        resp = client.get("/api/settings")
        assert resp.status_code == 200, resp.content
        got = resp.json()["settings"]
        assert got["port"] == 9200, got
        assert got["api_retry"]["failed_count"] == 4, got
        # GET policy per project mengembalikan nilai AKTUAL masing-masing.
        ga = client.get(f"/api/projects/{pid_a}/policy").json()
        gb = client.get(f"/api/projects/{pid_b}/policy").json()
        assert ga["matrix"] == allow_matrix, ga
        assert gb["matrix"] == ask_matrix, gb
        print("[6] isolasi antar-project tetap OK setelah Global Settings berubah")
    finally:
        settings_mod.SETTINGS_PATH = previous_path
        services_mod._default_service = None

    # --- [7] Boundary frontend: dua panel terpisah, endpoint berbeda -------
    api_js = (FRONTEND_DIR / "api.js").read_text(encoding="utf-8")
    # Endpoint global & project policy BERBEDA.
    assert "/settings" in api_js and "/settings/update" in api_js, (
        "api.js harus memanggil endpoint Global Settings"
    )
    assert "/policy" in api_js, "api.js harus memanggil endpoint Project Policy"
    for fn in ("getGlobalSettings", "updateGlobalSettings"):
        assert fn in api_js, f"api.js harus mengekspor {fn}"
    for fn in ("getProjectPolicy", "saveProjectPolicy"):
        assert fn in api_js, f"api.js harus mengekspor {fn}"

    global_panel = (FRONTEND_DIR / "components" / "GlobalSettingsPanel.vue").read_text(
        encoding="utf-8"
    )
    policy_panel = (FRONTEND_DIR / "components" / "ProjectPolicyPanel.vue").read_text(
        encoding="utf-8"
    )
    # Panel GLOBAL hanya memakai API Global Settings; TIDAK memakai API Project
    # Policy (bukan sekadar menyebut namanya sebagai teks petunjuk).
    assert "getGlobalSettings" in global_panel and "updateGlobalSettings" in global_panel
    for bad in ("getProjectPolicy", "saveProjectPolicy", "/projects/"):
        assert bad not in global_panel, (
            f"GlobalSettingsPanel tidak boleh memakai API Project Policy ('{bad}')"
        )
    # Panel PROJECT hanya memakai API policy; TIDAK memuat setting global.
    assert "getProjectPolicy" in policy_panel and "saveProjectPolicy" in policy_panel
    for bad in (
        "getGlobalSettings",
        "updateGlobalSettings",
        "/settings",
        "settings.json",
    ):
        assert bad not in policy_panel, (
            f"ProjectPolicyPanel tidak boleh memuat '{bad}' (itu Global Settings)"
        )
    # TIDAK ada logic agent/runtime di kedua panel.
    for panel in (global_panel, policy_panel):
        for bad in ("AgentRuntime", "AgentLoop", "orchestrator", "Replanner"):
            assert bad not in panel, f"panel tidak boleh memuat '{bad}'"
    # Panel GLOBAL menandai scope-nya sendiri (Global AETHER Settings) agar user
    # tahu policy project TIDAK di sini.
    assert "Global AETHER Settings" in global_panel, (
        "GlobalSettingsPanel harus menandai dirinya sebagai Global AETHER Settings"
    )

    # SettingsView HANYA memuat GlobalSettingsPanel (bukan panel policy).
    settings_vue = (FRONTEND_DIR / "components" / "SettingsView.vue").read_text(
        encoding="utf-8"
    )
    assert "GlobalSettingsPanel" in settings_vue, "SettingsView harus memuat GlobalSettingsPanel"
    assert "ProjectPolicyPanel" not in settings_vue, (
        "Project Policy TIDAK boleh di-mount di Settings (global)"
    )
    # GlobalSettingsPanel tidak di-mount di page Projects.
    # App.vue = composition root; handler policy ada di ./composables/*.
    app_vue = (FRONTEND_DIR / "App.vue").read_text(encoding="utf-8") + "\n" + "\n".join(
        p.read_text(encoding="utf-8") for p in sorted((FRONTEND_DIR / "composables").glob("*.js"))
    )
    assert "ProjectPolicyPanel" in app_vue, "App.vue harus memuat ProjectPolicyPanel"
    assert "openProjectPolicy" in app_vue, "App.vue harus membuka Project Settings/Policy"
    assert "GlobalSettingsPanel" not in app_vue, (
        "GlobalSettingsPanel TIDAK boleh di-mount di page Projects"
    )
    # Sidebar Settings tetap ada (Global Settings AETHER).
    assert "activeNav = 'settings'" in app_vue, "Sidebar -> Settings harus tetap ada"
    assert "activeNav === 'projects'" in app_vue, "Sidebar -> Projects harus tetap ada"
    print("[7] boundary frontend OK -> dua panel & dua endpoint terpisah")

    # --- [8] Tidak ada subsystem konfigurasi kedua -------------------------
    services_src = (DJANGO_APP_DIR / "api" / "services.py").read_text(encoding="utf-8")
    # Gateway memakai loader/global settings & permission store EXISTING.
    assert "from agent_ai.config.settings import" in services_src, (
        "gateway harus memakai loader konfigurasi global existing"
    )
    assert "ProjectPermissionStore" in services_src, (
        "gateway harus memakai ProjectPermissionStore existing"
    )
    # View global settings & project policy terpisah (dua route, dua view).
    urls_src = (DJANGO_APP_DIR / "api" / "urls.py").read_text(encoding="utf-8")
    assert '"settings"' in urls_src and "global_settings" in urls_src, (
        "route /api/settings belum ada"
    )
    assert "project_policy" in urls_src and "/policy" in urls_src.replace(
        '"projects/<str:project_id>/policy"', "/policy"
    ), "route Project Policy belum ada"
    views_src = (DJANGO_APP_DIR / "api" / "views.py").read_text(encoding="utf-8")
    assert "def global_settings" in views_src and "def project_policy" in views_src, (
        "view Global Settings & Project Policy harus terpisah"
    )
    print("[8] boundary backend OK -> endpoint/store existing dipakai ulang (tanpa config kedua)")

    print()
    print(
        "[OK] Pemisahan konfigurasi AETHER bekerja: Global Settings vs "
        "Project Settings/Policy terpisah & tidak tercampur."
    )
    return 0


def main() -> int:
    print("=== Verifikasi Pemisahan Konfigurasi AETHER (Global vs Project) ===")
    try:
        return _run()
    finally:
        shutil.rmtree(FIX_ROOT, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
