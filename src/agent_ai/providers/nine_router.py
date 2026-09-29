"""Implementasi provider 9Router.

9Router menyediakan API berformat OpenAI-compatible, sehingga provider ini
mewarisi OpenAICompatibleProvider dan hanya menyesuaikan nama + konfigurasi.

Karakteristik:
- requires_model = False: 9Router sendiri yang memilih model/provider secara otomatis.
- Request TIDAK boleh mengandung field `model`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent_ai.config.settings import NineRouterConfig, settings
from agent_ai.providers.base import (
    GenerateOptions,
    GenerateResult,
    Message,
    ToolChoice,
    ToolDefinition,
)
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider


class NineRouterProvider(OpenAICompatibleProvider):
    """Provider AI untuk 9Router API (OpenAI-compatible, model ditentukan 9Router)."""

    name = "9router"
    #: 9Router menentukan model sendiri, tapi field `model` WAJIB dikirim.
    #: Nilai "auto-test" adalah model default yang disediakan 9Router
    #: (dari endpoint /v1/models). Model selection tetap oleh 9Router.
    requires_model = False

    def __init__(self, config: Optional[NineRouterConfig] = None) -> None:
        self.config = config or settings.nine_router

    def _build_payload(
        self,
        prompt: Optional[str],
        messages: Optional[List[Message]],
        options: Optional[GenerateOptions],
        tools: Optional[List[ToolDefinition]] = None,
        tool_choice: Optional[ToolChoice] = None,
    ) -> Dict[str, Any]:
        """Bangun payload untuk endpoint /chat/completions.

        9Router WAJIB menerima field `model` (berdasarkan test langsung:
        tanpa model -> 400 "Missing model"). Nilai "auto-test" adalah model
        default yang disediakan 9Router. Model selection tetap oleh 9Router.
        """
        opts = options or GenerateOptions()
        chat_messages = self._build_messages(prompt, messages)
        chat_messages = [self._to_openai_message(m) for m in chat_messages]
        # Nama tool pada riwayat di-encode provider-safe (konsisten dengan
        # definisi tool yang dikirim; lihat OpenAICompatibleProvider).
        chat_messages = [self._encode_message_tool_names(m) for m in chat_messages]

        payload: Dict[str, Any] = {
            "model": opts.model or "auto",
            "messages": chat_messages,
            "stream": False,
        }
        if opts.temperature is not None:
            payload["temperature"] = opts.temperature
        if opts.max_tokens is not None:
            payload["max_tokens"] = opts.max_tokens

        # Native tool calling: kirim definisi tool (format OpenAI-compatible).
        tool_defs = self._build_tool_definitions(tools)
        if tool_defs:
            payload["tools"] = [self._to_openai_tool(t) for t in tool_defs]
            if tool_choice is not None:
                payload["tool_choice"] = self._to_openai_tool_choice(tool_choice)

        payload.update(opts.extra or {})
        return payload