"""ActionClassifier: klasifikasi action/tool -> ActionClass (#54).

Deterministik, provider-agnostic. Klasifikasi TIDAK mengubah ToolRegistry dan
TIDAK mengeksekusi apa pun. Ia hanya memetakan nama action/tool (dan argumen
bila perlu) ke ActionClass.

Sumber klasifikasi:
    - Peta nama tool bawaan (read-only / write / delete-move / command).
    - Heuristik nama action untuk tool kustom (mis. mengandung "delete"/"move").
    - Argumen (mis. action "run_command" -> COMMAND_EXECUTION).

Bila tidak dapat diklasifikasi -> ActionClass.UNKNOWN (default aman).
"""

from __future__ import annotations

from typing import Dict, Optional

from agent_ai.permission.models import ActionClass

#: Tool read-only bawaan (lihat tools/filesystem.py).
_READ_ONLY_TOOLS = frozenset({
    "list_files",
    "read_file",
    "search_code",
})

#: Tool tulis workspace bawaan (lihat tools/workspace.py).
_WORKSPACE_WRITE_TOOLS = frozenset({
    "write_file",
    "edit_file",
})

#: Tool hapus/pindah bawaan (lihat tools/workspace.py).
_DELETE_MOVE_TOOLS = frozenset({
    "delete_file",
    "move_file",
})

#: Tool command/terminal bawaan (lihat tools/terminal.py).
_COMMAND_TOOLS = frozenset({
    "run_command",
})

#: Kata kunci nama action untuk heuristik tool kustom.
_DELETE_MOVE_KEYWORDS = ("delete", "remove", "move", "rename", "unlink", "rmdir")
_WRITE_KEYWORDS = ("write", "edit", "create", "append", "patch", "save")
_READ_KEYWORDS = ("read", "list", "search", "get", "find", "view", "show", "grep")
_COMMAND_KEYWORDS = ("command", "exec", "shell", "terminal", "run", "spawn")
_NETWORK_KEYWORDS = ("http", "fetch", "request", "download", "upload", "network", "web", "curl")


class ActionClassifier:
    """Klasifikasi action/tool menjadi ActionClass (deterministik)."""

    def classify(
        self,
        action: str,
        arguments: Optional[Dict] = None,
    ) -> ActionClass:
        """Klasifikasi sebuah action/tool.

        Args:
            action: nama action/tool.
            arguments: argumen action (opsional; dipakai untuk heuristik).

        Returns:
            ActionClass (UNKNOWN bila tidak dapat diklasifikasi).
        """
        name = (action or "").strip().lower()
        if not name:
            return ActionClass.UNKNOWN

        # 1) Peta tool bawaan (paling akurat).
        if name in _READ_ONLY_TOOLS:
            return ActionClass.READ_ONLY
        if name in _WORKSPACE_WRITE_TOOLS:
            return ActionClass.WORKSPACE_WRITE
        if name in _DELETE_MOVE_TOOLS:
            return ActionClass.DELETE_MOVE
        if name in _COMMAND_TOOLS:
            return ActionClass.COMMAND_EXECUTION

        # 2) Heuristik nama action (untuk tool kustom).
        #    Urutan penting: delete/move diperiksa sebelum write/read.
        if any(k in name for k in _DELETE_MOVE_KEYWORDS):
            return ActionClass.DELETE_MOVE
        if any(k in name for k in _COMMAND_KEYWORDS):
            return ActionClass.COMMAND_EXECUTION
        if any(k in name for k in _NETWORK_KEYWORDS):
            return ActionClass.EXTERNAL_NETWORK
        if any(k in name for k in _WRITE_KEYWORDS):
            return ActionClass.WORKSPACE_WRITE
        if any(k in name for k in _READ_KEYWORDS):
            return ActionClass.READ_ONLY

        # 3) Tidak terklasifikasi -> UNKNOWN (default aman).
        return ActionClass.UNKNOWN
