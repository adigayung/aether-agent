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

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from agent_ai.tools.base import BaseTool, ToolExecutionError, ToolValidationError
from agent_ai.tools.filesystem import _DEFAULT_ROOT, _resolve_within_root

#: Sink event perubahan filesystem: `callable(payload: dict) -> None`.
#: Dipanggil HANYA setelah operasi filesystem benar-benar berhasil.
ChangeSink = Callable[[Dict[str, Any]], None]

#: Batas panjang unified diff yang disertakan ke event (agar SSE tetap ringkas).
_MAX_DIFF_CHARS = 12000


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


def _atomic_write_text(target: Path, content: str) -> None:
    """Tulis teks ke file secara atomic (temp file di folder yang sama + replace).

    Mencegah file setengah isi bila proses gagal di tengah penulisan. Semantik
    encoding/newline sama dengan `Path.write_text(..., encoding="utf-8")`.
    Parent directory diasumsikan sudah ada (dibuat oleh caller).
    """
    parent = target.parent
    fd, tmp_name = tempfile.mkstemp(
        dir=str(parent), prefix=".aether_tmp_", suffix=".swp"
    )
    os.close(fd)
    try:
        Path(tmp_name).write_text(content, encoding="utf-8")
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Live filesystem change events (reuse diff engine existing)
# --------------------------------------------------------------------------- #
def _read_bytes(path: Path) -> Optional[bytes]:
    """Baca bytes file (None bila bukan file / gagal). Tidak melempar error."""
    try:
        if path.is_file():
            return path.read_bytes()
    except OSError:
        return None
    return None


def _count_diff_lines(diff_text: str) -> tuple:
    """Hitung jumlah baris ditambah/dihapus dari unified diff."""
    additions = 0
    deletions = 0
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions, deletions


def _diff_summary(before: Optional[bytes], after: Optional[bytes], rel_path: str):
    """Unified diff memakai diff engine existing (`agent_ai.changes.diff`).

    Reuse, BUKAN diff engine kedua.

    Returns:
        (diff_text, additions, deletions). Semuanya None bila tidak ada
        perubahan teks / file biner / gagal (tidak membuat diff palsu).
    """
    try:
        from agent_ai.changes import diff as _change_diff

        diff_text = _change_diff.generate(before, after, rel_path)
    except Exception:  # noqa: BLE001 - diff tidak boleh menggagalkan operasi
        return None, None, None
    if not diff_text:
        return None, None, None
    if len(diff_text) > _MAX_DIFF_CHARS:
        diff_text = diff_text[:_MAX_DIFF_CHARS] + "\n...[diff truncated]\n"
    additions, deletions = _count_diff_lines(diff_text)
    return diff_text, additions, deletions


class _WorkspaceChangeTool(BaseTool):
    """Base tool mutasi workspace + sink event perubahan (live filesystem).

    Sink `change_sink` dipanggil HANYA setelah operasi filesystem berhasil,
    dengan payload:
        {"path": <relative workspace path>, "kind": "created|modified|
         "deleted|moved", "tool": <name>, "old_path"?: <relative path>,
         "before_size"?, "after_size"?, "additions"?, "deletions"?, "diff"?}

    Path SELALU dinormalisasi menjadi relative terhadap workspace/project root
    (posix separator). Sink tidak boleh menggagalkan operasi (exception ditelan).
    """

    def __init__(
        self,
        root: Optional[Path] = None,
        change_sink: Optional[ChangeSink] = None,
    ) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT
        self._change_sink = change_sink

    def _to_rel(self, path: Any) -> str:
        """Normalisasi path menjadi relative terhadap workspace root (posix)."""
        text = str(path or "")
        try:
            candidate = Path(text)
            root_resolved = self.root.resolve()
            if candidate.is_absolute():
                resolved = candidate.resolve()
                if resolved == root_resolved or root_resolved in resolved.parents:
                    text = str(resolved.relative_to(root_resolved))
            else:
                # Buang prefix "./" tanpa menyentuh isi path.
                while text.startswith("./") or text.startswith(".\\"):
                    text = text[2:]
        except Exception:  # noqa: BLE001 - normalisasi best-effort
            pass
        return text.replace("\\", "/").strip()

    def _emit_change(
        self,
        *,
        path: Any,
        kind: str,
        old_path: Any = None,
        before: Optional[bytes] = None,
        after: Optional[bytes] = None,
    ) -> None:
        """Emit satu logical perubahan filesystem (setelah operasi sukses)."""
        sink = self._change_sink
        if sink is None:
            return
        rel_path = self._to_rel(path)
        payload: Dict[str, Any] = {
            "path": rel_path,
            "kind": kind,
            "tool": self.name,
            "before_size": len(before) if before is not None else None,
            "after_size": len(after) if after is not None else None,
        }
        if old_path is not None:
            payload["old_path"] = self._to_rel(old_path)
        diff_text, additions, deletions = _diff_summary(before, after, rel_path)
        if diff_text is not None:
            payload["diff"] = diff_text
            payload["additions"] = additions
            payload["deletions"] = deletions
        try:
            sink(payload)
        except Exception:  # noqa: BLE001 - observability tidak boleh crash
            return


class WriteFileTool(_WorkspaceChangeTool):
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

    def __init__(
        self,
        root: Optional[Path] = None,
        change_sink: Optional[ChangeSink] = None,
    ) -> None:
        super().__init__(root=root, change_sink=change_sink)

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

        existed = target.is_file()
        before = _read_bytes(target) if existed else None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Atomic: hindari file setengah isi bila penulisan terputus.
            _atomic_write_text(target, str(arguments["content"]))
        except OSError as exc:
            raise ToolExecutionError(f"Gagal menulis file '{rel_path}': {exc}") from exc

        after = _read_bytes(target)
        if after is None:
            after = str(arguments["content"]).encode("utf-8")
        # Live event HANYA setelah write benar-benar berhasil.
        self._emit_change(
            path=rel_path,
            kind="modified" if existed else "created",
            before=before,
            after=after,
        )

        return {"path": rel_path, "bytes": target.stat().st_size, "written": True}


class EditFileTool(_WorkspaceChangeTool):
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

    def __init__(
        self,
        root: Optional[Path] = None,
        change_sink: Optional[ChangeSink] = None,
    ) -> None:
        super().__init__(root=root, change_sink=change_sink)

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

        before = _read_bytes(target)
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

        after = _read_bytes(target)
        if after is None:
            after = new_content.encode("utf-8")
        # Live event HANYA setelah edit benar-benar berhasil.
        self._emit_change(path=rel_path, kind="modified", before=before, after=after)

        return {"path": rel_path, "replaced": 1, "edited": True}


class DeleteFileTool(_WorkspaceChangeTool):
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

    def __init__(
        self,
        root: Optional[Path] = None,
        change_sink: Optional[ChangeSink] = None,
    ) -> None:
        super().__init__(root=root, change_sink=change_sink)

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        rel_path = arguments.get("path")
        if not rel_path:
            raise ToolValidationError("Argumen 'path' wajib diisi.")

        target = _resolve_within_root(rel_path, self.root)
        _ensure_not_root(target, self.root, "menghapus")

        if not target.exists() and not target.is_symlink():
            raise ToolExecutionError(f"Path tidak ditemukan: {rel_path}")

        is_dir = target.is_dir() and not target.is_symlink()
        # Baca konten SEBELUM dihapus (untuk diff deleted), hanya untuk file.
        before = None if is_dir else _read_bytes(target)
        try:
            if is_dir:
                shutil.rmtree(target)
                kind = "dir"
            else:
                target.unlink()
                kind = "file"
        except OSError as exc:
            raise ToolExecutionError(f"Gagal menghapus '{rel_path}': {exc}") from exc

        # Live event HANYA setelah delete benar-benar berhasil.
        self._emit_change(
            path=rel_path,
            kind="deleted",
            before=before,
            after=None,
        )

        return {"path": rel_path, "type": kind, "deleted": True}


class MoveFileTool(_WorkspaceChangeTool):
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

    def __init__(
        self,
        root: Optional[Path] = None,
        change_sink: Optional[ChangeSink] = None,
    ) -> None:
        super().__init__(root=root, change_sink=change_sink)

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

        # Live event HANYA setelah move benar-benar berhasil.
        self._emit_change(
            path=destination,
            kind="moved",
            old_path=source,
        )

        return {"source": source, "destination": destination, "moved": True}
