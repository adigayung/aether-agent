"""Provider factory: bangun provider konkret dari konfigurasi LLM (SQLite).

Modul ini MENJEMBATANI domain konfigurasi LLM (`agent_ai.llm_config`) dengan
provider konkret yang sudah ada (`agent_ai.providers.*`). Ia TIDAK membuat
provider/abstraksi baru: hanya memetakan `provider_type` hasil
`LLMConfigService.resolve_runtime_config(...)` ke class provider existing, dan
menyuntikkan `api_url` / `api_key` / `model` dari konfigurasi ke config provider.

Ini melengkapi jalur default (`agent_ai.providers.registry.get_provider`) yang
membaca dari `settings` (.env). Factory ini dipakai ketika task menunjuk
provider instance dari konfigurasi tersimpan (UI konfigurasi LLM).

Provider-agnostic: pemetaan `provider_type` -> class memakai kunci yang sama
dengan `agent_ai/providers/registry.py` dan `agent_ai/llm_config/providers.py`.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent_ai.providers.base import BaseProvider, ProviderNotConfiguredError

#: Provider type yang membutuhkan API key (cloud, OpenAI-compatible).
_OPENAI_COMPATIBLE_TYPES = ("openrouter", "openai", "deepseek")


def _clean(value: Any) -> str:
    """Normalisasi nilai konfigurasi menjadi string bersih."""
    if value is None:
        return ""
    return str(value).strip()


def _build_openai_compatible_config(
    provider_type: str,
    api_url: str,
    api_key: str,
    model: str,
    timeout: Optional[int],
    context_window: Optional[int] = None,
) -> Any:
    """Bangun config dataclass spesifik provider (existing) dari resolved config."""
    if provider_type == "openrouter":
        from agent_ai.config.settings import OpenRouterConfig

        config_cls: Any = OpenRouterConfig
    elif provider_type == "openai":
        from agent_ai.config.settings import OpenAIConfig

        config_cls = OpenAIConfig
    elif provider_type == "deepseek":
        from agent_ai.config.settings import DeepSeekConfig

        config_cls = DeepSeekConfig
    else:  # pragma: no cover - dijaga caller
        raise ProviderNotConfiguredError(
            f"Provider type '{provider_type}' tidak dikenal."
        )

    kwargs: Dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if api_url:
        kwargs["base_url"] = api_url
    if model:
        kwargs["model"] = model
    if timeout:
        kwargs["timeout"] = int(timeout)
    # Context window (capability provider) diteruskan bila konfigurasi
    # menyediakannya. Bila tidak, dataclass memakai nilai dari environment
    # (`<PROVIDER>_CONTEXT_WINDOW`), dan 0 berarti "tidak diketahui" -> AETHER
    # memakai anggaran config global seperti sebelumnya.
    if context_window:
        kwargs["context_window"] = int(context_window)
    return config_cls(**kwargs)


def build_provider_from_config(config: Dict[str, Any]) -> BaseProvider:
    """Rakit provider konkret dari konfigurasi resolved (SQLite).

    Args:
        config: hasil `LLMConfigService.resolve_runtime_config(...)`; minimal
            berisi keys: `provider_type`, `api_url`, `api_key`, `model`.

    Returns:
        Instance provider existing (`OllamaProvider`, `OpenRouterProvider`,
        `OpenAICompatibleProvider`, atau `DeepSeekProvider`).

    Raises:
        ProviderNotConfiguredError: provider type tidak dikenal, atau provider
            cloud tidak memiliki API key (gagal lebih awal dengan pesan jelas).
    """
    provider_type = _clean(config.get("provider_type")).lower()
    api_url = _clean(config.get("api_url"))
    api_key = _clean(config.get("api_key"))
    model = _clean(config.get("model"))
    timeout = config.get("timeout")
    context_window = config.get("context_window")
    instance_name = _clean(config.get("instance_name")) or provider_type

    if provider_type == "ollama":
        from agent_ai.config.settings import OllamaConfig
        from agent_ai.providers.ollama import OllamaProvider

        # Model WAJIB berasal dari konfigurasi tersimpan (SQLite Provider
        # Instance -> Model), sama seperti DeepSeek/OpenRouter. TIDAK ada
        # fallback diam-diam ke model default hardcode (mis. qwen2.5-coder:7b):
        # bila tidak ada model terpilih/tersedia, gagal lebih awal dengan pesan
        # jelas agar runtime tidak memakai model yang salah.
        if not model:
            raise ProviderNotConfiguredError(
                f"Provider instance '{instance_name}' (ollama) belum memiliki "
                f"model. Tambahkan model pada provider instance Ollama lalu "
                f"pilih model tersebut."
            )
        defaults = OllamaConfig()
        kwargs: Dict[str, Any] = {
            "host": api_url or defaults.host,
            "model": model,
        }
        if timeout:
            kwargs["timeout"] = int(timeout)
        return OllamaProvider(config=OllamaConfig(**kwargs))

    if provider_type in _OPENAI_COMPATIBLE_TYPES:
        if not api_key:
            raise ProviderNotConfiguredError(
                f"Provider instance '{instance_name}' ({provider_type}) belum "
                f"memiliki API key. Set kredensial di .env lalu pilih ulang."
            )
        provider_config = _build_openai_compatible_config(
            provider_type, api_url, api_key, model, timeout, context_window
        )
        if provider_type == "openrouter":
            from agent_ai.providers.openrouter import OpenRouterProvider

            return OpenRouterProvider(config=provider_config)
        if provider_type == "deepseek":
            from agent_ai.providers.deepseek import DeepSeekProvider

            return DeepSeekProvider(config=provider_config)
        from agent_ai.providers.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(config=provider_config)

    raise ProviderNotConfiguredError(
        f"Provider type '{provider_type or '(kosong)'}' tidak dikenal. "
        f"Gunakan salah satu dari: ollama, openrouter, openai, deepseek."
    )


__all__ = ["build_provider_from_config"]
