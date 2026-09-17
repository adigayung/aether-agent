"""Layer MCP (Model Context Protocol) untuk AETHER.

MCP adalah adapter/extension untuk tool eksternal. AETHER tetap bisa
berjalan normal tanpa MCP: modul ini tidak diimpor secara wajib oleh
Agent Core, dan tidak ada dependency MCP yang membuat core gagal.

Struktur:
    - models    : MCPServerConfig, MCPTool, MCPToolResult
    - client    : MCPClient (interface) + InMemoryMCPClient (dummy)
    - transport : StdioMCPClient (transport nyata: stdio subprocess + JSON-RPC)
    - server    : MCPServerManager (register/load/lookup/lifecycle)
    - adapter   : MCPToolAdapter (MCP tool -> BaseTool AETHER)
"""

from agent_ai.mcp.adapter import MCPToolAdapter, adapt_tools
from agent_ai.mcp.client import (
    InMemoryMCPClient,
    MCPClient,
    MCPConnectionError,
    MCPError,
)
from agent_ai.mcp.models import MCPServerConfig, MCPTool, MCPToolResult
from agent_ai.mcp.server import MCPServerManager
from agent_ai.mcp.transport import (
    MCPProtocolError,
    MCPTimeoutError,
    StdioMCPClient,
    stdio_client_factory,
)

__all__ = [
    "MCPServerConfig",
    "MCPTool",
    "MCPToolResult",
    "MCPClient",
    "InMemoryMCPClient",
    "MCPError",
    "MCPConnectionError",
    "MCPServerManager",
    "MCPToolAdapter",
    "adapt_tools",
    "StdioMCPClient",
    "stdio_client_factory",
    "MCPTimeoutError",
    "MCPProtocolError",
]

