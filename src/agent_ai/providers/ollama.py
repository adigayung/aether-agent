"""Implementasi provider Ollama.

Berbicara ke server Ollama lokal melalui HTTP API (/api/chat dan /api/generate).
Tidak bergantung pada library pihak ketiga selain `requests`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from agent_ai.config.settings import OllamaConfig, settings
from agent_ai.providers.base import (
    BaseProvider,
    GenerateOptions,
    GenerateResult,
    Message,
    ProviderAPIError,
    ProviderResponseError,
    ProviderUnavailableError,
    ToolChoice,
    ToolDefinition,
)


class OllamaProvider(BaseProvider):
    """Provider AI yang memakai server Ollama lokal."""

    name = "ollama"

    def __init__(self, config: Optional[OllamaConfig] = None) -> None:
        self.config = config or settings.ollama

    # ------------------------------------------------------------------ #
    # Helper internal
    # ------------------------------------------------------------------ #
    def _build_payload(
        self,
        prompt: Optional[str],
        messages: Optional[List[Message]],
        options: Optional[GenerateOptions],
        tools: Optional[List[ToolDefinition]] = None,
        tool_choice: Optional[ToolChoice] = None,
    ) -> Dict[str, Any]:
        """Bangun payload untuk endpoint /api/chat."""
        opts = options or GenerateOptions()
        chat_messages = self._build_messages(prompt, messages)

        payload: Dict[str, Any] = {
            "model": opts.model or self.config.model,
            "messages": chat_messages,
            "stream": False,
        }

        # Native tool calling: Ollama memakai format tools OpenAI-compatible.
        tool_defs = self._build_tool_definitions(tools)
        if tool_defs:
            payload["tools"] = [self._to_ollama_tool(t) for t in tool_defs]
            if tool_choice is not None:
                payload["tool_choice"] = self._to_ollama_tool_choice(tool_choice)

        # Gabungkan opsi generasi umum + extra khusus Ollama.
        gen_options: Dict[str, Any] = {}
        if opts.temperature is not None:
            gen_options["temperature"] = opts.temperature
        if opts.max_tokens is not None:
            gen_options["num_predict"] = opts.max_tokens
        gen_options.update(opts.extra or {})
        if gen_options:
            payload["options"] = gen_options

        return payload

    @staticmethod
    def _to_ollama_tool(tool: ToolDefinition) -> Dict[str, Any]:
        """Konversi ToolDefinition (internal) -> format tools Ollama."""
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    @staticmethod
    def _to_ollama_tool_choice(choice: ToolChoice) -> Any:
        """Konversi ToolChoice (internal) -> format tool_choice Ollama."""
        if choice.mode == "specific" and choice.name:
            return {"type": "function", "function": {"name": choice.name}}
        return choice.mode

    # ------------------------------------------------------------------ #
    # Interface BaseProvider
    # ------------------------------------------------------------------ #
    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Message]] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[List[ToolDefinition]] = None,
        tool_choice: Optional[ToolChoice] = None,
    ) -> GenerateResult:
        """Hasilkan teks via Ollama /api/chat."""
        payload = self._build_payload(prompt, messages, options, tools, tool_choice)
        url = f"{self.config.host.rstrip('/')}/api/chat"

        try:
            response = requests.post(url, json=payload, timeout=self.config.timeout)
        except requests.RequestException as exc:
            raise ProviderUnavailableError(
                f"Gagal menghubungi provider '{self.name}' di {self.config.host}: "
                f"{type(exc).__name__}"
            ) from exc

        if response.status_code >= 400:
            raise ProviderAPIError(
                f"Provider '{self.name}' mengembalikan HTTP {response.status_code}."
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderResponseError(
                f"Response dari provider '{self.name}' bukan JSON yang valid."
            ) from exc

        text = ""
        if isinstance(data, dict):
            message = data.get("message") or {}
            text = message.get("content", "") or data.get("response", "")

        return GenerateResult(
            text=text,
            model=payload.get("model", self.config.model),
            provider=self.name,
            raw=data if isinstance(data, dict) else {},
        )

    def is_available(self) -> bool:
        """Cek apakah server Ollama dapat dihubungi."""
        try:
            url = f"{self.config.host.rstrip('/')}/api/tags"
            response = requests.get(url, timeout=5)
            return response.status_code == 200
        except requests.RequestException:
            return False

    # ------------------------------------------------------------------ #
    # Normalisasi response (Ollama -> LLMResponse)
    # ------------------------------------------------------------------ #
    def normalize_response(self, result: GenerateResult) -> "LLMResponse":
        """Ubah raw response Ollama menjadi LLMResponse.

        Memetakan:
            - message.content      -> text
            - message.tool_calls[] -> LLMAction (arguments sudah dict)
            - done_reason          -> finish_reason

        Tidak mengeksekusi tool apa pun. API key tidak relevan untuk Ollama
        (lokal) dan tidak pernah disertakan.
        """
        from agent_ai.core.response import (
            ActionType,
            FinishReason,
            LLMAction,
            LLMResponse,
        )

        raw = result.raw if isinstance(result.raw, dict) else {}
        message = raw.get("message") or {}

        text = result.text or message.get("content", "") or raw.get("response", "")

        actions: List[LLMAction] = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            name = function.get("name", "")
            arguments = function.get("arguments", {})
            # Ollama kadang mengirim arguments sebagai string JSON; jaga agar
            # tetap terstruktur (dict) tanpa parsing regex.
            if isinstance(arguments, str):
                try:
                    import json

                    arguments = json.loads(arguments)
                except (ValueError, TypeError):
                    arguments = {"_raw": arguments}
            if not isinstance(arguments, dict):
                arguments = {"_raw": arguments}
            actions.append(
                LLMAction(
                    name=name,
                    arguments=arguments,
                    type=ActionType.TOOL_CALL,
                    id=call.get("id"),
                )
            )

        if actions:
            finish_reason = FinishReason.TOOL_CALLS
        else:
            done_reason = raw.get("done_reason")
            if done_reason in (None, "stop") or raw.get("done") is True:
                finish_reason = FinishReason.STOP
            else:
                finish_reason = FinishReason.UNKNOWN

        return LLMResponse(
            text=text,
            actions=actions,
            finish_reason=finish_reason,
            raw=raw,
            provider=self.name,
            model=result.model or self.config.model,
        )


