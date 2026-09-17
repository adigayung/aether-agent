"""Model data untuk layer MCP (Model Context Protocol).

Model di sini sengaja sederhana (dataclass) dan TIDAK bergantung pada
implementasi transport MCP apa pun. Tujuannya agar Agent Core tetap bisa
berjalan normal walau MCP tidak tersedia.

Isi:
    - MCPServerConfig : konfigurasi sebuah MCP server.
    - MCPTool         : deskripsi tool yang diekspos oleh MCP server.
    - MCPToolResult   : hasil eksekusi sebuah MCP tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MCPServerConfig:
    """Konfigurasi untuk sebuah MCP server.

    Attributes:
        name: nama unik server (dipakai untuk lookup di manager).
        transport: jenis transport ("stdio", "sse", "http", ...). Bebas string,
            karena implementasi transport nyata belum ada di foundation ini.
        command: command untuk menjalankan server (mis. untuk transport stdio).
        args: argumen tambahan untuk command.
        url: URL server (untuk transport http/sse).
        env: environment variable tambahan.
        enabled: apakah server aktif.
        metadata: data bebas tambahan.
    """

    name: str
    transport: str = "stdio"
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    url: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "url": self.url,
            "env": dict(self.env),
            "enabled": self.enabled,
            "metadata": dict(self.metadata),
        }


@dataclass
class MCPTool:
    """Deskripsi sebuah tool yang diekspos oleh MCP server.

    Attributes:
        name: nama tool (lokal di server).
        description: deskripsi singkat.
        input_schema: JSON-schema-like untuk argumen tool.
        server: nama server asal tool (diisi oleh adapter/manager).
    """

    name: str
    description: str = ""
    input_schema: Dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    server: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "server": self.server,
        }


@dataclass
class MCPToolResult:
    """Hasil eksekusi sebuah MCP tool.

    Attributes:
        tool: nama tool yang dieksekusi.
        server: nama server asal.
        content: payload hasil (bebas, biasanya list/dict/str).
        is_error: menandai apakah eksekusi gagal.
        error: pesan error bila ada.
    """

    tool: str
    server: Optional[str] = None
    content: Any = None
    is_error: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "server": self.server,
            "content": self.content,
            "is_error": self.is_error,
            "error": self.error,
        }
