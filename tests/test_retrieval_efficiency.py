"""Regression tests: optimasi efisiensi retrieval/context AETHER.

Menguji (tanpa provider LLM eksternal; semua lokal/fake):

    1.  search_code default = locator-first (hasil ringkas, bukan source panjang).
    2.  read_file(symbol=...) hanya membaca symbol yang diminta.
    3.  read_file(symbol=..., context_lines=N) memberi konteks terbatas.
    4.  read_file(mode="structure") memberi outline tanpa full source.
    5.  duplicate read terdeteksi (scope task/registry).
    6.  duplicate read TIDAK terjadi setelah file berubah (edit_file).
    7.  content_numbered tidak dikirim bila mode="raw".
    8.  edit_file tidak memaksa read ulang (hasil memuat file/rentang/status).
    9.  behavior lama read_file(path) tetap PASS.
    10. behavior existing line-range tetap PASS.
    11. behavior edit_file(start_line/end_line) tetap PASS.
    12. registry Agent/Consultant existing tidak rusak + schema tool jelas.

Jalankan:
    python -m pytest tests/test_retrieval_efficiency.py
atau:
    python tests/test_retrieval_efficiency.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.consultant.tools import build_consultant_registry  # noqa: E402
from agent_ai.tools.base import ToolError  # noqa: E402
from agent_ai.tools.filesystem import ReadFileTool, SearchCodeTool  # noqa: E402
from agent_ai.tools.registry import build_registry  # noqa: E402
from agent_ai.tools.workspace import EditFileTool  # noqa: E402

PY_SAMPLE = (
    "import os\n"
    "\n"
    "def alpha(x):\n"
    "    y = x + 1\n"
    "    return y\n"
    "\n"
    "def beta(x):\n"
    "    z = x * 2\n"
    "    return z\n"
)


def _write(ws: Path, rel: str, text: str) -> Path:
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 1. search_code locator-first
# --------------------------------------------------------------------------- #
def test_search_code_default_is_locator_first(tmp_path: Path) -> None:
    long_tail = "tail_" + ("x" * 400)
    _write(
        tmp_path,
        "app.py",
        "def handler():\n"
        f"    marker_value = 1  # {long_tail}\n"
        "    return marker_value\n",
    )
    tool = SearchCodeTool(root=tmp_path)
    res = tool.execute(query="marker_value")

    assert res["count"] == 2
    first = res["matches"][0]
    assert first["file"].replace("\\", "/") == "app.py"
    assert first["line"] == 2
    # Symbol induk dianotasi -> Agent bisa langsung read_file(symbol=...).
    assert first["symbol"] == "handler"
    # Potongan baris DIBATASI (bukan source panjang mentah).
    assert len(first["text"]) <= 210
    # Tanpa context_lines: tidak ada snippet source.
    assert "snippet" not in first


def test_search_code_context_lines_opt_in(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", PY_SAMPLE)
    tool = SearchCodeTool(root=tmp_path)
    res = tool.execute(query="import os", context_lines=1)
    assert res["context_lines"] == 1
    assert "snippet" in res["matches"][0]
    # Default tetap locator-first tanpa snippet.
    res2 = tool.execute(query="import os")
    assert "snippet" not in res2["matches"][0]


# --------------------------------------------------------------------------- #
# 2 & 3. read_file(symbol=...) + context_lines
# --------------------------------------------------------------------------- #
def test_read_file_symbol_only_reads_symbol(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    tool = ReadFileTool(root=tmp_path)
    out = tool.execute(path="svc.py", symbol="beta")
    assert out["symbol"]["name"] == "beta"
    assert out["symbol"]["qualified_name"] == "beta"
    assert out["start_line"] == 7 and out["end_line"] == 9
    assert out["content"] == "def beta(x):\n    z = x * 2\n    return z"
    assert "import os" not in out["content"]
    assert "def alpha" not in out["content"]


def test_read_file_symbol_with_context_lines(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    tool = ReadFileTool(root=tmp_path)
    out = tool.execute(path="svc.py", symbol="beta", context_lines=1)
    # beta = 7..9; +1 baris konteks -> 6..9.
    assert out["start_line"] == 6 and out["end_line"] == 9
    assert out["context_lines"] == 1
    assert out["content"].endswith("    return z")
    assert "def alpha" not in out["content"]


def test_read_file_symbol_not_found_is_clear_error(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    tool = ReadFileTool(root=tmp_path)
    with pytest.raises(ToolError):
        tool.execute(path="svc.py", symbol="tidak_ada_symbol")


def test_read_file_symbol_ambiguous_requires_qualified(tmp_path: Path) -> None:
    source = (
        "class A:\n"
        "    def run(self):\n"
        "        return 1\n"
        "\n"
        "class B:\n"
        "    def run(self):\n"
        "        return 2\n"
    )
    _write(tmp_path, "dup.py", source)
    tool = ReadFileTool(root=tmp_path)
    with pytest.raises(ToolError):
        tool.execute(path="dup.py", symbol="run")
    out = tool.execute(path="dup.py", symbol="B.run")
    assert out["symbol"]["qualified_name"] == "B.run"
    assert "return 2" in out["content"]


# --------------------------------------------------------------------------- #
# 4. mode="structure"
# --------------------------------------------------------------------------- #
def test_read_file_structure_outline_without_body(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    tool = ReadFileTool(root=tmp_path)
    out = tool.execute(path="svc.py", mode="structure")
    assert "content" not in out  # tidak ada body source
    assert "outline" in out and out["symbol_count"] == 2
    assert "alpha" in out["outline"] and "beta" in out["outline"]
    assert "line 3" in out["outline"] and "line 7" in out["outline"]
    assert "import os" not in out["outline"]


# --------------------------------------------------------------------------- #
# 5. duplicate read terdeteksi (registry = scope task)
# --------------------------------------------------------------------------- #
def test_duplicate_read_detected(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    reg = build_registry(root=tmp_path)
    first = reg.execute("read_file", {"path": "svc.py", "start_line": 1, "end_line": 3})
    assert first["content"].startswith("import os")
    second = reg.execute("read_file", {"path": "svc.py", "start_line": 1, "end_line": 3})
    assert second.get("already_read") is True
    assert "content" not in second
    # force=true tetap boleh mengirim ulang source.
    forced = reg.execute(
        "read_file", {"path": "svc.py", "start_line": 1, "end_line": 3, "force": True}
    )
    assert forced["content"].startswith("import os")
    # Rentang BERBEDA bukan duplicate.
    other = reg.execute("read_file", {"path": "svc.py", "start_line": 1, "end_line": 4})
    assert other.get("content") is not None
    assert not other.get("already_read")


def test_duplicate_read_symbol(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    reg = build_registry(root=tmp_path)
    first = reg.execute("read_file", {"path": "svc.py", "symbol": "alpha"})
    assert first["content"]
    second = reg.execute("read_file", {"path": "svc.py", "symbol": "alpha"})
    assert second.get("already_read") is True


# --------------------------------------------------------------------------- #
# 6. duplicate read TIDAK terjadi setelah file berubah
# --------------------------------------------------------------------------- #
def test_no_duplicate_after_edit(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    reg = build_registry(root=tmp_path)
    reg.execute("read_file", {"path": "svc.py", "start_line": 1, "end_line": 3})
    edit = reg.execute(
        "edit_file",
        {"path": "svc.py", "old_text": "import os", "new_text": "import sys"},
    )
    assert edit["edited"] is True
    after = reg.execute("read_file", {"path": "svc.py", "start_line": 1, "end_line": 3})
    assert not after.get("already_read")
    assert after["content"].startswith("import sys")


# --------------------------------------------------------------------------- #
# 7. mode="raw" -> tanpa content_numbered
# --------------------------------------------------------------------------- #
def test_mode_raw_omits_content_numbered(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    tool = ReadFileTool(root=tmp_path)
    raw = tool.execute(path="svc.py", start_line=3, end_line=5, mode="raw")
    assert "content" in raw and "content_numbered" not in raw
    numbered = tool.execute(path="svc.py", start_line=3, end_line=5, mode="numbered")
    assert "content_numbered" in numbered
    assert numbered["content_numbered"].startswith("3: def alpha(x):")


# --------------------------------------------------------------------------- #
# 8. edit_file tidak memaksa read ulang
# --------------------------------------------------------------------------- #
def test_edit_file_result_is_self_describing(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    tool = EditFileTool(root=tmp_path)
    out = tool.execute(path="svc.py", old_text="import os", new_text="import sys")
    assert out["edited"] is True and out["replaced"] == 1
    assert out["changed_start_line"] == 1 and out["changed_end_line"] == 1
    assert out["total_lines"] == 9
    # File benar-benar berubah (bukti tanpa perlu read ulang).
    assert (tmp_path / "svc.py").read_text(encoding="utf-8").startswith("import sys")


# --------------------------------------------------------------------------- #
# 9 & 10. behavior lama tetap PASS
# --------------------------------------------------------------------------- #
def test_legacy_full_read(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    tool = ReadFileTool(root=tmp_path)
    out = tool.execute(path="svc.py")
    assert out["content"] == PY_SAMPLE
    assert out["total_lines"] == 9
    assert "content_numbered" not in out  # full read: shape lama


def test_legacy_line_range_read(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    tool = ReadFileTool(root=tmp_path)
    out = tool.execute(path="svc.py", start_line=3, end_line=5)
    assert out["content"] == "def alpha(x):\n    y = x + 1\n    return y"
    assert out["start_line"] == 3 and out["end_line"] == 5 and out["total_lines"] == 9
    assert out["content_numbered"] == (
        "3: def alpha(x):\n4:     y = x + 1\n5:     return y"
    )


# --------------------------------------------------------------------------- #
# 11. edit_file(start_line/end_line) tetap PASS
# --------------------------------------------------------------------------- #
def test_legacy_edit_file_with_range(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    tool = EditFileTool(root=tmp_path)
    out = tool.execute(
        path="svc.py",
        old_text="y = x + 1",
        new_text="y = x + 42",
        start_line=3,
        end_line=5,
    )
    assert out["edited"] is True
    assert out["start_line"] == 3 and out["end_line"] == 5
    assert "y = x + 42" in (tmp_path / "svc.py").read_text(encoding="utf-8")
    # Safety fence tetap berlaku: target di luar range gagal.
    with pytest.raises(ToolError):
        tool.execute(
            path="svc.py",
            old_text="import os",
            new_text="import sys",
            start_line=3,
            end_line=5,
        )


# --------------------------------------------------------------------------- #
# 12. registry Agent/Consultant existing tidak rusak + schema jelas
# --------------------------------------------------------------------------- #
def test_agent_registry_unchanged(tmp_path: Path) -> None:
    reg = build_registry(root=tmp_path)
    names = set(reg.list())
    for expected in (
        "list_files",
        "read_file",
        "search_code",
        "write_file",
        "edit_file",
        "delete_file",
        "move_file",
        "run_command",
        "atlas_query",
        "rig_query",
        "project_map_status",
        "refresh_project_map",
    ):
        assert expected in names, f"{expected} hilang dari registry Agent"
    # Tidak ada tool baru yang menggantikan read_file/search_code.
    assert "read_file_range" not in names and "read_lines" not in names

    # Schema read_file menjelaskan pola penggunaan (symbol/structure/mode).
    props = ReadFileTool.input_schema["properties"]
    assert {"path", "start_line", "end_line", "symbol", "context_lines", "mode", "force"} <= set(
        props
    )
    search_props = SearchCodeTool.input_schema["properties"]
    assert "context_lines" in search_props


def test_consultant_registry_unchanged(tmp_path: Path) -> None:
    quick = set(build_consultant_registry(tmp_path, mode="quick").list())
    assert "refresh_project_map" not in quick
    assert "read_file" not in quick  # quick tetap tanpa source tools
    assert "atlas_query" in quick

    investigate = set(build_consultant_registry(tmp_path, mode="investigate").list())
    assert "read_file" in investigate and "search_code" in investigate
    assert "edit_file" not in investigate  # Consultant tetap read-only
    assert "refresh_project_map" not in investigate


# --------------------------------------------------------------------------- #
# Standalone runner (tanpa pytest)
# --------------------------------------------------------------------------- #
def _standalone() -> int:
    import inspect

    checks = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = 0
    for name, func in checks:
        params = [p for p in inspect.signature(func).parameters]
        with tempfile.TemporaryDirectory(prefix="aether_retrieval_") as tmp:
            try:
                func(Path(tmp)) if params else func()
                print(f"[PASS] {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
    print()
    if failures:
        print(f"[FAILED] {failures} test gagal")
        return 1
    print(f"[OK] {len(checks)} test lulus (retrieval efficiency)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_standalone())
