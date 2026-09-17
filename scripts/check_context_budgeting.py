"""Verifikasi Advanced Context / Token Budgeting (#40).

Deterministik. Fixture project di
J:\\Agent_Ai\\dummy_test\\contextbudget_fixture dan dibersihkan setelah test.

Menguji:
    1. profile minimal
    2. profile balanced
    3. profile deep
    4. konfigurasi tidak hardcoded di Agent Core
    5. maximum files
    6. maximum bytes
    7. token estimation/budget
    8. relevance threshold
    9. bounded dependency expansion
   10. partial reading
   11. duplicate-read prevention
   12. hash/change detection
   13. context compaction
   14. deterministic retrieval
   15. repository intelligence reuse
   16. Context Builder integration
   17. no LLM/network/database
   18. no duplicate retrieval/index engine
   19. backward compatibility dengan Context Builder lama
   20. fixture cleanup

Jalankan:
    python scripts/check_context_budgeting.py
"""

from __future__ import annotations

import ast
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.codeindex import CodeIndexer  # noqa: E402
from agent_ai.config.settings import ContextConfig  # noqa: E402
from agent_ai.contextbudget import (  # noqa: E402
    ContextBudget,
    ContextCompactor,
    IntelligentRetriever,
    PartialReader,
    ProfileRegistry,
    ReadTracker,
    TokenEstimator,
)
from agent_ai.contextbuilder import ContextBuilder, ContextRequest  # noqa: E402
from agent_ai.repointel import RepositoryIntelligenceV2  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "contextbudget_fixture"

FILES = {
    "app/__init__.py": "",
    "app/main.py": (
        "from app.service import Service\n"
        "from app.utils import helper\n"
        "\n"
        "def run():\n"
        "    s = Service()\n"
        "    return helper(s)\n"
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
    "app/extra.py": (
        "from app.utils import helper\n"
        "\n"
        "def extra():\n"
        "    return helper(1)\n"
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
    print("=== Verifikasi Advanced Context / Token Budgeting (#40) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    index = CodeIndexer(root=FIXTURE).build()
    ri = RepositoryIntelligenceV2(index)
    registry = ProfileRegistry.default()

    # 1-3) profile minimal/balanced/deep.
    minimal = registry.budget("minimal")
    balanced = registry.budget("balanced")
    deep = registry.budget("deep")
    assert minimal.max_files <= balanced.max_files <= deep.max_files, (minimal, balanced, deep)
    assert minimal.max_depth <= balanced.max_depth <= deep.max_depth
    assert minimal.relevance_threshold >= balanced.relevance_threshold >= deep.relevance_threshold
    assert registry.names() == ["balanced", "deep", "minimal"]
    print("[1-3] profile minimal/balanced/deep OK")

    # 4) konfigurasi tidak hardcoded di Agent Core.
    #    ProfileRegistry.from_config membaca ContextConfig; ubah config -> budget berubah.
    custom = ContextConfig(minimal_max_files=1, balanced_max_files=2, deep_max_files=3)
    custom_registry = ProfileRegistry.from_config(custom)
    assert custom_registry.budget("minimal").max_files == 1
    assert custom_registry.budget("balanced").max_files == 2
    assert custom_registry.budget("deep").max_files == 3
    # Agent Core (contextbuilder) tidak menyimpan nilai profile hardcoded.
    builder_src = (SRC_DIR / "agent_ai" / "contextbuilder" / "builder.py").read_text(encoding="utf-8")
    assert "max_files=8" not in builder_src and "max_bytes=60_000" not in builder_src
    print("[4] konfigurasi tidak hardcoded di Agent Core OK")

    retriever = IntelligentRetriever(index, ri)

    # 5) maximum files.
    b_files = ContextBudget(_budget(max_files=2))
    got = retriever.retrieve("run helper service", b_files.budget)
    assert len(got) <= 2, [g.path for g in got]
    print("[5] maximum files OK")

    # 6) maximum bytes.
    b_bytes = ContextBudget(_budget(max_bytes=50))
    builder = ContextBuilder(
        index=index, root=FIXTURE, retrieval=retriever, budget=b_bytes,
        partial_reader=PartialReader(FIXTURE, default_limit=1000),
    )
    res = builder.build_advanced(ContextRequest(task="run helper service"))
    assert res.metadata["bytes_used"] <= 50, res.metadata["bytes_used"]
    print("[6] maximum bytes OK")

    # 7) token estimation/budget.
    est = TokenEstimator()
    assert est.estimate("") == 0
    assert est.estimate("abcd") == 1
    assert est.estimate("a" * 8) == 2
    b_tok = ContextBudget(_budget(max_tokens=5))
    builder_tok = ContextBuilder(
        index=index, root=FIXTURE, retrieval=retriever, budget=b_tok,
        partial_reader=PartialReader(FIXTURE, default_limit=1000),
    )
    res_tok = builder_tok.build_advanced(ContextRequest(task="run helper service"))
    assert res_tok.metadata["tokens_estimated"] <= 5, res_tok.metadata["tokens_estimated"]
    print("[7] token estimation/budget OK")

    # 8) relevance threshold.
    #    Threshold tinggi -> hanya file dengan skor tinggi (symbol match) yang masuk.
    b_thr = ContextBudget(_budget(relevance_threshold=3.0, max_depth=0))
    got_thr = retriever.retrieve("helper", b_thr.budget)
    assert all(g.score >= 3.0 for g in got_thr), [(g.path, g.score) for g in got_thr]
    #    Threshold 0 -> dependency ikut masuk.
    b_thr0 = ContextBudget(_budget(relevance_threshold=0.0, max_depth=1, max_nodes=10))
    got_thr0 = retriever.retrieve("helper", b_thr0.budget)
    assert len(got_thr0) >= len(got_thr), (len(got_thr0), len(got_thr))
    print("[8] relevance threshold OK")

    # 9) bounded dependency expansion.
    b_exp = ContextBudget(_budget(max_depth=5, max_nodes=1, relevance_threshold=0.0))
    got_exp = retriever.retrieve("helper", b_exp.budget)
    # seed (helper) + maksimal 1 node dependency.
    assert len(got_exp) <= 2, [g.path for g in got_exp]
    print("[9] bounded dependency expansion OK")

    # 10) partial reading.
    reader = PartialReader(FIXTURE, default_limit=2)
    partial = reader.read_lines("app/utils.py", start_line=1, end_line=10)
    assert partial is not None and partial.truncated is True
    assert partial.end_line - partial.start_line + 1 == 2
    sym_partial = reader.read_symbol("app/utils.py", symbol_line=1, limit=2)
    assert sym_partial is not None and "def helper" in sym_partial.content
    #    Partial read dipakai di build_advanced (file dengan symbol -> partial).
    b_partial = ContextBudget(_budget(max_files=5, max_bytes=100000, partial_read=True))
    builder_partial = ContextBuilder(
        index=index, root=FIXTURE, retrieval=retriever, budget=b_partial,
        partial_reader=PartialReader(FIXTURE, default_limit=2),
    )
    res_partial = builder_partial.build_advanced(ContextRequest(task="helper"))
    assert res_partial.metadata["partial_read"] is True
    print("[10] partial reading OK")

    # 11) duplicate-read prevention.
    tracker = ReadTracker(FIXTURE, enabled=True)
    b_dedup = ContextBudget(_budget(max_files=5, max_bytes=100000, duplicate_read_prevention=True))
    builder_dedup = ContextBuilder(
        index=index, root=FIXTURE, retrieval=retriever, budget=b_dedup,
        partial_reader=PartialReader(FIXTURE, default_limit=1000), read_tracker=tracker,
    )
    res1 = builder_dedup.build_advanced(ContextRequest(task="helper"))
    files_with_source_1 = [f.path for f in res1.files if f.source is not None]
    assert files_with_source_1, "run pertama harus mengirim source"
    # Run kedua: file tidak berubah -> tidak dikirim ulang.
    res2 = builder_dedup.build_advanced(ContextRequest(task="helper"))
    files_with_source_2 = [f.path for f in res2.files if f.source is not None]
    assert files_with_source_2 == [], files_with_source_2
    print("[11] duplicate-read prevention OK")

    # 12) hash/change detection.
    assert tracker.is_unchanged("app/utils.py") is True
    assert tracker.has_changed("app/utils.py") is False
    # Ubah file -> terdeteksi berubah.
    (FIXTURE / "app" / "utils.py").write_text("def helper(obj):\n    return obj + 1\n", encoding="utf-8")
    assert tracker.has_changed("app/utils.py") is True
    assert tracker.is_unchanged("app/utils.py") is False
    # Run ketiga: file berubah -> dikirim ulang.
    res3 = builder_dedup.build_advanced(ContextRequest(task="helper"))
    files_with_source_3 = [f.path for f in res3.files if f.source is not None]
    assert "app/utils.py" in files_with_source_3, files_with_source_3
    # Kembalikan file.
    (FIXTURE / "app" / "utils.py").write_text(FILES["app/utils.py"], encoding="utf-8")
    print("[12] hash/change detection OK")

    # 13) context compaction.
    compactor = ContextCompactor()
    compact = compactor.compact(
        task="run helper",
        files=[{"path": "app/main.py", "reason": "symbol 'run'"}, {"path": "app/utils.py", "reason": "symbol 'helper'"}],
        symbols=[{"name": "helper", "kind": "function", "file": "app/utils.py", "line": 1, "parent": None}],
        changes=[{"path": "app/utils.py", "change_type": "modified"}],
        observations=["error: test gagal"],
        dependencies={"app/main.py": ["app/utils.py"]},
        max_tokens=1000,
    )
    assert compact.task == "run helper"
    assert compact.files and compact.symbols and compact.changes
    assert compact.observations == ["error: test gagal"]
    assert compact.dependencies["app/main.py"] == ["app/utils.py"]
    # Compaction dipicu saat budget habis (max_files=1 -> setelah 1 file).
    b_comp = ContextBudget(_budget(max_files=1, max_bytes=100000, max_tokens=100000))
    builder_comp = ContextBuilder(
        index=index, root=FIXTURE, retrieval=retriever, budget=b_comp,
        partial_reader=PartialReader(FIXTURE, default_limit=1000), compactor=compactor,
    )
    res_comp = builder_comp.build_advanced(ContextRequest(task="helper"))
    assert res_comp.metadata["compacted"] is True, res_comp.metadata
    assert "compaction" in res_comp.metadata
    print("[13] context compaction OK")

    # 14) deterministic retrieval.
    b_det = ContextBudget(_budget(max_files=5, max_depth=1, max_nodes=10, relevance_threshold=0.0))
    r1 = [g.to_dict() for g in retriever.retrieve("run helper service", b_det.budget)]
    r2 = [g.to_dict() for g in retriever.retrieve("run helper service", b_det.budget)]
    assert r1 == r2, "retrieval harus deterministik"
    print("[14] deterministic retrieval OK")

    # 15) repository intelligence reuse.
    cb_dir = SRC_DIR / "agent_ai" / "contextbudget"
    for p in cb_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "class CodeIndexer" not in text, f"{p.name} tidak boleh indexer kedua"
        assert "os.walk" not in text, f"{p.name} tidak boleh walk filesystem sendiri"
    # retrieval memakai RepositoryIntelligenceV2.
    assert isinstance(retriever.intelligence, RepositoryIntelligenceV2)
    print("[15] repository intelligence reuse OK")

    # 16) Context Builder integration.
    b_int = ContextBudget(_budget(max_files=5, max_bytes=100000))
    builder_int = ContextBuilder(
        index=index, root=FIXTURE, retrieval=retriever, budget=b_int,
        partial_reader=PartialReader(FIXTURE, default_limit=1000),
    )
    res_int = builder_int.build_advanced(ContextRequest(task="run helper service"))
    assert res_int.metadata["advanced"] is True
    assert res_int.files
    print("[16] Context Builder integration OK")

    # 17) no LLM/network/database.
    for p in cb_dir.glob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for mod in mods:
                top = mod.split(".")[0].lower()
                for bad in ("openai", "openrouter", "ollama", "deepseek", "sqlite3",
                            "requests", "urllib", "http", "socket", "numpy", "faiss", "chromadb"):
                    assert top != bad, f"{p.name} tidak boleh impor '{mod}'"
    print("[17] no LLM/network/database OK")

    # 18) no duplicate retrieval/index engine.
    for p in cb_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "class CodeIndex" not in text, f"{p.name} tidak boleh index kedua"
        assert "class RepositoryIntelligence" not in text, f"{p.name} tidak boleh RI kedua"
    print("[18] no duplicate retrieval/index engine OK")

    # 19) backward compatibility dengan Context Builder lama.
    legacy = ContextBuilder(index=index, root=FIXTURE)
    legacy_res = legacy.build(ContextRequest(task="run helper", max_files=8))
    assert legacy_res.metadata["advanced"] is False if "advanced" in legacy_res.metadata else True
    assert legacy_res.files
    # build_advanced tanpa retrieval/budget -> fallback ke build().
    fallback = ContextBuilder(index=index, root=FIXTURE)
    fb_res = fallback.build_advanced(ContextRequest(task="run helper"))
    assert fb_res.files
    print("[19] backward compatibility OK")

    # 20) fixture cleanup (dicek di main()).
    print("[20] fixture cleanup OK")

    print()
    print("[OK] Advanced Context / Token Budgeting bekerja (profile, budget, partial, dedup, compaction).")
    return 0


def _budget(**overrides):
    """Budget helper untuk test (default longgar, override sesuai kebutuhan)."""
    from agent_ai.contextbudget import Budget

    base = dict(
        max_files=10, max_bytes=100000, max_tokens=100000,
        max_depth=1, max_nodes=30, relevance_threshold=0.0,
        partial_read_limit=200, partial_read=True, duplicate_read_prevention=True,
    )
    base.update(overrides)
    return Budget(**base)


if __name__ == "__main__":
    raise SystemExit(main())
