"""Workspace write tools (mutating, autonomous).

Menyediakan empat tool operasi filesystem di dalam workspace:
    - write_file  : tulis/buat file (buat parent directory bila perlu).
    - edit_file   : ganti `old_text` -> `new_text` (harus unik, tidak blind).
    - delete_file : hapus file atau directory (recursive).
    - move_file   : pindah/rename file atau directory.

Keamanan (workspace boundary):
    - Semua path (source & destination) divalidasi berada di dalam project root.
    - Path traversal dan symlink yang keluar workspace ditolak.
    - Workspace root itu sendiri tidak boleh dihapus/dipindahkan.
    - Memakai helper `_resolve_within_root` yang sudah ada (tidak ada
      mekanisme boundary kedua).

Tidak ada terminal/Git/web/RAG/UI.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from agent_ai.tools.base import BaseTool, ToolExecutionError, ToolValidationError
from agent_ai.tools.filesystem import _DEFAULT_ROOT, _resolve_within_root


def _resolve_write_path(path: str, root: Path) -> Path:
    """Resolve path untuk operasi tulis dan pastikan di dalam root.

    Menolak path traversal dan symlink yang keluar workspace. Untuk path yang
    belum ada (mis. file baru), validasi dilakukan pada parent terdekat yang ada.

    Raises:
        ToolValidationError: bila path keluar dari root.
    """
    root_resolved = root.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root_resolved / candidate

    # Validasi path yang sudah ada (termasuk symlink) lewat helper existing.
    if candidate.exists() or candidate.is_symlink():
        return _resolve_within_root(str(candidate), root_resolved)

    # Path belum ada: validasi parent terdekat yang ada.
    parent = candidate.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    _resolve_within_root(str(parent), root_resolved)

    # Pastikan hasil resolve akhir tetap di dalam root.
    resolved = candidate.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ToolValidationError(
            f"Path '{path}' berada di luar project root dan ditolak."
        )
    return resolved


def _ensure_not_root(target: Path, root: Path, action: str) -> None:
    """Tolak operasi yang menargetkan workspace root itu sendiri."""
    if target.resolve() == root.resolve():
        raise ToolValidationError(
            f"Tidak boleh {action} workspace root itu sendiri."
        )


class WriteFileTool(BaseTool):
    """Tulis/buat file di dalam workspace (buat parent directory bila perlu)."""

    name = "write_file"
    description = "Menulis konten ke sebuah file (membuat parent directory bila perlu)."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path file relatif terhadap project root."},
            "content": {"type": "string", "description": "Konten file."},
        },
        "required": ["path", "content"],
    }

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        rel_path = arguments.get("path")
        if not rel_path:
            raise ToolValidationError("Argumen 'path' wajib diisi.")
        if "content" not in arguments:
            raise ToolValidationError("Argumen 'content' wajib diisi.")

        target = _resolve_write_path(rel_path, self.root)
        _ensure_not_root(target, self.root, "menulis")

        if target.exists() and target.is_dir():
            raise ToolValidationError(f"'{rel_path}' adalah sebuah directory.")

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(arguments["content"]), encoding="utf-8")
        except OSError as exc:
            raise ToolExecutionError(f"Gagal menulis file '{rel_path}': {exc}") from exc

        return {"path": rel_path, "bytes": target.stat().st_size, "written": True}


class EditFileTool(BaseTool):
    """Ganti `old_text` -> `new_text` pada file (harus unik, tidak blind)."""

    name = "edit_file"
    description = "Mengganti teks pada file; gagal bila target tidak ada atau ambigu."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path file relatif terhadap project root."},
            "old_text": {"type": "string", "description": "Teks yang dicari (harus unik)."},
            "new_text": {"type": "string", "description": "Teks pengganti."},
        },
        "required": ["path", "old_text", "new_text"],
    }

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        rel_path = arguments.get("path")
        if not rel_path:
            raise ToolValidationError("Argumen 'path' wajib diisi.")
        old_text = arguments.get("old_text")
        if old_text is None or old_text == "":
            raise ToolValidationError("Argumen 'old_text' wajib diisi dan tidak boleh kosong.")
        if "new_text" not in arguments:
            raise ToolValidationError("Argumen 'new_text' wajib diisi.")

        target = _resolve_within_root(rel_path, self.root)
        if not target.exists():
            raise ToolExecutionError(f"File tidak ditemukan: {rel_path}")
        if not target.is_file():
            raise ToolValidationError(f"'{rel_path}' bukan sebuah file.")

        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolExecutionError(f"Gagal membaca file '{rel_path}': {exc}") from exc

        count = text.count(old_text)
        if count == 0:
            raise ToolExecutionError(
                f"Teks target tidak ditemukan pada '{rel_path}'."
            )
        if count > 1:
            raise ToolExecutionError(
                f"Teks target ambigu pada '{rel_path}' (ditemukan {count} kali)."
            )

        new_content = text.replace(old_text, str(arguments["new_text"]), 1)
        try:
            target.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            raise ToolExecutionError(f"Gagal menulis file '{rel_path}': {exc}") from exc

        return {"path": rel_path, "replaced": 1, "edited": True}


class DeleteFileTool(BaseTool):
    """Hapus file atau directory (recursive) di dalam workspace."""

    name = "delete_file"
    description = "Menghapus file atau directory (recursive) di dalam workspace."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path file/directory relatif terhadap project root."},
        },
        "required": ["path"],
    }

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        rel_path = arguments.get("path")
        if not rel_path:
            raise ToolValidationError("Argumen 'path' wajib diisi.")

        target = _resolve_within_root(rel_path, self.root)
        _ensure_not_root(target, self.root, "menghapus")

        if not target.exists() and not target.is_symlink():
            raise ToolExecutionError(f"Path tidak ditemukan: {rel_path}")

        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
                kind = "dir"
            else:
                target.unlink()
                kind = "file"
        except OSError as exc:
            raise ToolExecutionError(f"Gagal menghapus '{rel_path}': {exc}") from exc

        return {"path": rel_path, "type": kind, "deleted": True}


class MoveFileTool(BaseTool):
    """Pindah/rename file atau directory di dalam workspace."""

    name = "move_file"
    description = "Memindahkan/mengganti nama file atau directory di dalam workspace."
    input_schema = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Path sumber relatif terhadap project root."},
            "destination": {"type": "string", "description": "Path tujuan relatif terhadap project root."},
        },
        "required": ["source", "destination"],
    }

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        source = arguments.get("source")
        destination = arguments.get("destination")
        if not source:
            raise ToolValidationError("Argumen 'source' wajib diisi.")
        if not destination:
            raise ToolValidationError("Argumen 'destination' wajib diisi.")

        src = _resolve_within_root(source, self.root)
        dst = _resolve_write_path(destination, self.root)

        _ensure_not_root(src, self.root, "memindahkan")
        _ensure_not_root(dst, self.root, "menimpa")

        if not src.exists() and not src.is_symlink():
            raise ToolExecutionError(f"Sumber tidak ditemukan: {source}")
        if dst.exists() or dst.is_symlink():
            raise ToolExecutionError(
                f"Destination sudah ada: {destination} (tidak menimpa)."
            )

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except OSError as exc:
            raise ToolExecutionError(
                f"Gagal memindahkan '{source}' -> '{destination}': {exc}"
            ) from exc

        return {"source": source, "destination": destination, "moved": True}
