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
    ToolChoice,
    ToolDefinition,
)
from agent_ai.providers.retry import (
    InfrastructureRetryPolicy,
    post_with_infrastructure_retry,
)


class OpenAICompatibleProvider(BaseProvider):
    """Provider untuk API berformat OpenAI-compatible."""

    name = "openai"

    #: Policy retry INFRASTRUKTUR (network/timeout/HTTP 429/5xx). Bila None,
    #: dibaca dari config settings.provider_retry saat generate() dipanggil.
    #: Retry terjadi di dalam generate() sehingga loop/history tidak terpengaruh.
    retry_policy: Optional[InfrastructureRetryPolicy] = None

    def __init__(
        self,
        config: Optional[OpenAIConfig] = None,
        retry_policy: Optional[InfrastructureRetryPolicy] = None,
    ) -> None:
        self.config = config or settings.openai
        self.retry_policy = retry_policy

    def _retry_policy(self) -> InfrastructureRetryPolicy:
        """Policy retry efektif (instance override atau dari config)."""
        if self.retry_policy is None:
            self.retry_policy = InfrastructureRetryPolicy.from_settings()
        return self.retry_policy

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

    def _extra_headers(self) -> Dict[str, str]:
        """Header tambahan yang ditambahkan ke setiap request HTTP.

        Override oleh subclass untuk menambahkan atribusi atau
        metadata lainnya. Default: kosong.

        Jangan menyertakan API key atau credential di sini.
        """
        return {}

    def _build_headers(self) -> Dict[str, str]:
        """Bangun header HTTP untuk request ke provider."""
        headers: Dict[str, str] = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        headers.update(self._extra_headers())
        return headers

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
        headers = self._build_headers()

        # Retry INFRASTRUKTUR (technical only) dibatasi di layer ini: network/
        # timeout/connection + HTTP 429/500/529. Retry terjadi di dalam satu
        # pemanggilan generate(), sehingga loop/history tidak melihat retry
        # (tidak ada pesan/tool yang terduplikasi). Kegagalan logika agent
        # (tool/command/validation) tidak melalui jalur ini.
        response = post_with_infrastructure_retry(
            lambda: requests.post(
                url, json=payload, headers=headers, timeout=self.config.timeout
            ),
            policy=self._retry_policy(),
            provider_name=self.name,
            endpoint=self.config.base_url,
        )

        if response.status_code >= 400:
            raise ProviderAPIError(
                f"Provider '{self.name}' mengembalikan HTTP {response.status_code}.",
                status_code=response.status_code,
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
        ProviderResponseError yang jelas (bukan crash tersembunyi) — KECUALI
        bila response memang TERPOTONG oleh batas token (finish_reason=length):
        dalam kasus itu tool-call yang argumennya tidak lengkap DIBUANG (tidak
        ditebak/diperbaiki) dan ditandai `truncated` agar caller dapat
        melanjutkan secara recoverable, bukan gagal langsung. Ini mencegah
        "beberapa write_file besar dalam satu response" mematikan task.
        Tool-call yang lengkap tetap dikembalikan apa adanya.
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
        raw_reason = choices[0].get("finish_reason") if choices else None
        # Provider memotong output karena batas token: tool-call terakhir bisa
        # tidak lengkap (JSON argumen terpotong).
        length_truncated = raw_reason == "length"

        text = result.text or message.get("content") or ""

        actions: List[LLMAction] = []
        dropped = 0
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
                        if length_truncated:
                            # Respons terpotong: JANGAN menebak/memperbaiki JSON.
                            # Buang tool-call tak lengkap (tidak menulis file
                            # parsial) agar agent melanjutkan di turn berikutnya.
                            dropped += 1
                            continue
                        raise ProviderResponseError(
                            f"Argumen tool '{name}' bukan JSON yang valid: {exc}"
                        ) from exc
            if not isinstance(arguments, dict):
                if length_truncated:
                    dropped += 1
                    continue
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
            # Ditandai "truncated" hanya bila benar-benar ada tool-call yang
            # dibuang karena terpotong (length + ada argumen tak lengkap).
            truncated=length_truncated and dropped > 0,
            incomplete_tool_calls=dropped,
        )

