"""Provider GENERIK untuk endpoint berformat OpenAI-compatible.

Ini adalah SATU implementasi generik yang dipakai untuk endpoint apa pun yang
kompatibel dengan skema OpenAI (`POST {base_url}/chat/completions`), mis.:

    - Gerry   -> http://gerry.com/v1
    - Bariska -> https://bariska.example/v1
    - 9Router -> http://127.0.0.1:20128/v1
    - server lokal (LM Studio/vLLM/Ollama OpenAI-compat/...)

TIDAK ada kelas per-layanan (`GerryProvider`, `BariskaProvider`, ...). Cukup
konfigurasi Provider Instance (name/Base URL/api_key_env/model), sehingga
Settings AETHER berperan sebagai konfigurator endpoint LLM, bukan daftar
provider hardcode.

Perbedaan dari `OpenAICompatibleProvider` (dipakai OpenAI/DeepSeek/OpenRouter):
    - Base URL wajib diisi user (tidak ada default OpenAI).
    - API key OPSIONAL: sebagian endpoint lokal tidak butuh Bearer token. Bila
      kosong, header Authorization tidak dikirim.
    - Model FLEKSIBEL: nilai bisa model konkret, alias routing ("auto"), atau
      kosong. Provider tetap mengirim field `model` selama ada nilainya (lihat
      `send_model_field`).
    - Model DISCOVERY: bila instance TIDAK punya model eksplisit, AETHER
      melakukan `GET {base_url}/models` (OpenAI-compatible) dan memakai model
      ID pertama yang valid untuk `POST /chat/completions`. Ini meniru klien
      OpenAI-compatible pada umumnya dan memperbaiki endpoint yang menolak
      request tanpa model (mis. `403 no access to model`). Bila endpoint tidak
      menyediakan `/models`, perilaku lama (field `model` di-omit -> server
      menentukan) tetap berlaku. TIDAK ada hardcode nama layanan/model.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent_ai.config.settings import OpenAIConfig, settings
from agent_ai.providers.base import ProviderNotConfiguredError
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider


class CustomOpenAIProvider(OpenAICompatibleProvider):
    """Provider generik OpenAI-compatible (endpoint yang dikonfigurasi user)."""

    name = "custom"

    #: User TIDAK dipaksa memilih model konkret; nilai `model` (mis. "auto")
    #: tetap dikirim bila ada.
    requires_model = False

    #: Endpoint OpenAI-compatible generik: bila instance tidak punya model
    #: eksplisit, AETHER menemukan model ID valid lewat `GET {base_url}/models`
    #: (lihat `_discover_first_model`). Factory juga menyetel ini dari katalog
    #: provider type (`supports_model_discovery`).
    supports_model_discovery = True

    def __init__(
        self,
        config: Optional[OpenAIConfig] = None,
        retry_policy: Optional[Any] = None,
    ) -> None:
        self.config = config or settings.openai
        self.retry_policy = retry_policy

    def _require_config(self) -> None:
        """Hanya Base URL yang wajib; API key OPSIONAL untuk endpoint lokal.

        Raises:
            ProviderNotConfiguredError: bila base URL kosong (pesan jelas, tanpa
                fallback diam-diam ke base URL provider lain).
        """
        if not self.config.base_url:
            raise ProviderNotConfiguredError(
                f"Provider '{self.name}' belum dikonfigurasi: Base URL kosong. "
                f"Isi Base URL endpoint (mis. http://host:port/v1) di Settings."
            )

    def _build_headers(self) -> Dict[str, str]:
        """Header HTTP; Authorization HANYA bila API key tersedia."""
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(self._extra_headers())
        return headers

    def is_available(self) -> bool:
        """Tersedia bila Base URL sudah diisi (API key opsional)."""
        return bool(self.config.base_url)
