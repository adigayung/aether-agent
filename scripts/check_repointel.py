"""Verifikasi Repository Intelligence v2.

Deterministik. Fixture project di J:\\Agent_Ai\\dummy_test\\repointel_fixture
dan dibersihkan setelah test.

Menguji:
    1. dependency expansion (bounded, forward/backward)
    2. symbol relationship (definition/reference/import/dependency)
    3. cross-language awareness (python + generic)
    4. repository map (files/languages/symbols/imports/relationships/entry points)
    5. relevant dependency query (file & symbol, bounded, deterministik)
    6. incremental/rebuildable (stateless)
    7. context builder integration (dependency expansion)
    8. no LLM/embeddings/vector/db dependency
    9. reuse Code Index (tidak ada indexer kedua)

Jalankan:
    python scripts/check_repointel.py
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
from agent_ai.repointel import (  # noqa: E402
    RelationKind,
    RepositoryIntelligenceV2,
)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "repointel_fixture"

FILES = {
    "app/__init__.py": "",
    "app/main.py": (
        "from app.service import Service\n"
        "from app.utils import helper\n"
        "\n"
        "def run():\n"
        "    s = Service()\n"
        "    return helper(s)\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    run()\n"
    ),
    "app/service.py": (
        "from app.utils import helper\n"
        "\n"
        "class Service:\n"
        "    def execute(self):\n"
        "        return helper(self)\n"
    ),
    "app/utils.py": (
        "def helper(obj):\n"
        "    return obj\n"
        "\n"
        "def unused():\n"
        "    return helper(None)\n"
    ),
    "web/index.js": (
        "import { helper } from './utils';\n"
        "function main() { return helper(); }\n"
    ),
}


def setup_fixture() -> None:
    for rel, content in FILES.items():
        path = FIXTURE / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Repository Intelligence v2 ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    index = CodeIndexer(root=FIXTURE).build()
    ri = RepositoryIntelligenceV2(index)

    # 1) dependency expansion (bounded, forward/backward).
    fwd = ri.expand(["app/main.py"], max_depth=1, direction="forward")
    fwd_paths = {n.path for n in fwd}
    assert "app/main.py" in fwd_paths
    assert "app/service.py" in fwd_paths and "app/utils.py" in fwd_paths, fwd_paths
    # Bounded: max_nodes membatasi hasil.
    bounded = ri.expand(["app/main.py"], max_depth=5, max_nodes=1, direction="forward")
    assert len(bounded) <= 1 + 1, bounded  # seed + maksimal 1 node
    # Backward: siapa yang mengimpor utils.
    bwd = ri.expand(["app/utils.py"], max_depth=1, direction="backward")
    bwd_paths = {n.path for n in bwd}
    assert "app/main.py" in bwd_paths and "app/service.py" in bwd_paths, bwd_paths
    # Deterministik: urut (depth, path).
    keys = [(n.depth, n.path) for n in fwd]
    assert keys == sorted(keys), keys
    print("[1] dependency expansion OK")

    # 2) symbol relationship (definition/reference/import/dependency).
    rels = ri.relations("helper")
    kinds = {r.kind for r in rels}
    assert RelationKind.DEFINITION in kinds, kinds
    assert RelationKind.REFERENCE in kinds, kinds
    assert RelationKind.IMPORT in kinds, kinds
    assert RelationKind.DEPENDENCY in kinds, kinds
    # Definition ada di utils.py.
    defs = [r for r in rels if r.kind == RelationKind.DEFINITION]
    assert any(r.file == "app/utils.py" for r in defs), defs
    print("[2] symbol relationship OK")

    # 3) cross-language awareness (python + generic).
    langs = index.languages()
    assert "python" in langs and "javascript" in langs, langs
    js_entry = index.file("web/index.js")
    assert js_entry is not None and js_entry.language == "javascript"
    print("[3] cross-language awareness OK")

    # 4) repository map.
    rmap = ri.map()
    assert rmap.root
    assert "app/main.py" in rmap.files
    assert "python" in rmap.languages and "javascript" in rmap.languages
    assert any(s["name"] == "helper" for s in rmap.symbols)
    assert any(i["module"] == "app.utils" for i in rmap.imports)
    assert "file_imports" in rmap.relationships
    # Entry points: main.py (nama) + main guard.
    ep_paths = {e.path for e in rmap.entry_points}
    assert "app/main.py" in ep_paths, ep_paths
    # Map tidak berisi source code.
    assert "source" not in rmap.to_dict()
    print("[4] repository map OK")

    # 5) relevant dependency query (file & symbol, bounded, deterministik).
    q_file = ri.query.for_file("app/main.py", max_depth=1, max_files=10)
    assert q_file.target_type == "file"
    assert "app/service.py" in q_file.dependencies
    assert "app/utils.py" in q_file.dependencies
    assert q_file.files  # file terkait
    # Bounded.
    q_bounded = ri.query.for_file("app/main.py", max_depth=5, max_files=2)
    assert len(q_bounded.files) <= 2, q_bounded.files
    assert q_bounded.truncated is True
    # Symbol query.
    q_sym = ri.query.for_symbol("helper", max_files=10)
    assert q_sym.target_type == "symbol"
    assert q_sym.relations
    assert "app/utils.py" in q_sym.files
    # Deterministik: dua kali query sama -> hasil sama.
    assert q_file.to_dict() == ri.query.for_file("app/main.py", max_depth=1, max_files=10).to_dict()
    # File tidak ada -> hasil kosong dengan error metadata.
    q_missing = ri.query.for_file("tidak/ada.py")
    assert q_missing.files == [] and "error" in q_missing.metadata
    print("[5] relevant dependency query OK")

    # 6) incremental/rebuildable (stateless).
    index2 = CodeIndexer(root=FIXTURE).build()
    ri2 = RepositoryIntelligenceV2(index2)
    assert ri.map().to_dict() == ri2.map().to_dict(), "rebuild harus deterministik"
    print("[6] incremental/rebuildable OK")

    # 7) context builder integration (dependency expansion).
    builder = ContextBuilder(index=index, root=FIXTURE, dependency_query=ri.query)
    result = builder.build(ContextRequest(task="run helper", max_files=8, max_bytes=100000))
    assert result.metadata["dependency_expansion"] is True
    selected_paths = {f.path for f in result.files}
    # File yang mengandung symbol 'helper'/'run' + dependency-nya.
    assert "app/utils.py" in selected_paths, selected_paths
    # Tanpa dependency_query -> tidak ada expansion.
    builder_plain = ContextBuilder(index=index, root=FIXTURE)
    result_plain = builder_plain.build(ContextRequest(task="run helper", max_files=8))
    assert result_plain.metadata["dependency_expansion"] is False
    print("[7] context builder integration OK")

    # 8) no LLM/embeddings/vector/db dependency.
    ri_dir = SRC_DIR / "agent_ai" / "repointel"
    files = {p.name for p in ri_dir.glob("*.py")}
    assert files == {"__init__.py", "models.py", "expansion.py", "symbols.py", "map.py", "query.py"}, files
    import ast
    for p in ri_dir.glob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for mod in mods:
                top = mod.split(".")[0].lower()
                for bad in ("openai", "openrouter", "ollama", "deepseek", "sqlite3", "requests", "numpy", "faiss", "chromadb"):
                    assert top != bad, f"{p.name} tidak boleh impor '{mod}'"
    print("[8] no LLM/embeddings/vector/db dependency OK")

    # 9) reuse Code Index (tidak ada indexer kedua).
    for p in ri_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "class CodeIndexer" not in text, f"{p.name} tidak boleh mendefinisikan indexer kedua"
        assert "os.walk" not in text, f"{p.name} tidak boleh melakukan walk filesystem sendiri"
    print("[9] reuse Code Index (tanpa indexer kedua) OK")

    print()
    print("[OK] Repository Intelligence v2 bekerja (expansion, symbol relations, map, query, integrasi).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
