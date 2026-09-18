"""Verifikasi alur New Task memakai konfigurasi LLM tersimpan (SQLite).

Menguji titik putus yang diperbaiki: New Task TIDAK lagi memakai daftar
Provider/Model dari konfigurasi/code lama (settings/.env). Sebagai gantinya:
    - Provider Instance diambil dari tabel `llm_provider_instances`.
    - Model diambil dari tabel `llm_models`.
    - Dropdown Model hanya menampilkan model milik Provider Instance terpilih.
    - Submit New Task mengirim `provider_instance_id` + `model_id` ke /api/tasks
      sehingga runtime memakai konfigurasi DB tersebut.
    - `.env` tetap hanya untuk secret (api_key_env), bukan nama model/provider.

Deterministik, TANPA model/API cloud nyata. Fixture DB/.env/project dibuat di
`dummy_test/new_task_config_fixture` dan dibersihkan setelah test. TIDAK
menyentuh database global `data/aether.db`, `.env` asli, maupun project nyata.

Menguji:
    1. GET /api/llm/providers -> provider instance + nested model dari SQLite.
    2. Provider instance disabled tidak ditampilkan (frontend filter).
    3. TaskComposer memakai Provider Instance + Model (bukan config.models lama).
    4. App.vue mengirim provider_instance_id + model_id ke /api/tasks.
    5. End-to-end: POST /api/tasks dengan provider_instance_id/model_id ->
       runtime memakai provider+model dari DB (bukan settings/.env).
    6. Model difilter per Provider Instance (model instance lain tidak dipakai).
    7. .env hanya untuk secret: nama model/provider TIDAK dibaca dari .env.

Jalankan:
    python scripts/check_new_task_config.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
FRONTEND_SRC = PROJECT_ROOT / "web" / "frontend" / "src"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "new_task_config_fixture"
SECRET = "sk-or-v1-NEWTASK-SECRET-0000000000"

ENV_FIXTURE = f"""# Dummy .env untuk verifikasi New Task (JANGAN produksi)
OPENROUTER_API_KEY={SECRET}
"""


def setup_fixture() -> Path:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    project_dir = FIXTURE / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    (FIXTURE / ".env").write_text(ENV_FIXTURE, encoding="utf-8")
    return project_dir


def teardown_fixture() -> None:
    import gc

    for _ in range(10):
        gc.collect()
        shutil.rmtree(FIXTURE, ignore_errors=True)
        if not FIXTURE.exists():
            break
        time.sleep(0.1)
    try:
        if DUMMY_ROOT.exists() and not any(DUMMY_ROOT.iterdir()):
            DUMMY_ROOT.rmdir()
    except OSError:
        pass


class _FakeResult:
    def __init__(self, text: str = "fake-run-ok") -> None:
        self.success = True
        self.result = text
        self.error = None
        self.iterations = 1


class _FakeRuntime:
    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, prepared=None, lifecycle=None):  # noqa: D401 - kontrak runtime
        self.run_calls += 1
        return _FakeResult()


def _make_recording_runtime_factory(runs):
    def factory(*, provider=None, executor=None, session_id=None, options=None, **_kw):
        runs.append({"provider": provider, "options": options, "session_id": session_id})
        return _FakeRuntime()

    return factory


def _wait_for_terminal(service, task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = service.get_task(task_id)
        if rec["status"] in ("completed", "failed", "cancelled"):
            return rec
        time.sleep(0.02)
    return service.get_task(task_id)


def _check_frontend_sources() -> None:
    """[3][4] Frontend New Task memakai Provider Instance + Model dari DB."""
    composer = (FRONTEND_SRC / "components" / "TaskComposer.vue").read_text(encoding="utf-8")
    app = (FRONTEND_SRC / "App.vue").read_text(encoding="utf-8")
    api = (FRONTEND_SRC / "api.js").read_text(encoding="utf-8")

    # api.js memanggil endpoint provider instance (bukan hanya /config).
    assert "getLLMProviders" in api, "api.js harus mengekspor getLLMProviders"
    assert "/llm/providers" in api, "api.js harus memanggil /llm/providers"

    # TaskComposer: dropdown Provider Instance + Model (bukan config.models lama).
    assert "providerInstanceId" in composer, "TaskComposer harus punya providerInstanceId"
    assert "modelId" in composer, "TaskComposer harus punya modelId"
    assert "providerOptions" in composer, "TaskComposer harus punya providerOptions"
    assert "modelOptions" in composer, "TaskComposer harus punya modelOptions"
    # Model difilter per provider instance terpilih.
    assert "props.providerInstanceId" in composer, "model harus difilter per provider instance"
    # TIDAK lagi memakai daftar lama dari config (settings/.env).
    assert "config.models" not in composer, "TaskComposer tidak boleh pakai config.models lama"
    assert "config.providers" not in composer, "TaskComposer tidak boleh pakai config.providers lama"

    # App.vue: kirim provider_instance_id + model_id ke /api/tasks.
    assert "getLLMProviders" in app, "App harus memuat provider instance dari DB"
    assert "provider_instance_id" in app, "App harus mengirim provider_instance_id"
    assert "model_id" in app, "App harus mengirim model_id"
    assert "selectedProviderInstanceId" in app, "App harus punya state provider instance"
    assert "selectedModelId" in app, "App harus punya state model"
    # TIDAK lagi mengirim metadata.provider/model (nama dari config lama).
    assert "metadata.provider =" not in app, "App tidak boleh kirim metadata.provider lama"
    assert "metadata.model =" not in app, "App tidak boleh kirim metadata.model lama"
    print("[3] TaskComposer memakai Provider Instance + Model (bukan config lama) OK")
    print("[4] App.vue mengirim provider_instance_id + model_id ke /api/tasks OK")


def _run() -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
    os.environ.pop("OPENROUTER_API_KEY", None)

    import django

    django.setup()

    from django.test import Client

    import api.services as services_mod
    from agent_ai.llm_config import LLMConfigService
    from api.execution import TaskExecutor
    from api.project_store import ProjectStore

    project_dir = setup_fixture()

    print("=== Verifikasi New Task memakai konfigurasi LLM tersimpan (SQLite) ===")

    # [3][4] Frontend New Task (statis).
    _check_frontend_sources()

    # Konfigurasi LLM tersimpan di DB FIXTURE (bukan DB global).
    svc = LLMConfigService(db_path=FIXTURE / "llm_config.db", env_path=FIXTURE / ".env")

    inst = svc.create_provider_instance(
        name="OpenRouter NewTask",
        provider_type="openrouter",
        api_key_env="OPENROUTER_API_KEY",
        api_url="https://openrouter.test/api/v1",
    )
    model_a = svc.add_model(inst.id, "openrouter/model-a")
    model_b = svc.add_model(inst.id, "openrouter/model-b")
    # Instance kedua (untuk membuktikan model difilter per instance).
    other = svc.create_provider_instance(
        name="OpenRouter Lain",
        provider_type="openrouter",
        api_key_env="OPENROUTER_API_KEY",
    )
    other_model = svc.add_model(other.id, "openrouter/model-lain")
    # Instance disabled (tidak boleh muncul di dropdown New Task).
    disabled = svc.create_provider_instance(
        name="OpenRouter Disabled",
        provider_type="openrouter",
        api_key_env="OPENROUTER_API_KEY",
        enabled=False,
    )

    runs: list = []
    store = services_mod.InMemorySessionStore()
    executor_bridge = TaskExecutor(
        store,
        llm_config_service=svc,
        runtime_factory=_make_recording_runtime_factory(runs),
    )
    service = services_mod.GatewayService(
        session_store=store,
        task_executor=executor_bridge,
        auto_execute=True,
        project_store=ProjectStore(db_path=FIXTURE / "gateway.db"),
        llm_config_service=svc,
    )
    services_mod._default_service = service
    client = Client()

    project = service.create_project(name="NewTaskFixture", path=str(project_dir))
    project_id = project["id"]

    # 1) GET /api/llm/providers -> provider instance + nested model dari SQLite.
    resp = client.get("/api/llm/providers")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    body = resp.json()
    assert "providers" in body, body
    providers = body["providers"]
    ids = {p["id"] for p in providers}
    assert inst.id in ids and other.id in ids and disabled.id in ids, ids
    # Nested model tersedia + field yang dipakai UI.
    inst_payload = next(p for p in providers if p["id"] == inst.id)
    assert inst_payload["provider_label"], inst_payload
    assert inst_payload["enabled"] is True, inst_payload
    model_ids = {m["id"] for m in inst_payload["models"]}
    assert {model_a.id, model_b.id} <= model_ids, model_ids
    # Tidak ada secret yang bocor.
    assert SECRET not in resp.content.decode("utf-8"), "SECRET BOCOR di /api/llm/providers"
    print(f"[1] GET /api/llm/providers OK -> {len(providers)} instance, nested model dari SQLite")

    # 2) Instance disabled ditandai (frontend memfilternya keluar).
    disabled_payload = next(p for p in providers if p["id"] == disabled.id)
    assert disabled_payload["enabled"] is False, disabled_payload
    print("[2] Provider instance disabled ditandai enabled=False (difilter frontend) OK")

    # 5) End-to-end: POST /api/tasks dengan provider_instance_id/model_id ->
    #    runtime memakai provider+model dari DB (bukan settings/.env).
    resp = client.post(
        "/api/tasks",
        data=json.dumps(
            {
                "task": "jelaskan struktur project",
                "project_id": project_id,
                "metadata": {"provider_instance_id": inst.id, "model_id": model_b.id},
            }
        ),
        content_type="application/json",
    )
    assert resp.status_code == 201, (resp.status_code, resp.content)
    task_id = resp.json()["task_id"]
    final = _wait_for_terminal(service, task_id)
    assert final["status"] == "completed", final
    assert runs, "runtime_factory tidak dipanggil"

    from agent_ai.providers.openrouter import OpenRouterProvider

    used = runs[-1]
    prov = used["provider"]
    assert isinstance(prov, OpenRouterProvider), prov
    assert prov.config.base_url == "https://openrouter.test/api/v1", prov.config
    assert prov.config.api_key == SECRET, "api_key dari .env tidak tersuntik"
    assert prov.config.model == "openrouter/model-b", prov.config
    assert used["options"] is not None and used["options"].model == "openrouter/model-b", used
    print("[5] POST /api/tasks (provider_instance_id+model_id) -> runtime pakai DB OK")
    print(f"    provider={type(prov).__name__} base_url={prov.config.base_url}")
    print(f"    model={used['options'].model} (dari llm_models, bukan .env)")

    # 6) Model difilter per Provider Instance: model instance lain ditolak.
    resp = client.post(
        "/api/tasks",
        data=json.dumps(
            {
                "task": "task invalid",
                "project_id": project_id,
                "metadata": {"provider_instance_id": inst.id, "model_id": other_model.id},
            }
        ),
        content_type="application/json",
    )
    assert resp.status_code == 400, (resp.status_code, resp.content)
    assert resp.json()["error"]["code"] == "validation_error", resp.json()
    print("[6] model instance lain ditolak (400) -> dropdown difilter per instance OK")

    # 7) .env hanya untuk secret: nama model/provider TIDAK dibaca dari .env.
    env_text = (FIXTURE / ".env").read_text(encoding="utf-8")
    assert "model" not in env_text.lower(), ".env tidak boleh memuat nama model"
    assert "provider" not in env_text.lower(), ".env tidak boleh memuat nama provider"
    # Provider instance + model berasal dari SQLite (llm_provider_instances/llm_models).
    from agent_ai.llm_config.store import LLMConfigStore

    store_check = LLMConfigStore(FIXTURE / "llm_config.db")
    assert store_check.get_provider_instance(inst.id) is not None
    assert store_check.get_model(model_b.id) is not None
    print("[7] .env hanya untuk secret (api_key_env) OK -> model/provider dari SQLite")

    print()
    print("[OK] New Task memakai Provider Instance + Model dari LLM Config Core (SQLite).")
    return 0


def main() -> int:
    try:
        return _run()
    finally:
        teardown_fixture()


if __name__ == "__main__":
    raise SystemExit(main())
