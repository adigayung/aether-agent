"""MCP Server Manager.

Mengelola beberapa MCP server: register/load, lookup berdasarkan nama,
dan lifecycle sederhana (connect/close).

Manager TIDAK mengikat Agent Core ke implementasi transport tertentu.
Ia hanya menyimpan `MCPServerConfig` + factory pembuat `MCPClient`.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from agent_ai.mcp.client import MCPClient
from agent_ai.mcp.models import MCPServerConfig

# Factory: menerima config, mengembalikan MCPClient.
MCPClientFactory = Callable[[MCPServerConfig], MCPClient]


class MCPServerManager:
    """Registry + lifecycle untuk MCP server.

    Contoh:
        manager = MCPServerManager()
        manager.register(MCPServerConfig(name="demo"), factory=my_factory)
        client = manager.connect("demo")
        tools = client.list_tools()
        manager.close("demo")
    """

    def __init__(self) -> None:
        self._configs: Dict[str, MCPServerConfig] = {}
        self._factories: Dict[str, MCPClientFactory] = {}
        self._clients: Dict[str, MCPClient] = {}

    # -- registrasi ---------------------------------------------------------
    def register(
        self,
        config: MCPServerConfig,
        factory: Optional[MCPClientFactory] = None,
    ) -> None:
        """Daftarkan sebuah MCP server.

        Args:
            config: konfigurasi server.
            factory: opsional, callable(config) -> MCPClient. Bila None,
                server hanya terdaftar sebagai konfigurasi (belum bisa connect).
        """
        if not config or not config.name:
            raise ValueError("MCPServerConfig harus punya 'name'.")
        key = config.name.lower()
        self._configs[key] = config
        if factory is not None:
            self._factories[key] = factory

    def load(self, configs: List[MCPServerConfig]) -> None:
        """Daftarkan banyak server sekaligus (tanpa factory)."""
        for config in configs:
            self.register(config)

    # -- lookup -------------------------------------------------------------
    def get(self, name: str) -> MCPServerConfig:
        """Ambil konfigurasi server berdasarkan nama.

        Raises:
            KeyError: bila server belum terdaftar.
        """
        key = (name or "").lower()
        if key not in self._configs:
            available = ", ".join(sorted(self._configs)) or "(kosong)"
            raise KeyError(f"MCP server '{name}' tidak terdaftar. Tersedia: {available}")
        return self._configs[key]

    def has(self, name: str) -> bool:
        return (name or "").lower() in self._configs

    def list(self) -> List[str]:
        """Daftar nama server yang terdaftar."""
        return sorted(self._configs)

    def configs(self) -> List[MCPServerConfig]:
        return [self._configs[key] for key in sorted(self._configs)]

    # -- lifecycle ----------------------------------------------------------
    def connect(self, name: str) -> MCPClient:
        """Connect ke server dan kembalikan client-nya.

        Raises:
            KeyError: bila server tidak terdaftar.
            RuntimeError: bila server tidak punya factory.
        """
        key = (name or "").lower()
        config = self.get(name)
        if key in self._clients and self._clients[key].connected:
            return self._clients[key]
        factory = self._factories.get(key)
        if factory is None:
            raise RuntimeError(
                f"MCP server '{name}' tidak punya factory/client; "
                "hanya konfigurasi yang terdaftar."
            )
        client = factory(config)
        client.connect()
        self._clients[key] = client
        return client

    def client(self, name: str) -> Optional[MCPClient]:
        """Ambil client yang sudah ter-connect (bila ada)."""
        return self._clients.get((name or "").lower())

    def close(self, name: str) -> None:
        """Tutup koneksi server tertentu (bila ada)."""
        key = (name or "").lower()
        client = self._clients.pop(key, None)
        if client is not None:
            client.close()

    def close_all(self) -> None:
        """Tutup semua koneksi."""
        for key in list(self._clients):
            self.close(key)
