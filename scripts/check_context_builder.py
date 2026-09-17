"""Verifikasi Context Builder AETHER.

Membuat fixture project dummy di J:\Agent_Ai\dummy_test (workspace testing
terisolasi, BUKAN bagian source AETHER), membangun Code Index, lalu membangun
context untuk sebuah task. Fixture dibersihkan setelah test.

Jalankan:
    python scripts/check_context_builder.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.codeindex import CodeIndexer  # noqa: E402
from agent_ai.contextbuilder import ContextBuilder, ContextRequest  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "context_fixture"

_FILES = {
    "calculator/calculator.py": (
        "def add(a, b):\n"
        "    return a - b\n"
        "\n"
        "def subtract(a, b):\n"
        "    return a - b\n"
    ),
    "calculator/test_calculator.py": (
        "from calculator import add\n"
        "\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
    ),
    "unrelated/other.py": (
        "def totally_unrelated_thing():\n"
        "    return 1\n"
    ),
    "unrelated/big.py": (
        "def filler():\n"
        "    return '" + ("x" * 5000) + "'\n"
    ),
}


class FakeBrain:
    """Brain palsu untuk menguji integrasi Bible/Brain context."""

    def __init__(self) -> None:
        self.calls = 0

    def get_context(self, categories=None, include_empty=False):
        self.calls += 1

        class _Ctx:
            text = "# Project Intelligence\n## rules\n- Gunakan type hints"

        return _Ctx()


def setup_fixture() -> None:
    for rel, content in _FILES.items():
        path = FIXTURE / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Context Builder AETHER ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    before = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}

    index = CodeIndexer(root=FIXTURE).build()
    brain = FakeBrain()
    builder = ContextBuilder(index=index, root=FIXTURE, brain=brain)

    # 1) task menghasilkan context.
    result = builder.build(ContextRequest(task="Perbaiki bug pada add di calculator"))
    print(f"metadata: {result.metadata}")
    assert result.task and result.files
    print("task menghasilkan context : OK")

    # 2) symbol relevan ditemukan melalui Code Index.
    names = [s["name"] for s in result.symbols]
    assert "add" in names, f"symbol 'add' tidak ditemukan: {names}"
    print(f"symbol relevan : {names}")

    # 3) source file relevan masuk context.
    calc = next((f for f in result.files if f.path == "calculator/calculator.py"), None)
    assert calc is not None and calc.source is not None
    assert "def add" in calc.source
    print(f"source file relevan : {calc.path} (included={calc.source is not None})")

    # 4) Bible/Project Intelligence masuk context.
    assert "Project Intelligence" in result.intelligence
    assert "type hints" in result.intelligence
    print("Bible/Intelligence masuk context : OK")

    # 5) Brain context dapat digunakan.
    assert result.brain and "Project Intelligence" in result.brain
    print("Brain context dapat digunakan : OK")

    # 6) file tidak relevan tidak ikut ketika masih di luar limit.
    paths = [f.path for f in result.files]
    assert "unrelated/other.py" not in paths, f"file tidak relevan ikut: {paths}"
    print(f"file relevan saja : {paths}")

    # 7) context size limit bekerja.
    small = builder.build(
        ContextRequest(task="Perbaiki bug pada add di calculator", max_bytes=50)
    )
    total_bytes = sum(len(f.source.encode("utf-8")) for f in small.files if f.source)
    assert total_bytes <= 50, f"melebihi limit: {total_bytes}"
    assert any(f.truncated for f in small.files), "harus ada file yang dipotong"
    print(f"size limit bekerja : bytes_used={small.metadata['bytes_used']} <= 50")

    # 7b) max_files limit bekerja.
    limited = builder.build(
        ContextRequest(task="add calculator unrelated filler", max_files=1)
    )
    assert len(limited.files) <= 1
    print(f"max_files limit bekerja : files={len(limited.files)}")

    # 8) tidak ada duplicate file.
    assert len(paths) == len(set(paths)), "ada file duplikat"
    print("tidak ada duplicate file : OK")

    # 9) source project tidak berubah (read-only).
    after = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}
    assert before == after, "source project berubah!"
    print("source project tidak berubah : OK")

    # 10) to_text terstruktur.
    text = result.to_text()
    for section in ("# Task", "# Relevant Symbols", "# Relevant Files", "# Metadata"):
        assert section in text, f"section '{section}' hilang"
    print("context terstruktur (to_text) : OK")

    print()
    print("[OK] Context Builder bekerja (relevan, terbatas, read-only, deterministik).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
