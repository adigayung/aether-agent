"""Implementasi provider OpenCode Zen.

OpenCode Zen (https://opencode.ai/docs/zen) adalah layanan model LLM resmi
berformat OpenAI-compatible langsung dari OpenCode cloud:
    Endpoint : https://opencode.ai/zen/v1
    Auth     : Authorization: Bearer <OPENCODE_API_KEY>

Model-model yang didukung:
    - claude-sonnet-4-5, claude-opus-4-6, claude-haiku-4-5
    - gpt-5.5, gpt-5.4, gpt-5.2-codex
    - gemini-3.8-flash, gemini-3.1-pro
    - deepseek-v4-flash, qwen3.8-max
    - free models: deepseek-v4-flash-free, fledge-alpha-free, dll.

Referensi: https://opencode.ai/docs/providers/#opencode-zen
"""

from __future__ import annotations

from typing import Optional

from agent_ai.config.settings import OpenCodeConfig, settings
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider


class OpenCodeProvider(OpenAICompatibleProvider):
    """Provider AI untuk OpenCode Zen API (OpenAI-compatible, cloud, Bearer auth)."""

    name = "opencode"

    def __init__(self, config: Optional[OpenCodeConfig] = None) -> None:
        self.config = config or settings.opencode
