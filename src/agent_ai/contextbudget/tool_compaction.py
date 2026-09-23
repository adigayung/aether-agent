"""Tool Result Compaction: representasi ringkas TERSTRUKTUR untuk hasil tool.

Modul ini MELENGKAPI runtime context compaction (Task 1,
`ConversationHistory.compile_compacted_messages`). Alih-alih memotong konten
hasil tool lama secara BUTA (head/tail), modul ini menghasilkan representasi
ringkas yang MEMPERTAHANKAN informasi penting per tool sehingga:

    - Agent tetap mampu melanjutkan pekerjaan seolah informasi sebelumnya
      masih tersedia;
    - Agent tetap dapat MENGAMBIL ULANG detail (retrieval) lewat tool yang
      SUDAH ADA (read_file/search_code/run_command/...) karena locator
      (path/range/symbol/query/command) ikut dipertahankan.

Prinsip:
    - Deterministik: tanpa LLM/embeddings/network/database.
    - Tidak mengubah nama/skema tool; hanya memadatkan ISI.
    - Identitas pesan (role/tool_call_id/name) TIDAK disentuh oleh modul ini
      (dilakukan pemanggil) sehingga protokol tool calling tetap valid.
    - Fallback aman: konten yang tidak dikenali dipadatkan head/tail, dan
      konten pendek dikembalikan APA ADANYA (tanpa memadatkan yang tak perlu).

Bentuk ringkas per tool:

    read_file            -> path + range/symbol + preview + locator read_file
    search_code          -> query + (file,line,symbol) tiap match + locator
    run_command          -> command + exit_code + status + stdout/stderr penting
    edit_file            -> path + rentang baris berubah + operasi + status
    list_files           -> path + jumlah + entri penting
    atlas_query/rig_query-> query + kind + entity (file+baris) + relasi
    project_map_status   -> status ringkas (atlas/rig/freshness)

Catatan arsitektur: modul ini HANYA memakai standard library sehingga tidak
menambah dependency dan tidak menimbulkan siklus impor.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

#: Penanda DETERMINISTIK bahwa isi telah dipadatkan (tanpa LLM).
MARKER = "[dipadatkan]"

#: Petunjuk retrieval yang disertakan saat memadatkan (bukan instruksi baru):
#: hanya menegaskan bahwa detail dapat diambil ulang lewat tool yang ada.
RETRIEVAL_HINT = "detail dapat diambil ulang via tool bila perlu"

#: Batas default agar representasi ringkas tidak tumbuh liar.
DEFAULT_MAX_CHARS = 2000
DEFAULT_MAX_ITEMS = 8
DEFAULT_SNIPPET_CHARS = 200
DEFAULT_BLOB_CHARS = 500
DEFAULT_HEAD_CHARS = 400
DEFAULT_TAIL_CHARS = 200

#: Penanda section pada pesan kegagalan command (bentuk existing executor).
_ERROR_MARKERS = ("tool execution failed", "exit_code=", "stderr:")

_WS_RE = re.compile(r"\s+")

#: Awalan header representasi ringkas (dipakai juga untuk deteksi idempoten).
_HEADER_PREFIX = "[tool result dipadatkan:"


# --------------------------------------------------------------------------- #
# Helper deterministik
# --------------------------------------------------------------------------- #
def _safe_int(value: Any) -> Optional[int]:
    """Konversi ke int bila memungkinkan (tanpa melempar)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _one_line(value: Any, limit: int) -> str:
    """Ringkas nilai menjadi satu baris terpotong (deterministik)."""
    text = "" if value is None else str(value)
    text = _WS_RE.sub(" ", text).strip()
    if limit > 0 and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _clip_head_tail(text: str, head: int, tail: int) -> str:
    """Potong teks menjadi cuplikan awal + penanda + cuplikan akhir.

    Teks pendek dikembalikan APA ADANYA. Teks panjang dipotong deterministik
    sehingga informasi paling awal (mis. path/status) dan paling akhir (mis.
    error/ringkasan) tetap terlihat.
    """
    text = text or ""
    head = max(int(head), 0)
    tail = max(int(tail), 0)
    if head + tail <= 0 or len(text) <= head + tail:
        return text
    head_part = text[:head].rstrip()
    tail_part = text[-tail:].lstrip() if tail > 0 else ""
    omitted = len(text) - len(head_part) - len(tail_part)
    return (
        f"{head_part}\n… {MARKER} {omitted} karakter dipotong …\n{tail_part}"
    )


def _try_json_dict(raw: str) -> Optional[Dict[str, Any]]:
    """Parse konten menjadi dict JSON bila memang berbentuk objek JSON."""
    text = (raw or "").strip()
    if not text or text[0] != "{":
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _header(tool: str, suffix: Optional[str] = None) -> str:
    """Header deterministik representasi ringkas (memuat penanda compaction)."""
    label = tool or "tool"
    if suffix:
        label = f"{label}:{suffix}"
    return f"{_HEADER_PREFIX} {label}]"


def _kv(label: str, value: Any) -> Optional[str]:
    """Baris 'label: value' (None bila value kosong/None)."""
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return f"{label}: {text}"


def _split_error_sections(text: str) -> Optional[Dict[str, Optional[str]]]:
    """Pisahkan meta/stdout/stderr dari pesan kegagalan command (bila cocok)."""
    lowered = (text or "").lower()
    if not any(marker in lowered for marker in _ERROR_MARKERS):
        return None
    body = text or ""
    stderr_body: Optional[str] = None
    idx = body.find("stderr:")
    if idx != -1:
        stderr_body = body[idx + len("stderr:"):].lstrip("\n")
        body = body[:idx]
    stdout_body: Optional[str] = None
    idx2 = body.find("stdout:")
    if idx2 != -1:
        stdout_body = body[idx2 + len("stdout:"):].lstrip("\n")
        body = body[:idx2]
    return {
        "meta": body.strip("\n"),
        "stdout": stdout_body,
        "stderr": stderr_body,
    }


# --------------------------------------------------------------------------- #
# Hasil compaction
# --------------------------------------------------------------------------- #
@dataclass
class CompactedToolResult:
    """Hasil pemadatan satu tool result (deterministik).

    Attributes:
        tool: nama tool.
        kind: jenis pemadatan (read_file/search_code/run_command/edit_file/
            list_files/map_query/project_map_status/generic/text/verbatim).
        text: konten ringkas siap dipakai sebagai isi pesan role "tool".
        locators: informasi lokasi/identitas untuk RETRIEVAL ulang (path,
            start_line, end_line, symbol, query, command, file, ...).
        raw_chars: panjang konten asli (karakter).
        compact_chars: panjang konten ringkas (karakter).
        truncated: True bila ada bagian yang dipotong/diringkas.
    """

    tool: str
    kind: str
    text: str
    locators: Dict[str, Any] = field(default_factory=dict)
    raw_chars: int = 0
    compact_chars: int = 0
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "kind": self.kind,
            "text": self.text,
            "locators": self.locators,
            "raw_chars": self.raw_chars,
            "compact_chars": self.compact_chars,
            "truncated": self.truncated,
        }


# --------------------------------------------------------------------------- #
# Compactor
# --------------------------------------------------------------------------- #
class ToolResultCompactor:
    """Menghasilkan representasi ringkas terstruktur dari hasil tool.

    Deterministik dan provider-agnostic. Hanya mengubah ISI; pemanggil tetap
    memegang role/tool_call_id/name sehingga protokol tool calling utuh.

    Args:
        max_chars: batas keras panjang representasi ringkas (safety cap).
        max_items: batas jumlah item yang didaftar (match/entry/result).
        snippet_chars: batas panjang satu potongan baris/teks.
        blob_chars: batas panjang blok (stdout/stderr/preview).
        head_chars: cuplikan awal default saat memadatkan teks bebas.
        tail_chars: cuplikan akhir default saat memadatkan teks bebas.
    """

    def __init__(
        self,
        *,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_items: int = DEFAULT_MAX_ITEMS,
        snippet_chars: int = DEFAULT_SNIPPET_CHARS,
        blob_chars: int = DEFAULT_BLOB_CHARS,
        head_chars: int = DEFAULT_HEAD_CHARS,
        tail_chars: int = DEFAULT_TAIL_CHARS,
    ) -> None:
        self.max_chars = max(int(max_chars), 64)
        self.max_items = max(int(max_items), 1)
        self.snippet_chars = max(int(snippet_chars), 16)
        self.blob_chars = max(int(blob_chars), 32)
        self.head_chars = max(int(head_chars), 0)
        self.tail_chars = max(int(tail_chars), 0)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def compact(
        self,
        tool_name: str,
        content: Any,
        *,
        head_chars: Optional[int] = None,
        tail_chars: Optional[int] = None,
    ) -> str:
        """Kembalikan ISI ringkas (string) untuk sebuah hasil tool."""
        return self.compact_result(
            tool_name, content, head_chars=head_chars, tail_chars=tail_chars
        ).text

    def compact_result(
        self,
        tool_name: str,
        content: Any,
        *,
        head_chars: Optional[int] = None,
        tail_chars: Optional[int] = None,
    ) -> CompactedToolResult:
        """Padatkan hasil tool -> CompactedToolResult (deterministik).

        Konten pendek (<= head+tail) dikembalikan APA ADANYA (verbatim). Bila
        representasi terstruktur TIDAK lebih pendek dari konten asli, dipakai
        pemadatan head/tail generik; bila itu pun tidak lebih pendek, konten
        asli dipertahankan. Dengan demikian compaction tidak pernah memperbesar
        konteks. Idempoten: konten yang sudah dipadatkan tidak dipadatkan ulang.
        """
        raw = "" if content is None else str(content)
        raw_len = len(raw)
        hc = self.head_chars if head_chars is None else max(int(head_chars), 0)
        tc = self.tail_chars if tail_chars is None else max(int(tail_chars), 0)
        tool = (tool_name or "").strip()

        # Idempoten: konten yang sudah dipadatkan dikembalikan apa adanya.
        if raw.startswith(_HEADER_PREFIX):
            return CompactedToolResult(
                tool=tool,
                kind="verbatim",
                text=raw,
                locators={},
                raw_chars=raw_len,
                compact_chars=raw_len,
                truncated=False,
            )

        # Konten pendek: tidak perlu dipadatkan -> apa adanya (tanpa marker).
        if raw_len <= hc + tc:
            return CompactedToolResult(
                tool=tool,
                kind="verbatim",
                text=raw,
                locators={},
                raw_chars=raw_len,
                compact_chars=raw_len,
                truncated=False,
            )

        try:
            result = self._build(tool, raw, hc, tc)
        except Exception:  # noqa: BLE001 - compaction tidak boleh menggagalkan task
            result = self._fallback(tool, raw, hc, tc, kind="text")

        # Safety cap.
        if len(result.text) > self.max_chars:
            result.text = _clip_head_tail(
                result.text, self.max_chars // 2, self.max_chars - self.max_chars // 2
            )
            result.truncated = True

        # Pilih representasi TERPENDEK agar compaction tidak pernah memperbesar
        # konteks; utamakan representasi terstruktur bila setara/lebih pendek.
        generic = _fallback_text(raw, hc, tc)
        if len(result.text) >= raw_len:
            if len(generic) < raw_len:
                result = self._fallback(tool, raw, hc, tc, kind="generic")
            else:
                result = CompactedToolResult(
                    tool=tool,
                    kind="verbatim",
                    text=raw,
                    locators=result.locators,
                    raw_chars=raw_len,
                    compact_chars=raw_len,
                    truncated=False,
                )
        elif result.kind == "generic" and len(generic) < len(result.text):
            # Generik head/tail lebih hemat daripada representasi generik dict.
            result = self._fallback(tool, raw, hc, tc, kind="generic")

        result.raw_chars = raw_len
        result.compact_chars = len(result.text)
        result.truncated = result.compact_chars < raw_len
        return result

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #
    def _build(
        self, tool: str, raw: str, hc: int, tc: int
    ) -> CompactedToolResult:
        data = _try_json_dict(raw)
        if data is not None:
            return self._structured(tool, data, raw, hc, tc)
        return self._fallback(tool, raw, hc, tc, kind="text")

    def _structured(
        self,
        tool: str,
        data: Dict[str, Any],
        raw: str,
        hc: int,
        tc: int,
    ) -> CompactedToolResult:
        handler = {
            "read_file": self._read_file,
            "search_code": self._search_code,
            "run_command": self._run_command,
            "edit_file": self._edit_file,
            "list_files": self._list_files,
            "atlas_query": self._map_query,
            "rig_query": self._map_query,
            "project_map_status": self._project_map_status,
        }.get(tool)
        if handler is None:
            return self._generic_dict(tool, data, raw)
        return handler(tool, data, raw, hc, tc)

    # ------------------------------------------------------------------ #
    # read_file
    # ------------------------------------------------------------------ #
    def _read_file(
        self, tool: str, data: Dict[str, Any], raw: str, hc: int, tc: int
    ) -> CompactedToolResult:
        path = data.get("path")
        start = _safe_int(data.get("start_line"))
        end = _safe_int(data.get("end_line"))
        total = _safe_int(data.get("total_lines"))
        symbol = data.get("symbol")
        mode = data.get("mode")

        lines: List[str] = [_header(tool)]
        for label, value in (
            ("path", path),
            ("mode", mode),
        ):
            line = _kv(label, value)
            if line:
                lines.append(line)
        if start is not None and end is not None:
            rng = f"{start}-{end}"
            if total is not None:
                rng += f" / total {total}"
            lines.append(f"lines: {rng}")
        elif total is not None:
            lines.append(f"total_lines: {total}")

        locators: Dict[str, Any] = {"tool": "read_file"}
        if path:
            locators["path"] = path
        if start is not None:
            locators["start_line"] = start
        if end is not None:
            locators["end_line"] = end

        if isinstance(symbol, dict):
            qual = symbol.get("qualified_name") or symbol.get("name")
            kind = symbol.get("kind")
            s_start = _safe_int(symbol.get("start_line"))
            s_end = _safe_int(symbol.get("end_line"))
            pieces = [str(qual)] if qual else []
            if kind:
                pieces.append(f"[{kind}]")
            if s_start is not None and s_end is not None:
                pieces.append(f"lines {s_start}-{s_end}")
            if pieces:
                lines.append("symbol: " + " ".join(pieces))
            if qual:
                locators["symbol"] = qual

        # already_read stub: tanpa konten (hemat), cukup lokasi + pesan.
        if data.get("already_read"):
            msg = data.get("message")
            if msg:
                lines.append(f"already_read: true ({_one_line(msg, self.snippet_chars)})")
            return CompactedToolResult(
                tool=tool, kind="read_file", text="\n".join(lines), locators=locators
            )

        # mode=structure: outline (tanpa body) adalah informasi utama.
        if mode == "structure" or data.get("outline") is not None:
            symbol_count = data.get("symbol_count")
            language = data.get("language")
            if language:
                lines.append(f"language: {language}")
            if symbol_count is not None:
                lines.append(f"symbol_count: {symbol_count}")
            outline = data.get("outline")
            if outline:
                lines.append("outline:")
                lines.append(_clip_head_tail(str(outline), self.blob_chars // 2, self.blob_chars // 2))
            note = data.get("note")
            if note:
                lines.append(f"note: {_one_line(note, self.snippet_chars)}")
            lines.append(
                "retrieval: read_file(path='{}', mode='structure')".format(path or "")
            )
            return CompactedToolResult(
                tool=tool, kind="read_file", text="\n".join(lines), locators=locators
            )

        content = data.get("content")
        if content is not None:
            preview = _clip_head_tail(
                str(content), self.blob_chars, self.blob_chars // 2
            )
            lines.append("preview:")
            lines.append(preview)
        # Locator eksplisit agar Agent bisa read_file ulang rentang/symbol ini.
        if path and start is not None and end is not None:
            lines.append(
                "retrieval: read_file(path='{}', start_line={}, end_line={})".format(
                    path, start, end
                )
            )
        elif path and locators.get("symbol"):
            lines.append(f"retrieval: read_file(path='{path}', symbol='{locators['symbol']}')")
        elif path:
            lines.append(f"retrieval: read_file(path='{path}')")
        return CompactedToolResult(
            tool=tool, kind="read_file", text="\n".join(lines), locators=locators
        )

    # ------------------------------------------------------------------ #
    # search_code
    # ------------------------------------------------------------------ #
    def _search_code(
        self, tool: str, data: Dict[str, Any], raw: str, hc: int, tc: int
    ) -> CompactedToolResult:
        query = data.get("query")
        path = data.get("path")
        count = _safe_int(data.get("count"))
        truncated = data.get("truncated")
        matches = data.get("matches") if isinstance(data.get("matches"), list) else []

        lines: List[str] = [_header(tool)]
        for label, value in (
            ("query", query),
            ("path", path),
        ):
            line = _kv(label, value)
            if line:
                lines.append(line)
        summary = []
        if count is not None:
            summary.append(f"count: {count}")
        if truncated is not None:
            summary.append(f"truncated: {bool(truncated)}")
        if summary:
            lines.append(" ".join(summary))

        files: List[str] = []
        shown = matches[: self.max_items]
        lines.append(f"matches (showing {len(shown)} of {len(matches)}):")
        for match in shown:
            if not isinstance(match, dict):
                continue
            file = match.get("file")
            lineno = _safe_int(match.get("line"))
            symbol = match.get("symbol")
            where = str(file) if file else "?"
            if lineno is not None:
                where += f":{lineno}"
            tag = f" [{symbol}]" if symbol else ""
            text = _one_line(match.get("text"), self.snippet_chars)
            lines.append(f"- {where}{tag} {text}".rstrip())
            if file and file not in files:
                files.append(str(file))
            snippet = match.get("snippet")
            if snippet:
                lines.append("  " + _one_line(snippet, self.snippet_chars))
        if len(matches) > len(shown):
            lines.append(f"- … (+{len(matches) - len(shown)} match lain)")

        locators: Dict[str, Any] = {"tool": "search_code"}
        if query:
            locators["query"] = query
        if path:
            locators["path"] = path
        if files:
            locators["files"] = files
        hint = "retrieval: search_code(query='{}'".format(query or "")
        if path:
            hint += ", path='{}'".format(path)
        hint += ")"
        lines.append(hint)
        return CompactedToolResult(
            tool=tool, kind="search_code", text="\n".join(lines), locators=locators
        )

    # ------------------------------------------------------------------ #
    # run_command
    # ------------------------------------------------------------------ #
    def _run_command(
        self, tool: str, data: Dict[str, Any], raw: str, hc: int, tc: int
    ) -> CompactedToolResult:
        command = data.get("command")
        lines: List[str] = [_header(tool)]
        line = _kv("command", command)
        if line:
            lines.append(line)
        status: List[str] = []
        for key in ("exit_code", "success", "timed_out", "outcome", "duration"):
            if data.get(key) is not None:
                status.append(f"{key}: {data.get(key)}")
        if status:
            lines.append("  ".join(status))
        if data.get("error"):
            lines.append(f"error: {_one_line(data.get('error'), self.snippet_chars)}")
        for stream_key in ("stdout", "stderr"):
            stream = data.get(stream_key)
            if stream is None:
                continue
            body = _clip_head_tail(
                str(stream), self.blob_chars // 2, self.blob_chars // 2
            )
            lines.append(f"{stream_key}:")
            lines.append(body if body else "(kosong)")
        locators: Dict[str, Any] = {"tool": "run_command"}
        if command:
            locators["command"] = command
        if data.get("exit_code") is not None:
            locators["exit_code"] = data.get("exit_code")
        if command:
            lines.append(f"retrieval: run_command(command='{_one_line(command, self.snippet_chars)}')")
        return CompactedToolResult(
            tool=tool, kind="run_command", text="\n".join(lines), locators=locators
        )

    # ------------------------------------------------------------------ #
    # edit_file
    # ------------------------------------------------------------------ #
    def _edit_file(
        self, tool: str, data: Dict[str, Any], raw: str, hc: int, tc: int
    ) -> CompactedToolResult:
        path = data.get("path")
        cs = _safe_int(data.get("changed_start_line"))
        ce = _safe_int(data.get("changed_end_line"))
        total = _safe_int(data.get("total_lines"))
        replaced = data.get("replaced")
        edited = data.get("edited")
        s = _safe_int(data.get("start_line"))
        e = _safe_int(data.get("end_line"))

        lines: List[str] = [_header(tool)]
        for label, value in (
            ("path", path),
            ("operation", "replace" if replaced is not None else None),
            ("replaced", replaced),
            ("edited", edited),
        ):
            line = _kv(label, value)
            if line:
                lines.append(line)
        if cs is not None and ce is not None:
            rng = f"{cs}-{ce}"
            if total is not None:
                rng += f" / total {total}"
            lines.append(f"changed_lines: {rng}")
        if s is not None and e is not None:
            lines.append(f"searched_range: {s}-{e}")

        locators: Dict[str, Any] = {"tool": "edit_file"}
        if path:
            locators["path"] = path
        if cs is not None:
            locators["changed_start_line"] = cs
        if ce is not None:
            locators["changed_end_line"] = ce
        if path and cs is not None and ce is not None:
            lines.append(
                "retrieval: read_file(path='{}', start_line={}, end_line={})".format(
                    path, cs, ce
                )
            )
        elif path:
            lines.append(f"retrieval: read_file(path='{path}')")
        return CompactedToolResult(
            tool=tool, kind="edit_file", text="\n".join(lines), locators=locators
        )

    # ------------------------------------------------------------------ #
    # list_files
    # ------------------------------------------------------------------ #
    def _list_files(
        self, tool: str, data: Dict[str, Any], raw: str, hc: int, tc: int
    ) -> CompactedToolResult:
        path = data.get("path")
        count = _safe_int(data.get("count"))
        truncated = data.get("truncated")
        entries = data.get("entries") if isinstance(data.get("entries"), list) else []

        lines: List[str] = [_header(tool)]
        line = _kv("path", path)
        if line:
            lines.append(line)
        summary = []
        if count is not None:
            summary.append(f"count: {count}")
        if truncated is not None:
            summary.append(f"truncated: {bool(truncated)}")
        if summary:
            lines.append(" ".join(summary))

        shown = entries[: self.max_items]
        lines.append(f"entries (showing {len(shown)} of {len(entries)}):")
        for entry in shown:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            etype = entry.get("type")
            size = entry.get("size")
            suffix = "/" if etype == "dir" else ""
            extra = f" ({size} B)" if isinstance(size, int) and etype != "dir" else ""
            lines.append(f"- {name}{suffix}{extra}")
        if len(entries) > len(shown):
            lines.append(f"- … (+{len(entries) - len(shown)} entri lain)")

        locators: Dict[str, Any] = {"tool": "list_files"}
        if path:
            locators["path"] = path
        lines.append(f"retrieval: list_files(path='{path or '.'}')")
        return CompactedToolResult(
            tool=tool, kind="list_files", text="\n".join(lines), locators=locators
        )

    # ------------------------------------------------------------------ #
    # atlas_query / rig_query
    # ------------------------------------------------------------------ #
    def _map_query(
        self, tool: str, data: Dict[str, Any], raw: str, hc: int, tc: int
    ) -> CompactedToolResult:
        query = data.get("query")
        kind = data.get("kind")
        relation = data.get("relation")
        total = _safe_int(data.get("total"))
        returned = _safe_int(data.get("returned"))
        truncated = data.get("truncated")
        map_status = data.get("map_status")
        results = data.get("results") if isinstance(data.get("results"), list) else []

        lines: List[str] = [_header(tool)]
        for label, value in (
            ("query", query),
            ("kind", kind),
            ("relation", relation),
            ("map_status", map_status),
        ):
            line = _kv(label, value)
            if line:
                lines.append(line)
        summary = []
        if total is not None:
            summary.append(f"total: {total}")
        if returned is not None:
            summary.append(f"returned: {returned}")
        if truncated is not None:
            summary.append(f"truncated: {bool(truncated)}")
        if summary:
            lines.append(" ".join(summary))

        shown = results[: self.max_items]
        lines.append(f"results (showing {len(shown)} of {len(results)}):")
        item_locators: List[Dict[str, Any]] = []
        for item in shown:
            if not isinstance(item, dict):
                continue
            ekind = item.get("kind")
            name = item.get("qualified") or item.get("name")
            file = item.get("file")
            ls = _safe_int(item.get("line_start"))
            le = _safe_int(item.get("line_end"))
            where = ""
            if file:
                where = f" @ {file}"
                if ls is not None:
                    where += f":{ls}"
                if le is not None:
                    where += f"-{le}"
            prefix = f"- {ekind} " if ekind else "- "
            lines.append(f"{prefix}{name}{where}".rstrip())
            item_locator: Dict[str, Any] = {}
            if name:
                item_locator["name"] = name
            if file:
                item_locator["file"] = file
            if ls is not None:
                item_locator["line_start"] = ls
            if item_locator:
                item_locators.append(item_locator)
            related = item.get("related")
            if isinstance(related, list) and related:
                rel_bits = []
                for rel in related[: self.max_items]:
                    if not isinstance(rel, dict):
                        continue
                    rel_name = rel.get("qualified") or rel.get("name")
                    edge = rel.get("edge") or rel.get("direction")
                    if rel_name:
                        rel_bits.append(f"{edge}:{rel_name}" if edge else str(rel_name))
                if rel_bits:
                    lines.append("  related: " + ", ".join(rel_bits))
        if len(results) > len(shown):
            lines.append(f"- … (+{len(results) - len(shown)} hasil lain)")

        for key in ("hint", "message"):
            if data.get(key):
                lines.append(f"{key}: {_one_line(data.get(key), self.snippet_chars)}")

        locators: Dict[str, Any] = {"tool": tool}
        if query:
            locators["query"] = query
        if kind:
            locators["kind"] = kind
        if relation:
            locators["relation"] = relation
        if item_locators:
            locators["results"] = item_locators
        hint = f"retrieval: {tool}(query='{query or ''}'"
        if kind:
            hint += f", kind='{kind}'"
        hint += ")"
        lines.append(hint)
        return CompactedToolResult(
            tool=tool, kind="map_query", text="\n".join(lines), locators=locators
        )

    # ------------------------------------------------------------------ #
    # project_map_status
    # ------------------------------------------------------------------ #
    def _project_map_status(
        self, tool: str, data: Dict[str, Any], raw: str, hc: int, tc: int
    ) -> CompactedToolResult:
        lines: List[str] = [_header(tool)]
        for key in (
            "project",
            "overall",
            "atlas",
            "rig",
            "freshness",
            "atlas_freshness",
            "rig_freshness",
            "stale",
            "invalid",
            "missing",
            "reasons",
            "errors",
        ):
            if data.get(key) is not None:
                lines.append(f"{key}: {_one_line(data.get(key), self.snippet_chars)}")
        locators: Dict[str, Any] = {"tool": tool, "freshness": data.get("freshness")}
        return CompactedToolResult(
            tool=tool, kind="project_map_status", text="\n".join(lines), locators=locators
        )

    # ------------------------------------------------------------------ #
    # Generic structured dict (tool tak dikenal tetapi hasilnya dict JSON)
    # ------------------------------------------------------------------ #
    def _generic_dict(
        self, tool: str, data: Dict[str, Any], raw: str
    ) -> CompactedToolResult:
        lines: List[str] = [_header(tool)]
        locators: Dict[str, Any] = {"tool": tool}
        for key, value in data.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                if value is None:
                    continue
                text = value if isinstance(value, (int, float, bool)) else _one_line(value, self.snippet_chars)
                lines.append(f"{key}: {text}")
                if key in ("path", "query", "command", "file") and value:
                    locators[key] = value
            elif isinstance(value, list):
                lines.append(f"{key}: [{len(value)} item]")
            elif isinstance(value, dict):
                lines.append(f"{key}: {{{len(value)} field}}")
        return CompactedToolResult(
            tool=tool, kind="generic", text="\n".join(lines), locators=locators
        )

    # ------------------------------------------------------------------ #
    # Fallback (teks bebas / pesan error / command failure)
    # ------------------------------------------------------------------ #
    def _fallback(
        self, tool: str, raw: str, hc: int, tc: int, *, kind: str = "text"
    ) -> CompactedToolResult:
        sections = _split_error_sections(raw)
        if sections is not None and kind != "generic":
            text = self._format_error_sections(tool, sections, hc, tc)
            return CompactedToolResult(
                tool=tool, kind="run_command_error", text=text, locators={"tool": tool}
            )
        return CompactedToolResult(
            tool=tool, kind=kind, text=_fallback_text(raw, hc, tc), locators={"tool": tool}
        )

    def _format_error_sections(
        self, tool: str, sections: Dict[str, Optional[str]], hc: int, tc: int
    ) -> str:
        """Format pesan kegagalan command: meta utuh, stdout/stderr dipadatkan.

        stderr SELALU dipertahankan (bagian penting) dengan cuplikan awal+akhir
        sehingga pesan error tidak hilang meski output besar.
        """
        lines: List[str] = [_header(tool, "error")]
        meta = sections.get("meta") or ""
        if meta:
            lines.append(_clip_head_tail(meta, self.blob_chars, self.blob_chars // 2))
        for key in ("stdout", "stderr"):
            body = sections.get(key)
            if body is None:
                continue
            lines.append(f"{key}:")
            lines.append(_clip_head_tail(body, self.blob_chars // 2, self.blob_chars // 2))
        return "\n".join(lines)


def _fallback_text(raw: str, hc: int, tc: int) -> str:
    """Teks fallback head/tail (dipakai juga untuk pemilihan ukuran terpendek)."""
    return _clip_head_tail(raw, hc, tc)


__all__ = [
    "MARKER",
    "RETRIEVAL_HINT",
    "CompactedToolResult",
    "ToolResultCompactor",
]
