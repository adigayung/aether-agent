"""Registry tool.

Mendaftarkan tool berdasarkan nama, mengambilnya kembali, dan mengeksekusinya
melalui interface yang konsisten.

    from agent_ai.tools import ToolRegistry

    registry = ToolRegistry()
    registry.register(MyTool())
    result = registry.execute("my_tool", {"arg": 1})
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from agent_ai.tools.base import (
    BaseTool,
    ToolExecutionError,
    ToolNotFoundError,
    ToolValidationError,
)


class ToolRegistry:
    """Kumpulan tool yang terdaftar, diakses lewat nama unik."""

    def __init__(self) -> None:
        self._tools: Dict[str, BaseTool] = {}
        #: Cache retrieval (ToolReadCache) yang dipakai tool read/search registry
        #: ini, bila ada. Dibiarkan `None` secara default sehingga registry
        #: generik/uji tetap berperilaku sama. `build_registry()` mengisinya agar
        #: runtime dapat MENYELARASKAN tanda "tersedia" dengan konteks yang
        #: benar-benar dikirim ke LLM (menghindari false positive saat context
        #: compaction membuang detail sumber). Bukan subsystem baru: hanya
        #: referensi ke cache yang sudah dipakai tool.
        self.read_cache: Any = None

    def register(self, tool: BaseTool) -> None:
        """Daftarkan sebuah tool berdasarkan atribut `name`.

        Raises:
            ValueError: bila nama tool kosong atau masih "base".
        """
        name = getattr(tool, "name", None)
        if not name or name == "base":
            raise ValueError("Tool harus punya atribut 'name' yang unik dan bukan 'base'.")
        self._tools[name.lower()] = tool

    def get(self, name: str) -> BaseTool:
        """Ambil tool berdasarkan nama.

        Raises:
            ToolNotFoundError: bila nama tool belum terdaftar.
        """
        key = (name or "").lower()
        if key not in self._tools:
            available = ", ".join(sorted(self._tools)) or "(kosong)"
            raise ToolNotFoundError(f"Tool '{name}' tidak terdaftar. Tersedia: {available}")
        return self._tools[key]

    def has(self, name: str) -> bool:
        """Cek apakah tool terdaftar."""
        return (name or "").lower() in self._tools

    def list(self) -> List[str]:
        """Daftar nama tool yang terdaftar."""
        return sorted(self._tools)

    def specs(self) -> List[Dict[str, Any]]:
        """Daftar spesifikasi semua tool (untuk dokumentasi/LLM)."""
        return [tool.to_spec() for tool in self._tools.values()]

    def execute(self, name: str, arguments: Dict[str, Any] | None = None) -> Any:
        """Eksekusi tool berdasarkan nama.

        Args:
            name: nama tool.
            arguments: argumen untuk tool (dict).

        Returns:
            Hasil eksekusi tool.

        Raises:
            ToolNotFoundError: bila tool tidak terdaftar.
            ToolValidationError: bila argumen tidak valid.
            ToolExecutionError: bila eksekusi gagal (error asli di __cause__).
        """
        tool = self.get(name)
        args = arguments or {}

        tool.validate(args)

        try:
            return tool.execute(**args)
        except (ToolValidationError, ToolNotFoundError):
            # Error tool yang sudah jelas: teruskan apa adanya.
            raise
        except Exception as exc:  # noqa: BLE001 - bungkus error asli, jangan ditelan
            raise ToolExecutionError(
                f"Tool '{name}' gagal dieksekusi: {type(exc).__name__}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------

# Registry global + pendaftaran tool bawaan (read-only filesystem tools).
# ---------------------------------------------------------------------------
registry = ToolRegistry()

#: Sentinel: "buat ToolReadCache baru" (per-task default). Bila pemanggil
#: mengirim `read_cache=None` eksplisit, dedup duplicate-read DIMATIKAN.
_AUTO_READ_CACHE = object()


def build_registry(
    root: "Path | None" = None,
    change_sink: "Callable[[dict], None] | None" = None,
    cancel_token: "Any | None" = None,
    read_cache: "Any" = _AUTO_READ_CACHE,
) -> ToolRegistry:
    """Bangun ToolRegistry dengan semua tool bawaan.

    Args:
        root: workspace root opsional untuk tool filesystem/workspace. Bila
            None, tool memakai default root-nya (root project AETHER). Bila
            diisi (mis. active project root), semua operasi file dibatasi ke
            root tersebut. Ini TIDAK membuat tool/subsystem baru: hanya
            mengarahkan root tool yang sudah ada.
        change_sink: callback opsional `(payload: dict) -> None` yang dipanggil
            tool mutasi workspace (write/edit/delete/move) SEGERA setelah
            operasi file berhasil. Dipakai untuk live filesystem event
            (Explorer/Changes) tanpa menunggu task selesai. Bila None,
            tool berperilaku persis seperti sebelumnya (backward compatible).
        cancel_token: token pembatalan kooperatif opsional (CancellationToken).
            Diteruskan ke `run_command` agar proses yang sedang berjalan dapat
            dihentikan saat user Stop (bukan hanya menunggu timeout). Bila None,
            perilaku run_command persis seperti sebelumnya.
        read_cache: cache duplicate-read opsional (ToolReadCache). Default
            (sentinel internal) = buat instance BARU per build_registry, sehingga
            dedup read_file ter-scope per task/session (build_registry dipanggil
            per task). Kirim `None` eksplisit untuk MEMATIKAN dedup (dipakai
            untuk registry global; agar tidak ada cache lintas task).

    Returns:
        ToolRegistry baru berisi seluruh tool bawaan.

    Catatan Project Map: capability `atlas_query`, `rig_query`,
    `project_map_status`, dan `refresh_project_map` terdaftar di sini (Agent).
    Tool ini hanya bekerja bila `root` project diketahui (lokasi
    `<root>/.aether/map/`). Registry Consultant dibangun terpisah
    (`agent_ai.consultant.tools.build_consultant_registry`) TANPA
    `refresh_project_map`, sehingga Consultant tetap read-only terhadap map.
    """
    from pathlib import Path as _Path

    from agent_ai.tools.filesystem import (
        ListFilesTool,
        ReadFileTool,
        SearchCodeTool,
    )
    from agent_ai.tools.project_map import build_project_map_tools
    from agent_ai.tools.read_cache import ToolReadCache
    from agent_ai.tools.terminal import RunCommandTool
    from agent_ai.tools.workspace import (
        DeleteFileTool,
        EditFileTool,
        MoveFileTool,
        WriteFileTool,
    )


    resolved = _Path(root) if root is not None else None
    # Cache duplicate-read: SATU instance per build_registry (= per task), dibagi
    # antara ReadFileTool (yang mengisi) dan tool mutasi (yang menginvalidasi).
    # Scope task-scoped, bukan singleton global.
    if read_cache is _AUTO_READ_CACHE:
        read_cache = ToolReadCache()
    reg = ToolRegistry()
    # Ekspos cache ke runtime (AgentRuntime -> AgentOrchestrator) via registry
    # agar runtime dapat menyelaraskan tanda "tersedia" dengan konteks yang
    # benar-benar dikirim ke LLM saat compaction membuang detail sumber.
    reg.read_cache = read_cache
    reg.register(ListFilesTool(root=resolved))
    reg.register(ReadFileTool(root=resolved, read_cache=read_cache))
    reg.register(SearchCodeTool(root=resolved, read_cache=read_cache))
    reg.register(WriteFileTool(root=resolved, change_sink=change_sink, read_cache=read_cache))
    reg.register(EditFileTool(root=resolved, change_sink=change_sink, read_cache=read_cache))
    reg.register(DeleteFileTool(root=resolved, change_sink=change_sink, read_cache=read_cache))
    reg.register(MoveFileTool(root=resolved, change_sink=change_sink, read_cache=read_cache))
    reg.register(RunCommandTool(root=resolved, cancel_token=cancel_token))
    # Project Map (Agent): termasuk refresh_project_map (Agent-only).
    for tool in build_project_map_tools(root=resolved, include_refresh=True):
        reg.register(tool)
    return reg


# Daftarkan tool bawaan ke registry global (default root = project AETHER).
# read_cache=None -> dedup duplicate-read DIMATIKAN untuk registry global agar
# tidak ada cache read lintas task yang bocor antar task fallback.
for _tool in build_registry(read_cache=None)._tools.values():  # noqa: SLF001 - internal init
    registry.register(_tool)

