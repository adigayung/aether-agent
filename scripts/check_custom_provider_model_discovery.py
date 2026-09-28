"""Verifikasi MODEL DISCOVERY provider generik "Custom OpenAI Compatible".

Sasaran (memperbaiki kegagalan nyata endpoint OpenAI-compatible seperti
Bariska yang membalas ``HTTP 403: This token has no access to model``):

Skenario yang direproduksi TANPA jaringan (HTTP di-inject) — meniru tepat
perilaku gateway OpenAI-compatible yang menyediakan ``GET /models``:

    * ``GET {base_url}/models`` -> ``{"data": [{"id": "<valid-model>"}]}``
    * ``POST {base_url}/chat/completions`` -> 200 HANYA bila field ``model``
      berisi model ID yang diizinkan; selain itu 403
      ``"This token has no access to model"``.

Yang diverifikasi:
    A. BUKTI FAIL LAMA: instance custom TANPA model eksplisit + discovery OFF
       -> payload TIDAK memuat `model` -> server membalas 403
       "This token has no access to model" (persis keluhan user).
    B. PERBAIKAN: instance custom TANPA model eksplisit + discovery ON
       -> AETHER GET /models, memakai model ID valid, POST sukses 200.
       Payload HTTP aktual dicetak (URL + field model) untuk dibandingkan
       dengan script Python yang berhasil.
    C. Model EKSPLISIT tetap dipakai apa adanya (tidak ada GET /models).
    D. Jalur Consultant end-to-end (GatewayService.consult -> CustomOpenAIProvider)
       sukses (bukan sekadar "Test Connection").
    E. 9Router TIDAK berubah (tetap mengirim model default-nya, tanpa discovery).

Fixture DB/.env dibuat di `dummy_test/custom_provider_discovery_fixture`, lalu
dibersihkan. TIDAK menyentuh `data/aether.db`, `.env` asli, maupun project user.

Jalankan:
    python scripts/check_custom_provider_model_discovery.py
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
FIXTURE = DUMMY_ROOT / "custom_provider_discovery_fixture"

BARISKA_SECRET = "sk-bariska-EXAMPLE-0987654321"
#: Model ID "valid" yang disediakan endpoint via /models. Nama SENGAJA generik
#: (bukan nama model Bariska) — AETHER TIDAK hardcode model apa pun.
VALID_MODEL_ID = "bariska-chat-default"
ALLOWED_MODELS = {VALID_MODEL_ID}

ENV_FIXTURE = f"""# Dummy .env untuk verifikasi discovery (JANGAN produksi)
BARISKA_API_KEY={BARISKA_SECRET}
"""


# --------------------------------------------------------------------------- #
# Fake HTTP: meniru gateway OpenAI-compatible (models + authz by model)
# --------------------------------------------------------------------------- #
_POSTS: list = []
_GETS: list = []


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._payload


class _FakeRequests:
    @staticmethod
    def get(url, headers=None, timeout=None):  # noqa: A002 - mirror nama param
        _GETS.append({"url": url, "headers": headers, "timeout": timeout})
        if url.endswith("/models"):
            return _FakeResponse(
                200,
                {
                    "object": "list",
                    "data": [{"id": VALID_MODEL_ID, "object": "model"}],
                },
            )
        return _FakeResponse(404, {"error": {"message": "not found"}})

    @staticmethod
    def post(url, json=None, headers=None, timeout=None):  # noqa: A002 - mirror nama param
        payload = json or {}
        _POSTS.append({"url": url, "json": payload, "headers": headers, "timeout": timeout})
        model = payload.get("model")
        # Endpoint "Bariska" hanya mengizinkan model ID dari /models; endpoint
        # lain (9Router) memakai model default-nya sendiri. Meniru authz server.
        allowed = ALLOWED_MODELS if "bariska" in url else {"auto-test"}
        if model not in allowed:
            return _FakeResponse(
                403,
                {"error": {"message": "This token has no access to model", "model": model}},
            )
        return _FakeResponse(
            200,
            {
                "choices": [
                    {
                        "message": {"content": "pong dari endpoint OpenAI-compatible"},
                        "finish_reason": "stop",
                    }
                ],
                "model": model,
            },
        )


def _install_fake_http() -> None:
    import agent_ai.providers.openai_compatible as oc

    oc.requests = _FakeRequests
    _POSTS.clear()
    _GETS.clear()


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


def _make_bariska_instance(svc, *, name: str = "Bariska", model: str = ""):
    """Provider instance custom "Bariska" (tanpa hardcode model)."""
    inst = svc.create_provider_instance(
        name=name,
        provider_type="custom",
        api_key_env="BARISKA_API_KEY",
        api_url="https://api.bariska.cloud/v1",
    )
    if model:
        svc.add_model(inst.id, model)
    return inst


# --------------------------------------------------------------------------- #
# A. BUKTI FAIL LAMA: discovery OFF + model kosong -> 403
# --------------------------------------------------------------------------- #
def _check_old_failure(svc) -> None:
    from agent_ai.providers.base import ProviderAPIError
    from agent_ai.providers.factory import build_provider_from_config

    inst = _make_bariska_instance(svc, name="Bariska (old)", model="")
    resolved = svc.resolve_runtime_config(inst.id)
    assert resolved["model"] == "", resolved
    provider = build_provider_from_config(resolved)
    # Simulasikan perilaku LAMA (tanpa discovery) untuk membuktikan akar masalah.
    provider.supports_model_discovery = False
    _freeze_retry(provider)
    _install_fake_http()

    try:
        provider.generate(prompt="ping")
    except ProviderAPIError as exc:
        assert exc.status_code == 403, exc.status_code
        assert "no access to model" in str(exc), str(exc)
        assert "model" not in _POSTS[-1]["json"], _POSTS[-1]["json"]
        print("[A] TANPA discovery + model kosong -> payload tanpa 'model' -> 403 OK")
        print(f"    POST {_POSTS[-1]['url']} body model={'<TIDAK ADA>'}")
        return
    raise AssertionError("seharusnya 403 (tanpa model) seperti sebelumnya")


# --------------------------------------------------------------------------- #
# B. PERBAIKAN: discovery ON + model kosong -> GET /models -> model valid -> 200
# --------------------------------------------------------------------------- #
def _check_discovery(svc) -> None:
    from agent_ai.providers.custom import CustomOpenAIProvider
    from agent_ai.providers.factory import build_provider_from_config

    inst = _make_bariska_instance(svc, name="Bariska (discovery)", model="")
    resolved = svc.resolve_runtime_config(inst.id)
    assert resolved["provider_type"] == "custom", resolved
    assert resolved["api_url"] == "https://api.bariska.cloud/v1", resolved
    assert resolved["model"] == "", resolved  # tidak ada model tersimpan

    provider = build_provider_from_config(resolved)
    assert isinstance(provider, CustomOpenAIProvider), provider
    assert provider.supports_model_discovery is True, provider
    assert provider.config.model == "", provider.config
    _freeze_retry(provider)
    _install_fake_http()

    result = provider.generate(prompt="ping")

    # GET /models dipakai untuk menemukan model ID valid.
    assert _GETS and _GETS[0]["url"] == "https://api.bariska.cloud/v1/models", _GETS
    assert _GETS[0]["headers"]["Authorization"] == f"Bearer {BARISKA_SECRET}", _GETS[0]
    # POST memakai model hasil discovery (BUKAN "auto", BUKAN nama provider).
    last = _POSTS[-1]
    assert last["url"] == "https://api.bariska.cloud/v1/chat/completions", last["url"]
    assert last["json"]["model"] == VALID_MODEL_ID, last["json"]
    assert last["json"]["model"] != "auto", last["json"]
    assert last["headers"]["Authorization"] == f"Bearer {BARISKA_SECRET}", last["headers"]
    assert result.text.startswith("pong"), result
    # Discovery di-cache: generate kedua TIDAK GET /models lagi.
    gets_after_first = len(_GETS)
    provider.generate(prompt="ping lagi")
    assert len(_GETS) == gets_after_first, "discovery harus di-cache (satu GET)"
    print("[B] discovery ON + model kosong -> GET /models -> model valid -> 200 OK")
    print(f"    GET  {_GETS[0]['url']}")
    print(f"    POST {last['url']}")
    print(f"    payload.model = {last['json']['model']!r} (hasil discovery, bukan 'auto')")


# --------------------------------------------------------------------------- #
# C. Model EKSPLISIT tetap dipakai apa adanya (tanpa GET /models)
# --------------------------------------------------------------------------- #
def _check_explicit_model(svc) -> None:
    from agent_ai.providers.factory import build_provider_from_config

    inst = _make_bariska_instance(svc, model=VALID_MODEL_ID)
    resolved = svc.resolve_runtime_config(inst.id)
    assert resolved["model"] == VALID_MODEL_ID, resolved
    provider = build_provider_from_config(resolved)
    _freeze_retry(provider)
    _install_fake_http()
    provider.generate(prompt="ping")
    assert not _GETS, f"model eksplisit -> TIDAK boleh discovery: {_GETS}"
    assert _POSTS[-1]["json"]["model"] == VALID_MODEL_ID, _POSTS[-1]["json"]
    print("[C] model eksplisit -> dipakai apa adanya, tanpa GET /models OK")


# --------------------------------------------------------------------------- #
# E. 9Router tidak berubah
# --------------------------------------------------------------------------- #
def _check_9router_unchanged(svc) -> None:
    from agent_ai.providers.factory import build_provider_from_config
    from agent_ai.providers.nine_router import NineRouterProvider

    inst = svc.create_provider_instance(
        name="9Router Regresi",
        provider_type="9router",
        api_key_env="SEMBILAN_ROUTER_API_KEY",
        api_url="http://127.0.0.1:20128/v1",
    )
    resolved = svc.resolve_runtime_config(inst.id)
    provider = build_provider_from_config(resolved)
    assert isinstance(provider, NineRouterProvider), provider
    assert provider.supports_model_discovery is False, provider
    _freeze_retry(provider)
    _install_fake_http()
    provider.generate(prompt="ping")
    assert not _GETS, f"9Router TIDAK boleh discovery: {_GETS}"
    assert _POSTS[-1]["json"]["model"] == "auto-test", _POSTS[-1]["json"]
    print("[E] 9Router tetap mengirim model default 'auto-test' (tanpa discovery) OK")


# --------------------------------------------------------------------------- #
# D. Consultant end-to-end (GatewayService.consult -> CustomOpenAIProvider)
# --------------------------------------------------------------------------- #
def _check_consultant(default_inst_id: str) -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"

    import django

    django.setup()

    import api.services as services_mod
    from agent_ai.consultant import ConsultantService
    from agent_ai.llm_config import LLMConfigService
    from api.project_store import ProjectStore

    svc = LLMConfigService(db_path=FIXTURE / "gateway_llm.db", env_path=FIXTURE / ".env")
    inst = _make_bariska_instance(svc, name="Bariska (consultant)", model="")
    service = services_mod.GatewayService(
        llm_config_service=svc,
        project_store=ProjectStore(db_path=FIXTURE / "gateway.db"),
        consultant_service=ConsultantService(),
        auto_execute=False,
    )

    _install_fake_http()
    root = str(PROJECT_ROOT)
    result = service.consult(
        "Jelaskan singkat struktur repository ini.",
        provider_instance_id=inst.id,
        root=root,
        mode="quick",
    )
    assert result["status"] == "done", result
    assert result["reply"], result
    # Bukti payload aktual jalur Consultant: GET discovery + POST model valid.
    assert _GETS and _GETS[0]["url"] == "https://api.bariska.cloud/v1/models", _GETS
    consultant_posts = [
        p for p in _POSTS if p["url"] == "https://api.bariska.cloud/v1/chat/completions"
    ]
    assert consultant_posts, _POSTS
    assert consultant_posts[-1]["json"]["model"] == VALID_MODEL_ID, consultant_posts[-1]
    print("[D] Consultant end-to-end (Custom Provider -> Bariska) sukses OK")
    print(f"    reply = {result['reply'][:60]!r}...")
    print(f"    POST model = {consultant_posts[-1]['json']['model']!r}")


def _run() -> int:
    print("=== Verifikasi Model Discovery 'Custom OpenAI Compatible' (Bariska-like) ===")
    for name in ("BARISKA_API_KEY",):
        os.environ.pop(name, None)

    setup_fixture()
    _install_fake_http()

    from agent_ai.llm_config import LLMConfigService

    svc = LLMConfigService(db_path=FIXTURE / "llm.db", env_path=FIXTURE / ".env")

    _check_old_failure(svc)
    _check_discovery(svc)
    _check_explicit_model(svc)
    _check_9router_unchanged(svc)
    _check_consultant(default_inst_id="")

    print()
    print("[OK] Custom Provider kompatibel dengan endpoint OpenAI-compatible ber-/models.")
    return 0


def main() -> int:
    try:
        return _run()
    finally:
        teardown_fixture()


if __name__ == "__main__":
    raise SystemExit(main())
