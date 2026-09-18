"""Verifikasi halaman Settings web (#50) — konfigurasi LLM.

Deterministik, tanpa API key/model/API cloud. Memakai Django test client.
Fixture (.env + SQLite + workspace) dibuat di
J:\\Agent_Ai\\dummy_test\\web_settings_fixture dan dibersihkan setelah test.

Yang diuji:
    1. Django project dapat di-load
    2. GET  /api/llm/config    -> credential/provider_type/provider instance
    3. POST /api/llm/credentials -> set API key .env (hanya masked, tanpa secret)
    4. validasi credential (nama tidak valid -> 400)
    5. POST /api/llm/providers  -> provider instance (api_key_present, tanpa secret)
    6. konflik provider (nama duplikat -> 409)
    7. validasi provider (type tidak dikenal / butuh api key -> 400)
    8. POST /api/llm/models     -> model + konflik duplikat -> 409 / provider 404
    9. PUT  provider & model    -> update enabled
   10. GET  /api/config         -> regresi tetap jalan (instances tanpa secret)
   11. DELETE credential        -> relasi (400 tanpa force, 200 dengan force)
   12. DELETE model & provider  -> 200 lalu 404
   13. method tidak diizinkan   -> 405
   14. boundary: tidak ada secret yang bocor dari API
   15. boundary: memakai facade LLMConfigService AETHER (tanpa config kedua)
   16. boundary: frontend SettingsView konsisten + tanpa logic agent
   17. serving: "/" menyajikan bundle build terbaru + header cache benar

Jalankan:
    python scripts/check_web_settings.py
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
FIXTURE = DUMMY_ROOT / "web_settings_fixture"

# Nilai secret unik untuk deteksi kebocoran (harus TIDAK pernah muncul di response).
SECRET_VALUE = "sk-uji-rahasia-jangan-bocor-1234567890"


def setup_fixture() -> Path:
    """Siapkan folder fixture + file .env kosong (terisolasi)."""
    shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir(parents=True, exist_ok=True)
    env_path = FIXTURE / ".env"
    env_path.write_text("# fixture .env untuk verifikasi Settings\n", encoding="utf-8")
    return env_path


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Web Settings (#50) — konfigurasi LLM ===")
    env_path = setup_fixture()
    cleanup: list[str] = []
    try:
        return _run(env_path, cleanup)
    finally:
        for path in cleanup:
            shutil.rmtree(path, ignore_errors=True)
        teardown_fixture()


def _assert_no_secret(resp, secret: str) -> None:
    """Pastikan nilai secret TIDAK pernah muncul di body response."""
    assert secret.encode("utf-8") not in resp.content, "SECRET BOCOR di response!"


def _run(env_path: Path, cleanup: list[str]) -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

    # 1) Django project dapat di-load.
    import django

    django.setup()
    from django.test import Client

    client = Client()
    print("[1] Django project load OK -> settings + urls")

    # Isolasi total: LLMConfigService + ProjectStore + ProjectRegistry memakai
    # fixture dummy_test (TIDAK menyentuh data/aether.db maupun .env produksi).
    from agent_ai.llm_config import LLMConfigService
    from agent_ai.projects.registry import ProjectRegistry

    import api.services as services_mod
    from api.project_store import ProjectStore

    tmp_dir = tempfile.mkdtemp(prefix="web_settings_", dir=str(DUMMY_ROOT))
    cleanup.append(tmp_dir)
    workspace = Path(tmp_dir) / "projects"
    registry = ProjectRegistry(workspace=workspace)
    store = ProjectStore(db_path=Path(tmp_dir) / "store.db")
    llm_service = LLMConfigService(db_path=Path(tmp_dir) / "llm.db", env_path=env_path)

    services_mod._default_service = services_mod.GatewayService(
        project_registry=registry,
        project_store=store,
        auto_execute=False,
        llm_config_service=llm_service,
    )

    def post(url: str, payload: dict):
        return client.post(url, data=json.dumps(payload), content_type="application/json")

    def put(url: str, payload: dict):
        return client.put(url, data=json.dumps(payload), content_type="application/json")

    # 2) GET /api/llm/config.
    resp = client.get("/api/llm/config")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    cfg = resp.json()
    for key in ("credentials", "provider_types", "providers"):
        assert key in cfg, f"respons config harus punya '{key}'"
    assert cfg["provider_types"], "provider_types tidak boleh kosong"
    # Katalog provider type membawa field yang dipakai UI.
    spec = cfg["provider_types"][0]
    for key in ("key", "label", "env_prefix", "default_api_url", "requires_api_key"):
        assert key in spec, spec
    print(
        f"[2] GET /api/llm/config OK -> {len(cfg['provider_types'])} provider type, "
        f"{len(cfg['credentials'])} credential, {len(cfg['providers'])} provider instance"
    )

    # 3) POST /api/llm/credentials -> set API key (.env). Response hanya masked.
    resp = post(
        "/api/llm/credentials",
        {"name": "OPENROUTER_API_KEY", "value": SECRET_VALUE},
    )
    assert resp.status_code == 201, (resp.status_code, resp.content)
    cred = resp.json()
    assert cred["name"] == "OPENROUTER_API_KEY", cred
    assert cred["is_set"] is True, cred
    assert "value" not in cred and "secret" not in cred, cred
    assert cred["masked"] and cred["masked"] != SECRET_VALUE, cred
    _assert_no_secret(resp, SECRET_VALUE)
    assert env_path.read_text(encoding="utf-8").find(SECRET_VALUE) != -1, "nilai .env tidak tertulis"
    print(f"[3] POST /api/llm/credentials OK -> masked={cred['masked']} (tanpa secret)")

    # GET config lagi: credential muncul, masked, dan secret TIDAK bocor.
    resp = client.get("/api/llm/config")
    names = {c["name"] for c in resp.json()["credentials"]}
    assert "OPENROUTER_API_KEY" in names, names
    _assert_no_secret(resp, SECRET_VALUE)

    # 4) validasi credential: nama di luar pola -> 400.
    resp = post("/api/llm/credentials", {"name": "PATH", "value": "x"})
    assert resp.status_code == 400, (resp.status_code, resp.content)
    assert resp.json()["error"]["code"] == "validation_error"
    # nama valid tapi nilai kosong -> 400.
    resp = post("/api/llm/credentials", {"name": "OPENAI_API_KEY", "value": ""})
    assert resp.status_code == 400, resp.status_code
    print("[4] validasi credential OK -> 400 untuk nama/nilai tidak valid")

    # 5) POST /api/llm/providers -> provider instance (api_key_present).
    resp = post(
        "/api/llm/providers",
        {
            "name": "OpenRouter Utama",
            "provider_type": "openrouter",
            "api_key_env": "OPENROUTER_API_KEY",
        },
    )
    assert resp.status_code == 201, (resp.status_code, resp.content)
    provider = resp.json()
    provider_id = provider["id"]
    assert provider["api_key_present"] is True, provider
    assert "api_key" not in provider, "nilai api_key tidak boleh dikembalikan"
    _assert_no_secret(resp, SECRET_VALUE)
    print(f"[5] POST /api/llm/providers OK -> id={provider_id[:8]}..., api_key_present=True")

    # 6) konflik provider: nama duplikat -> 409.
    resp = post(
        "/api/llm/providers",
        {"name": "OpenRouter Utama", "provider_type": "openrouter", "api_key_env": "OPENROUTER_API_KEY"},
    )
    assert resp.status_code == 409, (resp.status_code, resp.content)
    assert resp.json()["error"]["code"] == "conflict"
    print("[6] konflik provider OK -> 409 untuk nama duplikat")

    # 7) validasi provider: type tidak dikenal -> 400; butuh key tanpa api_key_env -> 400.
    resp = post("/api/llm/providers", {"name": "X", "provider_type": "tidak_ada"})
    assert resp.status_code == 400, (resp.status_code, resp.content)
    resp = post("/api/llm/providers", {"name": "Y", "provider_type": "openai"})
    assert resp.status_code == 400, (resp.status_code, resp.content)
    print("[7] validasi provider OK -> 400 untuk type/nama tidak valid")

    # 8) POST /api/llm/models -> model + konflik duplikat + provider tidak ada.
    resp = post(
        "/api/llm/models",
        {"provider_id": provider_id, "model_name": "openai/gpt-4o-mini"},
    )
    assert resp.status_code == 201, (resp.status_code, resp.content)
    model = resp.json()
    model_id = model["id"]
    assert model["provider_id"] == provider_id, model
    assert model["enabled"] is True, model
    # duplikat -> 409.
    resp = post(
        "/api/llm/models",
        {"provider_id": provider_id, "model_name": "openai/gpt-4o-mini"},
    )
    assert resp.status_code == 409, (resp.status_code, resp.content)
    # provider tidak ada -> 404.
    resp = post("/api/llm/models", {"provider_id": "tidak_ada", "model_name": "m"})
    assert resp.status_code == 404, (resp.status_code, resp.content)
    print(f"[8] POST /api/llm/models OK -> id={model_id[:8]}... (409 duplikat, 404 provider)")

    # 9) PUT provider & model -> update enabled.
    resp = put(f"/api/llm/models/{model_id}", {"enabled": False})
    assert resp.status_code == 200, (resp.status_code, resp.content)
    assert resp.json()["enabled"] is False, resp.json()
    resp = put(f"/api/llm/providers/{provider_id}", {"enabled": False})
    assert resp.status_code == 200, (resp.status_code, resp.content)
    assert resp.json()["enabled"] is False, resp.json()
    # kembalikan ke enabled agar konsisten untuk cek berikutnya.
    put(f"/api/llm/providers/{provider_id}", {"enabled": True})
    put(f"/api/llm/models/{model_id}", {"enabled": True})
    print("[9] PUT provider & model OK -> enabled ter-update")

    # 10) regresi: GET /api/config tetap jalan + instances tanpa secret.
    resp = client.get("/api/config")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    config = resp.json()
    assert "provider_instances" in config, config
    ids = {p["id"] for p in config["provider_instances"]}
    assert provider_id in ids, ids
    _assert_no_secret(resp, SECRET_VALUE)
    print("[10] GET /api/config OK -> regresi jalan, provider instance terekspos (tanpa secret)")

    # 11) DELETE credential: relasi (dipakai provider) -> 400 tanpa force, 200 dengan force.
    resp = post("/api/llm/credentials/delete", {"name": "OPENROUTER_API_KEY"})
    assert resp.status_code == 400, (resp.status_code, resp.content)
    assert resp.json()["error"]["code"] == "validation_error"
    resp = post(
        "/api/llm/credentials/delete",
        {"name": "OPENROUTER_API_KEY", "force": True},
    )
    assert resp.status_code == 200, (resp.status_code, resp.content)
    assert resp.json()["deleted"] is True, resp.json()
    # credential sudah tidak ada di .env.
    assert env_path.read_text(encoding="utf-8").find(SECRET_VALUE) == -1, ".env harus bersih"
    print("[11] DELETE credential OK -> 400 tanpa force, 200 dengan force")

    # 12) DELETE model & provider -> 200 lalu 404.
    resp = client.delete(f"/api/llm/models/{model_id}")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    assert resp.json()["deleted"] is True, resp.json()
    resp = client.delete(f"/api/llm/models/{model_id}")
    assert resp.status_code == 404, resp.status_code
    resp = client.delete(f"/api/llm/providers/{provider_id}")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    assert resp.json()["deleted"] is True, resp.json()
    resp = client.delete(f"/api/llm/providers/{provider_id}")
    assert resp.status_code == 404, resp.status_code
    print("[12] DELETE model & provider OK -> 200 lalu 404")

    # 13) method tidak diizinkan.
    resp = client.get("/api/llm/providers")
    assert resp.status_code == 405, resp.status_code
    resp = client.delete("/api/llm/config")
    assert resp.status_code == 405, resp.status_code
    print("[13] method tidak diizinkan OK -> 405")

    # 14) boundary: tidak ada secret yang bocor (semua koleksi config aman).
    resp = client.get("/api/llm/config")
    body_text = resp.content.decode("utf-8")
    assert SECRET_VALUE not in body_text, "SECRET BOCOR di /api/llm/config!"
    # Respons tidak boleh memuat field nilai secret apa pun.
    assert "api_key_value" not in body_text and '"api_key":' not in body_text
    print("[14] boundary OK -> tidak ada secret bocor dari API")

    # 15) boundary: gateway memakai facade LLMConfigService AETHER (satu konfigurasi).
    api_dir = DJANGO_APP_DIR / "api"
    services_src = (api_dir / "services.py").read_text(encoding="utf-8")
    views_src = (api_dir / "views.py").read_text(encoding="utf-8")
    assert "from agent_ai.llm_config import" in services_src, "harus pakai facade llm_config AETHER"
    assert "LLMConfigService" in services_src, "harus memakai LLMConfigService AETHER"
    # Gateway TIDAK boleh membaca NILAI api key (hanya masked) lewat jalur http.
    assert "include_api_key=True" not in services_src, "gateway tidak boleh sertakan nilai api_key"
    assert "get_api_key(" not in services_src, "gateway tidak boleh memanggil get_api_key"
    assert "get_api_key(" not in views_src, "views tidak boleh membaca nilai api key"
    assert "mask_secret" not in views_src, "views bukan tempat masking (itu layer AETHER)"
    print("[15] boundary OK -> memakai facade LLMConfigService AETHER (tanpa config kedua)")

    # 16) boundary frontend: SettingsView ada, memakai endpoint /llm, tanpa logic agent & secret.
    api_js = (FRONTEND_DIR / "api.js").read_text(encoding="utf-8")
    for fn in (
        "getLLMConfig",
        "createLLMProvider",
        "updateLLMProvider",
        "deleteLLMProvider",
        "createLLMModel",
        "updateLLMModel",
        "deleteLLMModel",
        "createLLMCredential",
        "deleteLLMCredential",
    ):
        assert fn in api_js, f"api.js harus mengekspor {fn}"
    assert "/llm/config" in api_js, "api.js harus memanggil /llm/config"

    settings_vue = (FRONTEND_DIR / "components" / "SettingsView.vue").read_text(encoding="utf-8")
    # Frontend TIDAK menyimpan secret di storage lokal.
    assert "localStorage" not in settings_vue, "frontend tidak boleh menyimpan secret (localStorage)"
    # TIDAK ada logic agent/runtime di komponen Settings.
    for bad in ("AgentRuntime", "AgentLoop", "orchestrator", "Replanner"):
        assert bad not in settings_vue, f"SettingsView tidak boleh memuat '{bad}'"
    # Memakai endpoint LLM dari api.js.
    for fn in ("getLLMConfig", "createLLMProvider", "createLLMCredential"):
        assert fn in settings_vue, f"SettingsView harus memakai {fn}"
    print("[16] boundary frontend OK -> SettingsView + api.js konsisten (tanpa logic agent)")

    # 17) serving: "/" benar-benar menyajikan bundle build terbaru + header cache.
    #     Ini menutup akar masalah "UI Settings lama": static.serve tanpa
    #     Cache-Control membuat browser menyajikan index.html/bundle lama.
    dist_dir = PROJECT_ROOT / "web" / "frontend" / "dist"
    if dist_dir.is_dir():
        import re as _re

        def _read(response) -> str:
            if hasattr(response, "streaming_content"):
                return b"".join(response.streaming_content).decode("utf-8", "replace")
            return response.content.decode("utf-8", "replace")

        resp = client.get("/")
        assert resp.status_code == 200, resp.status_code
        html = _read(resp)
        # index.html TIDAK boleh di-cache (selalu revalidate -> bundle terbaru).
        assert "no-cache" in resp.headers.get("Cache-Control", ""), dict(resp.headers)
        # index.html harus menunjuk asset ber-hash yang benar-benar ada di dist.
        refs = _re.findall(r"/assets/([^\"]+)", html)
        assert refs, html
        on_disk = {p.name for p in (dist_dir / "assets").iterdir() if p.is_file()}
        missing = [r for r in refs if r not in on_disk]
        assert not missing, f"index.html menunjuk asset tidak ada di dist: {missing}"
        # Bundle yang disajikan harus = build SettingsView terbaru (bukan UI lama).
        js_ref = next((r for r in refs if r.endswith(".js")), None)
        assert js_ref, html
        resp_js = client.get(f"/assets/{js_ref}")
        assert resp_js.status_code == 200, resp_js.status_code
        # Asset ber-hash bersifat immutable -> boleh di-cache lama.
        assert "immutable" in resp_js.headers.get("Cache-Control", ""), dict(resp_js.headers)
        bundle = _read(resp_js)
        assert "API Credentials" in bundle, "bundle tidak memuat SettingsView terbaru"
        assert "Active Provider" in bundle, "bundle tidak memuat SettingsView terbaru"
        assert (
            "Provider, model, and workspace currently used by AETHER" not in bundle
        ), "bundle masih memuat UI Settings lama!"
        print(
            f"[17] serving OK -> '/' menyajikan bundle terbaru ({js_ref}); "
            "index.html no-cache, asset immutable"
        )
    else:
        print("[17] serving SKIP -> dist belum di-build (jalankan 'npm run build' di web/frontend)")

    print()
    print("[OK] Web Settings bekerja (CRUD konfigurasi LLM via facade AETHER, tanpa secret bocor).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
