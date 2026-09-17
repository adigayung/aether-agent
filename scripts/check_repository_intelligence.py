"""Verifikasi Repository Intelligence (Code Index relations).

Membuat fixture project dummy kecil di J:\Agent_Ai\dummy_test
(workspace testing terisolasi, BUKAN bagian source AETHER), membangun index,
dan memverifikasi relasi antar-code. Fixture dibersihkan setelah test.

Struktur fixture:
    app/
      main.py     -> import service, models
      service.py  -> import models, utils
      models.py
      utils.py

Jalankan:
    python scripts/check_repository_intelligence.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.codeindex import CodeIndexer, RepositoryIntelligence  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "repo_intel_fixture"

_FILES = {
    "app/main.py": (
        "from app.service import Service\n"
        "from app.models import User\n"
        "\n"
        "def run():\n"
        "    svc = Service()\n"
        "    return svc.handle(User())\n"
    ),
    "app/service.py": (
        "from app.models import User\n"
        "from app.utils import helper\n"
        "\n"
        "class Service:\n"
        "    def handle(self, user):\n"
        "        return helper(user.name)\n"
    ),
    "app/models.py": (
        "class User:\n"
        "    def __init__(self, name='x'):\n"
        "        self.name = name\n"
    ),
    "app/utils.py": (
        "def helper(name):\n"
        "    return name.upper()\n"
    ),
    # Relative import fixture.
    "app/pkg/__init__.py": "",
    "app/pkg/consumer.py": (
        "from . import sibling\n"
        "from ..utils import helper\n"
        "\n"
        "def use():\n"
        "    return helper('a')\n"
    ),
    "app/pkg/sibling.py": "def sibling():\n    return 1\n",
}


def setup_fixture() -> None:
    for rel, content in _FILES.items():
        path = FIXTURE / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Repository Intelligence ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    before = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}

    index = CodeIndexer(root=FIXTURE).build()
    ri = RepositoryIntelligence(index)
    print(f"stats: {index.stats()}")
    print()

    # 1) Import resolution (absolute).
    res = {r.module: r for r in ri.resolve_imports(file="app/main.py")}
    assert res["app.service"].resolved and res["app.service"].target == "app/service.py"
    assert res["app.models"].resolved and res["app.models"].target == "app/models.py"
    print(f"[1] import resolution OK -> {[(r.module, r.target) for r in ri.resolve_imports(file='app/main.py')]}")

    # 2) Relative import.
    rel = {r.module: r for r in ri.resolve_imports(file="app/pkg/consumer.py")}
    assert rel[""].resolved and rel[""].target == "app/pkg/sibling.py", rel
    assert rel["utils"].resolved and rel["utils"].target == "app/utils.py", rel
    print(f"[2] relative import OK -> {[(r.module, r.target) for r in ri.resolve_imports(file='app/pkg/consumer.py')]}")

    # 3) dependencies().
    deps = ri.dependencies("app/service.py")
    assert deps == ["app/models.py", "app/utils.py"], deps
    print(f"[3] dependencies('app/service.py') = {deps}")

    # 4) dependents().
    dependents = ri.dependents("app/models.py")
    assert "app/service.py" in dependents and "app/main.py" in dependents, dependents
    print(f"[4] dependents('app/models.py') = {dependents}")

    # 5) symbol references.
    refs = ri.references.find("helper")
    ref_files = sorted({r.file for r in refs})
    assert "app/service.py" in ref_files and "app/pkg/consumer.py" in ref_files, ref_files
    # definisi sendiri tidak dihitung sebagai reference.
    assert not any(r.file == "app/utils.py" and r.line == 1 for r in refs)
    print(f"[5] references('helper') files = {ref_files}")

    # 6) related_symbols().
    related = ri.related_symbols("helper")
    assert related["definitions"] and related["definitions"][0]["file"] == "app/utils.py"
    assert related["references"], "harus ada reference"
    print(f"[6] related_symbols('helper') -> defs={len(related['definitions'])}, refs={len(related['references'])}")

    # 7) graph tidak membuat duplicate.
    graph = ri.graph()
    for path, imports in graph.file_imports.items():
        assert len(imports) == len(set(imports)), f"duplikat imports di {path}"
    for path, refs_list in graph.file_references.items():
        assert len(refs_list) == len(set(refs_list)), f"duplikat references di {path}"
    assert graph.file_imports["app/service.py"] == ["app/models.py", "app/utils.py"]
    assert "Service" in graph.file_contains["app/service.py"]
    print(f"[7] graph OK -> imports={graph.file_imports['app/service.py']}")

    # 8) source project tidak berubah (read-only).
    after = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}
    assert before == after, "source project berubah saat indexing!"
    print("[8] source project tidak berubah : OK")

    # 9) AETHER tetap berjalan tanpa dummy_test.
    from agent_ai.codeindex import CodeIndex  # noqa: E402
    empty = CodeIndex(root="")
    assert empty.dependencies("x") == []
    assert empty.dependents("x") == []
    assert empty.graph()["file_imports"] == {}
    print("[9] AETHER tanpa dummy_test OK -> query pada index kosong aman")

    print()
    print("[OK] Repository Intelligence bekerja (deterministik, read-only, tanpa LLM/embeddings).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
