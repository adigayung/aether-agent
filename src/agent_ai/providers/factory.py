"""Provider factory: bangun provider konkret dari konfigurasi LLM (SQLite).

Modul ini MENJEMBATANI domain konfigurasi LLM (`agent_ai.llm_config`) dengan
provider konkret yang sudah ada (`agent_ai.providers.*`). Ia hanya memetakan
`provider_type` hasil `LLMConfigService.resolve_runtime_config(...)` ke class
provider existing, dan menyuntikkan `api_url` / `api_key` / `model` dari
konfigurasi ke config provider. Untuk provider type generik "custom" dipakai
SATU implementasi generik (`CustomOpenAIProvider`) — bukan kelas per-layanan.

Ini melengkapi jalur default (`agent_ai.providers.registry.get_provider`) yang
membaca dari `settings` (.env). Factory ini dipakai ketika task menunjuk
provider instance dari konfigurasi tersimpan (UI konfigurasi LLM).

Sumber tunggal kebutuhan (requires_api_key / needs_model_field) = katalog
provider type (`agent_ai.llm_config.providers`), sehingga factory TIDAK
menduplikasi daftar/hardcode per provider.

Provider-agnostic: pemetaan `provider_type` -> class memakai kunci yang sama
dengan `agent_ai/providers/registry.py` dan `agent_ai/llm_config/providers.py`.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent_ai.providers.base import BaseProvider, ProviderNotConfiguredError

#: Provider type yang memetakan ke implementasi OpenAI-compatible.
_OPENAI_COMPATIBLE_TYPES = ("openrouter", "openai", "deepseek", "9router", "custom", "opencode")


def _clean(value: Any) -> str:
    """Normalisasi nilai konfigurasi menjadi string bersih."""
    if value is None:
        return ""
    return str(value).strip()


def _spec(provider_type: str) -> Any:
    """Ambil spec provider type dari katalog (None bila tak dikenal)."""
    from agent_ai.llm_config.providers import get_provider_type

    return get_provider_type(provider_type)


def _requires_api_key(provider_type: str) -> bool:
    """Apakah provider type ini mewajibkan API key (dari katalog)."""
    spec = _spec(provider_type)
    return spec.requires_api_key if spec is not None else True


def _needs_model_field(provider_type: str) -> bool:
    """Apakah AETHER menyertakan field `model` pada request (dari katalog)."""
    spec = _spec(provider_type)
    return spec.needs_model_field if spec is not None else True


def _supports_model_discovery(provider_type: str) -> bool:
    """Apakah provider boleh menemukan model via `GET /models` (dari katalog)."""
    spec = _spec(provider_type)
    return spec.supports_model_discovery if spec is not None else False


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
    elif provider_type == "9router":
        from agent_ai.config.settings import NineRouterConfig

        config_cls = NineRouterConfig
        # 9Router menentukan model sendiri; payload provider default ke
        # "auto-test". Jangan set model kosong di kwargs agar tidak menimpa
        # perilaku tersebut.
        model = ""
    elif provider_type == "opencode":
        from agent_ai.config.settings import OpenCodeConfig

        config_cls = OpenCodeConfig
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
        `DeepSeekProvider`, `NineRouterProvider`, `OpenAICompatibleProvider`,
        atau `CustomOpenAIProvider` untuk provider type "custom").

    Raises:
        ProviderNotConfiguredError: provider type tidak dikenal, provider cloud
            tidak memiliki API key, atau provider "custom" belum punya Base URL
            (gagal lebih awal dengan pesan jelas).
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

    # Provider GENERIK: endpoint OpenAI-compatible apa pun (Gerry/Bariska/9Router/
    # server lokal). Base URL & model bebas; API key opsional. Nilai api_key dan
    # model SELALU di-set eksplisit (walau kosong) agar TIDAK ada fallback diam-
    # diam ke default/env provider lain.
    if provider_type == "custom":
        if not api_url:
            raise ProviderNotConfiguredError(
                f"Provider instance '{instance_name}' (custom) belum memiliki "
                f"Base URL. Isi Base URL endpoint OpenAI-compatible (mis. "
                f"http://host:port/v1) di Settings."
            )
        from agent_ai.config.settings import OpenAIConfig
        from agent_ai.providers.custom import CustomOpenAIProvider

        custom_kwargs: Dict[str, Any] = {
            "base_url": api_url,
            "api_key": api_key,
            "model": model,
        }
        if timeout:
            custom_kwargs["timeout"] = int(timeout)
        if context_window:
            custom_kwargs["context_window"] = int(context_window)
        provider = CustomOpenAIProvider(config=OpenAIConfig(**custom_kwargs))
        provider.send_model_field = _needs_model_field(provider_type)
        # Bila instance tidak punya model eksplisit, provider generik boleh
        # menemukan model ID valid lewat `GET {base_url}/models`.
        provider.supports_model_discovery = _supports_model_discovery(provider_type)
        return provider

    if provider_type in _OPENAI_COMPATIBLE_TYPES:
        if _requires_api_key(provider_type) and not api_key:
            raise ProviderNotConfiguredError(
                f"Provider instance '{instance_name}' ({provider_type}) belum "
                f"memiliki API key. Set kredensial di .env lalu pilih ulang."
            )
        provider_config = _build_openai_compatible_config(
            provider_type, api_url, api_key, model, timeout, context_window
        )
        if provider_type == "openrouter":
            from agent_ai.providers.openrouter import OpenRouterProvider

            provider = OpenRouterProvider(config=provider_config)
        elif provider_type == "deepseek":
            from agent_ai.providers.deepseek import DeepSeekProvider

            provider = DeepSeekProvider(config=provider_config)
        elif provider_type == "9router":
            from agent_ai.providers.nine_router import NineRouterProvider

            provider = NineRouterProvider(config=provider_config)
        elif provider_type == "opencode":
            from agent_ai.providers.opencode import OpenCodeProvider

            provider = OpenCodeProvider(config=provider_config)
        else:
            from agent_ai.providers.openai_compatible import OpenAICompatibleProvider

            provider = OpenAICompatibleProvider(config=provider_config)
        # Hormati katalog: apakah field `model` dikirim ke endpoint.
        provider.send_model_field = _needs_model_field(provider_type)
        # Discovery model hanya untuk provider yang mendukungnya (katalog).
        provider.supports_model_discovery = _supports_model_discovery(provider_type)
        return provider

    raise ProviderNotConfiguredError(
        f"Provider type '{provider_type or '(kosong)'}' tidak dikenal. "
        f"Gunakan salah satu dari: ollama, openrouter, openai, deepseek, 9router, custom, opencode."
    )


__all__ = ["build_provider_from_config"]
