"""Verifikasi workspace write tools.

Menguji write_file, edit_file, delete_file, move_file beserta boundary workspace.

Jalankan:
    python scripts/check_workspace_write_tools.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.tools import (  # noqa: E402
    DeleteFileTool,
    EditFileTool,
    MoveFileTool,
    ReadFileTool,
    ToolError,
    WriteFileTool,
)


def expect_error(fn, label):
    try:
        fn()
    except ToolError as exc:
        print(f"{label} -> ditolak OK ({type(exc).__name__})")
        return
    raise AssertionError(f"{label} seharusnya ditolak")


def main() -> int:
    print("=== Verifikasi Workspace Write Tools ===")
    ws = Path(tempfile.mkdtemp(prefix="ws_write_"))
    outside = Path(tempfile.mkdtemp(prefix="ws_outside_"))
    try:
        write = WriteFileTool(root=ws)
        edit = EditFileTool(root=ws)
        delete = DeleteFileTool(root=ws)
        move = MoveFileTool(root=ws)
        read = ReadFileTool(root=ws)

        # File di luar workspace (harus tidak tersentuh).
        outside_file = outside / "secret.txt"
        outside_file.write_text("rahasia", encoding="utf-8")

        # 1) write berhasil (termasuk nested parent directory).
        r = write.execute(path="src/app/main.py", content="print('hi')\n")
        print(f"write : {r}")
        assert (ws / "src" / "app" / "main.py").read_text(encoding="utf-8") == "print('hi')\n"

        # 2) edit berhasil.
        edit.execute(path="src/app/main.py", old_text="hi", new_text="hello")
        assert (ws / "src" / "app" / "main.py").read_text(encoding="utf-8") == "print('hello')\n"
        print("edit  : OK")

        # 3) edit target tidak ditemukan -> gagal.
        expect_error(
            lambda: edit.execute(path="src/app/main.py", old_text="tidak-ada", new_text="x"),
            "edit target tidak ditemukan",
        )

        # 4) edit target ambigu -> gagal.
        write.execute(path="dup.txt", content="aa aa")
        expect_error(
            lambda: edit.execute(path="dup.txt", old_text="aa", new_text="bb"),
            "edit target ambigu",
        )

        # 5) delete file berhasil.
        delete.execute(path="dup.txt")
        assert not (ws / "dup.txt").exists()
        print("delete file : OK")

        # 6) delete directory berhasil (recursive).
        write.execute(path="dir/sub/a.txt", content="a")
        write.execute(path="dir/sub/b.txt", content="b")
        delete.execute(path="dir")
        assert not (ws / "dir").exists()
        print("delete directory : OK")

        # 7) move/rename file berhasil.
        write.execute(path="old.txt", content="data")
        move.execute(source="old.txt", destination="new.txt")
        assert not (ws / "old.txt").exists() and (ws / "new.txt").read_text(encoding="utf-8") == "data"
        print("move file : OK")

        # 8) move directory berhasil.
        write.execute(path="d1/x.txt", content="x")
        move.execute(source="d1", destination="d2")
        assert not (ws / "d1").exists() and (ws / "d2" / "x.txt").exists()
        print("move directory : OK")

        # 9) destination yang sudah ada -> gagal.
        write.execute(path="a.txt", content="a")
        write.execute(path="b.txt", content="b")
        expect_error(
            lambda: move.execute(source="a.txt", destination="b.txt"),
            "destination sudah ada",
        )

        # 10) workspace root tidak bisa dihapus/dipindahkan.
        expect_error(lambda: delete.execute(path="."), "delete workspace root")
        expect_error(lambda: move.execute(source=".", destination="moved"), "move workspace root")

        # 11) traversal keluar root ditolak.
        expect_error(lambda: write.execute(path="../evil.txt", content="x"), "write traversal")
        expect_error(lambda: delete.execute(path="../evil.txt"), "delete traversal")
        expect_error(
            lambda: move.execute(source="a.txt", destination="../evil.txt"),
            "move traversal destination",
        )

        # 12) nested directory aman.
        write.execute(path="deep/a/b/c/d.txt", content="deep")
        assert (ws / "deep" / "a" / "b" / "c" / "d.txt").read_text(encoding="utf-8") == "deep"
        print("nested directory : OK")

        # 13) file di luar workspace tidak tersentuh.
        assert outside_file.read_text(encoding="utf-8") == "rahasia"
        print("file luar workspace tidak tersentuh : OK")

        # 14) existing read-only tools tetap bekerja.
        listing = read.execute(path="new.txt")
        assert "data" in str(listing)
        print("read-only tool tetap bekerja : OK")

        print()
        print("[OK] Workspace write tools bekerja (boundary enforced, autonomous).")
        return 0
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Pytest entry point (dipakai scripts/test_critical.bat).
#
# Mengunci kontrak keamanan yang sama dengan verifier di atas dalam bentuk
# assertion pytest: boundary workspace + isolasi antar-root. Tidak ada test
# yang dilewati/dinonaktifkan di sini (semua skenario verifier dijalankan).
# --------------------------------------------------------------------------- #
@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture()
def outside(tmp_path):
    out = tmp_path / "outside"
    out.mkdir()
    (out / "secret.txt").write_text("rahasia", encoding="utf-8")
    return out


def test_write_edit_delete_move_roundtrip(workspace) -> None:
    write, edit = WriteFileTool(root=workspace), EditFileTool(root=workspace)
    delete, move = DeleteFileTool(root=workspace), MoveFileTool(root=workspace)

    write.execute(path="src/app/main.py", content="print('hi')\n")
    assert (workspace / "src" / "app" / "main.py").read_text(encoding="utf-8") == "print('hi')\n"

    edit.execute(path="src/app/main.py", old_text="hi", new_text="hello")
    assert (workspace / "src" / "app" / "main.py").read_text(encoding="utf-8") == "print('hello')\n"

    write.execute(path="old.txt", content="data")
    move.execute(source="old.txt", destination="new.txt")
    assert not (workspace / "old.txt").exists()
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "data"

    delete.execute(path="new.txt")
    assert not (workspace / "new.txt").exists()


def test_delete_directory_recursive_and_move_directory(workspace) -> None:
    write, delete, move = (
        WriteFileTool(root=workspace),
        DeleteFileTool(root=workspace),
        MoveFileTool(root=workspace),
    )
    write.execute(path="dir/sub/a.txt", content="a")
    write.execute(path="dir/sub/b.txt", content="b")
    delete.execute(path="dir")
    assert not (workspace / "dir").exists()

    write.execute(path="d1/x.txt", content="x")
    move.execute(source="d1", destination="d2")
    assert not (workspace / "d1").exists()
    assert (workspace / "d2" / "x.txt").exists()


def test_edit_missing_and_ambiguous_target_rejected(workspace) -> None:
    write, edit = WriteFileTool(root=workspace), EditFileTool(root=workspace)
    write.execute(path="src/app/main.py", content="print('hi')\n")
    with pytest.raises(ToolError):
        edit.execute(path="src/app/main.py", old_text="tidak-ada", new_text="x")

    write.execute(path="dup.txt", content="aa aa")
    with pytest.raises(ToolError):
        edit.execute(path="dup.txt", old_text="aa", new_text="bb")


def test_move_destination_exists_rejected(workspace) -> None:
    write, move = WriteFileTool(root=workspace), MoveFileTool(root=workspace)
    write.execute(path="a.txt", content="a")
    write.execute(path="b.txt", content="b")
    with pytest.raises(ToolError):
        move.execute(source="a.txt", destination="b.txt")


def test_workspace_root_cannot_be_deleted_or_moved(workspace) -> None:
    delete, move = DeleteFileTool(root=workspace), MoveFileTool(root=workspace)
    with pytest.raises(ToolError):
        delete.execute(path=".")
    with pytest.raises(ToolError):
        move.execute(source=".", destination="moved")


def test_traversal_outside_root_rejected(workspace) -> None:
    write, delete, move = (
        WriteFileTool(root=workspace),
        DeleteFileTool(root=workspace),
        MoveFileTool(root=workspace),
    )
    write.execute(path="a.txt", content="a")
    with pytest.raises(ToolError):
        write.execute(path="../evil.txt", content="x")
    with pytest.raises(ToolError):
        delete.execute(path="../evil.txt")
    with pytest.raises(ToolError):
        move.execute(source="a.txt", destination="../evil.txt")


def test_nested_directories_and_outside_files_untouched(workspace, outside) -> None:
    write, read = WriteFileTool(root=workspace), ReadFileTool(root=workspace)
    write.execute(path="deep/a/b/c/d.txt", content="deep")
    assert (workspace / "deep" / "a" / "b" / "c" / "d.txt").read_text(encoding="utf-8") == "deep"

    # File di luar workspace tidak tersentuh (isolasi antarproyek).
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "rahasia"

    # Tool read-only existing tetap bekerja.
    assert "deep" in str(read.execute(path="deep/a/b/c/d.txt"))


if __name__ == "__main__":
    raise SystemExit(main())
