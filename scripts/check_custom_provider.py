"""Verifikasi provider GENERIK "Custom OpenAI Compatible" (end-to-end lokal).

Sasaran: membuktikan Settings AETHER berfungsi sebagai KONFIGURATOR ENDPOINT LLM
(bukan daftar provider hardcode). Satu implementasi generik dipakai untuk
endpoint OpenAI-compatible apa pun (Gerry/Bariska/9Router/server lokal).

Diuji TANPA API cloud nyata (request HTTP di-inject):
    A. Provider type "custom" ada di katalog (label + flags).
    B. Nama instance, Base URL, API key env, dan model BEBAS.
       - env bebas: GERRY_API_KEY, BARISKA_API_KEY, MY_ROUTER_API_KEY, dst.
       - pola <PREFIX>_API_KEY tetap satu-satunya aturan validasi (generik).
       - provider type lain (openrouter) TETAP menolak prefix asing (regresi).
    C. Model FLEKSIBEL: model konkret, "auto", dan alias routing.
    D. resolve_runtime_config -> build_provider_from_config (factory) generik.
    E. Payload yang benar-benar dikirim: URL {base_url}/chat/completions + field
       `model` sesuai konfigurasi; API key opsional (tanpa Bearer bila kosong).
    F. Berbagai jalur runtime memakai resolver yang SAMA:
       Test Connection (gateway), Consultant (dengan & tanpa instance id).
    G. 9Router tetap bekerja (tidak ada regresi).

Fixture DB/.env dibuat di `dummy_test/custom_provider_fixture`, dibersihkan lagi.

Jalankan:
    python scripts/check_custom_provider.py
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
FIXTURE = DUMMY_ROOT / "custom_provider_fixture"

GERRY_SECRET = "sk-gerry-EXAMPLE-1234567890"
BARISKA_SECRET = "sk-bariska-EXAMPLE-0987654321"

ENV_FIXTURE = f"""# Dummy .env untuk verifikasi provider generik (JANGAN produksi)
GERRY_API_KEY={GERRY_SECRET}
BARISKA_API_KEY={BARISKA_SECRET}
"""


# --------------------------------------------------------------------------- #
# Fake HTTP (tanpa jaringan): menangkap URL + payload + header
# --------------------------------------------------------------------------- #
_CAPTURED: list = []
#: Respons `GET {base_url}/models` (bisa diubah test). Default OpenAI-compatible.
_MODELS_DATA: list = [{"id": "discovered-model-from-models", "object": "model"}]
_MODELS_STATUS: int = 200


class _FakeResponse:
    status_code = 200

    def __init__(self, text: str = '{"ok": true}') -> None:
        self.text = text
        self.content = text.encode("utf-8")

    def json(self):
        return {
            "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
            "model": "auto",
        }


class _FakeModelsResponse:
    def __init__(self, status_code: int = 200, data: list | None = None) -> None:
        self.status_code = status_code
        self._data = data if data is not None else _MODELS_DATA
        self.text = "{}"

    def json(self):
        return {"object": "list", "data": self._data}


class _FakeRequests:
    @staticmethod
    def post(url, json=None, headers=None, timeout=None):  # noqa: A002 - mirror nama param
        _CAPTURED.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return _FakeResponse()

    @staticmethod
    def get(url, headers=None, timeout=None):  # noqa: A002 - mirror nama param
        _CAPTURED.append({"url": url, "json": None, "headers": headers, "timeout": timeout, "method": "GET"})
        return _FakeModelsResponse(status_code=_MODELS_STATUS, data=_MODELS_DATA)


def _install_fake_http() -> None:
    import agent_ai.providers.openai_compatible as oc

    _CAPTURED.clear()
    oc.requests = _FakeRequests


def _freeze_retry(provider) -> None:
    from agent_ai.providers.retry import InfrastructureRetryPolicy

    provider.retry_policy = InfrastructureRetryPolicy(enabled=False)


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / ".env").write_text(ENV_FIXTURE, encoding="utf-8")


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


# --------------------------------------------------------------------------- #
# A. Katalog provider type
# --------------------------------------------------------------------------- #
def _check_catalog() -> None:
    from agent_ai.llm_config import list_provider_types

    specs = {t.key: t for t in list_provider_types()}
    assert "custom" in specs, sorted(specs)
    spec = specs["custom"]
    assert spec.label == "Custom OpenAI Compatible", spec.label
    assert spec.requires_api_key is False, spec
    assert spec.requires_model is False, spec
    assert spec.allow_custom_env is True, spec
    assert spec.supports_model_discovery is True, spec
    d = spec.to_dict()
    for key in ("key", "label", "env_prefix", "default_api_url", "requires_api_key",
                "requires_model", "needs_model_field", "allow_custom_env",
                "supports_model_discovery"):
        assert key in d, (key, d)
    print("[A] katalog: provider type 'custom' (Custom OpenAI Compatible) ada")
    print("    (supports_model_discovery=True -> GET /models bila model kosong)")


# --------------------------------------------------------------------------- #
# B. Env API key bebas + validasi generik + regresi provider lain
# --------------------------------------------------------------------------- #
def _check_env_names(svc) -> None:
    from agent_ai.llm_config import LLMConfigValidationError

    # Nama env bebas (selama mengikuti pola baku <PREFIX>_API_KEY) DITERIMA.
    for i, env_name in enumerate(
        (
            "GERRY_API_KEY",
            "BARISKA_API_KEY",
            "MY_ROUTER_API_KEY",
            "9ROUTER_API_KEY",
            "SEMBILAN_ROUTER_API_KEY",
            "GERRY_API_KEY_AKUN_2",
        )
    ):
        inst = svc.create_provider_instance(
            name=f"Custom Env {i}",
            provider_type="custom",
            api_key_env=env_name,
            api_url="http://localhost:9000/v1",
        )
        assert inst.api_key_env == env_name, inst

    # Pola malformed tetap DITOLAK (satu aturan validasi generik konsisten).
    for bad in ("gerry_api_key", "GERRY_MODEL", "GERRY_APIKEY", "API_KEY", "GERRY_API_KEY_"):
        try:
            svc.create_provider_instance(
                name=f"Bad {bad}", provider_type="custom", api_key_env=bad,
                api_url="http://localhost:9000/v1",
            )
        except LLMConfigValidationError:
            pass
        else:
            raise AssertionError(f"env '{bad}' seharusnya ditolak")

    # API key OPSIONAL untuk custom (endpoint lokal boleh tanpa key).
    keyless = svc.create_provider_instance(
        name="Custom Keyless", provider_type="custom", api_key_env="",
        api_url="http://localhost:9001/v1",
    )
    assert keyless.api_key_env == "", keyless

    # REGRESI: provider type lain TETAP menolak prefix asing.
    try:
        svc.create_provider_instance(
            name="OR Salah Prefix", provider_type="openrouter",
            api_key_env="GERRY_API_KEY",
        )
    except LLMConfigValidationError:
        pass
    else:
        raise AssertionError("openrouter dengan prefix asing seharusnya ditolak")
    print("[B] env API key bebas untuk custom; prefix tetap valid untuk openrouter")
    print("    (API key opsional untuk custom; pola <PREFIX>_API_KEY satu aturan)")


# --------------------------------------------------------------------------- #
# C/D/E. Model fleksibel + factory + payload
# --------------------------------------------------------------------------- #
def _check_custom_runtime(svc) -> None:
    from agent_ai.providers.custom import CustomOpenAIProvider
    from agent_ai.providers.factory import build_provider_from_config

    inst = svc.create_provider_instance(
        name="Gerry",
        provider_type="custom",
        api_key_env="GERRY_API_KEY",
        api_url="http://gerry.com/v1",
    )
    cfg = svc.get_provider_config(inst.id)
    assert cfg["provider_label"] == "Custom OpenAI Compatible", cfg
    assert cfg["requires_model"] is False, cfg

    # --- model "auto" (routing) ---
    svc.add_model(inst.id, "auto")
    resolved = svc.resolve_runtime_config(inst.id)
    assert resolved["provider_type"] == "custom", resolved
    assert resolved["api_url"] == "http://gerry.com/v1", resolved
    assert resolved["api_key"] == GERRY_SECRET, resolved
    assert resolved["model"] == "auto", resolved

    provider = build_provider_from_config(resolved)
    assert isinstance(provider, CustomOpenAIProvider), provider
    assert provider.config.base_url == "http://gerry.com/v1", provider.config
    assert provider.config.model == "auto", provider.config
    assert provider.config.api_key == GERRY_SECRET, provider.config

    _freeze_retry(provider)
    result = provider.generate(prompt="ping")
    assert _CAPTURED, "request tidak terkirim"
    last = _CAPTURED[-1]
    assert last["url"] == "http://gerry.com/v1/chat/completions", last["url"]
    assert last["json"]["model"] == "auto", last["json"]
    assert last["headers"]["Authorization"] == f"Bearer {GERRY_SECRET}", last["headers"]
    assert result.text == "pong", result
    print("[C] custom + model 'auto' -> payload model='auto' OK")
    print(f"    POST {last['url']} (Authorization terisi)")

    # --- model konkret (fixed) ---
    inst2 = svc.create_provider_instance(
        name="Bariska",
        provider_type="custom",
        api_key_env="BARISKA_API_KEY",
        api_url="https://bariska.example/v1",
    )
    svc.add_model(inst2.id, "deepseek-v4.1-flash")
    resolved2 = svc.resolve_runtime_config(inst2.id)
    assert resolved2["model"] == "deepseek-v4.1-flash", resolved2
    provider2 = build_provider_from_config(resolved2)
    _freeze_retry(provider2)
    _install_fake_http()
    provider2.generate(prompt="ping")
    assert _CAPTURED[-1]["json"]["model"] == "deepseek-v4.1-flash", _CAPTURED[-1]
    assert _CAPTURED[-1]["url"] == "https://bariska.example/v1/chat/completions"
    print("[C] custom + model konkret 'deepseek-v4.1-flash' OK")

    # --- model alias routing "auto-test" (tanpa special-case 9Router) ---
    inst3 = svc.create_provider_instance(
        name="Alias Router",
        provider_type="custom",
        api_key_env="MY_ROUTER_API_KEY",
        api_url="http://127.0.0.1:20128/v1",
    )
    svc.add_model(inst3.id, "auto-test")
    resolved3 = svc.resolve_runtime_config(inst3.id)
    assert resolved3["model"] == "auto-test", resolved3
    provider3 = build_provider_from_config(resolved3)
    _freeze_retry(provider3)
    _install_fake_http()
    provider3.generate(prompt="ping")
    assert _CAPTURED[-1]["json"]["model"] == "auto-test", _CAPTURED[-1]
    print("[C] custom + alias routing 'auto-test' OK")

    # --- keyless: tanpa API key -> TIDAK ada header Authorization ---
    keyless = svc.create_provider_instance(
        name="Local Keyless",
        provider_type="custom",
        api_key_env="",
        api_url="http://127.0.0.1:1234/v1",
    )
    svc.add_model(keyless.id, "auto")
    resolved_keyless = svc.resolve_runtime_config(keyless.id)
    assert resolved_keyless["api_key"] is None, resolved_keyless
    provider_keyless = build_provider_from_config(resolved_keyless)
    _freeze_retry(provider_keyless)
    _install_fake_http()
    provider_keyless.generate(prompt="ping")
    assert "Authorization" not in _CAPTURED[-1]["headers"], _CAPTURED[-1]["headers"]
    print("[E] custom keyless -> request tanpa header Authorization OK")

    # --- model kosong (tak ada model) -> DISCOVERY dari GET /models ---
    nomodel = svc.create_provider_instance(
        name="No Model", provider_type="custom", api_key_env="",
        api_url="http://127.0.0.1:4321/v1",
    )
    resolved_nomodel = svc.resolve_runtime_config(nomodel.id)
    assert resolved_nomodel["model"] == "", resolved_nomodel
    provider_nomodel = build_provider_from_config(resolved_nomodel)
    assert provider_nomodel.supports_model_discovery is True, provider_nomodel
    _freeze_retry(provider_nomodel)
    _install_fake_http()
    provider_nomodel.generate(prompt="ping")
    get_calls = [c for c in _CAPTURED if c.get("method") == "GET"]
    assert get_calls, "discovery harus GET /models"
    assert get_calls[0]["url"] == "http://127.0.0.1:4321/v1/models", get_calls[0]["url"]
    assert _CAPTURED[-1]["json"]["model"] == _MODELS_DATA[0]["id"], _CAPTURED[-1]["json"]
    print("[D] custom tanpa model -> GET /models, model hasil discovery dipakai OK")

    # --- model kosong + endpoint TIDAK menyediakan /models -> field omitted ---
    global _MODELS_STATUS
    nomodel2 = svc.create_provider_instance(
        name="No Model No Discovery", provider_type="custom", api_key_env="",
        api_url="http://127.0.0.1:4322/v1",
    )
    resolved_nomodel2 = svc.resolve_runtime_config(nomodel2.id)
    provider_nomodel2 = build_provider_from_config(resolved_nomodel2)
    _freeze_retry(provider_nomodel2)
    _install_fake_http()
    _MODELS_STATUS = 404
    try:
        provider_nomodel2.generate(prompt="ping")
    finally:
        _MODELS_STATUS = 200
    assert "model" not in _CAPTURED[-1]["json"], _CAPTURED[-1]["json"]
    print("[D] custom tanpa model + /models 404 -> field 'model' di-omit OK")

    # --- Base URL wajib: custom tanpa Base URL -> error jelas (bukan fallback) ---
    from agent_ai.providers.base import ProviderNotConfiguredError

    try:
        build_provider_from_config({"provider_type": "custom", "instance_name": "X", "model": "auto"})
    except ProviderNotConfiguredError as exc:
        assert "Base URL" in str(exc), exc
        print(f"[D] custom tanpa Base URL -> ProviderNotConfiguredError OK: {exc}")
    else:
        raise AssertionError("custom tanpa Base URL seharusnya ditolak")

    return inst


# --------------------------------------------------------------------------- #
# G. Regresi 9Router (tetap bekerja, tanpa special-case baru)
# --------------------------------------------------------------------------- #
def _check_9router_regression(svc) -> None:
    from agent_ai.providers.factory import build_provider_from_config
    from agent_ai.providers.nine_router import NineRouterProvider

    inst = svc.create_provider_instance(
        name="9Router Regresi",
        provider_type="9router",
        api_key_env="SEMBILAN_ROUTER_API_KEY",
        api_url="http://127.0.0.1:20128/v1",
    )
    resolved = svc.resolve_runtime_config(inst.id)
    assert resolved["provider_type"] == "9router", resolved
    provider = build_provider_from_config(resolved)
    assert isinstance(provider, NineRouterProvider), provider
    _freeze_retry(provider)
    _install_fake_http()
    provider.generate(prompt="ping")
    # 9Router tetap mengirim field model (default "auto-test").
    assert _CAPTURED[-1]["json"]["model"] == "auto-test", _CAPTURED[-1]["json"]
    assert _CAPTURED[-1]["url"] == "http://127.0.0.1:20128/v1/chat/completions"
    print("[G] 9Router tetap bekerja (model default 'auto-test') OK")


# --------------------------------------------------------------------------- #
# F. Jalur runtime SAMA: Test Connection + Consultant (gateway)
# --------------------------------------------------------------------------- #
def _check_gateway(default_inst_id: str) -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"

    import django

    django.setup()

    import api.services as services_mod
    from agent_ai.llm_config import LLMConfigService
    from agent_ai.providers.custom import CustomOpenAIProvider
    from api.project_store import ProjectStore

    svc = LLMConfigService(db_path=FIXTURE / "gateway_llm.db", env_path=FIXTURE / ".env")
    inst = svc.create_provider_instance(
        name="Gerry Gateway",
        provider_type="custom",
        api_key_env="GERRY_API_KEY",
        api_url="http://gerry.com/v1",
    )
    svc.add_model(inst.id, "auto")

    service = services_mod.GatewayService(
        llm_config_service=svc,
        project_store=ProjectStore(db_path=FIXTURE / "gateway.db"),
        auto_execute=False,
    )

    # F.1 Test Connection memakai resolver + factory yang SAMA.
    _install_fake_http()
    result = service.test_llm_provider(inst.id)
    assert result["status"] == "ok", result
    assert _CAPTURED and _CAPTURED[-1]["url"] == "http://gerry.com/v1/chat/completions"
    print("[F] Test Connection -> resolver+factory generik OK")

    # F.2 Consultant dengan provider_instance_id.
    _install_fake_http()
    provider = service._build_consultant_provider(inst.id, None)
    assert isinstance(provider, CustomOpenAIProvider), provider
    assert provider.config.base_url == "http://gerry.com/v1", provider.config
    assert provider.config.model == "auto", provider.config
    print("[F] Consultant (dengan instance id) -> CustomOpenAIProvider OK")

    # F.3 Consultant TANPA provider_instance_id -> default dari Provider Instance DB.
    provider_default = service._build_consultant_provider(None, None)
    assert isinstance(provider_default, CustomOpenAIProvider), provider_default
    assert provider_default.config.base_url == "http://gerry.com/v1", provider_default.config
    print("[F] Consultant (tanpa instance id) -> default instance DB OK (bukan .env)")


def _run() -> int:
    print("=== Verifikasi Provider GENERIK 'Custom OpenAI Compatible' ===")
    # Isolasi: jangan biarkan environment proses mengganggu penentuan API key.
    for name in ("GERRY_API_KEY", "BARISKA_API_KEY", "MY_ROUTER_API_KEY"):
        os.environ.pop(name, None)

    setup_fixture()
    _install_fake_http()

    from agent_ai.llm_config import LLMConfigService

    svc = LLMConfigService(db_path=FIXTURE / "llm.db", env_path=FIXTURE / ".env")

    _check_catalog()
    _check_env_names(svc)
    _check_custom_runtime(svc)
    _check_9router_regression(svc)
    _check_gateway(default_inst_id="")

    print()
    print("[OK] Provider generik OpenAI-compatible bekerja (Settings->DB->resolver->factory->API).")
    return 0


def main() -> int:
    try:
        return _run()
    finally:
        teardown_fixture()


if __name__ == "__main__":
    raise SystemExit(main())
