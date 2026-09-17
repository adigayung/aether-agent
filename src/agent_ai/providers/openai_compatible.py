"""Provider generic untuk API yang menggunakan format OpenAI-compatible.

Tidak dikunci hanya untuk OpenAI. Base URL, API key, dan model diambil dari
konfigurasi, sehingga provider ini bisa dipakai untuk OpenRouter, Together,
Groq, atau API lain yang kompatibel dengan skema /chat/completions OpenAI.

Endpoint yang dipakai: POST {base_url}/chat/completions
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from agent_ai.config.settings import OpenAIConfig, settings
from agent_ai.providers.base import (
    BaseProvider,
    GenerateOptions,
    GenerateResult,
    Message,
    ProviderAPIError,
    ProviderNotConfiguredError,
    ProviderResponseError,
    ProviderUnavailableError,
    ToolChoice,
    ToolDefinition,
)


class OpenAICompatibleProvider(BaseProvider):
    """Provider untuk API berformat OpenAI-compatible."""

    name = "openai"

    def __init__(self, config: Optional[OpenAIConfig] = None) -> None:
        self.config = config or settings.openai

    # ------------------------------------------------------------------ #
    # Helper internal
    # ------------------------------------------------------------------ #
    def _require_config(self) -> None:
        """Pastikan API key dan base URL sudah dikonfigurasi.

        Raises:
            ProviderNotConfiguredError: bila API key / base URL kosong.
        """
        if not self.config.api_key:
            raise ProviderNotConfiguredError(
                f"Provider '{self.name}' belum dikonfigurasi: API key kosong. "
                f"Set variabel environment yang sesuai di .env."
            )
        if not self.config.base_url:
            raise ProviderNotConfiguredError(
                f"Provider '{self.name}' belum dikonfigurasi: base URL kosong."
            )

    def _build_payload(
        self,
        prompt: Optional[str],
        messages: Optional[List[Message]],
        options: Optional[GenerateOptions],
        tools: Optional[List[ToolDefinition]] = None,
        tool_choice: Optional[ToolChoice] = None,
    ) -> Dict[str, Any]:
        """Bangun payload untuk endpoint /chat/completions."""
        opts = options or GenerateOptions()
        chat_messages = self._build_messages(prompt, messages)

        payload: Dict[str, Any] = {
            "model": opts.model or self.config.model,
            "messages": chat_messages,
        }
        if opts.temperature is not None:
            payload["temperature"] = opts.temperature
        if opts.max_tokens is not None:
            payload["max_tokens"] = opts.max_tokens

        # Native tool calling: kirim definisi tool (format OpenAI-compatible).
        tool_defs = self._build_tool_definitions(tools)
        if tool_defs:
            payload["tools"] = [self._to_openai_tool(t) for t in tool_defs]
            # tool_choice hanya dikirim bila diminta; default tidak dipaksa.
            if tool_choice is not None:
                payload["tool_choice"] = self._to_openai_tool_choice(tool_choice)

        payload.update(opts.extra or {})
        return payload

    @staticmethod
    def _to_openai_tool(tool: ToolDefinition) -> Dict[str, Any]:
        """Konversi ToolDefinition (internal) -> format tools OpenAI."""
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    @staticmethod
    def _to_openai_tool_choice(choice: ToolChoice) -> Any:
        """Konversi ToolChoice (internal) -> format tool_choice OpenAI."""
        if choice.mode == "specific" and choice.name:
            return {"type": "function", "function": {"name": choice.name}}
        # "auto" | "none" | "required" dipetakan langsung.
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
        """Hasilkan teks via endpoint /chat/completions."""
        self._require_config()
        payload = self._build_payload(prompt, messages, options, tools, tool_choice)
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                url, json=payload, headers=headers, timeout=self.config.timeout
            )
        except requests.RequestException as exc:
            # Jangan sertakan header/API key pada pesan error.
            raise ProviderUnavailableError(
                f"Gagal menghubungi provider '{self.name}' di {self.config.base_url}: "
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

        text = self._extract_text(data)
        return GenerateResult(
            text=text,
            model=payload.get("model", self.config.model),
            provider=self.name,
            raw=data if isinstance(data, dict) else {},
        )

    @staticmethod
    def _extract_text(data: Any) -> str:
        """Ambil teks dari response format OpenAI-compatible.

        Raises:
            ProviderResponseError: bila struktur response tidak sesuai.
        """
        try:
            choices = data["choices"]
            message = choices[0]["message"]
            content = message.get("content", "")
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError(
                "Struktur response provider tidak sesuai format OpenAI-compatible."
            ) from exc
        return content or ""

    def is_available(self) -> bool:
        """Provider dianggap tersedia bila API key dan base URL sudah diisi.

        Tidak melakukan request jaringan agar tidak memanggil API cloud
        hanya untuk pengecekan.
        """
        return bool(self.config.api_key) and bool(self.config.base_url)

    # ------------------------------------------------------------------ #
    # Normalisasi response (OpenAI-compatible -> LLMResponse)
    # ------------------------------------------------------------------ #
    def normalize_response(self, result: GenerateResult) -> "LLMResponse":
        """Ubah raw response OpenAI-compatible menjadi LLMResponse.

        Memetakan:
            - choices[0].message.content      -> text
            - choices[0].message.tool_calls[] -> LLMAction (arguments dict)
            - choices[0].finish_reason        -> finish_reason

        Argument JSON string diparse menjadi dict. JSON invalid menghasilkan
        ProviderResponseError yang jelas (bukan crash tersembunyi).
        Tidak mengeksekusi tool apa pun. API key tidak pernah disertakan.
        """
        import json

        from agent_ai.core.response import (
            ActionType,
            FinishReason,
            LLMAction,
            LLMResponse,
        )

        raw = result.raw if isinstance(result.raw, dict) else {}
        choices = raw.get("choices") or []
        message = (choices[0].get("message") if choices else {}) or {}

        text = result.text or message.get("content") or ""

        actions: List[LLMAction] = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            name = function.get("name", "")
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                if arguments.strip() == "":
                    arguments = {}
                else:
                    try:
                        arguments = json.loads(arguments)
                    except (ValueError, TypeError) as exc:
                        raise ProviderResponseError(
                            f"Argumen tool '{name}' bukan JSON yang valid: {exc}"
                        ) from exc
            if not isinstance(arguments, dict):
                raise ProviderResponseError(
                    f"Argumen tool '{name}' harus berupa objek JSON."
                )
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
            raw_reason = choices[0].get("finish_reason") if choices else None
            if raw_reason in (None, "stop"):
                finish_reason = FinishReason.STOP
            elif raw_reason == "length":
                finish_reason = FinishReason.LENGTH
            elif raw_reason == "tool_calls":
                finish_reason = FinishReason.TOOL_CALLS
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

