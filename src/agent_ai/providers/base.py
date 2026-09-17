"""Interface/abstract base untuk semua provider AI.

Agent Core nantinya cukup memanggil `provider.generate(...)` tanpa perlu
tahu apakah provider tersebut Ollama, DeepSeek, OpenAI, Gemini, atau lainnya.
Semua provider konkret WAJIB mengimplementasikan interface ini.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Exception hierarchy untuk provider
# ---------------------------------------------------------------------------
class ProviderError(Exception):
    """Base exception untuk semua error provider."""


class ProviderNotConfiguredError(ProviderError):
    """Provider belum dikonfigurasi (mis. API key / base URL kosong)."""


class ProviderUnavailableError(ProviderError):
    """Provider tidak dapat dihubungi (connection error / server mati)."""


class ProviderAPIError(ProviderError):
    """Provider mengembalikan HTTP/API error."""


class ProviderResponseError(ProviderError):
    """Response dari provider tidak valid atau tidak dapat diparse."""


@dataclass
class Message:
    """Representasi satu pesan dalam percakapan (format netral/agnostik)."""

    role: str  # "system" | "user" | "assistant"
    content: str

    def to_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class GenerateOptions:
    """Opsi generasi yang bersifat umum (tidak terikat provider tertentu)."""

    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    model: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolDefinition:
    """Definisi tool yang provider-agnostic.

    Format internal AETHER, BUKAN format OpenAI/DeepSeek/Ollama. Provider
    bertanggung jawab mengonversi ke format API masing-masing.

    Attributes:
        name: nama tool.
        description: deskripsi singkat.
        parameters: JSON-schema-like untuk argumen tool.
    """

    name: str
    description: str = ""
    parameters: Dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    @classmethod
    def from_spec(cls, spec: Dict[str, Any]) -> "ToolDefinition":
        """Bangun ToolDefinition dari spec tool (mis. BaseTool.to_spec())."""
        return cls(
            name=spec.get("name", ""),
            description=spec.get("description", ""),
            parameters=spec.get("input_schema")
            or spec.get("parameters")
            or {"type": "object", "properties": {}},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass
class ToolChoice:
    """Preferensi pemilihan tool (provider-agnostic).

    Attributes:
        mode: "auto" | "none" | "required" | "specific".
        name: nama tool bila mode == "specific".
    """

    mode: str = "auto"
    name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode, "name": self.name}


@dataclass
class GenerateResult:
    """Hasil standar dari sebuah pemanggilan generate()."""

    text: str
    model: str = ""
    provider: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


class BaseProvider(ABC):
    """Abstract base class untuk semua provider AI.

    Kontrak utama:
        - name: nama unik provider (mis. "ollama", "deepseek", "openai").
        - generate(...): menerima prompt atau daftar pesan, mengembalikan GenerateResult.
        - is_available(): cek apakah provider siap dipakai (mis. API key ada / server hidup).
    """

    #: Nama unik provider. Wajib di-override oleh subclass.
    name: str = "base"

    @abstractmethod
    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Message]] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[List[ToolDefinition]] = None,
        tool_choice: Optional[ToolChoice] = None,
    ) -> GenerateResult:
        """Hasilkan teks dari model.

        Args:
            prompt: prompt tunggal (string). Alternatif dari `messages`.
            messages: daftar Message untuk mode chat.
            options: opsi generasi (temperature, max_tokens, model, dll).
            tools: definisi tool (provider-agnostic) untuk native tool calling.
            tool_choice: preferensi pemilihan tool (opsional).

        Returns:
            GenerateResult berisi teks hasil dan metadata.

        Catatan:
            Minimal salah satu dari `prompt` atau `messages` harus diisi.
        """
        raise NotImplementedError

    def is_available(self) -> bool:
        """Cek apakah provider siap dipakai. Default: True.

        Provider cloud dapat meng-override untuk mengecek keberadaan API key.
        """

        return True

    def normalize_response(self, result: "GenerateResult") -> "LLMResponse":
        """Ubah GenerateResult menjadi LLMResponse (provider-agnostic).

        Implementasi default: text-only, tanpa tool call. Provider yang
        mendukung tool call dapat meng-override method ini.
        Args:
            result: hasil dari generate().
        Returns:
            LLMResponse ternormalisasi.
        """
        # Import lokal untuk menghindari import cycle (core <-> providers).
        from agent_ai.core.response import FinishReason, LLMResponse

        return LLMResponse(
            text=result.text or "",
            actions=[],
            finish_reason=FinishReason.STOP,
            raw=result.raw,
            provider=result.provider or self.name,
            model=result.model,
        )

    @staticmethod
    def _build_messages(
        prompt: Optional[str],
        messages: Optional[List[Message]],
    ) -> List[Dict[str, str]]:
        """Normalisasi input menjadi daftar pesan format chat.

        Args:
            prompt: prompt tunggal (string).
            messages: daftar Message.

        Returns:
            Daftar dict {"role", "content"}.

        Raises:
            ValueError: bila keduanya kosong.
        """
        if messages:
            return [m.to_dict() for m in messages]
        if prompt is not None:
            return [{"role": "user", "content": prompt}]
        raise ValueError("Salah satu dari 'prompt' atau 'messages' harus diisi.")

    @staticmethod
    def _build_tool_definitions(
        tools: Optional[List[ToolDefinition]],
    ) -> List[ToolDefinition]:
        """Normalisasi input tools menjadi daftar ToolDefinition.

        Menerima ToolDefinition atau spec dict (mis. BaseTool.to_spec()).
        """
        if not tools:
            return []
        normalized: List[ToolDefinition] = []
        for tool in tools:
            if isinstance(tool, ToolDefinition):
                normalized.append(tool)
            elif isinstance(tool, dict):
                normalized.append(ToolDefinition.from_spec(tool))
            else:
                raise ValueError(
                    "Tool harus berupa ToolDefinition atau spec dict."
                )
        return normalized

    def __repr__(self) -> str:  # pragma: no cover - bantuan debug
        return f"<{self.__class__.__name__} name={self.name!r}>"

