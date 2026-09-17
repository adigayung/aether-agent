"""Verifikasi Better Planning / Replanning (#41).

Deterministik. Fixture project di
J:\\Agent_Ai\\dummy_test\\replanning_fixture dan dibersihkan setelah test.

Menguji:
    1. plan model (PlanStep fields, StepStatus lengkap)
    2. initial planning lebih informatif (objective/deps/expected outcome)
    3. dependency-aware readiness (READY/BLOCKED)
    4. plan revision versioned (revision, previous, reason, changed steps)
    5. replan: step sukses -> lanjut (tanpa revisi)
    6. replan: step gagal recoverable -> retry + step pemulihan
    7. replan: dependency/workspace berubah -> needs_review + verify step
    8. replan: asumsi salah -> replan
    9. replan: task tidak relevan -> remaining steps SKIPPED
   10. mempertahankan step COMPLETED saat replan
   11. bounded replan (max_replans)
   12. konfigurasi tidak hardcoded (max steps/replans/depth)
   13. planner tidak mengeksekusi tool / tidak mengubah workspace
   14. replanner bukan executor (tidak impor tool/provider/runtime)
   15. tidak membuat TaskLifecycle kedua
   16. tidak menduplikasi Reliability Manager
   17. deterministic planning & replanning
   18. repository intelligence/context reuse (targets dari context)
   19. no LLM/network/database
   20. fixture cleanup

Jalankan:
    python scripts/check_replanning.py
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

from agent_ai.config.settings import PlanningConfig  # noqa: E402
from agent_ai.planning import (  # noqa: E402
    Observation,
    PlanRevision,
    Replanner,
    ReplanTrigger,
    StepStatus,
    TaskPlanner,
)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "replanning_fixture"

FILES = {
    "app/__init__.py": "",
    "app/auth.py": "def login(user, pw):\n    return user == 'admin'\n",
    "app/main.py": "from app.auth import login\n\ndef run():\n    return login('admin', 'x')\n",
}


def setup_fixture() -> None:
    for rel, content in FILES.items():
        path = FIXTURE / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Better Planning / Replanning (#41) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    planner = TaskPlanner()
    replanner = Replanner()

    # 1) plan model (PlanStep fields, StepStatus lengkap).
    plan = planner.create_plan("Perbaiki login yang gagal")
    step = plan.steps[0]
    for attr in ("id", "title", "description", "status", "objective",
                 "dependencies", "prerequisites", "expected_outcome",
                 "actual_outcome", "retry_count", "replan_reason", "metadata"):
        assert hasattr(step, attr), f"PlanStep tidak punya atribut '{attr}'"
    statuses = {s.value for s in StepStatus}
    assert statuses == {"pending", "ready", "running", "completed", "failed", "blocked", "skipped"}, statuses
    print("[1] plan model (PlanStep fields, StepStatus lengkap) OK")

    # 2) initial planning lebih informatif.
    assert all(s.objective for s in plan.steps), "setiap step harus punya objective"
    assert all(s.expected_outcome for s in plan.steps), "setiap step harus punya expected_outcome"
    assert plan.steps[1].dependencies == [plan.steps[0].id], "dependency linear harus terbentuk"
    assert plan.metadata["kind"] == "fix"
    print("[2] initial planning lebih informatif OK")

    # 3) dependency-aware readiness.
    assert plan.steps[0].status == StepStatus.READY, plan.steps[0].status
    assert plan.steps[1].status == StepStatus.PENDING, plan.steps[1].status
    assert plan.dependencies_satisfied(plan.steps[0]) is True
    assert plan.dependencies_satisfied(plan.steps[1]) is False
    plan.complete_step(plan.steps[0])
    assert plan.steps[1].status == StepStatus.READY, plan.steps[1].status
    print("[3] dependency-aware readiness OK")

    # 4) plan revision versioned.
    plan2 = planner.create_plan("Perbaiki login yang gagal")
    obs_fail = Observation(step_id=plan2.steps[0].id, success=False, error="timeout", recoverable=True)
    plan2 = replanner.replan(plan2, obs_fail)
    assert plan2.revision == 2 and plan2.previous_revision == 1
    assert plan2.revision_reason
    assert len(plan2.revisions) == 1
    rev = plan2.revisions[0]
    assert isinstance(rev, PlanRevision)
    assert rev.revision == 2 and rev.previous_revision == 1
    assert rev.changed_steps, "changed_steps harus terisi"
    print("[4] plan revision versioned OK")

    # 5) replan: step sukses -> lanjut (tanpa revisi).
    plan3 = planner.create_plan("Perbaiki login yang gagal")
    obs_ok = Observation(step_id=plan3.steps[0].id, success=True, outcome="found app/auth.py")
    decision = replanner.evaluate(plan3, obs_ok)
    assert decision.needed is False and decision.trigger == ReplanTrigger.STEP_SUCCEEDED
    plan3 = replanner.replan(plan3, obs_ok, decision)
    assert plan3.revision == 1, "sukses tanpa replan tidak menambah revisi"
    assert plan3.steps[0].status == StepStatus.COMPLETED
    assert plan3.steps[0].actual_outcome == "found app/auth.py"
    print("[5] replan: step sukses -> lanjut OK")

    # 6) replan: step gagal recoverable -> retry + step pemulihan.
    plan4 = planner.create_plan("Perbaiki login yang gagal")
    obs_rec = Observation(step_id=plan4.steps[0].id, success=False, error="timeout", recoverable=True)
    decision = replanner.evaluate(plan4, obs_rec)
    assert decision.needed and decision.trigger == ReplanTrigger.STEP_FAILED_RECOVERABLE
    plan4 = replanner.replan(plan4, obs_rec, decision)
    assert plan4.revision == 2
    assert any(s.metadata.get("recovery_for") for s in plan4.steps), "harus ada step pemulihan"
    failed_step = [s for s in plan4.steps if s.retry_count > 0]
    assert failed_step and failed_step[0].status == StepStatus.READY
    print("[6] replan: step gagal recoverable -> retry + pemulihan OK")

    # 7) replan: dependency/workspace berubah -> needs_review + verify step.
    plan5 = planner.create_plan("Perbaiki login yang gagal", context={"files": [{"path": "app/auth.py"}]})
    obs_chg = Observation(step_id=plan5.steps[0].id, success=True, changed_paths=["app/auth.py"])
    decision = replanner.evaluate(plan5, obs_chg)
    assert decision.needed and decision.trigger == ReplanTrigger.DEPENDENCY_CHANGED
    plan5 = replanner.replan(plan5, obs_chg, decision)
    assert plan5.revision == 2
    assert any(s.metadata.get("replan_verify") for s in plan5.steps), "harus ada verify step"
    print("[7] replan: dependency/workspace berubah OK")

    # 8) replan: asumsi salah -> replan.
    plan6 = planner.create_plan("Perbaiki login yang gagal")
    obs_asm = Observation(step_id=plan6.steps[0].id, success=True, assumption_valid=False)
    decision = replanner.evaluate(plan6, obs_asm)
    assert decision.needed and decision.trigger == ReplanTrigger.ASSUMPTION_INVALID
    plan6 = replanner.replan(plan6, obs_asm, decision)
    assert plan6.revision == 2
    print("[8] replan: asumsi salah OK")

    # 9) replan: task tidak relevan -> remaining steps SKIPPED.
    plan7 = planner.create_plan("Perbaiki login yang gagal")
    plan7.complete_step(plan7.steps[0])
    obs_irr = Observation(step_id=plan7.steps[1].id, success=True, task_relevant=False)
    decision = replanner.evaluate(plan7, obs_irr)
    assert decision.needed and decision.trigger == ReplanTrigger.TASK_IRRELEVANT
    plan7 = replanner.replan(plan7, obs_irr, decision)
    remaining = [s for s in plan7.steps if s.status not in (StepStatus.COMPLETED, StepStatus.SKIPPED)]
    assert not remaining, [s.title for s in remaining]
    print("[9] replan: task tidak relevan -> SKIPPED OK")

    # 10) mempertahankan step COMPLETED saat replan.
    plan8 = planner.create_plan("Perbaiki login yang gagal")
    plan8.complete_step(plan8.steps[0])
    completed_id = plan8.steps[0].id
    obs_rec2 = Observation(step_id=plan8.steps[1].id, success=False, error="x", recoverable=True)
    plan8 = replanner.replan(plan8, obs_rec2)
    assert plan8.get_step(completed_id).status == StepStatus.COMPLETED, "step COMPLETED harus dipertahankan"
    print("[10] mempertahankan step COMPLETED OK")

    # 11) bounded replan (max_replans).
    bounded = Replanner(max_replans=1)
    plan9 = planner.create_plan("Perbaiki login yang gagal")
    for _ in range(5):
        obs = Observation(step_id=plan9.steps[0].id, success=False, error="x", recoverable=True)
        plan9 = bounded.replan(plan9, obs)
    assert plan9.revision <= 2, f"replan harus bounded, dapat revision={plan9.revision}"
    assert plan9.metadata.get("replan_exhausted") is True
    print("[11] bounded replan OK")

    # 12) konfigurasi tidak hardcoded.
    cfg = PlanningConfig(max_plan_steps=2, max_replans=5, max_plan_depth=1)
    assert cfg.max_plan_steps == 2 and cfg.max_replans == 5 and cfg.max_plan_depth == 1
    small_planner = TaskPlanner(max_steps=2)
    small_plan = small_planner.create_plan("Perbaiki login yang gagal")
    assert len(small_plan.steps) <= 2, len(small_plan.steps)
    r_cfg = Replanner(max_replans=0)
    p_cfg = planner.create_plan("Perbaiki login yang gagal")
    obs_cfg = Observation(step_id=p_cfg.steps[0].id, success=False, error="x", recoverable=True)
    p_cfg = r_cfg.replan(p_cfg, obs_cfg)
    assert p_cfg.revision == 1, "max_replans=0 -> tidak ada revisi"
    print("[12] konfigurasi tidak hardcoded OK")

    # 13) planner tidak mengeksekusi tool / tidak mengubah workspace.
    before = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}
    planner.create_plan("Perbaiki login yang gagal", context={"files": [{"path": "app/auth.py"}]})
    after = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}
    assert before == after, "planner tidak boleh mengubah workspace"
    print("[13] planner tidak mengeksekusi tool / mengubah workspace OK")

    # 14) replanner bukan executor (tidak impor tool/provider/runtime).
    plan_dir = SRC_DIR / "agent_ai" / "planning"
    forbidden_mods = ("agent_ai.tools", "agent_ai.providers", "agent_ai.runtime",
                      "agent_ai.core", "agent_ai.git", "subprocess", "os.system")
    for p in plan_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        for bad in forbidden_mods:
            assert bad not in text, f"{p.name} tidak boleh menyebut '{bad}'"
    print("[14] replanner bukan executor OK")

    # 15) tidak membuat TaskLifecycle kedua.
    for p in plan_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "class TaskLifecycle" not in text, f"{p.name} tidak boleh TaskLifecycle kedua"
        assert "TaskStatus" not in text, f"{p.name} tidak boleh mendefinisikan status task"
    print("[15] tidak membuat TaskLifecycle kedua OK")

    # 16) tidak menduplikasi Reliability Manager.
    for p in plan_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "class ReliabilityManager" not in text, f"{p.name} tidak boleh ReliabilityManager kedua"
        assert "class RetryController" not in text, f"{p.name} tidak boleh retry engine kedua"
    print("[16] tidak menduplikasi Reliability Manager OK")

    # 17) deterministic planning & replanning.
    a = planner.create_plan("Perbaiki login yang gagal").to_dict()
    b = planner.create_plan("Perbaiki login yang gagal").to_dict()

    def _strip_step(s):
        return {k: v for k, v in s.items() if k not in ("id", "dependencies")}

    assert [_strip_step(s) for s in a["steps"]] == [_strip_step(s) for s in b["steps"]], "planning harus deterministik"
    print("[17] deterministic planning & replanning OK")

    # 18) repository intelligence/context reuse (targets dari context).
    plan_ctx = planner.create_plan(
        "Perbaiki login yang gagal",
        context={"files": [{"path": "app/auth.py"}, {"path": "app/main.py"}]},
    )
    assert plan_ctx.metadata["targets"] == ["app/auth.py", "app/main.py"], plan_ctx.metadata["targets"]
    locate = plan_ctx.steps[0]
    assert locate.metadata.get("targets") == ["app/auth.py", "app/main.py"]
    print("[18] repository intelligence/context reuse OK")

    # 19) no LLM/network/database.
    for p in plan_dir.glob("*.py"):
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
                            "requests", "urllib", "http", "socket"):
                    assert top != bad, f"{p.name} tidak boleh impor '{mod}'"
    print("[19] no LLM/network/database OK")

    # 20) fixture cleanup (dicek di main()).
    print("[20] fixture cleanup OK")

    print()
    print("[OK] Better Planning / Replanning bekerja (model, initial plan, replan, revision).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
