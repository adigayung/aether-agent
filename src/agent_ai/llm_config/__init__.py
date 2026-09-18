"""Sistem konfigurasi LLM AETHER (core).

Paket ini mengelola PERSISTENSI konfigurasi LLM di database GLOBAL AETHER
(`data/aether.db`) dan referensi credential di `.env`.

Model data & relasi:
    API Key (.env)  ->  Provider Instance  ->  Model

Prinsip:
    - API key TIDAK PERNAH disimpan di SQLite. Yang disimpan hanya NAMA variabel
      `.env` (`api_key_env`). Nilai sebenarnya tetap berada di `.env`.
    - Provider type MENENTUKAN prefix env API key, default API URL, dan apakah
      API key wajib (lihat `agent_ai.llm_config.providers`).
    - Modul ini TIDAK berisi logic Agent, UI, atau pemanggilan provider.
      Ia hanya menyimpan/membaca konfigurasi agar modul lain (runtime) dapat
      memakainya.

Contoh:
    from agent_ai.llm_config import LLMConfigService

    svc = LLMConfigService()
    inst = svc.create_provider_instance(
        name="OpenRouter Utama",
        provider_type="openrouter",
        api_key_env="OPENROUTER_API_KEY",
    )
    svc.add_model(inst.id, "openai/gpt-4o-mini")
    cfg = svc.resolve_runtime_config(inst.id)
"""

from __future__ import annotations

from agent_ai.llm_config.env_file import EnvFile, mask_secret
from agent_ai.llm_config.errors import (
    LLMConfigConflictError,
    LLMConfigError,
    LLMConfigNotFoundError,
    LLMConfigValidationError,
)
from agent_ai.llm_config.models import ModelConfig, ProviderInstance
from agent_ai.llm_config.providers import (
    API_KEY_ENV_PATTERN,
    ProviderTypeSpec,
    get_provider_type,
    is_api_key_env_name,
    list_provider_types,
    parse_api_key_env_name,
    provider_type_for_env_prefix,
    provider_type_keys,
    require_provider_type,
    suffix_label,
)
from agent_ai.llm_config.service import CredentialInfo, LLMConfigService
from agent_ai.llm_config.store import LLMConfigStore

__all__ = [
    # service
    "LLMConfigService",
    "CredentialInfo",
    # store
    "LLMConfigStore",
    # models
    "ProviderInstance",
    "ModelConfig",
    # providers
    "ProviderTypeSpec",
    "list_provider_types",
    "provider_type_keys",
    "get_provider_type",
    "require_provider_type",
    "provider_type_for_env_prefix",
    "is_api_key_env_name",
    "parse_api_key_env_name",
    "suffix_label",
    "API_KEY_ENV_PATTERN",
    # env
    "EnvFile",
    "mask_secret",
    # errors
    "LLMConfigError",
    "LLMConfigNotFoundError",
    "LLMConfigValidationError",
    "LLMConfigConflictError",
]
