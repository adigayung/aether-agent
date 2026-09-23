"""Verifikasi parameter line range pada tool read_file & edit_file.

Menguji (deterministik, memakai fixture temporary):
    read_file:
        1. tanpa range        -> seluruh file (perilaku lama tidak berubah).
        2. start_line+end_line -> tepat baris start..end (1-based, inklusif).
        3. hanya start_line    -> start..akhir file.
        4. hanya end_line      -> awal..end.
        5. boundary            -> start_line=1, end_line=total, start_line=end_line.
        6. invalid             -> start_line/end_line < 1, start_line > end_line,
                                  start_line di luar panjang file, end_line di-clamp.
        7. content_numbered    -> nomor baris tersedia untuk mode range.
    edit_file:
        8. tanpa range         -> perilaku lama (old_text unik) tetap bekerja.
        9. range-targeted      -> edit sukses bila old_text ada di dalam range.
       10. safety fence        -> old_text di luar range GAGAL (tanpa fallback).
       11. ambiguous di range  -> gagal, file tidak berubah.
       12. unik di range tapi   duplikat di luar range -> boleh (range mempersempit).
       13. boundary            -> old_text tepat pada start_line / end_line.
       14. line ending asli    -> CRLF baris lain tidak berubah.
    kontrak:
       15. schema memuat start_line/end_line; tidak ada tool baru.

Jalankan:
    python scripts/check_file_range_tools.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.tools.base import ToolError  # noqa: E402
from agent_ai.tools.filesystem import ReadFileTool  # noqa: E402
from agent_ai.tools.registry import build_registry  # noqa: E402
from agent_ai.tools.workspace import EditFileTool  # noqa: E402

SAMPLE = "line one\nline two\nline three\nline four\nline five\n"


def expect_error(fn, label: str) -> str:
    """Jalankan `fn`; pastikan gagal dengan ToolError. Kembalikan pesan error."""
    try:
        fn()
    except ToolError as exc:
        print(f"  {label} -> ditolak OK ({type(exc).__name__}): {exc}")
        return str(exc)
    raise AssertionError(f"{label} seharusnya gagal (ToolError)")


def main() -> int:
    print("=== Verifikasi Line Range: read_file & edit_file ===")
    ws = Path(tempfile.mkdtemp(prefix="ws_range_"))
    try:
        read = ReadFileTool(root=ws)
        edit = EditFileTool(root=ws)
        (ws / "sample.txt").write_text(SAMPLE, encoding="utf-8")

        # -------------------------------------------------------------- read_file
        print("[read_file]")
        full = read.execute(path="sample.txt")
        assert full["total_lines"] == 5, full
        assert full["content"] == SAMPLE, "full read harus utuh"
        assert "start_line" not in full and "end_line" not in full, "tanpa range -> shape lama"
        print("  1) tanpa range -> seluruh file OK")

        r = read.execute(path="sample.txt", start_line=3, end_line=5)
        assert r["content"] == "line three\nline four\nline five", r["content"]
        assert r["start_line"] == 3 and r["end_line"] == 5 and r["total_lines"] == 5
        assert r["content_numbered"] == "3: line three\n4: line four\n5: line five"
        print("  2) start_line=3,end_line=5 -> tepat baris 3..5 OK")

        r = read.execute(path="sample.txt", start_line=3)
        assert r["content"] == "line three\nline four\nline five", r["content"]
        assert r["start_line"] == 3 and r["end_line"] == 5
        print("  3) hanya start_line=3 -> 3..akhir OK")

        r = read.execute(path="sample.txt", end_line=4)
        assert r["content"] == "line one\nline two\nline three\nline four", r["content"]
        assert r["start_line"] == 1 and r["end_line"] == 4
        print("  4) hanya end_line=4 -> awal..4 OK")

        r = read.execute(path="sample.txt", start_line=1, end_line=5)
        assert r["content"] == "line one\nline two\nline three\nline four\nline five"
        single = read.execute(path="sample.txt", start_line=3, end_line=3)
        assert single["content"] == "line three"
        print("  5) boundary (1..total & start==end) OK")

        expect_error(lambda: read.execute(path="sample.txt", start_line=0), "start_line=0")
        expect_error(lambda: read.execute(path="sample.txt", end_line=0), "end_line=0")
        expect_error(
            lambda: read.execute(path="sample.txt", start_line=4, end_line=2),
            "start_line>end_line",
        )
        expect_error(
            lambda: read.execute(path="sample.txt", start_line=600),
            "start_line di luar panjang file",
        )
        expect_error(
            lambda: read.execute(path="sample.txt", start_line="abc"),
            "start_line bukan integer",
        )
        clamped = read.execute(path="sample.txt", end_line=9999)
        assert clamped["end_line"] == 5 and clamped["total_lines"] == 5
        print("  6) validasi invalid + clamp end_line OK")
        print("  7) content_numbered tersedia pada mode range OK")

        # -------------------------------------------------------------- edit_file
        print("[edit_file]")

        # 8) backward compat: tanpa range.
        (ws / "compat.txt").write_text("hello world\n", encoding="utf-8")
        edit.execute(path="compat.txt", old_text="world", new_text="aether")
        assert (ws / "compat.txt").read_text(encoding="utf-8") == "hello aether\n"
        print("  8) tanpa range -> perilaku lama OK")

        (ws / "dup.txt").write_text("aa aa\n", encoding="utf-8")
        expect_error(
            lambda: edit.execute(path="dup.txt", old_text="aa", new_text="bb"),
            "ambigu tanpa range",
        )
        expect_error(
            lambda: edit.execute(path="dup.txt", old_text="zz", new_text="bb"),
            "tidak ditemukan tanpa range",
        )

        # 9) range-targeted: old_text ada di dalam range.
        (ws / "r.txt").write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
        out = edit.execute(
            path="r.txt", old_text="gamma", new_text="GAMMA", start_line=2, end_line=4
        )
        assert out["edited"] is True and out["replaced"] == 1
        assert out["start_line"] == 2 and out["end_line"] == 4
        assert (ws / "r.txt").read_text(encoding="utf-8") == "alpha\nbeta\nGAMMA\ndelta\n"
        print("  9) range-targeted edit OK")

        # 10) safety fence: old_text di luar range -> GAGAL, file tidak berubah.
        before = (ws / "r.txt").read_text(encoding="utf-8")
        msg = expect_error(
            lambda: edit.execute(
                path="r.txt", old_text="GAMMA", new_text="x", start_line=1, end_line=2
            ),
            "target di luar range",
        )
        assert "range line 1-2" in msg, msg
        assert (ws / "r.txt").read_text(encoding="utf-8") == before, "file berubah padahal harus gagal"
        print(" 10) tidak ada fallback ke seluruh file OK")

        # 11) ambiguous DI DALAM range -> gagal, file tidak berubah.
        (ws / "amb.txt").write_text("t\nmid\nt\ntail\n", encoding="utf-8")
        before = (ws / "amb.txt").read_text(encoding="utf-8")
        expect_error(
            lambda: edit.execute(
                path="amb.txt", old_text="t", new_text="X", start_line=1, end_line=3
            ),
            "ambigu di dalam range",
        )
        assert (ws / "amb.txt").read_text(encoding="utf-8") == before
        print(" 11) ambigu di dalam range tetap ditolak OK")

        # 12) unik di range walau duplikat di luar range -> boleh (range mempersempit).
        (ws / "scope.txt").write_text("t\nmid\nt\n", encoding="utf-8")
        edit.execute(path="scope.txt", old_text="t", new_text="NEW", start_line=1, end_line=1)
        assert (ws / "scope.txt").read_text(encoding="utf-8") == "NEW\nmid\nt\n"
        print(" 12) range mempersempit pencarian (duplikat di luar diabaikan) OK")

        # 13) boundary: old_text tepat pada start_line dan end_line.
        (ws / "b.txt").write_text("A\nB\nC\nD\n", encoding="utf-8")
        edit.execute(path="b.txt", old_text="A", new_text="AA", start_line=1, end_line=2)
        edit.execute(path="b.txt", old_text="D", new_text="DD", start_line=3, end_line=4)
        assert (ws / "b.txt").read_text(encoding="utf-8") == "AA\nB\nC\nDD\n"
        print(" 13) boundary start_line/end_line OK")

        # 14) line ending asli (CRLF) baris lain dipertahankan.
        (ws / "crlf.txt").write_bytes(b"a\r\nb\r\nc\r\n")
        edit.execute(path="crlf.txt", old_text="b", new_text="B", start_line=2, end_line=2)
        assert (ws / "crlf.txt").read_bytes() == b"a\r\nB\r\nc\r\n", (ws / "crlf.txt").read_bytes()
        print(" 14) CRLF dipertahankan OK")

        # -------------------------------------------------------------- kontrak
        print("[kontrak]")
        read_schema = ReadFileTool.input_schema["properties"]
        edit_schema = EditFileTool.input_schema["properties"]
        assert "start_line" in read_schema and "end_line" in read_schema
        assert "start_line" in edit_schema and "end_line" in edit_schema
        assert EditFileTool.input_schema["required"] == ["path", "old_text", "new_text"]
        assert ReadFileTool.input_schema["required"] == ["path"]
        print(" 15) schema read_file & edit_file memuat start_line/end_line OK")

        reg = build_registry(root=ws)
        names = set(reg.list())
        assert "read_file" in names and "edit_file" in names
        banned = {"read_file_range", "edit_file_range", "read_lines", "write_lines"}
        assert not (banned & names), f"tool baru tidak boleh ada: {banned & names}"
        print(f" 16) tidak ada tool baru OK -> read_file & edit_file in-place")

        print()
        print("[OK] Line range read_file & edit_file bekerja (validasi, safety fence, backward compatible).")
        return 0
    finally:
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
