"""Verifikasi integrasi konfigurasi LLM (SQLite) -> Runtime eksekusi.

Menguji titik putus yang diperbaiki: task dapat menunjuk **provider instance +
model** dari konfigurasi LLM tersimpan (SQLite), bukan hanya dari settings/.env.
Backend merakit provider konkret (api_url/api_key/model) dari konfigurasi ini
lewat `agent_ai.providers.factory`, DAN model yang dipilih benar-benar dipakai
runtime (GenerateOptions.model).

Deterministik, TANPA model/API cloud nyata:
    - Jalur provider-instance diverifikasi dengan memeriksa provider yang
      dirakit (tipe + base_url + api_key + model) via runtime_factory (fake).
    - Jalur default (tanpa provider_instance_id) tetap lewat ProviderRegistry
      (di-monkeypatch agar tidak memanggil jaringan).

Fixture DB/.env/project dibuat di `dummy_test/llm_runtime_fixture` dan
dibersihkan setelah test. TIDAK menyentuh database global `data/aether.db`,
`.env` asli, maupun project nyata.

Jalankan:
    python scripts/check_llm_runtime_integration.py
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

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "llm_runtime_fixture"
SECRET = "sk-or-v1-VERIFIER-SECRET-0000000000"

ENV_FIXTURE = f"""# Dummy .env untuk verifikasi integrasi runtime (JANGAN produksi)
OPENROUTER_API_KEY={SECRET}
"""


def setup_fixture() -> Path:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    project_dir = FIXTURE / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    (FIXTURE / ".env").write_text(ENV_FIXTURE, encoding="utf-8")
    return project_dir


def teardown_fixture() -> None:
    # Windows: koneksi sqlite dapat menahan file sesaat setelah dipakai.
    # gc.collect() + retry agar fixture benar-benar bersih.
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
    """Hasil runtime fake (kontrak minimal AgentRuntime.run)."""

    def __init__(self, text: str = "fake-run-ok") -> None:
        self.success = True
        self.result = text
        self.error = None
        self.iterations = 1


class _FakeRuntime:
    """Runtime fake: hanya mencatat provider+options yang diterima."""

    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, prepared=None, lifecycle=None):  # noqa: D401 - kontrak runtime
        self.run_calls += 1
        return _FakeResult()


def _make_recording_runtime_factory(runs):
    """runtime_factory yang mencatat provider+options tiap kali runtime dibuat."""

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


def _check_factory_mapping() -> None:
    """[A] Factory memetakan provider_type -> class provider existing."""
    from agent_ai.llm_config import LLMConfigValidationError  # noqa: F401
    from agent_ai.providers.base import ProviderNotConfiguredError
    from agent_ai.providers.deepseek import DeepSeekProvider
    from agent_ai.providers.factory import build_provider_from_config
    from agent_ai.providers.ollama import OllamaProvider
    from agent_ai.providers.openai_compatible import OpenAICompatibleProvider
    from agent_ai.providers.openrouter import OpenRouterProvider

    # openrouter
    prov = build_provider_from_config(
        {
            "provider_type": "openrouter",
            "instance_name": "OR",
            "api_url": "https://openrouter.test/api/v1",
            "api_key": "sk-or-test",
            "model": "openai/gpt-4o-mini",
        }
    )
    assert isinstance(prov, OpenRouterProvider), prov
    assert prov.config.base_url == "https://openrouter.test/api/v1", prov.config
    assert prov.config.api_key == "sk-or-test", prov.config
    assert prov.config.model == "openai/gpt-4o-mini", prov.config
    print(f"[A] openrouter -> {type(prov).__name__} base_url+api_key+model disuntik OK")

    # deepseek
    prov = build_provider_from_config(
        {
            "provider_type": "deepseek",
            "api_url": "https://api.deepseek.com",
            "api_key": "sk-ds",
            "model": "deepseek-chat",
        }
    )
    assert isinstance(prov, DeepSeekProvider), prov
    assert prov.config.model == "deepseek-chat", prov.config
    print(f"[A] deepseek   -> {type(prov).__name__} OK")

    # openai (OpenAI-compatible)
    prov = build_provider_from_config(
        {
            "provider_type": "openai",
            "api_url": "https://api.openai.test/v1",
            "api_key": "sk-openai",
            "model": "gpt-4o-mini",
        }
    )
    assert isinstance(prov, OpenAICompatibleProvider), prov
    assert prov.config.base_url == "https://api.openai.test/v1", prov.config
    print(f"[A] openai     -> {type(prov).__name__} OK")

    # ollama (lokal, tanpa API key)
    prov = build_provider_from_config(
        {
            "provider_type": "ollama",
            "api_url": "http://127.0.0.1:11434",
            "api_key": "",
            "model": "qwen2.5-coder:7b",
        }
    )
    assert isinstance(prov, OllamaProvider), prov
    assert prov.config.host == "http://127.0.0.1:11434", prov.config
    assert prov.config.model == "qwen2.5-coder:7b", prov.config
    print(f"[A] ollama     -> {type(prov).__name__} host+model OK")

    # provider cloud TANPA api key -> error jelas (bukan crash senyap).
    try:
        build_provider_from_config(
            {"provider_type": "openrouter", "instance_name": "OR", "model": "m"}
        )
    except ProviderNotConfiguredError as exc:
        print(f"[A] tanpa api key -> ProviderNotConfiguredError OK: {exc}")
    else:
        raise AssertionError("openrouter tanpa api key seharusnya ditolak")

    # provider type tak dikenal -> error jelas.
    try:
        build_provider_from_config({"provider_type": "ngawur", "model": "m"})
    except ProviderNotConfiguredError as exc:
        print(f"[A] type asing    -> ProviderNotConfiguredError OK: {exc}")
    else:
        raise AssertionError("provider type asing seharusnya ditolak")
    print()


def _run() -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
    # Isolasi: pastikan env proses tidak mengganggu penentuan API key.
    os.environ.pop("OPENROUTER_API_KEY", None)
    os.environ.pop("OPENROUTER_API_KEY_MISSING", None)

    import django

    django.setup()

    from django.test import Client

    import api.services as services_mod
    from agent_ai.llm_config import LLMConfigService
    from api.execution import TaskExecutor
    from api.project_store import ProjectStore

    project_dir = setup_fixture()

    print("=== Verifikasi Integrasi Konfigurasi LLM -> Runtime ===")
    _check_factory_mapping()

    # Konfigurasi LLM tersimpan di DB FIXTURE (bukan DB global).
    svc = LLMConfigService(
        db_path=FIXTURE / "llm_config.db", env_path=FIXTURE / ".env"
    )

    # ------------------------------------------------------------------ #
    # [B] Task menunjuk provider instance + model -> provider dirakit dari
    #     konfigurasi SQLite, dan model terpilih dipakai runtime.
    # ------------------------------------------------------------------ #
    inst = svc.create_provider_instance(
        name="OpenRouter Verifier",
        provider_type="openrouter",
        api_key_env="OPENROUTER_API_KEY",
        api_url="https://openrouter.test/api/v1",
    )
    model_a = svc.add_model(inst.id, "openrouter/model-a")
    model_b = svc.add_model(inst.id, "openrouter/model-b")

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

    project = service.create_project(name="RuntimeFixture", path=str(project_dir))
    project_id = project["id"]

    # GET /api/config mengekspos provider instance + model + default terpilih.
    resp = client.get("/api/config")
    assert resp.status_code == 200, resp.status_code
    cfg = resp.json()
    assert "provider_instances" in cfg, cfg
    ids = [i["id"] for i in cfg["provider_instances"]]
    assert inst.id in ids, cfg
    assert cfg["provider_instance_id"] == inst.id, cfg
    assert cfg["model_id"] in (model_a.id, model_b.id), cfg
    print(f"[B] GET /api/config -> provider_instances={len(ids)} default_instance={inst.id[:8]}..")
    print(f"    default_model={cfg['model_id'][:8]}.. (dari SQLite, bukan hardcode)")

    # Pilih model KEDUA (model-b) untuk membuktikan model_id dihormati
    # (bukan asal ambil model enabled pertama).
    resp = client.post(
        "/api/tasks",
        data=json.dumps(
            {
                "task": "jelaskan struktur project",
                "project_id": project_id,
                "metadata": {
                    "provider_instance_id": inst.id,
                    "model_id": model_b.id,
                },
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
    assert used["options"] is not None, used
    assert used["options"].model == "openrouter/model-b", used["options"]
    print(f"[B] task selesai -> provider={type(prov).__name__} (SQLite-driven) OK")
    print(f"    base_url={prov.config.base_url} api_key=***{SECRET[-4:]}")
    print(f"    model (config+GenerateOptions) = {used['options'].model} (model_id dihormati)")

    # Relasi Provider Instance -> Model divalidasi SEBELUM task dijalankan.
    other_inst = svc.create_provider_instance(
        name="OpenRouter Lain",
        provider_type="openrouter",
        api_key_env="OPENROUTER_API_KEY",
    )
    other_model = svc.add_model(other_inst.id, "openrouter/model-lain")

    for meta, desc in (
        ({"provider_instance_id": "tidak-ada"}, "instance tidak ada"),
        (
            {"provider_instance_id": inst.id, "model_id": other_model.id},
            "model bukan milik instance",
        ),
        ({"model_id": "tidak-ada"}, "model tidak ada"),
    ):
        resp = client.post(
            "/api/tasks",
            data=json.dumps(
                {"task": "task invalid", "project_id": project_id, "metadata": meta}
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400, (desc, resp.status_code, resp.content)
        body = resp.json()
        assert body["error"]["code"] == "validation_error", body
        print(f"    tolak ({desc}) -> 400 validation_error OK")
    print()

    # ------------------------------------------------------------------ #
    # [C] Provider instance tanpa API key -> task FAILED dengan pesan jelas
    #     (tidak crash / tidak menggantung).
    # ------------------------------------------------------------------ #
    no_key_inst = svc.create_provider_instance(
        name="OpenRouter Tanpa Key",
        provider_type="openrouter",
        api_key_env="OPENROUTER_API_KEY_MISSING",
    )
    no_key_model = svc.add_model(no_key_inst.id, "openrouter/model-x")
    resp = client.post(
        "/api/tasks",
        data=json.dumps(
            {
                "task": "task tanpa key",
                "project_id": project_id,
                "metadata": {
                    "provider_instance_id": no_key_inst.id,
                    "model_id": no_key_model.id,
                },
            }
        ),
        content_type="application/json",
    )
    assert resp.status_code == 201, (resp.status_code, resp.content)
    no_key_task = resp.json()["task_id"]
    final = _wait_for_terminal(service, no_key_task)
    assert final["status"] == "failed", final
    assert final["error"] and "API key" in final["error"], final["error"]
    print(f"[C] instance tanpa api key -> status=failed OK")
    print(f"    error={final['error']}")
    print()

    # ------------------------------------------------------------------ #
    # [D] Backward compatible: TANPA provider_instance_id -> jalur default
    #     (ProviderRegistry). get_provider di-monkeypatch (tanpa jaringan).
    # ------------------------------------------------------------------ #
    registry_mod = sys.modules["agent_ai.providers.registry"]
    original_get_provider = registry_mod.get_provider
    sentinel = object()

    def fake_get_provider(name=None):
        return sentinel

    registry_mod.get_provider = fake_get_provider
    try:
        runs.clear()
        resp = client.post(
            "/api/tasks",
            data=json.dumps({"task": "task default", "project_id": project_id}),
            content_type="application/json",
        )
        assert resp.status_code == 201, (resp.status_code, resp.content)
        default_task = resp.json()["task_id"]
        final = _wait_for_terminal(service, default_task)
        assert final["status"] == "completed", final
        assert runs and runs[-1]["provider"] is sentinel, runs
        assert runs[-1]["options"] is None, runs[-1]["options"]
        print("[D] tanpa provider_instance_id -> jalur default (registry) OK")
        print("    provider dari registry, options=None (backward compatible)")
    finally:
        registry_mod.get_provider = original_get_provider
    print()

    print("[OK] Konfigurasi LLM (SQLite) terintegrasi ke Runtime (provider+model benar-benar dipakai).")
    return 0


def main() -> int:
    try:
        return _run()
    finally:
        teardown_fixture()


if __name__ == "__main__":
    raise SystemExit(main())
