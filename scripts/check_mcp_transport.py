"""Verifikasi Real MCP Transport (stdio subprocess + JSON-RPC).

Deterministik. Membuat MCP server dummy (Python) di
J:\\Agent_Ai\\dummy_test\\mcp_fixture dan menghapusnya setelah test.

Menguji:
    1. connect + initialize/handshake
    2. capability/tool discovery (tools/list)
    3. request/response handling (tools/call)
    4. error handling (tool error & JSON-RPC error)
    5. timeout
    6. lifecycle/close yang aman
    7. adapter ke ToolRegistry (namespace server__tool)
    8. tidak ada tool execution engine kedua
    9. MCPServerManager + factory

Jalankan:
    python scripts/check_mcp_transport.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.mcp import (  # noqa: E402
    MCPServerConfig,
    MCPServerManager,
    MCPTimeoutError,
    StdioMCPClient,
    adapt_tools,
    stdio_client_factory,
)
from agent_ai.tools.base import ToolExecutionError  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "mcp_fixture"
SERVER_SCRIPT = FIXTURE / "dummy_mcp_server.py"

#: MCP server dummy: JSON-RPC line-delimited via stdio.
SERVER_CODE = r'''
import json
import sys
import time

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "dummy", "version": "1.0"},
            }})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": "echo", "description": "Echo input",
                 "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
                {"name": "boom", "description": "Selalu error",
                 "inputSchema": {"type": "object", "properties": {}}},
                {"name": "slow", "description": "Lambat",
                 "inputSchema": {"type": "object", "properties": {}}},
            ]}})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "echo":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": args.get("text", "")}],
                    "isError": False,
                }})
            elif name == "boom":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "boom"}],
                    "isError": True,
                }})
            elif name == "slow":
                time.sleep(5)
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [], "isError": False}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown tool"}})
        else:
            if mid is not None:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method"}})

if __name__ == "__main__":
    main()
'''


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    SERVER_SCRIPT.write_text(SERVER_CODE, encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def _config() -> MCPServerConfig:
    return MCPServerConfig(
        name="dummy",
        transport="stdio",
        command=sys.executable,
        args=[str(SERVER_SCRIPT)],
    )


def main() -> int:
    print("=== Verifikasi Real MCP Transport (stdio + JSON-RPC) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    # 1) connect + initialize/handshake.
    client = StdioMCPClient(_config())
    client.connect()
    assert client.connected is True
    assert client.server_info.get("name") == "dummy", client.server_info
    assert "tools" in client.capabilities, client.capabilities
    print("[1] connect + initialize/handshake OK")

    # 2) capability/tool discovery.
    tools = client.list_tools()
    names = {t.name for t in tools}
    assert names == {"echo", "boom", "slow"}, names
    echo = next(t for t in tools if t.name == "echo")
    assert echo.server == "dummy"
    assert echo.input_schema.get("properties", {}).get("text")
    print("[2] capability/tool discovery OK")

    # 3) request/response handling.
    result = client.call_tool("echo", {"text": "halo"})
    assert result.is_error is False
    assert result.content == [{"type": "text", "text": "halo"}], result.content
    print("[3] request/response handling OK")

    # 4) error handling.
    #    Tool error (isError=True) -> MCPToolResult.is_error.
    boom = client.call_tool("boom", {})
    assert boom.is_error is True
    #    JSON-RPC error (unknown tool) -> dibungkus jadi is_error.
    unknown = client.call_tool("tidak_ada", {})
    assert unknown.is_error is True and unknown.error
    print("[4] error handling OK")

    # 5) timeout.
    #    Timeout ditegakkan di level request (call_tool membungkusnya jadi
    #    MCPToolResult.is_error, sesuai kontrak MCPClient).
    slow_client = StdioMCPClient(_config(), timeout=1.0)
    slow_client.connect()
    try:
        slow_client._request("tools/call", {"name": "slow", "arguments": {}})
        raise AssertionError("harus timeout")
    except MCPTimeoutError:
        pass
    # call_tool membungkus timeout jadi hasil error (tidak crash).
    slow_result = slow_client.call_tool("slow", {})
    assert slow_result.is_error is True
    slow_client.close()
    print("[5] timeout OK")

    # 6) lifecycle/close yang aman.
    client.close()
    assert client.connected is False
    client.close()  # idempotent, tidak error
    print("[6] lifecycle/close OK")

    # 7) adapter ke ToolRegistry (namespace server__tool).
    client2 = StdioMCPClient(_config())
    client2.connect()
    registry = ToolRegistry()
    for tool in adapt_tools(client2):
        registry.register(tool)
    assert registry.has("dummy__echo"), registry.list()
    out = registry.execute("dummy__echo", {"text": "via-registry"})
    assert out == [{"type": "text", "text": "via-registry"}], out
    # Tool error dari MCP -> ToolExecutionError (bukan crash).
    try:
        registry.execute("dummy__boom", {})
        raise AssertionError("harus ToolExecutionError")
    except ToolExecutionError:
        pass
    client2.close()
    print("[7] adapter ke ToolRegistry (namespace) OK")

    # 8) tidak ada tool execution engine kedua.
    #    Adapter hanya membungkus MCPClient.call_tool; eksekusi tetap di registry.
    from agent_ai.mcp.adapter import MCPToolAdapter
    assert issubclass(MCPToolAdapter, __import__("agent_ai.tools.base", fromlist=["BaseTool"]).BaseTool)
    mcp_dir = SRC_DIR / "agent_ai" / "mcp"
    for p in mcp_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "class ToolRegistry" not in text, f"{p.name} tidak boleh mendefinisikan ToolRegistry kedua"
    print("[8] tidak ada tool execution engine kedua OK")

    # 9) MCPServerManager + factory.
    manager = MCPServerManager()
    manager.register(_config(), factory=stdio_client_factory)
    assert manager.has("dummy")
    managed = manager.connect("dummy")
    assert managed.connected is True
    assert {t.name for t in managed.list_tools()} == {"echo", "boom", "slow"}
    manager.close("dummy")
    assert manager.client("dummy") is None
    manager.close_all()
    print("[9] MCPServerManager + factory OK")

    print()
    print("[OK] Real MCP Transport bekerja (stdio, JSON-RPC, discovery, adapter, lifecycle).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
