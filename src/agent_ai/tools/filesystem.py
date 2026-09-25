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
from agent_ai.tools.read_cache import ToolReadCache
from agent_ai.tools.source_symbols import (
    enclosing_symbol,
    extract_symbols,
    find_symbols,
    structure_payload,
)

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


def _coerce_non_negative_int(value: Any, name: str, default: int = 0) -> int:
    """Konversi argumen menjadi integer >= 0 (atau `default` bila tidak diisi).

    Raises:
        ToolValidationError: bila nilai bukan integer >= 0.
    """
    if value is None:
        return default
    if isinstance(value, bool):  # bool adalah subclass int; tolak eksplisit.
        raise ToolValidationError(f"Argumen '{name}' harus integer >= 0, bukan boolean.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            number = int(text)
        except ValueError:
            raise ToolValidationError(
                f"Argumen '{name}' harus berupa integer >= 0, bukan {value!r}."
            ) from None
    else:
        raise ToolValidationError(
            f"Argumen '{name}' harus berupa integer >= 0, bukan tipe "
            f"{type(value).__name__}."
        )
    if number < 0:
        raise ToolValidationError(f"Argumen '{name}' harus >= 0.")
    return number


def _coerce_cache_int(value: Any) -> tuple[Optional[int], bool]:
    """Konversi nilai line-number untuk fast-path cache.

    Returns:
        (number, ok). `ok=False` berarti nilai TIDAK dapat dipakai untuk
        fast-path cache (mis. string non-numerik) sehingga validasi normal
        (yang akan melempar error yang tepat) harus dijalankan.
    """
    if value is None:
        return None, True
    if isinstance(value, bool):  # bool adalah subclass int; tolak eksplisit.
        return None, False
    if isinstance(value, int):
        return value, True
    if isinstance(value, float):
        if value.is_integer():
            return int(value), True
        return None, False
    if isinstance(value, str):
        try:
            return int(value.strip()), True
        except ValueError:
            return None, False
    return None, False


# Batas panjang potongan baris match pada search_code (locator-first).
_MATCH_SNIPPET_LIMIT = 200


def _clip_match(line: str, query: str, limit: int = _MATCH_SNIPPET_LIMIT) -> str:
    """Potong baris match agar tidak mengirim potongan source panjang.

    Bila baris lebih panjang dari `limit`, jendela difokuskan di sekitar posisi
    match sehingga token yang dicari tetap terlihat.
    """
    stripped = line.strip()
    if len(stripped) <= limit:
        return stripped
    index = stripped.find(query)
    if index < 0:
        return stripped[: limit - 1] + "…"
    half = max(0, (limit - len(query)) // 2)
    start = max(0, index - half)
    end = min(len(stripped), start + limit)
    start = max(0, end - limit)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(stripped) else ""
    return f"{prefix}{stripped[start:end]}{suffix}"


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
    """Baca isi file (read-only): seluruh file, rentang baris, symbol, atau struktur.

    Pola penggunaan yang disarankan (hemat context):

        search_code  -> locate (file + line + symbol)
        read_file(symbol=...) / read_file(start_line/end_line) -> inspect bagian yg tepat
        read_file(mode="structure") -> outline file tanpa body source
        edit_file    -> modify

    Backward compatible:
        - read_file(path)                    -> SELURUH file (perilaku lama).
        - read_file(path, start_line, end_line) -> rentang baris 1-based inklusif.
    """


    name = "read_file"
    description = (
        "Membaca isi sebuah file. Ini cara utama membaca file (bukan cat/type). "
        "PILIH yang paling hemat: "
        "(1) read_file(path, symbol='nama') -> HANYA function/class/method itu "
        "(pakai hasil search_code/atlas_query); error jelas bila symbol tak ada. "
        "(2) read_file(path, start_line, end_line) -> rentang baris 1-based, "
        "inklusif; tanpa keduanya membaca SELURUH file. "
        "(3) read_file(path, mode='structure') -> outline (class/function + line) "
        "tanpa body source, lalu pilih symbol untuk dibaca. "
        "context_lines=N menambah N baris sebelum/sesudah symbol/range. "
        "mode='raw' -> hanya 'content'; mode='numbered' -> sertakan "
        "'content_numbered' (prefix nomor baris). "
        "Rentang yang sama (atau yang sudah tercakup) dalam satu task diringkas "
        "menjadi penanda 'already_available'/'already_read'; force=true memaksa "
        "kirim ulang. Isi directory: list_files. Mencari teks: search_code."
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
            "symbol": {
                "type": "string",
                "description": (
                    "Baca HANYA symbol (function/class/method) ini. Gunakan "
                    "'Class.method' bila ambigu. Tidak dapat digabung dengan "
                    "start_line/end_line."
                ),
            },
            "context_lines": {
                "type": "integer",
                "description": (
                    "Jumlah baris konteks sebelum/sesudah symbol atau rentang "
                    "(default 0 = tanpa konteks tambahan)."
                ),
            },
            "mode": {
                "type": "string",
                "description": (
                    "Salah satu: 'raw' (hanya content), 'numbered' (content + "
                    "content_numbered), 'structure' (outline file tanpa body)."
                ),
            },
            "force": {
                "type": "boolean",
                "description": (
                    "True untuk mengabaikan deteksi duplicate-read dan tetap "
                    "mengirim source."
                ),
            },
        },
        "required": ["path"],
    }

    #: Mode valid untuk argumen `mode`.
    _VALID_MODES = ("raw", "numbered", "structure")

    def __init__(
        self,
        root: Optional[Path] = None,
        read_cache: Optional[ToolReadCache] = None,
    ) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT
        # Cache duplicate-read. Hanya aktif bila diberikan (per task/session,
        # dibuat oleh build_registry). None = tanpa dedup (backward compatible).
        self._read_cache = read_cache

    def _rel_key(self, target: Path) -> str:
        """Path relatif (posix) untuk kunci cache/pesan."""
        try:
            return target.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return str(target)




    @staticmethod
    def _already_available_stub(
        display_path: str,
        key_path: str,
        start: int,
        end: int,
        total_lines: int,
    ) -> Dict[str, Any]:
        """Stub eksplisit untuk LLM: source sudah tersedia, jangan reread.

        Ditandai `already_read` (kompatibilitas) + `already_available` (sinyal
        jelas) + `cache_hit` (observability). TIDAK memuat `content`.
        """
        return {
            "path": display_path,
            "start_line": start,
            "end_line": end,
            "total_lines": total_lines,
            "already_read": True,
            "already_available": True,
            "cache_hit": True,
            "message": (
                "ALREADY_AVAILABLE: The requested source context "
                f"({key_path}:{start}-{end}) is already available in the "
                "conversation and the source has NOT changed. Do not request the "
                "same file/range again; continue analysis using the existing "
                "context. Pass force=true only if the content must be re-sent."
            ),
        }

    def _cache_hit(
        self,
        cache: ToolReadCache,
        key_path: str,
        arguments: Dict[str, Any],
        signature: tuple,
    ) -> Optional[tuple]:
        """Cek fast-path: (start, end, total_lines) bila range sudah tersedia."""
        start, ok_start = _coerce_cache_int(arguments.get("start_line"))
        end, ok_end = _coerce_cache_int(arguments.get("end_line"))
        if not ok_start or not ok_end:
            return None
        if start is not None and start < 1:
            return None
        if end is not None and end < 1:
            return None
        if start is not None and end is not None and start > end:
            return None
        return cache.covered(key_path, start, end, signature)

    def _maybe_dedupe(
        self,
        key_path: str,
        display_path: str,
        start: int,
        end: int,
        total_lines: int,
        content: str,
        signature: tuple,
        result: Dict[str, Any],
        force: bool,
    ) -> Dict[str, Any]:
        """Stub 'already_available' bila konten rentang ini sudah dikirim identik."""
        cache = self._read_cache
        if cache is None:
            return result
        if not force and cache.is_duplicate(key_path, start, end, content):
            return self._already_available_stub(
                display_path, key_path, start, end, total_lines
            )
        cache.record_read(key_path, start, end, signature, total_lines, content)
        return result

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        rel_path = arguments.get("path")
        if not rel_path:
            raise ToolValidationError("Argumen 'path' wajib diisi.")

        mode = arguments.get("mode")
        if mode is not None:
            if not isinstance(mode, str) or mode.strip().lower() not in self._VALID_MODES:
                raise ToolValidationError(
                    "Argumen 'mode' harus salah satu dari: raw, numbered, structure."
                )
            mode = mode.strip().lower()

        symbol = arguments.get("symbol")
        if symbol is not None and (not isinstance(symbol, str) or not symbol.strip()):
            raise ToolValidationError("Argumen 'symbol' harus berupa string tak kosong.")
        symbol = symbol.strip() if isinstance(symbol, str) else None

        context_lines = _coerce_non_negative_int(
            arguments.get("context_lines"), "context_lines", 0
        )
        force = bool(arguments.get("force", False))

        if symbol is not None and (
            arguments.get("start_line") is not None
            or arguments.get("end_line") is not None
        ):
            raise ToolValidationError(
                "Gunakan 'symbol' ATAU 'start_line'/'end_line', bukan keduanya."
            )


        target = _resolve_within_root(rel_path, self.root)
        if not target.exists():
            raise ToolExecutionError(f"File tidak ditemukan: {rel_path}")
        if not target.is_file():
            raise ToolValidationError(f"'{rel_path}' bukan sebuah file.")

        try:
            stat = target.stat()
        except OSError as exc:
            raise ToolExecutionError(f"Gagal membaca file '{rel_path}': {exc}") from exc
        size = stat.st_size
        if size > _DEFAULT_MAX_FILE_BYTES:
            raise ToolExecutionError(
                f"File terlalu besar untuk dibaca ({size} bytes > {_DEFAULT_MAX_FILE_BYTES})."
            )
        # Signature murah (mtime+size): mendeteksi perubahan file TANPA membaca
        # isinya -> memungkinkan skip physical read yang aman.
        signature = (int(getattr(stat, "st_mtime_ns", 0)), int(size))
        key_path = self._rel_key(target)

        cache = self._read_cache
        # In-flight dedup: permintaan identik untuk path sama diserialkan,
        # sehingga request paralel tidak melakukan physical read berkali-kali.
        lock = cache.key_lock(key_path) if cache is not None else None
        if lock is not None:
            lock.acquire()
        try:
            # Fast path: rentang yang SUDAH tersedia (exact/covered) dikembalikan
            # tanpa physical read, selama file belum berubah (signature sama).
            if (
                cache is not None
                and not force
                and symbol is None
                and mode != "structure"
            ):
                hit = self._cache_hit(cache, key_path, arguments, signature)
                if hit is not None:
                    hit_start, hit_end, hit_total = hit
                    return self._already_available_stub(
                        rel_path, key_path, hit_start, hit_end, hit_total
                    )

            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise ToolExecutionError(
                    f"Gagal membaca file '{rel_path}': {exc}"
                ) from exc

            lines = text.splitlines()
            total_lines = len(lines)

            # --- mode structure: outline tanpa body source ------------------
            if mode == "structure":
                return structure_payload(key_path, text, total_lines)

            # --- symbol read ------------------------------------------------
            if symbol is not None:
                matches = find_symbols(key_path, text, symbol)
                if not matches:
                    raise ToolExecutionError(
                        f"Symbol '{symbol}' tidak ditemukan pada '{rel_path}'. "
                        "Gunakan read_file(mode='structure') untuk melihat daftar symbol."
                    )
                if len(matches) > 1:
                    names = ", ".join(sorted(m.qualified_name for m in matches))
                    raise ToolValidationError(
                        f"Symbol '{symbol}' ambigu pada '{rel_path}' "
                        f"({len(matches)} kandidat: {names}). Gunakan nama qualified "
                        "seperti 'Class.method'."
                    )
                span = matches[0]
                start_line = max(1, span.start_line - context_lines)
                end_line = span.end_line + context_lines
                if total_lines:
                    end_line = min(end_line, total_lines)
                end_line = max(start_line, end_line)
                selected = lines[start_line - 1 : end_line]
                content = "\n".join(selected)
                result: Dict[str, Any] = {
                    "path": rel_path,
                    "symbol": span.to_dict(),
                    "start_line": start_line,
                    "end_line": end_line,
                    "total_lines": total_lines,
                    "content": content,
                }
                if context_lines:
                    result["context_lines"] = context_lines
                if mode == "numbered":
                    result["content_numbered"] = format_numbered_lines(
                        selected, start_line
                    )
                return self._maybe_dedupe(
                    key_path, rel_path, start_line, end_line, total_lines, content,
                    signature, result, force,
                )

            # --- range / full-file read (perilaku lama) ---------------------
            start_line, end_line = normalize_line_range(
                arguments.get("start_line"),
                arguments.get("end_line"),
                total_lines,
            )

            if start_line is None:
                # Tanpa range: perilaku lama (seluruh file) tidak berubah.
                result = {
                    "path": rel_path,
                    "total_lines": total_lines,
                    "content": text,
                }
                if mode == "numbered" and total_lines:
                    result["content_numbered"] = format_numbered_lines(lines, 1)
                return self._maybe_dedupe(
                    key_path, rel_path, 1, total_lines, total_lines, text,
                    signature, result, force,
                )

            selected = lines[start_line - 1 : end_line]
            content = "\n".join(selected)
            result = {
                "path": rel_path,
                "start_line": start_line,
                "end_line": end_line,
                "total_lines": total_lines,
                "content": content,
            }
            # Default (mode tidak disebut) mempertahankan perilaku lama: mode
            # rentang menyertakan 'content_numbered'. mode='raw' menghilangkan
            # duplikasi, mode='numbered' memaksanya.
            if mode != "raw":
                result["content_numbered"] = format_numbered_lines(selected, start_line)
            return self._maybe_dedupe(
                key_path, rel_path, start_line, end_line, total_lines, content,
                signature, result, force,
            )
        finally:
            if lock is not None:
                lock.release()


class SearchCodeTool(BaseTool):
    """Cari teks di file dalam project root (read-only, LOCATOR-FIRST).

    Ini adalah cara utama untuk mencari source code di dalam project.
    Gunakan tool ini untuk inspeksi kode, bukan run_command dengan find/grep.

    Default (locator-first): tiap match hanya memuat lokasi ringkas
    (file, line, potongan match terbatas, dan symbol induk bila tersedia) —
    BUKAN potongan source panjang. Setelah menemukan lokasi, baca bagian yang
    tepat via read_file(symbol=...) atau read_file(start_line/end_line).
    """

    name = "search_code"
    description = (
        "Mencari teks pada file di dalam project root (locator-first: hanya "
        "lokasi + potongan pendek, bukan source panjang). Ini cara utama "
        "mencari source code (bukan find/grep). Setiap match memuat 'file', "
        "'line', 'text' (dipotong), dan 'symbol' induk bila terdeteksi. "
        "Alur: search_code -> read_file(symbol=...) -> edit_file. "
        "context_lines=N menambah potongan sekitar match. Query yang SAMA persis "
        "(query + path + context_lines) pada source yang sama diringkas menjadi "
        "penanda 'already_searched' (jangan diulang). Membaca file: read_file. "
        "Isi directory: list_files."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Teks yang dicari."},
            "path": {"type": "string", "description": "Sub-path relatif terhadap project root."},
            "max_results": {"type": "integer", "description": "Batas jumlah hasil."},
            "context_lines": {
                "type": "integer",
                "description": (
                    "Opsional. Bila > 0, sertakan 'snippet' N baris sebelum/"
                    "sesudah tiap match (default 0 = hanya lokasi)."
                ),
            },
        },
        "required": ["query"],
    }


    def __init__(
        self,
        root: Optional[Path] = None,
        read_cache: Optional[ToolReadCache] = None,
    ) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT
        # Cache retrieval per task (dibuat oleh build_registry/build_consultant_
        # registry). None = tanpa dedup search (backward compatible).
        self._read_cache = read_cache

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        query = arguments.get("query")
        if not query:
            raise ToolValidationError("Argumen 'query' wajib diisi.")

        rel_path = arguments.get("path", ".") or "."
        if arguments.get("max_results") is None:
            max_results = _DEFAULT_MAX_RESULTS
        else:
            try:
                max_results = int(arguments.get("max_results"))
            except (TypeError, ValueError):
                raise ToolValidationError(
                    "Argumen 'max_results' harus berupa integer."
                ) from None
        context_lines = _coerce_non_negative_int(
            arguments.get("context_lines"), "context_lines", 0
        )




        base = _resolve_within_root(rel_path, self.root)
        if not base.exists():
            raise ToolExecutionError(f"Path tidak ditemukan: {rel_path}")

        cache = self._read_cache
        # Dedup: pencarian (query, scope, context_lines) identik untuk state
        # source yang sama tidak dijalankan fisik dua kali dalam satu task.
        if cache is not None and cache.claim_search(query, rel_path, context_lines):
            return {
                "query": query,
                "path": rel_path,
                "already_searched": True,
                "search_dedup": True,
                "message": (
                    "ALREADY_SEARCHED: The same search_code(query, path) has "
                    "already been performed for the current source state and its "
                    "result is already in the conversation. Do NOT repeat it; use "
                    "the previous result. Call search_code again only with a "
                    "DIFFERENT query or scope."
                ),
            }

        root_resolved = self.root.resolve()
        matches: List[Dict[str, Any]] = []
        truncated = False
        # Cache symbol per file (dihitung sekali, hanya untuk file yang match).
        spans_by_file: Dict[str, List[Any]] = {}

        def _symbol_of(rel_file: str, text: str, lineno: int) -> Optional[str]:
            spans = spans_by_file.get(rel_file)
            if spans is None:
                # Guard: file sangat besar dilewati (anotasi best-effort saja).
                if len(text) > _DEFAULT_MAX_FILE_BYTES:
                    spans = []
                else:
                    try:
                        spans = extract_symbols(rel_file, text)
                    except Exception:  # noqa: BLE001 - anotasi bersifat best-effort
                        spans = []
                spans_by_file[rel_file] = spans
            if not spans:
                return None
            span = enclosing_symbol(spans, lineno)
            return span.qualified_name if span is not None else None

        files = [base] if base.is_file() else _iter_files(base)
        for file_path in files:
            if len(matches) >= max_results:
                truncated = True
                break
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeError):
                continue
            lines = text.splitlines()
            try:
                rel_file = str(file_path.relative_to(root_resolved))
            except ValueError:
                rel_file = str(file_path)
            for lineno, line in enumerate(lines, start=1):
                if query not in line:
                    continue
                match: Dict[str, Any] = {
                    "file": rel_file,
                    "line": lineno,
                    "text": _clip_match(line, query),
                }
                symbol_name = _symbol_of(rel_file, text, lineno)
                if symbol_name:
                    match["symbol"] = symbol_name
                if context_lines > 0:
                    start = max(1, lineno - context_lines)
                    end = min(len(lines), lineno + context_lines)
                    match["snippet"] = "\n".join(lines[start - 1 : end])
                matches.append(match)
                if len(matches) >= max_results:
                    truncated = True
                    break

        result: Dict[str, Any] = {
            "query": query,
            "path": rel_path,
            "count": len(matches),
            "truncated": truncated,
            "matches": matches,
        }
        if context_lines > 0:
            result["context_lines"] = context_lines
        return result
