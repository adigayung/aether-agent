"""Agent Core minimal.

Agent menerima task/prompt dari caller, memanggil provider AI melalui
abstraction (BaseProvider) yang diambil dari registry, lalu mengembalikan
response terstruktur.

Prinsip:
    - Agent TIDAK bergantung pada provider konkret (Ollama/DeepSeek/OpenAI).
    - Provider dipilih lewat nama (default dari settings.default_provider).
    - Error provider diteruskan apa adanya (tidak disembunyikan).

Belum ada: tool calling, planner, memory, Git, command execution,
file editing, database, atau model routing otomatis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent_ai.config.settings import settings
from agent_ai.providers.base import (
    BaseProvider,
    GenerateOptions,
    GenerateResult,
    Message,
)
from agent_ai.providers.registry import get_provider


@dataclass
class AgentResponse:
    """Response terstruktur dari Agent."""

    text: str
    provider: str = ""
    model: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_result(cls, result: GenerateResult) -> "AgentResponse":
        """Bangun AgentResponse dari GenerateResult provider."""
        return cls(
            text=result.text,
            provider=result.provider,
            model=result.model,
            raw=result.raw,
        )


class Agent:
    """Agent Core minimal yang memanggil provider AI via abstraction.

    Args:
        provider: instance BaseProvider (opsional). Bila None, provider
            diambil dari registry berdasarkan `provider_name` atau
            `settings.default_provider`.
        provider_name: nama provider di registry (mis. "ollama", "deepseek",
            "openai"). Bila None, gunakan settings.default_provider.
        options: GenerateOptions default untuk setiap pemanggilan.
    """

    def __init__(
        self,
        provider: Optional[BaseProvider] = None,
        provider_name: Optional[str] = None,
        options: Optional[GenerateOptions] = None,
    ) -> None:
        self._provider = provider
        self._provider_name = provider_name
        self._options = options

    # ------------------------------------------------------------------ #
    # Provider resolution
    # ------------------------------------------------------------------ #
    @property
    def provider(self) -> BaseProvider:
        """Provider aktif. Diambil dari registry bila belum di-set."""
        if self._provider is None:
            self._provider = get_provider(self._provider_name)
        return self._provider

    @property
    def provider_name(self) -> str:
        """Nama provider aktif."""
        return self.provider.name

    # ------------------------------------------------------------------ #
    # API utama
    # ------------------------------------------------------------------ #
    def run(
        self,
        task: Optional[str] = None,
        messages: Optional[List[Message]] = None,
        options: Optional[GenerateOptions] = None,
    ) -> AgentResponse:
        """Jalankan task/prompt dan kembalikan response terstruktur.

        Args:
            task: prompt tunggal (string).
            messages: daftar Message untuk mode chat.
            options: opsi generasi (menimpa options default Agent).

        Returns:
            AgentResponse.

        Raises:
            ValueError: bila task dan messages keduanya kosong.
            ProviderError: error dari provider diteruskan apa adanya.
        """
        if task is None and not messages:
            raise ValueError("Salah satu dari 'task' atau 'messages' harus diisi.")

        effective_options = options or self._options
        result = self.provider.generate(
            prompt=task,
            messages=messages,
            options=effective_options,
        )
        return AgentResponse.from_result(result)

    def is_available(self) -> bool:
        """Cek apakah provider aktif siap dipakai."""
        return self.provider.is_available()

    def __repr__(self) -> str:  # pragma: no cover - bantuan debug
        return f"<Agent provider={self._provider_name or settings.default_provider!r}>"
