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
    build_provider_api_error,
    from_provider_safe_tool_name,
    to_provider_safe_tool_name,
)
from agent_ai.providers.retry import (
    InfrastructureRetryPolicy,
    post_with_infrastructure_retry,
)


#: Token yang disisakan dari context window model untuk hal non-pesan
#: (system prompt, definisi tool, pertanyaan user, dan output). Nilai ini
#: menjaga anggaran pesan tetap berada DI BAWAH kemampuan provider, sehingga
#: request tidak pernah melebihi context window model.
_PROMPT_RESERVE_TOKENS = 4096


class OpenAICompatibleProvider(BaseProvider):
    """Provider untuk API berformat OpenAI-compatible."""

    name = "openai"

    #: Policy retry INFRASTRUKTUR (network/timeout/HTTP 429/5xx). Bila None,
    #: dibaca dari config settings.provider_retry saat generate() dipanggil.
    #: Retry terjadi di dalam generate() sehingga loop/history tidak terpengaruh.
    retry_policy: Optional[InfrastructureRetryPolicy] = None

    #: True bila field `model` disertakan pada payload. Endpoint yang menolak
    #: field `model` (routing murni di sisi server) dapat men-set False. Diisi
    #: oleh factory dari katalog provider type (`needs_model_field`).
    send_model_field: bool = True

    #: True bila provider boleh MENEMUKAN model lewat `GET {base_url}/models`
    #: ketika instance TIDAK punya model eksplisit. Default False: provider
    #: cloud (OpenAI/DeepSeek/OpenRouter) dan 9Router TIDAK berubah. Diisi oleh
    #: factory dari katalog (`supports_model_discovery`); provider generik
    #: "custom" mengaktifkannya.
    supports_model_discovery: bool = False

    #: Cache hasil discovery (per instance). Dibaca via `_discover_first_model`.
    _discovered_model: str = ""
    _model_discovery_done: bool = False

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
    # Context window (capability provider)
    # ------------------------------------------------------------------ #
    def knowledge_budget_tokens(self) -> Optional[int]:
        """Anggaran token prompt dari context window model yang DIKONFIGURASI.

        Provider cloud (OpenAI-compatible: OpenAI, DeepSeek, OpenRouter, dan
        API kompatibel lain) memakai context window modelnya sendiri. Tanpa
        laporan ini, AETHER memaksa seluruh provider cloud ke anggaran config
        global (`settings.context.max_tokens`, default 16.000) sehingga model
        dengan context window jauh lebih besar pun dipadatkan (compaction)
        hampir setiap iteration.

        Nilai diambil dari konfigurasi EXPLICIT milik provider
        (`OPENAI_CONTEXT_WINDOW` / `DEEPSEEK_CONTEXT_WINDOW` /
        `OPENROUTER_CONTEXT_WINDOW`). TIDAK ada tebakan/daftar model
        hardcode — capability harus dinyatakan oleh konfigurasi.

        Returns:
            Anggaran token prompt (context window - reserve), atau None bila
            context window belum dikonfigurasi / terlalu kecil. None berarti
            AETHER memakai anggaran config global seperti sebelumnya, sehingga
            perilaku lama TIDAK berubah.
        """
        window = int(getattr(self.config, "context_window", 0) or 0)
        if window <= 0:
            return None
        usable = window - _PROMPT_RESERVE_TOKENS
        if usable <= 0:
            return None
        return usable

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

    def _discover_first_model(self) -> str:
        """GET ``{base_url}/models`` dan ambil model ID PERTAMA yang valid.

        Dipanggil HANYA bila provider mengizinkan discovery
        (`supports_model_discovery`) dan instance TIDAK punya model eksplisit.
        Ini meniru klien OpenAI-compatible pada umumnya: sebagian gateway
        menolak request TANPA field `model` (mis. ``403 no access to model``),
        jadi model ID yang valid diambil dari endpoint alih-alih dikosongkan.

        Format respons yang didukung (provider-agnostic, tanpa hardcode
        layanan/model): ``{"data": [{"id": ...}, ...]}`` (OpenAI/OpenRouter),
        ``{"models": [...]}`` (mis. Ollama native), atau list biasa. Setiap item
        boleh berupa string atau object dengan kunci ``id``/``name``/``model``.

        Hasil di-cache per instance (satu GET per provider). Kegagalan apa pun
        (network / HTTP non-2xx / format tak dikenal) mengembalikan ``""``
        sehingga perilaku lama (field `model` di-omit -> server menentukan)
        tetap berlaku tanpa menambah kegagalan baru.
        """
        if getattr(self, "_model_discovery_done", False):
            return getattr(self, "_discovered_model", "")

        base_url = (getattr(self.config, "base_url", "") or "").rstrip("/")
        if not base_url:
            return ""
        url = f"{base_url}/models"
        # Tandai "sudah dicoba" SEGERA setelah percobaan pertama: discovery
        # adalah operasi opsional, cukup sekali per provider instance walau
        # gagal (hindari GET berulang di setiap iteration task yang panjang).
        self._model_discovery_done = True
        try:
            response = requests.get(
                url,
                headers=self._build_headers(),
                timeout=getattr(self.config, "timeout", None),
            )
        except Exception:  # noqa: BLE001 - discovery opsional, TIDAK boleh crash
            return ""

        if getattr(response, "status_code", 200) >= 400:
            return ""
        try:
            data = response.json()
        except Exception:  # noqa: BLE001 - body bukan JSON -> tidak ada model
            return ""

        model_id = self._first_model_id(data)
        self._discovered_model = model_id
        return model_id

    @staticmethod
    def _first_model_id(data: Any) -> str:
        """Ambil model ID pertama dari respons ``/models`` (defensif)."""
        items: Optional[List[Any]] = None
        if isinstance(data, dict):
            for key in ("data", "models"):
                value = data.get(key)
                if isinstance(value, list):
                    items = value
                    break
        elif isinstance(data, list):
            items = data
        if not items:
            return ""
        for item in items:
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, dict):
                for key in ("id", "name", "model"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        return ""

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
        chat_messages = [self._to_openai_message(m) for m in chat_messages]
        # Nama tool pada riwayat (assistant.tool_calls + pesan role "tool")
        # di-encode agar konsisten dengan definisi tool yang dikirim (lihat
        # `_to_openai_tool`). Nama aman tidak berubah (no-op).
        chat_messages = [self._encode_message_tool_names(m) for m in chat_messages]

        payload: Dict[str, Any] = {
            "messages": chat_messages,
        }
        # Model bersifat FLEKSIBEL: nilai bisa model konkret ("deepseek-v4.1-flash"),
        # nilai routing ("auto"/"auto-test"), KOSONG (server menentukan sendiri),
        # atau hasil DISCOVERY dari `GET /models` bila instance tidak punya model
        # eksplisit dan provider mengizinkannya (`supports_model_discovery`).
        # Field `model` hanya disertakan bila ada nilainya dan provider memang
        # menerimanya (`send_model_field`). Untuk semua provider bawaan model
        # selalu ada, sehingga perilaku lama TIDAK berubah.
        model = opts.model or self.config.model
        if not model and self.supports_model_discovery:
            model = self._discover_first_model()
        if model and self.send_model_field:
            payload["model"] = model
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

    # ------------------------------------------------------------------ #
    # Multimodal (image content parts) -> format OpenAI-compatible
    # ------------------------------------------------------------------ #
    @classmethod
    def _to_openai_message(cls, message: Dict[str, Any]) -> Dict[str, Any]:
        """Konversi pesan internal -> format OpenAI (text-only ATAU multimodal).

        Pesan text-only (``parts`` kosong) diteruskan apa adanya (content tetap
        string), sehingga perilaku request tanpa image TIDAK berubah.

        Pesan dengan ``parts`` image dikonversi menjadi content blocks:
            [{"type": "text", "text": <content>},
             {"type": "image_url", "image_url": {"url": "data:<mime>;base64,<data>"}}]
        Part yang tidak dikenali (bukan image) diabaikan; bagian teks tetap ada.
        """
        parts = message.get("parts")
        if not parts:
            return message
        blocks: List[Dict[str, Any]] = []
        text = message.get("content")
        if isinstance(text, str) and text.strip():
            blocks.append({"type": "text", "text": text})
        for part in parts:
            converted = cls._to_openai_content_part(part)
            if converted is not None:
                blocks.append(converted)
        if not blocks:
            return message
        result = dict(message)
        result["content"] = blocks
        result.pop("parts", None)
        return result

    @staticmethod
    def _to_openai_content_part(part: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Konversi satu content part internal -> content part OpenAI.

        Mendukung payload vision provider-agnostic:
            {"type": "image", "mime_type": "image/png",
             "encoding": "base64", "data": "<base64>"}
        -> {"type": "image_url", "image_url": {"url": "data:image/png;base64,<data>"}}

        Returns:
            Content part OpenAI, atau None bila part tidak didukung.
        """
        if not isinstance(part, dict):
            return None
        if part.get("type") == "text":
            return {"type": "text", "text": str(part.get("text", ""))}
        if part.get("type") != "image":
            return None
        data = part.get("data")
        mime = part.get("mime_type") or "image/png"
        encoding = part.get("encoding") or "base64"
        if not data:
            return None
        url = part.get("url")
        if not url:
            url = f"data:{mime};{encoding},{data}"
        return {"type": "image_url", "image_url": {"url": url}}

    @staticmethod
    def _to_openai_tool(tool: ToolDefinition) -> Dict[str, Any]:
        """Konversi ToolDefinition (internal) -> format tools OpenAI.

        Nama di-encode ke bentuk provider-safe (lihat
        `to_provider_safe_tool_name`) karena API OpenAI-compatible menolak nama
        di luar pola `^[a-zA-Z0-9_-]+$` — termasuk id Extension bertitik
        (mis. "aether.playwright.browser_click"). Nama internal AETHER tetap
        utuh; hanya representasi payload yang dinormalisasi.
        """
        return {
            "type": "function",
            "function": {
                "name": to_provider_safe_tool_name(tool.name),
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    @staticmethod
    def _encode_message_tool_names(message: Dict[str, Any]) -> Dict[str, Any]:
        """Encode nama tool pada SATU pesan riwayat (tanpa memutasi input).

        Meng-encode `tool_calls[].function.name` (pesan assistant) dan `name`
        (pesan role "tool") agar konsisten dengan definisi tool yang dikirim.
        Salinan dibuat agar riwayat asli AETHER tidak berubah.
        """
        tool_calls = message.get("tool_calls")
        role = message.get("role")
        name = message.get("name")
        needs_tool_name = role == "tool" and isinstance(name, str)
        if not isinstance(tool_calls, list) and not needs_tool_name:
            return message
        result = dict(message)
        if isinstance(tool_calls, list):
            new_calls: List[Any] = []
            for call in tool_calls:
                if isinstance(call, dict):
                    new_call = dict(call)
                    function = new_call.get("function")
                    if isinstance(function, dict):
                        new_function = dict(function)
                        if isinstance(new_function.get("name"), str):
                            new_function["name"] = to_provider_safe_tool_name(
                                new_function["name"]
                            )
                        new_call["function"] = new_function
                    new_calls.append(new_call)
                else:
                    new_calls.append(call)
            result["tool_calls"] = new_calls
        if needs_tool_name:
            result["name"] = to_provider_safe_tool_name(name)
        return result

    @staticmethod
    def _to_openai_tool_choice(choice: ToolChoice) -> Any:
        """Konversi ToolChoice (internal) -> format tool_choice OpenAI."""
        if choice.mode == "specific" and choice.name:
            return {
                "type": "function",
                "function": {"name": to_provider_safe_tool_name(choice.name)},
            }
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
            # Pesan DIAGNOSABLE (additive): status + endpoint + potongan body
            # respons. Perilaku (status_code/retryable/tipe exception) TIDAK
            # berubah; hanya pesan yang lebih informatif.
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
            # Nama tool yang dikirim ke API di-encode (provider-safe); kembalikan
            # ke identitas internal AETHER agar lookup registry tetap benar.
            # Nama yang sudah aman tidak berubah (no-op).
            if isinstance(name, str):
                name = from_provider_safe_tool_name(name)
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

