"""Interface/client dasar MCP.

Modul ini mendefinisikan KONTRAK (abstract interface) untuk MCP client,
tanpa mengikat Agent Core ke implementasi transport tertentu.

Operasi inti:
    - connect()
    - list_tools()
    - call_tool(name, arguments)
    - close()

Implementasi transport nyata (stdio/http/sse) bisa ditambahkan kemudian
dengan meng-subclass `MCPClient`. Foundation ini menyediakan
`InMemoryMCPClient` sebagai implementasi dummy untuk verifikasi/testing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from agent_ai.mcp.models import MCPTool, MCPToolResult


class MCPError(Exception):
    """Base exception untuk error MCP."""


class MCPConnectionError(MCPError):
    """Gagal connect/berkomunikasi dengan MCP server."""


class MCPClient(ABC):
    """Interface dasar MCP client.

    Subclass bertanggung jawab atas detail transport. Agent Core hanya
    berinteraksi lewat interface ini.
    """

    def __init__(self, server_name: str = "") -> None:
        self.server_name = server_name
        self._connected = False

    @property
    def connected(self) -> bool:
        """Apakah client sedang terhubung."""
        return self._connected

    @abstractmethod
    def connect(self) -> None:
        """Buka koneksi ke MCP server.

        Raises:
            MCPConnectionError: bila koneksi gagal.
        """
        raise NotImplementedError

    @abstractmethod
    def list_tools(self) -> List[MCPTool]:
        """Ambil daftar tool yang diekspos server."""
        raise NotImplementedError

    @abstractmethod
    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> MCPToolResult:
        """Panggil sebuah tool di server.

        Args:
            name: nama tool.
            arguments: argumen tool.

        Returns:
            MCPToolResult.
        """
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """Tutup koneksi ke server."""
        raise NotImplementedError

    def __enter__(self) -> "MCPClient":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class InMemoryMCPClient(MCPClient):
    """Implementasi MCP client dummy (in-memory) untuk verifikasi/testing.

    Tidak melakukan I/O apa pun. Tool dan hasilnya disediakan langsung
    lewat konstruktor. Berguna untuk menguji adapter & manager tanpa
    transport MCP nyata.
    """

    def __init__(
        self,
        server_name: str = "in-memory",
        tools: Optional[List[MCPTool]] = None,
        handlers: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(server_name=server_name)
        self._tools: List[MCPTool] = list(tools or [])
        # handlers: name -> callable(arguments) -> Any
        self._handlers: Dict[str, Any] = dict(handlers or {})

    def connect(self) -> None:
        self._connected = True

    def list_tools(self) -> List[MCPTool]:
        if not self._connected:
            raise MCPConnectionError(
                f"MCP client '{self.server_name}' belum terhubung."
            )
        return list(self._tools)

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> MCPToolResult:
        if not self._connected:
            raise MCPConnectionError(
                f"MCP client '{self.server_name}' belum terhubung."
            )
        args = arguments or {}
        handler = self._handlers.get(name)
        if handler is None:
            return MCPToolResult(
                tool=name,
                server=self.server_name,
                is_error=True,
                error=f"Tool '{name}' tidak tersedia di server '{self.server_name}'.",
            )
        try:
            content = handler(args)
        except Exception as exc:  # noqa: BLE001 - bungkus jadi hasil error
            return MCPToolResult(
                tool=name,
                server=self.server_name,
                is_error=True,
                error=f"{type(exc).__name__}: {exc}",
            )
        return MCPToolResult(tool=name, server=self.server_name, content=content)

    def close(self) -> None:
        self._connected = False
