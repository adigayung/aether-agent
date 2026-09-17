"""Interface/ABC browser/web tool (provider-agnostic).

Mengikuti pola tool yang sudah ada (BaseTool): name, description, input_schema,
execute(**arguments). Browser tool adalah unit kemampuan web yang bisa dipanggil
Agent Core melalui interface yang konsisten.

Prinsip:
    - Provider/model agnostic.
    - Tidak menjalankan JavaScript, tanpa browser otomasi, tanpa GUI.
    - Tidak ada crawler / search engine / OCR.
    - Error terstruktur (BrowserError) agar mudah dipakai Reliability/Recovery.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict

from agent_ai.browser.models import BrowserError


class BaseBrowserTool(ABC):
    """Abstract base class untuk semua browser/web tool.

    Kontrak utama:
        - name: nama unik tool (wajib di-override).
        - description: deskripsi singkat.
        - input_schema: definisi input (JSON-schema-like).
        - execute(**arguments): logika tool.
    """

    #: Nama unik tool. Wajib di-override oleh subclass.
    name: str = "base"

    #: Deskripsi singkat tool.
    description: str = ""

    #: Definisi input (JSON-schema-like).
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}}

    @abstractmethod
    def execute(self, **arguments: Any) -> Any:
        """Jalankan tool dengan argumen yang diberikan.

        Args:
            **arguments: argumen sesuai `input_schema`.

        Returns:
            Hasil eksekusi tool (bebas, sesuai tool).

        Raises:
            BrowserError: bila eksekusi gagal (error terstruktur).
        """
        raise NotImplementedError

    def validate(self, arguments: Dict[str, Any]) -> None:
        """Validasi argumen terhadap `input_schema` (validasi minimal).

        Hanya memeriksa field `required`. Override bila butuh validasi lebih ketat.

        Raises:
            BrowserError: bila field wajib tidak ada.
        """
        required = self.input_schema.get("required", []) if self.input_schema else []
        missing = [key for key in required if key not in arguments]
        if missing:
            from agent_ai.browser.models import BrowserStatus

            raise BrowserError(
                BrowserStatus.INVALID_URL,
                f"Tool '{self.name}' kekurangan argumen wajib: {', '.join(missing)}",
            )

    def to_spec(self) -> Dict[str, Any]:
        """Representasi tool untuk dokumentasi/LLM (name, description, schema)."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def __repr__(self) -> str:  # pragma: no cover - bantuan debug
        return f"<{self.__class__.__name__} name={self.name!r}>"
