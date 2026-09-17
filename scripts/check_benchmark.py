"""Verifikasi Benchmark Coding Tasks (#49).

Deterministik, tanpa model/API nyata. Memakai callable/mock target deterministic.
Fixture benchmark dibuat di J:\\Agent_Ai\\dummy_test\\benchmark_fixture (root
temporary) dan dibersihkan setelah test.

Menguji:
    1. model benchmark
    2. case registry
    3. register/get/list
    4. builtin cases (kategori task coding)
    5. fixture isolation (temporary, bukan project AETHER)
    6. fixture cleanup setelah case
    7. runner menjalankan case via Evaluation Framework #48
    8. successful case
    9. failed case
   10. case error isolation
   11. timeout (bounded execution)
   12. metrics: tool usage
   13. metrics: validation
   14. metrics: iterations
   15. metrics: recovery/replan
   16. metrics: provider errors
   17. metrics: elapsed time
   18. aggregate report
   19. per-case comparison
   20. provider/model agnostic
   21. architecture boundary (tanpa runtime/loop baru)

Jalankan:
    python scripts/check_benchmark.py
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.benchmark import (  # noqa: E402
    BenchmarkCase,
    BenchmarkCaseRegistry,
    BenchmarkExpected,
    BenchmarkInput,
    BenchmarkReportBuilder,
    BenchmarkResult,
    BenchmarkRun,
    BenchmarkRunner,
    BenchmarkStatus,
    FixtureSpec,
    build_registry,
    builtin_cases,
)
from agent_ai.evaluation import EvaluationInput  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE_ROOT = DUMMY_ROOT / "benchmark_fixture"


def setup_fixture() -> None:
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE_ROOT, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Benchmark Coding Tasks (#49) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    # 1) model benchmark.
    inp = BenchmarkInput(
        task="buat fungsi",
        fixture=FixtureSpec(files={"a.py": "x = 1\n"}),
        metadata={"language": "python"},
    )
    exp = BenchmarkExpected(success=True, expected_tools=["write_file"], max_iterations=5)
    case = BenchmarkCase(id="c1", input=inp, expected=exp, category="function", timeout=5.0)
    assert case.to_dict()["id"] == "c1"
    assert inp.to_dict()["fixture"]["files"] == {"a.py": "x = 1\n"}
    assert exp.to_dict()["expected_tools"] == ["write_file"]
    assert BenchmarkStatus.PASSED.value == "passed"
    assert FixtureSpec(files={"b.py": "y"}).to_dict()["files"] == {"b.py": "y"}
    print("[1] model benchmark OK -> Case/Input/Expected/FixtureSpec/Status")

    # 2) case registry.
    reg = BenchmarkCaseRegistry()
    assert len(reg) == 0
    print("[2] case registry OK -> kosong saat dibuat")

    # 3) register/get/list.
    reg.register(BenchmarkCase(id="b", input=BenchmarkInput(task="b")))
    reg.register(BenchmarkCase(id="a", input=BenchmarkInput(task="a")))
    assert reg.has("a") and reg.has("b")
    assert reg.get("a").id == "a"
    assert reg.ids() == ["a", "b"], reg.ids()
    assert [c.id for c in reg.list()] == ["a", "b"]
    try:
        reg.register(BenchmarkCase(id=""))
        raise AssertionError("harus menolak id kosong")
    except ValueError:
        pass
    try:
        reg.get("tidak_ada")
        raise AssertionError("harus KeyError")
    except KeyError:
        pass
    print("[3] register/get/list OK -> urut & validasi id")

    # 4) builtin cases (kategori task coding).
    builtin = build_registry()
    ids = builtin.ids()
    for expected_id in (
        "function_create",
        "function_edit",
        "bugfix",
        "multi_file",
        "terminal_validation",
        "recovery",
    ):
        assert expected_id in ids, expected_id
    categories = {c.category for c in builtin.list()}
    for cat in ("function", "bugfix", "multi_file", "terminal_validation", "recovery"):
        assert cat in categories, cat
    # Setiap case mendeskripsikan task + fixture + expected.
    for c in builtin.list():
        assert c.input.task, f"{c.id} tanpa task"
        assert c.input.fixture.files, f"{c.id} tanpa fixture"
        assert c.expected.success is not None, f"{c.id} tanpa expected outcome"
    print(f"[4] builtin cases OK -> {len(ids)} task: {ids}")

    # Target mock deterministic (tanpa model/API nyata).
    def target_ok(eval_input: EvaluationInput) -> dict:
        # Verifikasi fixture benar-benar ada saat target dipanggil.
        fixture_dir = Path(eval_input.metadata["fixture_dir"])
        assert fixture_dir.exists(), "fixture harus ada saat target dipanggil"
        return {
            "success": True,
            "tools_used": ["write_file", "edit_file", "read_file", "run_command"],
            "validation_success": True,
            "iterations": 3,
            "recovery_count": 0,
            "replan_count": 0,
            "provider_error_count": 0,
        }

    def target_fail(eval_input: EvaluationInput) -> dict:
        return {
            "success": False,
            "tools_used": ["run_command"],
            "validation_success": False,
            "iterations": 9,
            "recovery_count": 2,
            "replan_count": 1,
            "provider_error_count": 3,
        }

    def target_raise(eval_input: EvaluationInput) -> dict:
        raise RuntimeError("target meledak")

    def target_slow(eval_input: EvaluationInput) -> dict:
        time.sleep(2.0)
        return {"success": True}

    # 5) fixture isolation (temporary, bukan project AETHER).
    runner_ok = BenchmarkRunner(target=target_ok, fixture_root=FIXTURE_ROOT)
    case_iso = BenchmarkCase(
        id="iso",
        input=BenchmarkInput(
            task="t",
            fixture=FixtureSpec(files={"x.py": "print(1)\n"}, directories=["sub"]),
        ),
        expected=BenchmarkExpected(success=True),
    )
    res_iso = runner_ok.run_case(case_iso)
    fixture_dir = Path(res_iso.fixture_dir)
    # Fixture berada di bawah fixture_root (bukan project AETHER).
    assert str(fixture_dir).startswith(str(FIXTURE_ROOT)), fixture_dir
    assert "src" not in fixture_dir.parts, "fixture tidak boleh di dalam src"
    print(f"[5] fixture isolation OK -> {fixture_dir.name} di bawah dummy_test")

    # 6) fixture cleanup setelah case.
    assert not fixture_dir.exists(), "fixture harus dibersihkan setelah case"
    # Tidak ada sisa direktori fixture di root.
    leftovers = list(FIXTURE_ROOT.glob("aether_bench_*"))
    assert leftovers == [], f"masih ada fixture tersisa: {leftovers}"
    print("[6] fixture cleanup OK -> tidak ada sisa fixture")

    # 7) runner menjalankan case via Evaluation Framework #48.
    from agent_ai.evaluation import EvaluationRunner  # noqa: F401
    import agent_ai.benchmark.runner as bench_runner_mod
    src = Path(bench_runner_mod.__file__).read_text(encoding="utf-8")
    assert "from agent_ai.evaluation import" in src, "runner harus memakai Evaluation Framework #48"
    assert "EvaluationRunner" in src, "runner harus memakai EvaluationRunner #48"
    print("[7] runner memakai Evaluation Framework #48 OK")

    # 8) successful case.
    case_ok = BenchmarkCase(
        id="ok",
        input=BenchmarkInput(task="t", fixture=FixtureSpec(files={"a.py": "x\n"})),
        expected=BenchmarkExpected(
            success=True,
            expected_tools=["write_file"],
            validation_success=True,
            max_iterations=5,
        ),
    )
    res_ok = runner_ok.run_case(case_ok)
    assert res_ok.status == BenchmarkStatus.PASSED, res_ok.reasons
    assert res_ok.success is True
    print(f"[8] successful case OK -> status={res_ok.status.value}")

    # 9) failed case (ekspektasi tidak terpenuhi, bukan error).
    case_fail = BenchmarkCase(
        id="fail",
        input=BenchmarkInput(task="t", fixture=FixtureSpec(files={"a.py": "x\n"})),
        expected=BenchmarkExpected(success=True, expected_tools=["write_file"], max_iterations=5),
    )
    res_fail = BenchmarkRunner(target=target_fail, fixture_root=FIXTURE_ROOT).run_case(case_fail)
    assert res_fail.status == BenchmarkStatus.FAILED, res_fail.status
    assert res_fail.reasons, "harus ada alasan kegagalan"
    print(f"[9] failed case OK -> status={res_fail.status.value}, reasons={len(res_fail.reasons)}")

    # 10) case error isolation (exception tidak menghentikan run).
    cases = [
        BenchmarkCase(id="ok", input=BenchmarkInput(task="t", fixture=FixtureSpec(files={"a": "1"})),
                      expected=BenchmarkExpected(success=True)),
        BenchmarkCase(id="boom", input=BenchmarkInput(task="boom", fixture=FixtureSpec(files={"a": "1"}))),
        BenchmarkCase(id="ok2", input=BenchmarkInput(task="t", fixture=FixtureSpec(files={"a": "1"})),
                      expected=BenchmarkExpected(success=True)),
    ]

    def target_mixed(eval_input: EvaluationInput) -> dict:
        if eval_input.task == "boom":
            raise RuntimeError("boom")
        return {"success": True}

    run_mixed = BenchmarkRunner(target=target_mixed, fixture_root=FIXTURE_ROOT).run(cases, run_id="mixed")
    assert run_mixed.total == 3
    assert run_mixed.passed == 2, run_mixed.passed
    assert run_mixed.errors == 1, run_mixed.errors
    boom = [r for r in run_mixed.results if r.case_id == "boom"][0]
    assert boom.status == BenchmarkStatus.ERROR and "boom" in (boom.error or "")
    # Fixture semua case dibersihkan.
    assert list(FIXTURE_ROOT.glob("aether_bench_*")) == []
    print(f"[10] case error isolation OK -> passed={run_mixed.passed}, errors={run_mixed.errors}")

    # 11) timeout (bounded execution).
    case_slow = BenchmarkCase(
        id="slow",
        input=BenchmarkInput(task="t", fixture=FixtureSpec(files={"a": "1"})),
        timeout=0.3,
    )
    res_slow = BenchmarkRunner(target=target_slow, fixture_root=FIXTURE_ROOT).run_case(case_slow)
    assert res_slow.status == BenchmarkStatus.ERROR, res_slow.status
    assert "timeout" in (res_slow.error or "").lower()
    assert list(FIXTURE_ROOT.glob("aether_bench_*")) == [], "fixture timeout harus dibersihkan"
    print(f"[11] timeout OK -> status={res_slow.status.value}, error={res_slow.error!r}")

    # 12) metrics: tool usage.
    run_tools = BenchmarkRunner(target=target_ok, fixture_root=FIXTURE_ROOT).run(
        [
            BenchmarkCase(id="t1", input=BenchmarkInput(task="t", fixture=FixtureSpec(files={"a": "1"})),
                          expected=BenchmarkExpected(success=True, expected_tools=["write_file"])),
            BenchmarkCase(id="t2", input=BenchmarkInput(task="t", fixture=FixtureSpec(files={"a": "1"})),
                          expected=BenchmarkExpected(success=True, expected_tools=["write_file", "edit_file"])),
        ],
        run_id="tools",
    )
    report_tools = BenchmarkReportBuilder.build(run_tools)
    assert report_tools.aggregate["tool_usage"].get("write_file") == 2
    assert report_tools.aggregate["tool_usage"].get("edit_file") == 2
    print(f"[12] metrics tool usage OK -> {report_tools.aggregate['tool_usage']}")

    # 13) metrics: validation.
    assert report_tools.aggregate["validation_success"] == 2
    assert report_tools.aggregate["validation_total"] == 2
    print(f"[13] metrics validation OK -> {report_tools.aggregate['validation_success']}/2")

    # 14) metrics: iterations.
    res_iter = BenchmarkRunner(target=target_fail, fixture_root=FIXTURE_ROOT).run_case(
        BenchmarkCase(id="i1", input=BenchmarkInput(task="t", fixture=FixtureSpec(files={"a": "1"})))
    )
    assert res_iter.iterations == 9, res_iter.iterations
    print(f"[14] metrics iterations OK -> {res_iter.iterations}")

    # 15) metrics: recovery/replan.
    assert res_iter.recovery_count == 2, res_iter.recovery_count
    assert res_iter.replan_count == 1, res_iter.replan_count
    print(f"[15] metrics recovery/replan OK -> recovery={res_iter.recovery_count}, replan={res_iter.replan_count}")

    # 16) metrics: provider errors.
    assert res_iter.provider_error_count == 3, res_iter.provider_error_count
    print(f"[16] metrics provider errors OK -> {res_iter.provider_error_count}")

    # 17) metrics: elapsed time.
    assert res_iter.elapsed > 0.0, res_iter.elapsed
    assert report_tools.aggregate["total_elapsed"] >= 0.0
    print(f"[17] metrics elapsed time OK -> case={res_iter.elapsed:.4f}s")

    # 18) aggregate report.
    report = BenchmarkReportBuilder.build(run_mixed)
    assert report.total == 3 and report.passed == 2 and report.errors == 1
    assert report.failed == 0
    assert abs(report.success_rate - (2 / 3)) < 1e-9
    assert report.case("boom").status == "error"
    assert "total=3" in report.summary()
    print(f"[18] aggregate report OK -> {report.summary()}")

    # 19) per-case comparison.
    run_a = BenchmarkRunner(target=target_ok, fixture_root=FIXTURE_ROOT).run(
        [BenchmarkCase(id="c1", input=BenchmarkInput(task="t", fixture=FixtureSpec(files={"a": "1"})),
                       expected=BenchmarkExpected(success=True))],
        run_id="qwen",
    )
    run_b = BenchmarkRunner(target=target_fail, fixture_root=FIXTURE_ROOT).run(
        [BenchmarkCase(id="c1", input=BenchmarkInput(task="t", fixture=FixtureSpec(files={"a": "1"})),
                       expected=BenchmarkExpected(success=True))],
        run_id="deepseek",
    )
    cmp = BenchmarkReportBuilder.compare(
        [BenchmarkReportBuilder.build(run_a), BenchmarkReportBuilder.build(run_b)]
    )
    assert len(cmp["runs"]) == 2
    assert cmp["per_case"]["c1"]["qwen"] == "passed", cmp["per_case"]
    assert cmp["per_case"]["c1"]["deepseek"] == "failed", cmp["per_case"]
    print(f"[19] per-case comparison OK -> {cmp['per_case']['c1']}")

    # 20) provider/model agnostic.
    bench_dir = SRC_DIR / "agent_ai" / "benchmark"
    for p in bench_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8").lower()
        for bad in ("openai", "openrouter", "ollama", "deepseek", "anthropic", "gemini", "qwen"):
            # "qwen"/"deepseek" hanya boleh muncul di docstring contoh, bukan kode.
            if bad in ("qwen", "deepseek"):
                continue
            assert bad not in text, f"{p.name} tidak boleh menyebut provider '{bad}'"
        for bad in ("import requests", "import urllib", "import socket", "import subprocess"):
            assert bad not in text, f"{p.name} tidak boleh melakukan network/exec: {bad}"
        for bad in ("embedding", "vector", "faiss", "chromadb", "sqlite", "judge"):
            assert bad not in text, f"{p.name} tidak boleh memakai '{bad}'"
    print("[20] provider/model agnostic OK")

    # 21) architecture boundary (tanpa runtime/loop baru).
    core_dir = SRC_DIR / "agent_ai" / "core"
    for p in core_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "agent_ai.benchmark" not in text, f"core/{p.name} tidak boleh impor benchmark"
    for p in bench_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "agent_ai.core" not in text, f"{p.name} tidak boleh impor core"
        assert "agent_ai.runtime" not in text, f"{p.name} tidak boleh impor runtime"
        assert "class AgentRuntime" not in text, f"{p.name} tidak boleh membuat runtime baru"
        assert "class AgentLoop" not in text, f"{p.name} tidak boleh membuat loop baru"
        assert "class ModelRouter" not in text, f"{p.name} tidak boleh membuat router baru"
    print("[21] architecture boundary bersih OK")

    # Cleanup akhir: tidak ada fixture tersisa.
    assert list(FIXTURE_ROOT.glob("aether_bench_*")) == [], "fixture harus bersih di akhir"
    print()
    print("[OK] Benchmark Coding Tasks bekerja (reproducible, terisolasi, provider-agnostic).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
