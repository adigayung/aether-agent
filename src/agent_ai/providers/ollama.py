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
    ToolChoice,
    ToolDefinition,
    build_provider_api_error,
)
from agent_ai.providers.retry import (
    InfrastructureRetryPolicy,
    post_with_infrastructure_retry,
)


#: Token yang disisakan dari anggaran prompt untuk hal non-pengetahuan
#: (system prompt Consultant/Agent, definisi tool, pertanyaan user, dan output).
_OLLAMA_PROMPT_RESERVE_TOKENS = 4096

#: Ollama memotong prompt yang melebihi kapasitas pada sekitar SEPARUH `num_ctx`
#: (terukur pada Ollama lokal: num_ctx=16384 -> prompt dievaluasi maksimum
#: ~8194 token; num_ctx=32768 -> ~16386 token). Anggaran prompt dihitung
#: konservatif dari num_ctx/2 agar konteks tidak terpotong oleh server.
_OLLAMA_PROMPT_BUDGET_DIVISOR = 2


class OllamaProvider(BaseProvider):
    """Provider AI yang memakai server Ollama lokal."""

    name = "ollama"

    #: Policy retry INFRASTRUKTUR (network/timeout/HTTP 429/5xx). Bila None,
    #: dibaca dari config settings.provider_retry saat generate() dipanggil.
    retry_policy: Optional[InfrastructureRetryPolicy] = None

    def __init__(
        self,
        config: Optional[OllamaConfig] = None,
        retry_policy: Optional[InfrastructureRetryPolicy] = None,
    ) -> None:
        self.config = config or settings.ollama
        self.retry_policy = retry_policy

    def _retry_policy(self) -> InfrastructureRetryPolicy:
        """Policy retry efektif (instance override atau dari config)."""
        if self.retry_policy is None:
            self.retry_policy = InfrastructureRetryPolicy.from_settings()
        return self.retry_policy

    # ------------------------------------------------------------------ #
    # Context window (parity dengan provider lain)
    # ------------------------------------------------------------------ #
    def knowledge_budget_tokens(self) -> Optional[int]:
        """Anggaran token untuk konteks pengetahuan (Project Bible, dsb.).

        Ollama memakai context window SERVER-SIDE yang defaultnya jauh lebih
        kecil daripada konteks yang dikirim AETHER, dan memotong prompt dari
        DEPAN tanpa error. Provider lain (cloud) memakai context window
        modelnya sendiri, sehingga konteks yang sama "kebetulan" diterima.

        Anggaran dihitung dari `num_ctx` yang memang dikirim provider ini
        (lihat `_build_payload`), dikurangi cadangan untuk system prompt/tool/
        output, sehingga AgentOrchestrator dapat memotong konteks pengetahuan
        secara terkendali dan system prompt tidak pernah hilang.

        Returns:
            Anggaran token konteks pengetahuan, atau None bila `num_ctx` = 0
            (tidak menyetel context window; perilaku server default).
        """
        num_ctx = int(getattr(self.config, "num_ctx", 0) or 0)
        if num_ctx <= 0:
            return None
        usable_prompt = num_ctx // _OLLAMA_PROMPT_BUDGET_DIVISOR
        return max(0, usable_prompt - _OLLAMA_PROMPT_RESERVE_TOKENS)

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
        # Ollama mengharapkan `function.arguments` pada assistant.tool_calls
        # sebagai OBJECT (dict), bukan JSON string seperti OpenAI. Konversi di
        # sini agar turn lanjutan (assistant(tool_calls) + tool result) tidak
        # ditolak HTTP 400 ("Value looks like object, but can't find closing
        # '}' symbol"). Isolasi khusus Ollama; provider lain tidak terpengaruh.
        chat_messages = self._normalize_tool_call_arguments(chat_messages)
        # Multimodal: konversi content part image internal -> field `images`
        # (list base64) pada pesan Ollama. Pesan text-only tidak berubah.
        chat_messages = [self._to_ollama_message(m) for m in chat_messages]

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
        # Context window server-side. Default Ollama kecil (~4096) dan prompt
        # yang melebihi kapasitas dipotong DARI DEPAN tanpa error (system prompt
        # + awal konteks hilang). Mengirim `num_ctx` membuat Ollama benar-benar
        # menerima konteks yang dikirim AETHER, setara provider lain yang
        # memakai context window modelnya. 0 = pakai default server.
        num_ctx = int(getattr(self.config, "num_ctx", 0) or 0)
        if num_ctx > 0:
            gen_options["num_ctx"] = num_ctx
        if opts.temperature is not None:
            gen_options["temperature"] = opts.temperature
        if opts.max_tokens is not None:
            gen_options["num_predict"] = opts.max_tokens
        gen_options.update(opts.extra or {})
        if gen_options:
            payload["options"] = gen_options

        return payload

    @staticmethod
    def _to_ollama_message(message: Dict[str, Any]) -> Dict[str, Any]:
        """Konversi pesan internal -> format Ollama (multimodal-safe).

        Pesan text-only (tanpa `parts` image) diteruskan apa adanya.

        Pesan dengan part image internal
        ({"type": "image", "mime_type":..., "encoding":"base64", "data":...})
        dikonversi menjadi field ``images`` (list base64) sesuai format
        multimodal Ollama. Part non-image diabaikan. ``content`` tetap teks.
        """
        parts = message.get("parts")
        if not parts:
            return message
        images: List[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image" and part.get("data"):
                images.append(str(part["data"]))
        result = dict(message)
        result.pop("parts", None)
        if isinstance(result.get("content"), list):
            # content list (bentuk OpenAI) -> teks gabungan untuk Ollama.
            text_parts = [
                str(b.get("text", ""))
                for b in result["content"]
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            result["content"] = "\n".join(t for t in text_parts if t)
        if images:
            result["images"] = images
        return result

    @staticmethod
    def _normalize_tool_call_arguments(
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Ubah `function.arguments` pada assistant.tool_calls menjadi dict.

        Kontrak internal AETHER (selaras OpenAI) menyerialisasi argumen tool
        menjadi JSON string. Ollama mengharapkan argumen sebagai OBJECT (dict);
        mengirim string memicu HTTP 400 ("Value looks like object, but can't
        find closing '}' symbol") pada turn lanjutan. Konversi ini HANYA untuk
        payload Ollama; struktur internal/provider lain tidak diubah.

        Toleran: string JSON valid -> dict; dict -> dibiarkan; string non-JSON
        -> dibiarkan apa adanya (tidak menebak).
        """
        import json

        for message in messages:
            if not isinstance(message, dict):
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        function["arguments"] = json.loads(arguments)
                    except (ValueError, TypeError):
                        # Bukan JSON valid: biarkan apa adanya (jangan menebak).
                        pass
        return messages

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

        # Retry INFRASTRUKTUR (technical only) dibatasi di layer ini: network/
        # timeout/connection + HTTP 429/500/529. Retry terjadi di dalam satu
        # pemanggilan generate(), sehingga loop/history tidak melihat retry.
        response = post_with_infrastructure_retry(
            lambda: requests.post(url, json=payload, timeout=self.config.timeout),
            policy=self._retry_policy(),
            provider_name=self.name,
            endpoint=self.config.host,
        )

        if response.status_code >= 400:
            # Pesan DIAGNOSABLE (additive): status + endpoint + potongan body
            # respons, sehingga 404 "model not found" (body JSON) dapat
            # dibedakan dari 404 "page not found" (body non-JSON/HTML).
            raise build_provider_api_error(
                self.name,
                response.status_code,
                method="POST",
                url=url,
                response=response,
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
            # Model "thinking" (mis. Qwen3) kadang menaruh JAWABAN di kanal
            # reasoning lalu menutup turn dengan `content` KOSONG dan tanpa tool
            # call. Provider cloud tidak berperilaku begitu, sehingga di
            # Consultant hasilnya tampak "tidak menjawab" padahal model sudah
            # menjawab. Fallback ini HANYA aktif saat content benar-benar kosong
            # DAN turn tidak memanggil tool (agar reasoning tidak ikut masuk ke
            # turn assistant(tool_calls) pada percakapan berikutnya).
            if (
                not str(text or "").strip()
                and not message.get("tool_calls")
                and str(message.get("thinking") or "").strip()
            ):
                text = message.get("thinking")

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


