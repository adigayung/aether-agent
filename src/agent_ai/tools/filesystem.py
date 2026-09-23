"""Filesystem tools (read-only).

Menyediakan tiga tool:
    - list_files : daftar file/directory dalam sebuah directory.
    - read_file  : baca isi file (opsional line range).
    - search_code: cari teks di file dalam project root.

Keamanan:
    - Semua path dibatasi pada project root yang dikonfigurasi.
    - Path traversal (mis. "..\\") yang keluar dari root ditolak.
    - Tidak ada operasi tulis/hapus, tidak menjalankan command/shell.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_ai.tools.base import BaseTool, ToolExecutionError, ToolValidationError

# ---------------------------------------------------------------------------
# Project root yang diizinkan untuk diakses tool.
# Default: root project (dua level di atas file ini: tools/ -> agent_ai/ -> src/ -> root).
# ---------------------------------------------------------------------------
_DEFAULT_ROOT = Path(__file__).resolve().parents[3]

# Directory yang selalu diabaikan saat listing/search agar tidak scan berat.
_IGNORED_DIRS = {
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "env",
    "node_modules",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".aether",
}

# Batas default agar tidak membaca seluruh project tanpa sengaja.
_DEFAULT_MAX_ENTRIES = 200
_DEFAULT_MAX_RESULTS = 100
_DEFAULT_MAX_FILE_BYTES = 1_000_000  # 1 MB


def _resolve_within_root(path: str, root: Path) -> Path:
    """Resolve `path` relatif terhadap `root` dan pastikan tetap di dalam root.

    Args:
        path: path relatif (atau absolut) yang diminta.
        root: project root yang diizinkan.

    Returns:
        Path absolut yang sudah divalidasi berada di dalam root.

    Raises:
        ToolValidationError: bila path keluar dari root (path traversal).
    """
    root_resolved = root.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root_resolved / candidate
    resolved = candidate.resolve()

    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ToolValidationError(
            f"Path '{path}' berada di luar project root dan ditolak."
        )
    return resolved


def _iter_files(base: Path):
    """Iterasi file di bawah `base`, melewati directory yang diabaikan."""
    for dirpath, dirnames, filenames in os.walk(base):
        # Modifikasi in-place agar os.walk melewati directory terabaikan.
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
        for filename in filenames:
            yield Path(dirpath) / filename


# --------------------------------------------------------------------------- #
# Line range helper (1-based, inklusif) — dipakai bersama oleh read_file dan
# edit_file. Satu implementasi, satu konvensi penomoran baris.
# --------------------------------------------------------------------------- #
def _coerce_line_number(value: Any, name: str) -> Optional[int]:
    """Konversi argumen line number menjadi int (atau None bila tidak diberikan).

    Raises:
        ToolValidationError: bila nilai bukan integer 1-based yang valid.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool adalah subclass int; tolak eksplisit.
        raise ToolValidationError(
            f"Argumen '{name}' harus berupa integer (1-based), bukan boolean."
        )
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ToolValidationError(
                f"Argumen '{name}' harus berupa integer (1-based), bukan {value!r}."
            )
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            raise ToolValidationError(
                f"Argumen '{name}' harus berupa integer (1-based), bukan {value!r}."
            ) from None
    raise ToolValidationError(
        f"Argumen '{name}' harus berupa integer (1-based), bukan tipe "
        f"{type(value).__name__}."
    )


def normalize_line_range(
    start_line: Any,
    end_line: Any,
    total_lines: int,
) -> tuple[Optional[int], Optional[int]]:
    """Validasi & normalisasi line range 1-based (inklusif).

    Aturan:
        - Keduanya tidak diberikan -> (None, None): pemanggil memakai seluruh file.
        - start_line < 1 atau end_line < 1 -> error.
        - start_line > end_line -> error.
        - start_line melebihi jumlah baris file -> error (jangan diam-diam
          mengembalikan hasil kosong yang menyesatkan).
        - end_line melebihi jumlah baris file -> di-clamp ke jumlah baris nyata
          (tetap mengembalikan baris yang benar-benar ada).

    Args:
        start_line: nilai mentah argumen 'start_line' (atau None).
        end_line: nilai mentah argumen 'end_line' (atau None).
        total_lines: jumlah baris file target (hasil `str.splitlines()`).

    Returns:
        Tuple (start, end) 1-based inklusif, atau (None, None) bila range tidak
        diminta.

    Raises:
        ToolValidationError: bila range tidak valid.
    """
    start = _coerce_line_number(start_line, "start_line")
    end = _coerce_line_number(end_line, "end_line")

    if start is None and end is None:
        return None, None

    if total_lines <= 0:
        raise ToolValidationError(
            "File tidak memiliki baris; line range tidak dapat diterapkan."
        )
    if start is not None and start < 1:
        raise ToolValidationError("Argumen 'start_line' harus >= 1 (1-based).")
    if end is not None and end < 1:
        raise ToolValidationError("Argumen 'end_line' harus >= 1 (1-based).")

    resolved_start = start if start is not None else 1
    resolved_end = end if end is not None else total_lines

    if resolved_start > total_lines:
        raise ToolValidationError(
            f"Argumen 'start_line'={resolved_start} melebihi jumlah baris file "
            f"({total_lines})."
        )
    if resolved_start > resolved_end:
        raise ToolValidationError(
            f"Argumen 'start_line'={resolved_start} lebih besar dari "
            f"'end_line'={resolved_end}."
        )
    if resolved_end > total_lines:
        resolved_end = total_lines
    return resolved_start, resolved_end


def format_numbered_lines(lines: List[str], start_line: int) -> str:
    """Render baris dengan prefix nomor baris 1-based (mis. ``"42: teks"``)."""
    return "\n".join(
        f"{start_line + offset}: {text}" for offset, text in enumerate(lines)
    )


class ListFilesTool(BaseTool):
    """Daftar file/directory dalam sebuah directory (read-only).

    Ini adalah cara utama untuk melihat isi directory.
    Gunakan tool ini untuk inspeksi workspace, bukan run_command dengan ls/dir.
    """

    name = "list_files"
    description = (
        "Menampilkan daftar file dan directory dalam sebuah directory. "
        "Ini adalah cara utama untuk melihat isi directory. "
        "Gunakan tool ini untuk inspeksi workspace, bukan run_command dengan ls/dir. "
        "Untuk membaca file, gunakan read_file. "
        "Untuk mencari teks dalam file, gunakan search_code."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory relatif terhadap project root."},
            "max_entries": {"type": "integer", "description": "Batas jumlah entri."},
        },
        "required": [],
    }

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        rel_path = arguments.get("path", ".") or "."
        max_entries = int(arguments.get("max_entries", _DEFAULT_MAX_ENTRIES))

        target = _resolve_within_root(rel_path, self.root)
        if not target.exists():
            raise ToolExecutionError(f"Directory tidak ditemukan: {rel_path}")
        if not target.is_dir():
            raise ToolValidationError(f"'{rel_path}' bukan sebuah directory.")

        entries: List[Dict[str, Any]] = []
        truncated = False
        for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if child.name in _IGNORED_DIRS:
                continue
            if len(entries) >= max_entries:
                truncated = True
                break
            entries.append(
                {
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file",
                    "size": child.stat().st_size if child.is_file() else None,
                }
            )

        return {
            "path": rel_path,
            "count": len(entries),
            "truncated": truncated,
            "entries": entries,
        }


class ReadFileTool(BaseTool):
    """Baca isi file (read-only), dengan optional line range.

    Ini adalah cara utama untuk membaca isi file.
    Gunakan tool ini untuk membaca file, bukan run_command dengan cat/type.
    """

    name = "read_file"
    description = (
        "Membaca isi sebuah file, opsional dengan rentang baris (1-based, inklusif). "
        "Ini adalah cara utama untuk membaca isi file. "
        "Tanpa start_line/end_line: membaca SELURUH file. "
        "Hanya start_line: dari baris itu sampai akhir file. "
        "Hanya end_line: dari awal file sampai baris itu. "
        "Keduanya: hanya baris start_line..end_line. "
        "Pada mode rentang, hasil juga memuat 'content_numbered' (setiap baris "
        "diberi prefix nomor baris) sehingga lokasi baris dapat dipakai untuk "
        "edit_file. Gunakan tool ini untuk membaca file, bukan run_command "
        "dengan cat/type. Untuk melihat isi directory, gunakan list_files. "
        "Untuk mencari teks dalam file, gunakan search_code."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path file relatif terhadap project root."},
            "start_line": {
                "type": "integer",
                "description": (
                    "Baris awal (1-based, inklusif). Kosong = dari awal file."
                ),
            },
            "end_line": {
                "type": "integer",
                "description": (
                    "Baris akhir (1-based, inklusif). Kosong = sampai akhir file."
                ),
            },
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
        if not target.exists():
            raise ToolExecutionError(f"File tidak ditemukan: {rel_path}")
        if not target.is_file():
            raise ToolValidationError(f"'{rel_path}' bukan sebuah file.")

        size = target.stat().st_size
        if size > _DEFAULT_MAX_FILE_BYTES:
            raise ToolExecutionError(
                f"File terlalu besar untuk dibaca ({size} bytes > {_DEFAULT_MAX_FILE_BYTES})."
            )

        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolExecutionError(f"Gagal membaca file '{rel_path}': {exc}") from exc

        lines = text.splitlines()
        total_lines = len(lines)

        start_line, end_line = normalize_line_range(
            arguments.get("start_line"),
            arguments.get("end_line"),
            total_lines,
        )

        if start_line is None:
            # Tanpa range: perilaku lama (seluruh file) tidak berubah.
            return {
                "path": rel_path,
                "total_lines": total_lines,
                "content": text,
            }

        selected = lines[start_line - 1 : end_line]
        return {
            "path": rel_path,
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": total_lines,
            "content": "\n".join(selected),
            "content_numbered": format_numbered_lines(selected, start_line),
        }


class SearchCodeTool(BaseTool):
    """Cari teks di file dalam project root (read-only).

    Ini adalah cara utama untuk mencari source code di dalam project.
    Gunakan tool ini untuk inspeksi kode, bukan run_command dengan find/grep.
    """

    name = "search_code"
    description = (
        "Mencari teks pada file di dalam project root. "
        "Ini adalah cara utama untuk mencari source code. "
        "Gunakan tool ini untuk inspeksi kode, bukan run_command dengan find/grep. "
        "Untuk membaca file, gunakan read_file. "
        "Untuk melihat isi directory, gunakan list_files."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Teks yang dicari."},
            "path": {"type": "string", "description": "Sub-path relatif terhadap project root."},
            "max_results": {"type": "integer", "description": "Batas jumlah hasil."},
        },
        "required": ["query"],
    }

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        query = arguments.get("query")
        if not query:
            raise ToolValidationError("Argumen 'query' wajib diisi.")

        rel_path = arguments.get("path", ".") or "."
        max_results = int(arguments.get("max_results", _DEFAULT_MAX_RESULTS))

        base = _resolve_within_root(rel_path, self.root)
        if not base.exists():
            raise ToolExecutionError(f"Path tidak ditemukan: {rel_path}")

        matches: List[Dict[str, Any]] = []
        truncated = False

        files = [base] if base.is_file() else _iter_files(base)
        for file_path in files:
            if len(matches) >= max_results:
                truncated = True
                break
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    matches.append(
                        {
                            "file": str(file_path.relative_to(self.root.resolve())),
                            "line": lineno,
                            "text": line.strip(),
                        }
                    )
                    if len(matches) >= max_results:
                        truncated = True
                        break

        return {
            "query": query,
            "path": rel_path,
            "count": len(matches),
            "truncated": truncated,
            "matches": matches,
        }
