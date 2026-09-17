"""Registry tool.

Mendaftarkan tool berdasarkan nama, mengambilnya kembali, dan mengeksekusinya
melalui interface yang konsisten.

    from agent_ai.tools import ToolRegistry

    registry = ToolRegistry()
    registry.register(MyTool())
    result = registry.execute("my_tool", {"arg": 1})
"""

from __future__ import annotations

from typing import Any, Dict, List

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


def build_registry(root: "Path | None" = None) -> ToolRegistry:
    """Bangun ToolRegistry dengan semua tool bawaan.

    Args:
        root: workspace root opsional untuk tool filesystem/workspace. Bila
            None, tool memakai default root-nya (root project AETHER). Bila
            diisi (mis. active project root), semua operasi file dibatasi ke
            root tersebut. Ini TIDAK membuat tool/subsystem baru: hanya
            mengarahkan root tool yang sudah ada.

    Returns:
        ToolRegistry baru berisi seluruh tool bawaan.
    """
    from pathlib import Path as _Path

    from agent_ai.tools.filesystem import (
        ListFilesTool,
        ReadFileTool,
        SearchCodeTool,
    )
    from agent_ai.tools.terminal import RunCommandTool
    from agent_ai.tools.workspace import (
        DeleteFileTool,
        EditFileTool,
        MoveFileTool,
        WriteFileTool,
    )

    resolved = _Path(root) if root is not None else None
    reg = ToolRegistry()
    reg.register(ListFilesTool(root=resolved))
    reg.register(ReadFileTool(root=resolved))
    reg.register(SearchCodeTool(root=resolved))
    reg.register(WriteFileTool(root=resolved))
    reg.register(EditFileTool(root=resolved))
    reg.register(DeleteFileTool(root=resolved))
    reg.register(MoveFileTool(root=resolved))
    reg.register(RunCommandTool(root=resolved))
    return reg


# Daftarkan tool bawaan ke registry global (default root = project AETHER).
for _tool in build_registry()._tools.values():  # noqa: SLF001 - internal init
    registry.register(_tool)

