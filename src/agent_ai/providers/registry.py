"""Registry provider AI.

Mendaftarkan provider berdasarkan nama dan mengambilnya kembali.
Agent Core cukup memanggil `get_provider("ollama")` atau
`get_provider(settings.default_provider)` lalu memakai `provider.generate(...)`.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Type

from agent_ai.config.settings import settings
from agent_ai.providers.base import BaseProvider
from agent_ai.providers.deepseek import DeepSeekProvider
from agent_ai.providers.ollama import OllamaProvider
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider
from agent_ai.providers.openrouter import OpenRouterProvider


class ProviderRegistry:
    """Kumpulan provider yang terdaftar, diakses lewat nama unik."""

    def __init__(self) -> None:
        self._providers: Dict[str, Type[BaseProvider]] = {}

    def register(self, provider_cls: Type[BaseProvider]) -> None:
        """Daftarkan sebuah kelas provider berdasarkan atribut `name`."""
        name = getattr(provider_cls, "name", None)
        if not name or name == "base":
            raise ValueError("Provider harus punya atribut 'name' yang unik dan bukan 'base'.")
        self._providers[name.lower()] = provider_cls

    def get(self, name: str) -> BaseProvider:
        """Ambil instance provider berdasarkan nama.

        Raises:
            KeyError: bila nama provider belum terdaftar.
        """
        key = (name or "").lower()
        if key not in self._providers:
            available = ", ".join(sorted(self._providers)) or "(kosong)"
            raise KeyError(f"Provider '{name}' tidak terdaftar. Tersedia: {available}")
        return self._providers[key]()

    def list_providers(self) -> List[str]:
        """Daftar nama provider yang terdaftar."""
        return sorted(self._providers)

    def has(self, name: str) -> bool:
        """Cek apakah provider terdaftar."""
        return (name or "").lower() in self._providers


# ---------------------------------------------------------------------------
# Registry global + pendaftaran provider bawaan
# ---------------------------------------------------------------------------
registry = ProviderRegistry()
registry.register(OllamaProvider)
registry.register(DeepSeekProvider)
registry.register(OpenRouterProvider)
registry.register(OpenAICompatibleProvider)


def get_provider(name: Optional[str] = None) -> BaseProvider:
    """Ambil provider berdasarkan nama.

    Bila `name` None, gunakan provider default dari settings.
    """
    return registry.get(name or settings.default_provider)

