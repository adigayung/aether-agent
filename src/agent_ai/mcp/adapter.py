"""Adapter MCP tool -> BaseTool AETHER.

Tujuan: membuat MCP tool bisa masuk ke `ToolRegistry` AETHER melalui
interface `BaseTool`, sehingga Agent Core tidak perlu tahu apakah sebuah
tool itu native atau MCP.

    client = manager.connect("demo")
    tool = MCPToolAdapter(client, mcp_tool)
    registry.register(tool)          # sekarang bisa dipanggil seperti tool native
    registry.execute(tool.name, {...})
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent_ai.mcp.client import MCPClient
from agent_ai.mcp.models import MCPTool
from agent_ai.tools.base import BaseTool, ToolExecutionError


class MCPToolAdapter(BaseTool):
    """Membungkus sebuah MCP tool menjadi `BaseTool` AETHER.

    Nama tool di-namespace dengan nama server (mis. "demo__echo") agar tidak
    bentrok dengan tool native atau tool dari server lain.
    """

    def __init__(
        self,
        client: MCPClient,
        tool: MCPTool,
        namespace: bool = True,
    ) -> None:
        if client is None:
            raise ValueError("MCPToolAdapter butuh MCPClient.")
        if tool is None or not tool.name:
            raise ValueError("MCPToolAdapter butuh MCPTool dengan 'name'.")

        self._client = client
        self._mcp_tool = tool
        self._server = tool.server or client.server_name or "mcp"

        raw_name = tool.name
        self.name = f"{self._server}__{raw_name}" if namespace else raw_name
        self.description = tool.description or f"MCP tool '{raw_name}' dari '{self._server}'."
        self.input_schema = tool.input_schema or {"type": "object", "properties": {}}

    @property
    def mcp_tool(self) -> MCPTool:
        return self._mcp_tool

    @property
    def server(self) -> str:
        return self._server

    def execute(self, **arguments: Any) -> Any:
        """Panggil MCP tool lewat client dan kembalikan kontennya.

        Raises:
            ToolExecutionError: bila MCP mengembalikan error.
        """
        result = self._client.call_tool(self._mcp_tool.name, arguments)
        if result.is_error:
            raise ToolExecutionError(
                f"MCP tool '{self.name}' gagal: {result.error}"
            )
        return result.content

    def to_spec(self) -> Dict[str, Any]:
        """Spec tool untuk LLM/dokumentasi (kompatibel dengan BaseTool)."""
        spec = super().to_spec()
        spec["source"] = "mcp"
        spec["server"] = self._server
        spec["mcp_name"] = self._mcp_tool.name
        return spec


def adapt_tools(
    client: MCPClient,
    tools: Optional[list] = None,
    namespace: bool = True,
) -> list:
    """Adaptasi daftar MCP tool menjadi daftar `MCPToolAdapter`.

    Args:
        client: MCP client yang sudah ter-connect.
        tools: daftar MCPTool. Bila None, diambil dari client.list_tools().
        namespace: bungkus nama dengan nama server.

    Returns:
        List[MCPToolAdapter].
    """
    if tools is None:
        tools = client.list_tools()
    return [MCPToolAdapter(client, tool, namespace=namespace) for tool in tools]
