"""Regression tests: dedup retrieval (read_file/search_code) & scope per task.

Fokus (semua deterministik, tanpa network):

    1. read_file rentang IDENTIK kedua -> dedup, TANPA physical read ulang.
    2. read_file rentang yang sudah TERCakup (sub-range) -> skip physical read.
    3. Full read lalu sub-range -> skip physical read (covered).
    4. Tanpa cache (read_cache=None) -> TIDAK ada dedup (backward compatible).
    5. File berubah -> cache invalid -> source terbaru dikirim (bukan stub).
    6. force=True -> paksa kirim ulang (bypass dedup).
    7. Beberapa read paralel identik -> TEPAT satu physical read (in-flight dedup).
    8. search_code query identik kedua -> dedup (tanpa scan ulang).
    9. Scope cache per task: instance BARU (turn berikutnya) -> source dikirim lagi.
   10. Wiring registry: build_registry / build_consultant_registry(investigate)
       memakai SATU cache bersama untuk read_file + search_code.

Jalankan:
    python -m pytest tests/test_read_dedup.py
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.consultant.models import MODE_INVESTIGATE  # noqa: E402
from agent_ai.consultant.tools import build_consultant_registry  # noqa: E402
from agent_ai.tools.filesystem import ReadFileTool, SearchCodeTool  # noqa: E402
from agent_ai.tools.read_cache import ToolReadCache  # noqa: E402
from agent_ai.tools.registry import build_registry  # noqa: E402


def _count_reads(monkeypatch) -> Dict[str, int]:
    """Hitung physical read (Path.read_text) selama test berjalan."""
    counter = {"n": 0}
    original = Path.read_text

    def counting_read(self: Path, *args: Any, **kwargs: Any) -> str:
        counter["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read)
    return counter


# --------------------------------------------------------------------------- #
# 1. Rentang identik -> dedup + tidak physical read ulang
# --------------------------------------------------------------------------- #
def test_identical_range_is_deduped_without_physical_read(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")
    counter = _count_reads(monkeypatch)
    tool = ReadFileTool(root=tmp_path, read_cache=ToolReadCache())

    first = tool.execute(path="a.py", start_line=1, end_line=5)
    assert first["content"] == "l1\nl2\nl3\nl4\nl5"
    reads_after_first = counter["n"]
    assert reads_after_first == 1

    second = tool.execute(path="a.py", start_line=1, end_line=5)
    assert second.get("already_available") is True
    assert second.get("already_read") is True
    assert "content" not in second
    assert "ALREADY_AVAILABLE" in second["message"]
    # Request identik TIDAK memicu physical read lagi.
    assert counter["n"] == reads_after_first


# --------------------------------------------------------------------------- #
# 2. Sub-range yang sudah tercakup -> skip physical read
# --------------------------------------------------------------------------- #
def test_covered_subrange_skips_physical_read(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text(
        "\n".join(f"l{i}" for i in range(1, 21)) + "\n", encoding="utf-8"
    )
    counter = _count_reads(monkeypatch)
    tool = ReadFileTool(root=tmp_path, read_cache=ToolReadCache())

    tool.execute(path="a.py", start_line=1, end_line=10)
    reads = counter["n"]

    out = tool.execute(path="a.py", start_line=3, end_line=5)
    assert out.get("already_available") is True
    assert (out["start_line"], out["end_line"]) == (3, 5)
    # 3-5 tercakup oleh 1-10 -> tidak ada read fisik baru.
    assert counter["n"] == reads


# --------------------------------------------------------------------------- #
# 3. Full read lalu sub-range -> covered
# --------------------------------------------------------------------------- #
def test_full_read_then_subrange_is_covered(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x1\nx2\nx3\nx4\n", encoding="utf-8")
    counter = _count_reads(monkeypatch)
    tool = ReadFileTool(root=tmp_path, read_cache=ToolReadCache())

    tool.execute(path="a.py")  # seluruh file
    reads = counter["n"]

    out = tool.execute(path="a.py", start_line=2, end_line=3)
    assert out.get("already_available") is True
    assert counter["n"] == reads


# --------------------------------------------------------------------------- #
# 4. Tanpa cache -> perilaku lama (tidak ada dedup)
# --------------------------------------------------------------------------- #
def test_without_cache_no_dedup(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    counter = _count_reads(monkeypatch)
    tool = ReadFileTool(root=tmp_path)  # read_cache=None

    a = tool.execute(path="a.py", start_line=1, end_line=1)
    b = tool.execute(path="a.py", start_line=1, end_line=1)
    assert a["content"] == "x"
    assert b["content"] == "x"
    assert "already_available" not in b
    assert counter["n"] == 2


# --------------------------------------------------------------------------- #
# 5. File berubah -> cache invalid -> source terbaru dikirim
# --------------------------------------------------------------------------- #
def test_file_change_invalidates_cache(tmp_path, monkeypatch):
    path = tmp_path / "a.py"
    path.write_text("l1\nl2\nl3\n", encoding="utf-8")
    _count_reads(monkeypatch)
    tool = ReadFileTool(root=tmp_path, read_cache=ToolReadCache())

    tool.execute(path="a.py", start_line=1, end_line=3)

    # Ubah konten + majukan mtime agar signature pasti berbeda.
    path.write_text("z1\nz2\nz3\n", encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    out = tool.execute(path="a.py", start_line=1, end_line=3)
    assert out["content"] == "z1\nz2\nz3"
    assert "already_available" not in out


# --------------------------------------------------------------------------- #
# 6. force=True memaksa kirim ulang
# --------------------------------------------------------------------------- #
def test_force_bypasses_dedup(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("l1\nl2\n", encoding="utf-8")
    counter = _count_reads(monkeypatch)
    tool = ReadFileTool(root=tmp_path, read_cache=ToolReadCache())

    tool.execute(path="a.py", start_line=1, end_line=2)
    out = tool.execute(path="a.py", start_line=1, end_line=2, force=True)
    assert out["content"] == "l1\nl2"
    assert counter["n"] == 2


# --------------------------------------------------------------------------- #
# 7. Read paralel identik -> tepat satu physical read (in-flight dedup)
# --------------------------------------------------------------------------- #
def test_parallel_identical_reads_single_physical_read(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text(
        "".join(f"l{i}\n" for i in range(1, 51)), encoding="utf-8"
    )
    counter = _count_reads(monkeypatch)
    tool = ReadFileTool(root=tmp_path, read_cache=ToolReadCache())

    results: Dict[int, Dict[str, Any]] = {}
    parties = 8
    barrier = threading.Barrier(parties)

    def worker(index: int) -> None:
        barrier.wait()
        results[index] = tool.execute(path="a.py", start_line=1, end_line=20)

    threads = [
        threading.Thread(target=worker, args=(i,)) for i in range(parties)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # In-flight dedup: hanya sekali membaca fisik, sisanya pakai hasil tercatat.
    assert counter["n"] == 1
    assert len(results) == parties
    with_content = [r for r in results.values() if "content" in r]
    stubs = [r for r in results.values() if r.get("already_available")]
    assert len(with_content) == 1
    assert len(stubs) == parties - 1


# --------------------------------------------------------------------------- #
# 8. search_code query identik -> dedup (tanpa scan ulang)
# --------------------------------------------------------------------------- #
def test_search_code_identical_query_is_deduped(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("needle\nother\nneedle\n", encoding="utf-8")
    counter = _count_reads(monkeypatch)
    tool = SearchCodeTool(root=tmp_path, read_cache=ToolReadCache())

    first = tool.execute(query="needle")
    assert first["count"] == 2
    reads = counter["n"]

    second = tool.execute(query="needle")
    assert second.get("already_searched") is True
    assert "matches" not in second
    assert "ALREADY_SEARCHED" in second["message"]
    # Query identik tidak memicu scan file lagi.
    assert counter["n"] == reads


# --------------------------------------------------------------------------- #
# 9. Scope per task: instance cache baru -> source dikirim lagi
# --------------------------------------------------------------------------- #
def test_cache_is_turn_scoped(tmp_path):
    (tmp_path / "a.py").write_text("l1\nl2\n", encoding="utf-8")

    # Giliran 1.
    turn1 = ReadFileTool(root=tmp_path, read_cache=ToolReadCache())
    turn1.execute(path="a.py", start_line=1, end_line=2)

    # Giliran 2: instance cache BARU (fresh) -> tidak mengingat read giliran 1.
    turn2 = ReadFileTool(root=tmp_path, read_cache=ToolReadCache())
    out = turn2.execute(path="a.py", start_line=1, end_line=2)
    assert out["content"] == "l1\nl2"
    assert "already_available" not in out


# --------------------------------------------------------------------------- #
# 10. Wiring registry: read_file + search_code berbagi SATU cache per registry
# --------------------------------------------------------------------------- #
def test_build_registry_shares_single_cache(tmp_path):
    registry = build_registry(root=tmp_path)
    reader = registry.get("read_file")
    searcher = registry.get("search_code")
    assert reader._read_cache is not None
    assert searcher._read_cache is reader._read_cache


def test_consultant_investigate_uses_turn_cache(tmp_path):
    registry = build_consultant_registry(root=tmp_path, mode=MODE_INVESTIGATE)
    reader = registry.get("read_file")
    searcher = registry.get("search_code")
    assert reader._read_cache is not None
    assert searcher._read_cache is reader._read_cache
