"""Unit tests untuk OpenCodeProvider (OpenCode Zen).

Menguji:
    1. _build_headers() menghasilkan Bearer token standar.
    2. _require_config() gagal bila api_key atau base_url kosong.
    3. registry.get("opencode") mengembalikan OpenCodeProvider.
    4. build_provider_from_config() dengan provider_type="opencode" berhasil.
    5. Katalog get_provider_type("opencode") mengembalikan spec Zen yang benar.

Semua test DETERMINISTIK, tanpa network.

Jalankan:
    ./venv/bin/pytest tests/test_opencode_provider.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import pytest

from agent_ai.config.settings import OpenCodeConfig
from agent_ai.llm_config.providers import get_provider_type
from agent_ai.providers.base import ProviderNotConfiguredError
from agent_ai.providers.factory import build_provider_from_config
from agent_ai.providers.opencode import OpenCodeProvider
from agent_ai.providers.registry import get_provider


def _make_provider(
    base_url: str = "https://opencode.ai/zen/v1", api_key: str = "test-key"
) -> OpenCodeProvider:
    config = OpenCodeConfig(base_url=base_url, api_key=api_key)
    return OpenCodeProvider(config=config)


# --------------------------------------------------------------------------- #
# 1. _build_headers() — Bearer auth standar
# --------------------------------------------------------------------------- #
def test_build_headers_uses_bearer_token() -> None:
    api_key = "zen_secret_token_123"
    provider = _make_provider(api_key=api_key)
    headers = provider._build_headers()

    assert "Authorization" in headers
    assert headers["Authorization"] == f"Bearer {api_key}"
    assert headers["Content-Type"] == "application/json"


# --------------------------------------------------------------------------- #
# 2. _require_config() — validasi
# --------------------------------------------------------------------------- #
def test_require_config_fails_when_api_key_empty() -> None:
    provider = _make_provider(api_key="")
    with pytest.raises(ProviderNotConfiguredError, match="API key kosong"):
        provider._require_config()


def test_require_config_fails_when_base_url_empty() -> None:
    provider = _make_provider(base_url="", api_key="test-key")
    with pytest.raises(ProviderNotConfiguredError, match="base URL kosong"):
        provider._require_config()


def test_require_config_ok_with_both_key_and_url() -> None:
    provider = _make_provider()
    provider._require_config()  # tidak boleh raise


# --------------------------------------------------------------------------- #
# 3. Provider registry
# --------------------------------------------------------------------------- #
def test_registry_get_opencode_returns_opencode_provider() -> None:
    provider = get_provider("opencode")
    assert isinstance(provider, OpenCodeProvider)
    assert provider.name == "opencode"


# --------------------------------------------------------------------------- #
# 4. Factory: build_provider_from_config
# --------------------------------------------------------------------------- #
def test_factory_builds_opencode_provider() -> None:
    config: dict[str, Any] = {
        "provider_type": "opencode",
        "api_url": "https://opencode.ai/zen/v1",
        "api_key": "zen_test_key",
        "model": "claude-sonnet-4-5",
        "timeout": 60,
    }
    provider = build_provider_from_config(config)
    assert isinstance(provider, OpenCodeProvider)
    assert provider.name == "opencode"
    assert provider.config.base_url == "https://opencode.ai/zen/v1"
    assert provider.config.api_key == "zen_test_key"


def test_factory_opencode_missing_api_key_raises() -> None:
    config: dict[str, Any] = {
        "provider_type": "opencode",
        "api_url": "https://opencode.ai/zen/v1",
        "api_key": "",
        "model": "claude-sonnet-4-5",
    }
    with pytest.raises(ProviderNotConfiguredError, match="belum memiliki API key"):
        build_provider_from_config(config)


# --------------------------------------------------------------------------- #
# 5. Katalog provider type (OpenCode Zen)
# --------------------------------------------------------------------------- #
def test_catalog_opencode_spec_exists() -> None:
    spec = get_provider_type("opencode")
    assert spec is not None
    assert spec.key == "opencode"
    assert spec.label == "OpenCode Zen"


def test_catalog_opencode_requires_api_key() -> None:
    spec = get_provider_type("opencode")
    assert spec is not None
    assert spec.requires_api_key is True


def test_catalog_opencode_default_url_is_zen() -> None:
    spec = get_provider_type("opencode")
    assert spec is not None
    assert spec.default_api_url == "https://opencode.ai/zen/v1"


def test_catalog_opencode_requires_model() -> None:
    spec = get_provider_type("opencode")
    assert spec is not None
    assert spec.requires_model is True


def test_catalog_opencode_supports_model_discovery() -> None:
    spec = get_provider_type("opencode")
    assert spec is not None
    assert spec.supports_model_discovery is True


# --------------------------------------------------------------------------- #
# 6. LLMConfigService.ensure_default_providers() mendaftarkan OpenCode otomatis
# --------------------------------------------------------------------------- #
def test_llm_config_service_ensure_default_providers_registers_opencode(tmp_path: Path) -> None:
    from agent_ai.llm_config.service import LLMConfigService

    db_path = tmp_path / "test_aether.db"
    env_path = tmp_path / ".env"
    env_path.write_text("OPENCODE_API_KEY=test_zen_key\n", encoding="utf-8")

    svc = LLMConfigService(db_path=db_path, env_path=env_path)
    assert len(svc.store.list_provider_instances()) == 0

    # Panggil auto-integration: provider OpenCode Zen otomatis terintegrasi
    svc.ensure_default_providers()

    instances = svc.store.list_provider_instances()
    assert len(instances) == 1
    inst = instances[0]
    assert inst.provider_type == "opencode"
    assert inst.name == "OpenCode Zen"
    assert inst.enabled is True
    assert inst.api_key_env == "OPENCODE_API_KEY"

    # Model TIDAK di-hardcode (diisi secara dinamis / manual oleh user)
    models_initial = svc.store.list_models(inst.id)
    assert len(models_initial) == 0

    # User mengisi model secara dinamis / manual via UI
    model_user1 = svc.add_model(inst.id, "claude-sonnet-4-5")
    model_user2 = svc.add_model(inst.id, "glm-5.3-flash")
    models_after = svc.store.list_models(inst.id)
    assert len(models_after) == 2
    names = {m.model_name for m in models_after}
    assert names == {"claude-sonnet-4-5", "glm-5.3-flash"}

    # Idempotent: dipanggil lagi tidak menambah instance atau mengubah model user
    svc.ensure_default_providers()
    assert len(svc.store.list_provider_instances()) == 1
    assert len(svc.store.list_models(inst.id)) == 2


# --------------------------------------------------------------------------- #
# 7. LLMConfigService.ensure_default_providers() mengaktifkan legacy instance
# --------------------------------------------------------------------------- #
def test_llm_config_service_ensure_default_providers_updates_legacy_disabled(tmp_path: Path) -> None:
    from agent_ai.llm_config.service import LLMConfigService

    db_path = tmp_path / "legacy.db"
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")

    svc = LLMConfigService(db_path=db_path, env_path=env_path)
    # Simulasikan row legacy yang berstatus disabled dan bernama 'Opencode'
    legacy = svc.store.create_provider_instance(
        name="Opencode",
        provider_type="opencode",
        api_url="https://opencode.ai/zen/v1",
        api_key_env="OPENCODE_API_KEY",
        enabled=False,
    )
    # Model manual user tetap dipertahankan
    svc.store.create_model(provider_id=legacy.id, model_name="custom-model-x", enabled=True)

    # Jalankan auto-integration
    svc.ensure_default_providers()

    updated = svc.store.get_provider_instance(legacy.id)
    assert updated is not None
    assert updated.enabled is True
    assert updated.name == "OpenCode Zen"

    # Model manual user tidak dihapus atau ditimpa
    models = svc.store.list_models(legacy.id)
    assert len(models) == 1
    assert models[0].model_name == "custom-model-x"
