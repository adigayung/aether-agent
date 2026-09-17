"""Implementasi provider DeepSeek.

DeepSeek menyediakan API berformat OpenAI-compatible, sehingga provider ini
mewarisi OpenAICompatibleProvider dan hanya menyesuaikan nama + konfigurasi.
"""

from __future__ import annotations

from typing import Optional

from agent_ai.config.settings import DeepSeekConfig, settings
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider


class DeepSeekProvider(OpenAICompatibleProvider):
    """Provider AI untuk DeepSeek API (cloud)."""

    name = "deepseek"

    def __init__(self, config: Optional[DeepSeekConfig] = None) -> None:
        self.config = config or settings.deepseek
