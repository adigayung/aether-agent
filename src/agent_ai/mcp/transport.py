"""Transport MCP nyata: stdio subprocess + JSON-RPC.

Mengimplementasikan `MCPClient` lewat subprocess stdio dan protokol JSON-RPC
2.0 (line-delimited). Ini transport minimal yang diperlukan AETHER.

Desain:
    - `StdioMCPClient` menjalankan server MCP sebagai subprocess, berkomunikasi
      lewat stdin/stdout dengan pesan JSON-RPC per baris.
    - Handshake `initialize` + notifikasi `initialized`.
    - Discovery tool lewat `tools/list`.
    - Eksekusi lewat `tools/call`.
    - Timeout per request, error handling, dan close yang aman.

Abstraction tetap: transport lain (HTTP/SSE) dapat ditambahkan sebagai subclass
`MCPClient` tanpa mengubah Agent Core.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from agent_ai.mcp.client import MCPClient, MCPConnectionError, MCPError
from agent_ai.mcp.models import MCPServerConfig, MCPTool, MCPToolResult

#: Versi protokol MCP yang didukung client ini.
_PROTOCOL_VERSION = "2024-11-05"


class MCPTimeoutError(MCPError):
    """Request MCP melewati batas waktu."""


class MCPProtocolError(MCPError):
    """Pesan JSON-RPC tidak valid / server mengembalikan error protokol."""


class StdioMCPClient(MCPClient):
    """MCP client via subprocess stdio + JSON-RPC 2.0.

    Args:
        config: MCPServerConfig (butuh `command`; `args`/`env` opsional).
        timeout: batas waktu tiap request (detik).
        client_name: nama client untuk handshake.
        client_version: versi client untuk handshake.
    """

    def __init__(
        self,
        config: MCPServerConfig,
        timeout: float = 30.0,
        client_name: str = "aether",
        client_version: str = "0.1",
    ) -> None:
        super().__init__(server_name=config.name)
        self.config = config
        self.timeout = timeout
        self.client_name = client_name
        self.client_version = client_version

        self._proc: Optional[subprocess.Popen] = None
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._server_info: Dict[str, Any] = {}
        self._capabilities: Dict[str, Any] = {}
        # Reader thread + queue agar timeout benar-benar ditegakkan.
        self._inbox: "queue.Queue[Any]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._reader_stop = threading.Event()

    # ------------------------------------------------------------------ #
    # JSON-RPC helpers
    # ------------------------------------------------------------------ #
    def _new_id(self) -> int:
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def _write_message(self, message: Dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPConnectionError(f"MCP server '{self.server_name}' belum terhubung.")
        data = json.dumps(message) + "\n"
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MCPConnectionError(
                f"Gagal menulis ke MCP server '{self.server_name}': {exc}"
            ) from exc

    def _start_reader(self) -> None:
        """Mulai thread pembaca stdout -> queue (agar timeout bisa ditegakkan)."""
        self._reader_stop.clear()
        self._inbox = queue.Queue()

        def _pump() -> None:
            proc = self._proc
            if proc is None or proc.stdout is None:
                return
            try:
                for line in proc.stdout:
                    if self._reader_stop.is_set():
                        break
                    self._inbox.put(line)
            except (ValueError, OSError):
                pass
            finally:
                self._inbox.put(None)  # EOF sentinel

        self._reader = threading.Thread(target=_pump, daemon=True)
        self._reader.start()

    def _read_message(self, timeout: float) -> Dict[str, Any]:
        """Ambil satu pesan JSON dari queue dengan batas waktu.

        Raises:
            MCPTimeoutError: bila tidak ada pesan dalam `timeout` detik.
            MCPConnectionError: bila koneksi ditutup (EOF).
            MCPProtocolError: bila pesan bukan JSON valid.
        """
        try:
            line = self._inbox.get(timeout=timeout)
        except queue.Empty as exc:
            raise MCPTimeoutError(
                f"Tidak ada response dari '{self.server_name}' dalam {timeout:.2f} detik."
            ) from exc
        if line is None:
            raise MCPConnectionError(
                f"MCP server '{self.server_name}' menutup koneksi (EOF)."
            )
        try:
            return json.loads(line)
        except ValueError as exc:
            raise MCPProtocolError(
                f"Pesan dari MCP server '{self.server_name}' bukan JSON valid: {line!r}"
            ) from exc

    def _request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Kirim request JSON-RPC dan tunggu response (dengan timeout).

        Raises:
            MCPTimeoutError: bila melewati batas waktu.
            MCPProtocolError: bila server mengembalikan error JSON-RPC.
            MCPConnectionError: bila koneksi bermasalah.
        """
        request_id = self._new_id()
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params

        with self._io_lock:
            self._write_message(message)
            deadline = time.time() + self.timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise MCPTimeoutError(
                        f"Request '{method}' ke '{self.server_name}' timeout "
                        f"setelah {self.timeout} detik."
                    )
                response = self._read_message(remaining)
                # Lewati notifikasi server (tanpa id) sampai dapat response kita.
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    err = response["error"]
                    raise MCPProtocolError(
                        f"MCP '{method}' error {err.get('code')}: {err.get('message')}"
                    )
                return response.get("result", {})

    def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        """Kirim notifikasi JSON-RPC (tanpa response)."""
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        with self._io_lock:
            self._write_message(message)

    # ------------------------------------------------------------------ #
    # Interface MCPClient
    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        """Jalankan server sebagai subprocess dan lakukan handshake."""
        if self._connected:
            return
        if not self.config.command:
            raise MCPConnectionError(
                f"MCP server '{self.server_name}' tidak punya 'command' untuk transport stdio."
            )
        try:
            self._proc = subprocess.Popen(
                [self.config.command, *self.config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env={**__import__("os").environ, **self.config.env} if self.config.env else None,
            )
        except FileNotFoundError as exc:
            raise MCPConnectionError(
                f"Command MCP '{self.config.command}' tidak ditemukan."
            ) from exc
        except OSError as exc:
            raise MCPConnectionError(
                f"Gagal menjalankan MCP server '{self.server_name}': {exc}"
            ) from exc

        # Mulai reader thread sebelum handshake.
        self._start_reader()

        # Handshake: initialize -> initialized.
        result = self._request("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": self.client_name, "version": self.client_version},
        })
        self._server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
        self._capabilities = result.get("capabilities", {}) if isinstance(result, dict) else {}
        self._notify("notifications/initialized")
        self._connected = True

    def list_tools(self) -> List[MCPTool]:
        """Discovery tool lewat `tools/list`."""
        if not self._connected:
            raise MCPConnectionError(f"MCP client '{self.server_name}' belum terhubung.")
        result = self._request("tools/list")
        raw_tools = result.get("tools", []) if isinstance(result, dict) else []
        tools: List[MCPTool] = []
        for item in raw_tools:
            tools.append(
                MCPTool(
                    name=item.get("name", ""),
                    description=item.get("description", ""),
                    input_schema=item.get("inputSchema", {"type": "object", "properties": {}}),
                    server=self.server_name,
                )
            )
        return tools

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> MCPToolResult:
        """Eksekusi tool lewat `tools/call`."""
        if not self._connected:
            raise MCPConnectionError(f"MCP client '{self.server_name}' belum terhubung.")
        try:
            result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        except MCPError as exc:
            return MCPToolResult(
                tool=name, server=self.server_name, is_error=True, error=str(exc)
            )
        is_error = bool(result.get("isError", False)) if isinstance(result, dict) else False
        content = result.get("content") if isinstance(result, dict) else result
        return MCPToolResult(
            tool=name, server=self.server_name, content=content, is_error=is_error,
            error=None if not is_error else "MCP tool melaporkan error.",
        )

    def close(self) -> None:
        """Tutup koneksi dengan aman (terminate subprocess)."""
        proc = self._proc
        self._proc = None
        self._connected = False
        # Hentikan reader thread.
        self._reader_stop.set()
        if self._reader is not None:
            self._reader.join(timeout=2)
            self._reader = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    @property
    def server_info(self) -> Dict[str, Any]:
        return dict(self._server_info)

    @property
    def capabilities(self) -> Dict[str, Any]:
        return dict(self._capabilities)


def stdio_client_factory(config: MCPServerConfig) -> StdioMCPClient:
    """Factory untuk MCPServerManager: config -> StdioMCPClient."""
    return StdioMCPClient(config)
