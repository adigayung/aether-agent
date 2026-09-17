"""Verifikasi Code Index AETHER.

Membuat fixture project dummy multi-bahasa di J:\Agent_Ai\dummy_test
(workspace testing terisolasi, BUKAN bagian source AETHER), membangun index,
dan memverifikasi query/search. Fixture dibersihkan setelah test.

Jalankan:
    python scripts/check_code_index.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.codeindex import CodeIndexer, SymbolKind  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "codeindex_fixture"

_FILES = {
    "app/main.py": (
        "import os\n"
        "from app.utils import helper\n"
        "\n"
        "class Greeter:\n"
        "    def greet(self, name):\n"
        "        return helper(name)\n"
        "\n"
        "def run():\n"
        "    return Greeter().greet('x')\n"
    ),
    "app/utils.py": (
        "def helper(name):\n"
        "    return name.upper()\n"
    ),
    "web/index.js": (
        "import { foo } from './foo';\n"
        "class Widget {\n"
        "  render() { return 1; }\n"
        "}\n"
        "function boot() { return new Widget(); }\n"
    ),
    "web/foo.js": "export function foo() { return 42; }\n",
    "src/Service.php": (
        "<?php\n"
        "use App\\Repo;\n"
        "class Service {\n"
        "  public function handle() { return 1; }\n"
        "}\n"
    ),
    "src/Main.java": (
        "import java.util.List;\n"
        "public class Main {\n"
        "  public void run() { }\n"
        "}\n"
    ),
    "src/Program.cs": (
        "using System;\n"
        "public class Program {\n"
        "  public void Main() { }\n"
        "}\n"
    ),
    "native/lib.c": (
        "#include <stdio.h>\n"
        "int compute(int x) { return x; }\n"
    ),
    # File yang harus diabaikan (di dalam ignored dirs).
    "node_modules/pkg/index.js": "function shouldNotAppear() {}\n",
    "build/out.py": "def should_not_appear():\n    pass\n",
    ".git/hooks/x.py": "def git_hook():\n    pass\n",
}


def setup_fixture() -> None:
    for rel, content in _FILES.items():
        path = FIXTURE / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Code Index AETHER ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    # Snapshot source sebelum indexing (untuk cek read-only).
    before = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}

    index = CodeIndexer(root=FIXTURE).build()
    print(f"stats: {index.stats()}")
    print()

    # 1) menemukan file.
    assert "app/main.py" in index.paths()
    assert "web/index.js" in index.paths()
    print("menemukan file : OK")

    # 2) menemukan class.
    classes = index.find_symbols(name="Greeter", kind=SymbolKind.CLASS)
    assert classes and classes[0].file == "app/main.py"
    print(f"menemukan class : {classes[0].qualified_name} @ {classes[0].file}:{classes[0].line}")

    # 3) menemukan function/method.
    funcs = index.find_symbols(name="run", kind=SymbolKind.FUNCTION)
    assert funcs and funcs[0].file == "app/main.py"
    methods = index.find_symbols(name="greet", kind=SymbolKind.METHOD)
    assert methods and methods[0].parent == "Greeter"
    print(f"menemukan function : run @ {funcs[0].file}:{funcs[0].line}")
    print(f"menemukan method   : {methods[0].qualified_name} @ {methods[0].file}:{methods[0].line}")

    # 4) menemukan import.
    imports = index.find_imports(module="app.utils")
    assert imports and imports[0].file == "app/main.py"
    print(f"menemukan import : {imports[0].module} @ {imports[0].file}:{imports[0].line}")

    # 5) query symbol berdasarkan nama.
    hits = index.find_symbols(name="helper")
    assert any(s.name == "helper" for s in hits)
    print(f"query symbol 'helper' : {[s.qualified_name for s in hits]}")

    # 6) lokasi file + line benar.
    helper = index.find_symbols(name="helper", kind=SymbolKind.FUNCTION)[0]
    assert helper.file == "app/utils.py" and helper.line == 1
    print(f"lokasi benar : {helper.file}:{helper.line}")

    # 7) ignored directories tidak terindex.
    all_paths = index.paths()
    assert not any("node_modules" in p for p in all_paths)
    assert not any(p.startswith("build/") for p in all_paths)
    assert not any(".git" in p for p in all_paths)
    assert not any("shouldNotAppear" in s.name for s in index.all_symbols())
    print("ignored dirs tidak terindex : OK")

    # 8) multi-bahasa terdeteksi.
    langs = index.languages()
    for expected in ("python", "javascript", "php", "java", "csharp", "c"):
        assert expected in langs, f"bahasa {expected} tidak terdeteksi"
    print(f"bahasa terdeteksi : {langs}")

    # 9) source project tidak berubah (read-only).
    after = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}
    assert before == after, "source project berubah saat indexing!"
    print("source project tidak berubah : OK")

    # 10) search gabungan.
    result = index.search("helper")
    assert result["symbols"], "search harus menemukan symbol 'helper'"
    result_import = index.search("app.utils")
    assert result_import["imports"], "search harus menemukan import 'app.utils'"
    print("search gabungan : OK")

    print()
    print("[OK] Code Index bekerja (deterministik, read-only, multi-bahasa).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
