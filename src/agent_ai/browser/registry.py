"""Registry browser/web tool.

Mendaftarkan browser tool berdasarkan nama, mengambilnya kembali, dan
mengeksekusinya melalui interface yang konsisten. Mengikuti pola ToolRegistry
yang sudah ada (tidak membuat duplicate tool executor).

    from agent_ai.browser import BrowserToolRegistry, HttpFetchTool

    registry = BrowserToolRegistry()
    registry.register(HttpFetchTool())
    result = registry.execute("http_fetch", {"url": "https://example.com"})
"""

from __future__ import annotations

from typing import Any, Dict, List

from agent_ai.browser.base import BaseBrowserTool
from agent_ai.browser.models import BrowserError, BrowserStatus


class BrowserToolRegistry:
    """Kumpulan browser tool yang terdaftar, diakses lewat nama unik."""

    def __init__(self) -> None:
        self._tools: Dict[str, BaseBrowserTool] = {}

    def register(self, tool: BaseBrowserTool) -> None:
        """Daftarkan sebuah browser tool berdasarkan atribut `name`.

        Raises:
            ValueError: bila nama tool kosong atau masih "base".
        """
        name = getattr(tool, "name", None)
        if not name or name == "base":
            raise ValueError("Browser tool harus punya atribut 'name' yang unik dan bukan 'base'.")
        self._tools[name.lower()] = tool

    def get(self, name: str) -> BaseBrowserTool:
        """Ambil browser tool berdasarkan nama.

        Raises:
            BrowserError: bila nama tool belum terdaftar.
        """
        key = (name or "").lower()
        if key not in self._tools:
            available = ", ".join(sorted(self._tools)) or "(kosong)"
            raise BrowserError(
                BrowserStatus.UNKNOWN_ERROR,
                f"Browser tool '{name}' tidak terdaftar. Tersedia: {available}",
            )
        return self._tools[key]

    def has(self, name: str) -> bool:
        """Cek apakah browser tool terdaftar."""
        return (name or "").lower() in self._tools

    def list(self) -> List[str]:
        """Daftar nama browser tool yang terdaftar."""
        return sorted(self._tools)

    def specs(self) -> List[Dict[str, Any]]:
        """Daftar spesifikasi semua browser tool (untuk dokumentasi/LLM)."""
        return [tool.to_spec() for tool in self._tools.values()]

    def execute(self, name: str, arguments: Dict[str, Any] | None = None) -> Any:
        """Eksekusi browser tool berdasarkan nama.

        Args:
            name: nama tool.
            arguments: argumen untuk tool (dict).

        Returns:
            Hasil eksekusi tool.

        Raises:
            BrowserError: bila tool tidak terdaftar, argumen tidak valid, atau
                eksekusi gagal (error terstruktur).
        """
        tool = self.get(name)
        args = arguments or {}

        tool.validate(args)

        try:
            return tool.execute(**args)
        except BrowserError:
            # Error terstruktur: teruskan apa adanya.
            raise
        except Exception as exc:  # noqa: BLE001 - bungkus error asli, jangan ditelan
            raise BrowserError(
                BrowserStatus.UNKNOWN_ERROR,
                f"Browser tool '{name}' gagal dieksekusi: {type(exc).__name__}: {exc}",
            ) from exc


# ---------------------------------------------------------------------------
# Registry global + pendaftaran browser tool bawaan.
# ---------------------------------------------------------------------------
registry = BrowserToolRegistry()

from agent_ai.browser.fetch import HttpFetchTool  # noqa: E402

registry.register(HttpFetchTool())
