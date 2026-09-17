"""Verifikasi MCP Foundation.

Menguji:
    1. MCP models dapat dibuat.
    2. MCPServerManager: register/get/list.
    3. MCP client interface dapat digunakan (InMemoryMCPClient).
    4. MCP tool dapat diadaptasi menjadi BaseTool.
    5. Adapter menghasilkan tool spec yang valid.
    6. Adapter dapat didaftarkan ke ToolRegistry.
    7. Native tools tetap berjalan.
    8. AETHER tetap import/run tanpa MCP server.
    9. Tidak ada dependency MCP wajib yang membuat core gagal.

Tidak menyentuh filesystem dan tidak menjalankan command apa pun.

Jalankan:
    python scripts/check_mcp.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.mcp import (  # noqa: E402
    InMemoryMCPClient,
    MCPClient,
    MCPServerConfig,
    MCPServerManager,
    MCPTool,
    MCPToolAdapter,
    MCPToolResult,
    adapt_tools,
)
from agent_ai.tools import BaseTool, ToolRegistry  # noqa: E402


def _make_demo_client() -> InMemoryMCPClient:
    tools = [
        MCPTool(
            name="echo",
            description="Mengembalikan teks yang diberikan.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            server="demo",
        ),
        MCPTool(
            name="add",
            description="Menjumlahkan dua angka.",
            input_schema={
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"],
            },
            server="demo",
        ),
    ]
    handlers = {
        "echo": lambda args: args.get("text", ""),
        "add": lambda args: args["a"] + args["b"],
    }
    return InMemoryMCPClient(server_name="demo", tools=tools, handlers=handlers)


def main() -> int:
    print("=== Verifikasi MCP Foundation ===")

    # 1) Models.
    config = MCPServerConfig(name="demo", transport="stdio", command="demo-server")
    tool = MCPTool(name="echo", description="echo", server="demo")
    result = MCPToolResult(tool="echo", server="demo", content="hi")
    assert config.to_dict()["name"] == "demo"
    assert tool.to_dict()["name"] == "echo"
    assert result.to_dict()["content"] == "hi"
    print(f"[1] Models OK -> {config.name}, {tool.name}, {result.tool}")

    # 2) Server manager: register/get/list.
    manager = MCPServerManager()
    manager.register(config, factory=lambda cfg: _make_demo_client())
    manager.load([MCPServerConfig(name="other")])
    assert manager.has("demo") and manager.has("other")
    assert manager.get("demo").name == "demo"
    assert manager.list() == ["demo", "other"]
    print(f"[2] ServerManager OK -> list={manager.list()}")

    # 3) Client interface.
    assert issubclass(InMemoryMCPClient, MCPClient)
    client = manager.connect("demo")
    assert client.connected
    tools = client.list_tools()
    assert len(tools) == 2
    call = client.call_tool("add", {"a": 2, "b": 3})
    assert call.content == 5 and not call.is_error
    print(f"[3] Client OK -> tools={[t.name for t in tools]}, add(2,3)={call.content}")

    # 4) Adapter -> BaseTool.
    adapter = MCPToolAdapter(client, tools[0])
    assert isinstance(adapter, BaseTool)
    assert adapter.name == "demo__echo"
    assert adapter.execute(text="halo") == "halo"
    print(f"[4] Adapter OK -> {adapter.name} execute -> {adapter.execute(text='halo')}")

    # 5) Spec valid.
    spec = adapter.to_spec()
    assert spec["name"] == "demo__echo"
    assert spec["source"] == "mcp"
    assert spec["server"] == "demo"
    assert "input_schema" in spec
    print(f"[5] Spec OK -> {spec}")

    # 6) Register ke ToolRegistry.
    registry = ToolRegistry()
    for adapted in adapt_tools(client):
        registry.register(adapted)
    assert registry.has("demo__echo") and registry.has("demo__add")
    assert registry.execute("demo__add", {"a": 10, "b": 5}) == 15
    print(f"[6] Registry OK -> {registry.list()}")

    # 7) Native tools tetap berjalan.
    class NativeEcho(BaseTool):
        name = "native_echo"
        description = "native"
        input_schema = {"type": "object", "properties": {"text": {"type": "string"}}}

        def execute(self, **arguments):
            return arguments.get("text", "")

    registry.register(NativeEcho())
    assert registry.execute("native_echo", {"text": "ok"}) == "ok"
    assert registry.has("native_echo") and registry.has("demo__echo")
    print(f"[7] Native OK -> {registry.list()}")

    # 8) AETHER tetap import/run tanpa MCP server.
    from agent_ai.tools import registry as global_registry  # noqa: E402
    assert global_registry.list(), "native registry harus tetap terisi"
    print(f"[8] Core tanpa MCP OK -> native registry={global_registry.list()}")

    # 9) Tidak ada dependency MCP wajib.
    #    Manager tanpa factory tetap aman: hanya konfigurasi.
    try:
        manager.connect("other")
        print("[ERROR] seharusnya RuntimeError (tanpa factory)")
        return 1
    except RuntimeError as exc:
        print(f"[9] Tanpa factory OK -> {exc}")

    # Lifecycle: close.
    manager.close("demo")
    assert not client.connected
    manager.close_all()
    print("[10] Lifecycle OK -> close/close_all")

    print()
    print("[OK] MCP Foundation (models, manager, client, adapter, registry) bekerja.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
