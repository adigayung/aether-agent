"""Verifikasi Evaluation Framework (#48).

Deterministik, tanpa API cloud. Fixture kecil di
J:\\Agent_Ai\\dummy_test\\evaluation_fixture dan dibersihkan setelah test.

Menguji (>= 18 poin):
    1. model evaluation
    2. case registry
    3. register/get/list
    4. successful case
    5. failed case
    6. case error isolation
    7. timeout
    8. expected tool metrics
    9. unexpected tool metrics
   10. validation metric
   11. iteration metric
   12. recovery metric
   13. replan metric
   14. provider error metric
   15. elapsed time
   16. aggregate report
   17. provider/model agnostic
   18. architecture boundary

Jalankan:
    python scripts/check_evaluation.py
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

from agent_ai.evaluation import (  # noqa: E402
    EvaluationCase,
    EvaluationCaseRegistry,
    EvaluationExpected,
    EvaluationInput,
    EvaluationResult,
    EvaluationRun,
    EvaluationRunner,
    EvaluationStatus,
    MetricResult,
    MetricsCollector,
    ReportBuilder,
)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "evaluation_fixture"


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "note.txt").write_text("evaluation fixture\n", encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Evaluation Framework (#48) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    # 1) model evaluation.
    inp = EvaluationInput(task="perbaiki bug", metadata={"lang": "python"})
    exp = EvaluationExpected(success=True, expected_tools=["read_file"], max_iterations=5)
    case = EvaluationCase(id="c1", input=inp, expected=exp, description="contoh", timeout=5.0)
    assert case.to_dict()["id"] == "c1"
    assert inp.to_dict()["task"] == "perbaiki bug"
    assert exp.to_dict()["expected_tools"] == ["read_file"]
    assert EvaluationStatus.PASSED.value == "passed"
    assert MetricResult(name="x", value=1).to_dict()["name"] == "x"
    print("[1] model evaluation OK -> Case/Input/Expected/Status/MetricResult")

    # 2) case registry.
    reg = EvaluationCaseRegistry()
    assert len(reg) == 0
    print("[2] case registry OK -> kosong saat dibuat")

    # 3) register/get/list.
    reg.register(EvaluationCase(id="b", input=EvaluationInput(task="b")))
    reg.register(EvaluationCase(id="a", input=EvaluationInput(task="a")))
    assert reg.has("a") and reg.has("b")
    assert reg.get("a").id == "a"
    assert reg.ids() == ["a", "b"], reg.ids()
    assert [c.id for c in reg.list()] == ["a", "b"]
    try:
        reg.register(EvaluationCase(id=""))
        raise AssertionError("harus menolak id kosong")
    except ValueError:
        pass
    try:
        reg.get("tidak_ada")
        raise AssertionError("harus KeyError")
    except KeyError:
        pass
    print("[3] register/get/list OK -> urut & validasi id")

    # Target callable (provider/model agnostic, tanpa LLM).
    def target_ok(eval_input: EvaluationInput) -> dict:
        return {
            "success": True,
            "tools_used": ["read_file", "edit_file"],
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

    # 4) successful case.
    runner_ok = EvaluationRunner(target=target_ok)
    case_ok = EvaluationCase(
        id="ok",
        input=EvaluationInput(task="t"),
        expected=EvaluationExpected(
            success=True,
            expected_tools=["read_file"],
            validation_success=True,
            max_iterations=5,
        ),
    )
    res_ok = runner_ok.run_case(case_ok)
    assert res_ok.status == EvaluationStatus.PASSED, res_ok.reasons
    assert res_ok.success is True
    print(f"[4] successful case OK -> status={res_ok.status.value}")

    # 5) failed case (ekspektasi tidak terpenuhi, bukan error).
    case_fail = EvaluationCase(
        id="fail",
        input=EvaluationInput(task="t"),
        expected=EvaluationExpected(success=True, expected_tools=["read_file"], max_iterations=5),
    )
    res_fail = EvaluationRunner(target=target_fail).run_case(case_fail)
    assert res_fail.status == EvaluationStatus.FAILED, res_fail.status
    assert res_fail.reasons, "harus ada alasan kegagalan"
    print(f"[5] failed case OK -> status={res_fail.status.value}, reasons={len(res_fail.reasons)}")

    # 6) case error isolation (exception tidak menghentikan run).
    cases = [
        EvaluationCase(id="ok", input=EvaluationInput(task="t"), expected=EvaluationExpected(success=True)),
        EvaluationCase(id="boom", input=EvaluationInput(task="boom")),
        EvaluationCase(id="ok2", input=EvaluationInput(task="t"), expected=EvaluationExpected(success=True)),
    ]

    def target_mixed(eval_input: EvaluationInput) -> dict:
        # Case dengan task "boom" memicu exception; lainnya sukses.
        if eval_input.task == "boom":
            raise RuntimeError("boom")
        return {"success": True}

    run_mixed = EvaluationRunner(target=target_mixed).run(cases, run_id="mixed")
    assert run_mixed.total == 3
    assert run_mixed.passed == 2, run_mixed.passed
    assert run_mixed.errors == 1, run_mixed.errors
    boom = [r for r in run_mixed.results if r.case_id == "boom"][0]
    assert boom.status == EvaluationStatus.ERROR and "boom" in (boom.error or "")
    print(f"[6] case error isolation OK -> passed={run_mixed.passed}, errors={run_mixed.errors}")

    # 7) timeout.
    case_slow = EvaluationCase(id="slow", input=EvaluationInput(task="t"), timeout=0.3)
    res_slow = EvaluationRunner(target=target_slow).run_case(case_slow)
    assert res_slow.status == EvaluationStatus.ERROR, res_slow.status
    assert "timeout" in (res_slow.error or "").lower()
    print(f"[7] timeout OK -> status={res_slow.status.value}, error={res_slow.error!r}")

    # 8) expected tool metrics.
    collector = MetricsCollector()
    run_tools = EvaluationRunner(target=target_ok).run(
        [
            EvaluationCase(
                id="t1",
                input=EvaluationInput(task="t"),
                expected=EvaluationExpected(success=True, expected_tools=["read_file"]),
            ),
            EvaluationCase(
                id="t2",
                input=EvaluationInput(task="t"),
                expected=EvaluationExpected(success=True, expected_tools=["read_file", "edit_file"]),
            ),
        ],
        run_id="tools",
    )
    agg = {m.name: m for m in run_tools.metrics}
    # t1: read_file dipakai (1 hit dari 1), t2: read_file+edit_file (2 dari 2) = 3/3.
    assert agg["expected_tool_usage"].value == 3, agg["expected_tool_usage"].value
    assert agg["expected_tool_usage"].metadata["expected_total"] == 3
    print(f"[8] expected tool metrics OK -> {agg['expected_tool_usage'].value}/3")

    # 9) unexpected tool metrics.
    run_unexpected = EvaluationRunner(target=target_fail).run(
        [
            EvaluationCase(
                id="u1",
                input=EvaluationInput(task="t"),
                expected=EvaluationExpected(forbidden_tools=["run_command"]),
            ),
        ],
        run_id="unexpected",
    )
    agg_u = {m.name: m for m in run_unexpected.metrics}
    assert agg_u["unexpected_tool_usage"].value == 1, agg_u["unexpected_tool_usage"].value
    print(f"[9] unexpected tool metrics OK -> {agg_u['unexpected_tool_usage'].value}")

    # 10) validation metric.
    run_val = EvaluationRunner(target=target_ok).run(
        [EvaluationCase(id="v1", input=EvaluationInput(task="t"), expected=EvaluationExpected(validation_success=True))],
        run_id="validation",
    )
    agg_v = {m.name: m for m in run_val.metrics}
    assert agg_v["validation_success"].value == 1
    assert agg_v["validation_success"].metadata["validation_total"] == 1
    print(f"[10] validation metric OK -> {agg_v['validation_success'].value}/1")

    # 11) iteration metric.
    res_iter = EvaluationRunner(target=target_fail).run_case(EvaluationCase(id="i1", input=EvaluationInput(task="t")))
    assert res_iter.iterations == 9, res_iter.iterations
    case_metrics = {m.name: m for m in collector.case_metrics(res_iter)}
    assert case_metrics["iterations"].value == 9
    print(f"[11] iteration metric OK -> {res_iter.iterations}")

    # 12) recovery metric.
    assert res_iter.recovery_count == 2, res_iter.recovery_count
    assert case_metrics["recovery_count"].value == 2
    print(f"[12] recovery metric OK -> {res_iter.recovery_count}")

    # 13) replan metric.
    assert res_iter.replan_count == 1, res_iter.replan_count
    assert case_metrics["replan_count"].value == 1
    print(f"[13] replan metric OK -> {res_iter.replan_count}")

    # 14) provider error metric.
    assert res_iter.provider_error_count == 3, res_iter.provider_error_count
    assert case_metrics["provider_error_count"].value == 3
    print(f"[14] provider error metric OK -> {res_iter.provider_error_count}")

    # 15) elapsed time.
    assert res_iter.elapsed > 0.0, res_iter.elapsed
    assert case_metrics["elapsed"].unit == "seconds"
    agg_elapsed = {m.name: m for m in run_tools.metrics}["total_elapsed"]
    assert agg_elapsed.value >= 0.0
    print(f"[15] elapsed time OK -> case={res_iter.elapsed:.4f}s, total={agg_elapsed.value:.4f}s")

    # 16) aggregate report.
    report = ReportBuilder.build(run_mixed)
    assert report.total == 3 and report.passed == 2 and report.errors == 1
    assert report.failed == 0
    assert abs(report.success_rate - (2 / 3)) < 1e-9
    assert report.error_cases == ["boom"]
    assert report.metric("total_cases").value == 3
    assert "total=3" in report.summary()
    # compare (untuk membandingkan konfigurasi nanti).
    cmp = ReportBuilder.compare([report, ReportBuilder.build(run_tools)])
    assert len(cmp["runs"]) == 2
    print(f"[16] aggregate report OK -> {report.summary()}")

    # 17) provider/model agnostic.
    eval_dir = SRC_DIR / "agent_ai" / "evaluation"
    for p in eval_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8").lower()
        for bad in ("openai", "openrouter", "ollama", "deepseek", "anthropic", "gemini"):
            assert bad not in text, f"{p.name} tidak boleh menyebut provider '{bad}'"
        for bad in ("import requests", "import urllib", "import socket", "import subprocess"):
            assert bad not in text, f"{p.name} tidak boleh melakukan network/exec: {bad}"
        # Tidak ada LLM-as-a-judge / embeddings / vector DB / database.
        for bad in ("embedding", "vector", "faiss", "chromadb", "sqlite", "judge"):
            assert bad not in text, f"{p.name} tidak boleh memakai '{bad}'"
    print("[17] provider/model agnostic OK")

    # 18) architecture boundary.
    #     - core TIDAK boleh impor evaluation.
    core_dir = SRC_DIR / "agent_ai" / "core"
    for p in core_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "agent_ai.evaluation" not in text, f"core/{p.name} tidak boleh impor evaluation"
    #     - evaluation tidak boleh impor core/runtime/tools (boundary).
    for p in eval_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "agent_ai.core" not in text, f"{p.name} tidak boleh impor core"
        assert "agent_ai.runtime" not in text, f"{p.name} tidak boleh impor runtime"
        assert "agent_ai.tools" not in text, f"{p.name} tidak boleh impor tools"
        # Tidak membuat engine eksekusi baru.
        assert "class AgentRuntime" not in text, f"{p.name} tidak boleh membuat runtime baru"
        assert "class AgentLoop" not in text, f"{p.name} tidak boleh membuat loop baru"
    print("[18] architecture boundary bersih OK")

    print()
    print("[OK] Evaluation Framework bekerja (repeatable, terstruktur, provider-agnostic).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
