"""Implementasi provider OpenRouter.

OpenRouter menyediakan API berformat OpenAI-compatible, sehingga provider ini
mewarisi OpenAICompatibleProvider dan hanya menyesuaikan nama + konfigurasi.
Tidak ada duplikasi implementasi HTTP/OpenAI-compatible.
"""

from __future__ import annotations

from typing import Optional

from agent_ai.config.settings import OpenRouterConfig, settings
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider


class OpenRouterProvider(OpenAICompatibleProvider):
    """Provider AI untuk OpenRouter API (cloud, OpenAI-compatible)."""

    name = "openrouter"

    def __init__(self, config: Optional[OpenRouterConfig] = None) -> None:
        self.config = config or settings.openrouter
