"""Verifikasi Global Settings UX AETHER (`data/settings.json`).

Deterministik & offline: memakai Django test client dengan `SETTINGS_PATH`
diarahkan ke file SEMENTARA (tidak menyentuh `data/settings.json` produksi).

Yang diuji:
    1. `data/settings.json` tetap sumber konfigurasi global TUNGGAL (tidak ada
       file/skema konfigurasi kedua; loader existing dipakai ulang).
    2. GET  /api/settings  -> nilai AKTUAL dari `data/settings.json`.
    3. POST /api/settings  -> menyimpan perubahan ke file yang sama.
    4. Preservasi: key/setting lain (tanpa kontrol UI) TIDAK hilang saat simpan
       (deep-merge, bukan overwrite seluruh file).
    5. Validasi: tipe salah / key tak dikenal / nilai di luar rentang -> 400.
    6. Partial update: section yang tidak dikirim tidak berubah.
    7. Boundary: UX memakai Sidebar -> Settings; SettingsView memakai tab
       terpisah + endpoint /settings; frontend tipis (tanpa logic agent).

Jalankan:
    python scripts/check_global_settings.py
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


def _default_agent_prompt() -> str:
    """System Prompt Agent bawaan (untuk cek bentuk respons GET /api/settings)."""
    from agent_ai.core.agent_prompt import build_agent_system_prompt

    return build_agent_system_prompt()


def _document(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _run() -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    # Django test client memakai host "testserver" -> pastikan diizinkan
    # (override, bukan setdefault: nilai .env/ambiente bisa sudah terisi).
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"

    import django

    django.setup()
    from django.test import Client

    from agent_ai.config import settings as settings_mod
    from agent_ai.llm_config import LLMConfigService
    from agent_ai.projects.registry import ProjectRegistry

    import api.services as services_mod
    from api.project_store import ProjectStore

    # 1) Sumber konfigurasi global TUNGGAL.
    assert settings_mod.SETTINGS_PATH == PROJECT_ROOT / "data" / "settings.json", (
        "SETTINGS_PATH harus menunjuk data/settings.json"
    )
    source = (SRC_DIR / "agent_ai" / "config" / "settings.py").read_text(encoding="utf-8")
    for name in (
        "def global_settings(",
        "def update_global_settings(",
        "def port_setting(",
        "def compression_enabled(",
        "def write_log_response_api(",
        "def api_retry_config(",
    ):
        assert name in source, f"loader/global settings '{name}' tidak ditemukan"
    print("[1] sumber konfigurasi tunggal OK -> data/settings.json + loader existing")

    tmp_dir = Path(tempfile.mkdtemp(prefix="global_settings_", dir=str(DUMMY_ROOT)))
    try:
        settings_path = tmp_dir / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "port": 8123,
                    # Key yang BELUM punya kontrol UI -> wajib dipertahankan.
                    "future_section": {"remember": "me", "n": 7},
                    "compression": {"enabled": False, "level": "high"},
                    "write_log_response_api": True,
                    "api_retry": {"failed_count": 12, "failed_sleep": 3},
                }
            ),
            encoding="utf-8",
        )

        # Arahkan loader ke file sementara (isolasi total).
        previous_path = settings_mod.SETTINGS_PATH
        settings_mod.SETTINGS_PATH = settings_path

        # Isolasi total: GatewayService memakai fixture dummy_test (TIDAK
        # menyentuh data/aether.db maupun registry project produksi).
        services_mod._default_service = services_mod.GatewayService(
            project_registry=ProjectRegistry(workspace=tmp_dir / "projects"),
            project_store=ProjectStore(db_path=tmp_dir / "store.db"),
            llm_config_service=LLMConfigService(
                db_path=tmp_dir / "llm.db", env_path=tmp_dir / ".env"
            ),
            auto_execute=False,
        )
        client = Client()

        def post(payload: dict):
            return client.post(
                "/api/settings/update",
                data=json.dumps(payload),
                content_type="application/json",
            )

        try:
            # 2) GET -> nilai AKTUAL.
            resp = client.get("/api/settings")
            assert resp.status_code == 200, (resp.status_code, resp.content)
            got = resp.json()["settings"]
            assert got == {
                "port": 8123,
                "compression": {"enabled": False},
                "write_log_response_api": True,
                "api_retry": {"failed_count": 12, "failed_sleep": 3.0},
                "agent": {
                    "system_prompt": _default_agent_prompt(),
                    "default_system_prompt": _default_agent_prompt(),
                },
            }, got
            print(f"[2] GET /api/settings OK -> nilai aktual = {got}")

            # 3) POST -> menulis ke file yang SAMA, nilai langsung berubah.
            resp = post({"port": 9001, "write_log_response_api": False})
            assert resp.status_code == 200, (resp.status_code, resp.content)
            updated = resp.json()["settings"]
            assert updated["port"] == 9001, updated
            assert updated["write_log_response_api"] is False, updated
            on_disk = _document(settings_path)
            assert on_disk["port"] == 9001, on_disk
            assert on_disk["write_log_response_api"] is False, on_disk
            print("[3] POST /api/settings OK -> perubahan tersimpan ke data/settings.json")

            # 4) PRESERVASI: key tanpa kontrol UI TIDAK hilang.
            assert on_disk["future_section"] == {"remember": "me", "n": 7}, on_disk
            # Sub-key tanpa kontrol UI pada section yang DISENTUH juga tetap ada.
            assert on_disk["compression"]["level"] == "high", on_disk
            assert on_disk["api_retry"] == {"failed_count": 12, "failed_sleep": 3}, on_disk
            print("[4] preservasi OK -> key di luar UI (future_section, compression.level) tetap utuh")

            # 5) Validasi: tipe salah / key tak dikenal / di luar rentang -> 400.
            for bad in (
                {"port": "bukan-angka"},
                {"port": 70000},
                {"write_log_response_api": "yes"},
                {"api_retry": {"failed_count": -1}},
                {"api_retry": {"failed_sleep": "abc"}},
                {"compression": {"enabled": 3}},
                {"tidak_dikenal": True},
            ):
                resp = post(bad)
                assert resp.status_code == 400, (bad, resp.status_code, resp.content)
                assert resp.json()["error"]["code"] == "validation_error", resp.json()
            # File TIDAK berubah akibat payload tidak valid.
            assert _document(settings_path)["port"] == 9001, "file berubah saat payload invalid"
            print("[5] validasi OK -> 400 untuk tipe/key/rentang tidak valid (file tidak berubah)")

            # 6) Partial update: section lain tidak berubah.
            resp = post({"compression": {"enabled": True}})
            assert resp.status_code == 200, (resp.status_code, resp.content)
            after = _document(settings_path)
            assert after["compression"] == {"enabled": True, "level": "high"}, after
            assert after["port"] == 9001 and after["api_retry"]["failed_count"] == 12, after
            print("[6] partial update OK -> compression.enabled berubah, section lain tetap")

            # Nilai aktual mengikuti file (sumber kebenaran = file).
            resp = client.get("/api/settings")
            assert resp.json()["settings"]["compression"]["enabled"] is True
            print("[6b] GET mengikuti nilai file terbaru OK")
        finally:
            settings_mod.SETTINGS_PATH = previous_path
            services_mod._default_service = None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # 7) Boundary frontend & routing.
    urls_src = (DJANGO_APP_DIR / "api" / "urls.py").read_text(encoding="utf-8")
    assert '"settings"' in urls_src and "global_settings" in urls_src, "route /api/settings belum ada"
    views_src = (DJANGO_APP_DIR / "api" / "views.py").read_text(encoding="utf-8")
    assert "def global_settings" in views_src, "view global settings belum ada"
    services_src = (DJANGO_APP_DIR / "api" / "services.py").read_text(encoding="utf-8")
    assert "from agent_ai.config.settings import" in services_src, (
        "gateway harus memakai loader konfigurasi AETHER existing"
    )

    api_js = (FRONTEND_DIR / "api.js").read_text(encoding="utf-8")
    for fn in ("getGlobalSettings", "updateGlobalSettings"):
        assert fn in api_js, f"api.js harus mengekspor {fn}"
    assert "/settings" in api_js, "api.js harus memanggil /settings"

    panel = (FRONTEND_DIR / "components" / "GlobalSettingsPanel.vue").read_text(encoding="utf-8")
    assert "getGlobalSettings" in panel and "updateGlobalSettings" in panel, panel[:200]
    for bad in ("AgentRuntime", "AgentLoop", "orchestrator", "Replanner"):
        assert bad not in panel, f"GlobalSettingsPanel tidak boleh memuat '{bad}'"
    for section in ("Server", "Conversation", "Logging", "API Retry"):
        assert section in panel, f"section '{section}' harus ada (settings dipisah per section)"
    for key in ("port", "compression_enabled", "write_log_response_api", "failed_count", "failed_sleep"):
        assert key in panel, f"kontrol '{key}' harus ada di panel"

    settings_vue = (FRONTEND_DIR / "components" / "SettingsView.vue").read_text(encoding="utf-8")
    assert "GlobalSettingsPanel" in settings_vue, "SettingsView harus memuat GlobalSettingsPanel"
    assert "AgentSettingsPanel" in settings_vue, "SettingsView harus memuat AgentSettingsPanel"
    assert 'activeTab' in settings_vue and "sv-tabs" in settings_vue, (
        "SettingsView harus memisahkan settings ke tab/section"
    )
    assert "Agent" in settings_vue, "tab Settings -> Agent harus ada"

    # Panel Agent (System Prompt): memakai endpoint settings yang SAMA, tanpa
    # sistem prompt/konfigurasi kedua dan tanpa logic agent di frontend.
    agent_panel = (FRONTEND_DIR / "components" / "AgentSettingsPanel.vue").read_text(
        encoding="utf-8"
    )
    assert "getGlobalSettings" in agent_panel and "updateGlobalSettings" in agent_panel, (
        agent_panel[:200]
    )
    assert "system_prompt" in agent_panel, "editor harus memuat system_prompt"
    for bad in ("AgentRuntime", "AgentLoop", "orchestrator", "Replanner", "localStorage"):
        assert bad not in agent_panel, f"AgentSettingsPanel tidak boleh memuat '{bad}'"
    # Sidebar -> Settings sudah menu existing; pastikan item Settings tetap ada.
    app_vue = (FRONTEND_DIR / "App.vue").read_text(encoding="utf-8")
    assert 'activeNav = \'settings\'' in app_vue, "Sidebar -> Settings harus tetap tersedia"
    print(
        "[7] boundary frontend OK -> Sidebar Settings, tab terpisah, "
        "endpoint /settings, tanpa logic agent"
    )

    # 8) System Prompt Agent TIDAK membuat sistem prompt kedua: agent_prompt.py
    #    tetap satu-satunya sumber default, dan loader membaca dari settings.
    settings_src = (SRC_DIR / "agent_ai" / "config" / "settings.py").read_text(
        encoding="utf-8"
    )
    assert "def agent_system_prompt(" in settings_src, "loader System Prompt Agent belum ada"
    assert "build_agent_system_prompt" in settings_src, (
        "default harus memakai System Prompt Agent existing (tanpa sistem kedua)"
    )
    runtime_src = (SRC_DIR / "agent_ai" / "runtime" / "runtime.py").read_text(
        encoding="utf-8"
    )
    assert "agent_system_prompt" in runtime_src, (
        "AgentRuntime harus memakai System Prompt dari settings"
    )
    print("[8] integrasi Agent OK -> settings sebagai instruction dasar (tanpa prompt kedua)")

    print()
    print("[OK] Global Settings UX AETHER bekerja (data/settings.json sumber tunggal, merge preservasi).")
    return 0


def main() -> int:
    print("=== Verifikasi Global Settings UX AETHER (data/settings.json) ===")
    return _run()


if __name__ == "__main__":
    raise SystemExit(main())
