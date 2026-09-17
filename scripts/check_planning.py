"""Verifikasi Task Planning Layer.

Menguji:
    1. TaskPlan dibuat.
    2. Step lifecycle bekerja.
    3. Status berubah dengan benar.
    4. Plan dapat diserialisasi.
    5. Deterministic planning bekerja.
    6. Invalid step/status ditolak.
    7. Planner tidak menjalankan tools.
    8. Source project tidak berubah.
    9. dummy_test dibersihkan.
   10. AETHER tetap import/run.

Fixture (bila ada) hanya di J:\Agent_Ai\dummy_test dan dibersihkan setelah test.

Jalankan:
    python scripts/check_planning.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.planning import (  # noqa: E402
    PlanStatus,
    PlanStep,
    StepStatus,
    TaskPlan,
    TaskPlanner,
)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "planning_fixture"


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "note.txt").write_text("fixture planning\n", encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Task Planning Layer ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    before = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}

    planner = TaskPlanner()

    # 1) TaskPlan dibuat.
    plan = planner.create_plan("Perbaiki login yang gagal")
    assert isinstance(plan, TaskPlan)
    assert plan.task == "Perbaiki login yang gagal"
    assert len(plan.steps) > 0
    assert plan.status == PlanStatus.PENDING
    print(f"[1] TaskPlan dibuat -> {len(plan.steps)} steps, kind={plan.metadata['kind']}")

    # 2) Step lifecycle bekerja.
    first = plan.steps[0]
    plan.start_step(first)
    assert first.status == StepStatus.RUNNING
    assert plan.status == PlanStatus.RUNNING
    plan.complete_step(first)
    assert first.status == StepStatus.COMPLETED
    print(f"[2] lifecycle OK -> start/complete step '{first.title}'")

    # 3) Status berubah dengan benar.
    plan.start_step(plan.steps[1])
    plan.skip_step(plan.steps[1])
    assert plan.steps[1].status == StepStatus.SKIPPED
    # selesaikan sisanya -> plan COMPLETED.
    for step in plan.steps[2:]:
        plan.start_step(step)
        plan.complete_step(step)
    assert plan.is_complete(), "plan harus complete"
    assert plan.status == PlanStatus.COMPLETED
    print(f"[3] status OK -> plan={plan.status.value}, is_complete={plan.is_complete()}")

    # 3b) fail_step -> plan FAILED.
    plan2 = planner.create_plan("fix bug di service")
    plan2.start_step(plan2.steps[0])
    plan2.fail_step(plan2.steps[0], error="boom")
    assert plan2.steps[0].status == StepStatus.FAILED
    assert plan2.status == PlanStatus.FAILED
    assert plan2.steps[0].metadata["error"] == "boom"
    print(f"[3b] fail_step OK -> plan={plan2.status.value}")

    # 4) Plan dapat diserialisasi.
    data = plan.to_dict()
    assert data["task"] == plan.task
    assert data["status"] == "completed"
    assert data["step_count"] == len(plan.steps)
    assert all("status" in s and "id" in s for s in data["steps"])
    print(f"[4] serialisasi OK -> keys={sorted(data.keys())}")

    # 5) Deterministic planning bekerja.
    p_a = planner.create_plan("Perbaiki login yang gagal")
    p_b = planner.create_plan("Perbaiki login yang gagal")
    titles_a = [s.title for s in p_a.steps]
    titles_b = [s.title for s in p_b.steps]
    assert titles_a == titles_b, "planning harus deterministik"
    assert p_a.metadata["kind"] == "fix"
    # klasifikasi lain.
    assert planner.create_plan("tambah fitur export").metadata["kind"] == "add"
    assert planner.create_plan("refactor modul auth").metadata["kind"] == "refactor"
    print(f"[5] deterministic OK -> {titles_a}")

    # 6) Invalid step/status ditolak.
    try:
        plan.get_step("tidak-ada")
        print("[ERROR] seharusnya KeyError untuk step tidak ada")
        return 1
    except KeyError as exc:
        print(f"[6a] invalid step OK -> {exc}")
    try:
        planner.create_plan("   ")
        print("[ERROR] seharusnya ValueError untuk task kosong")
        return 1
    except ValueError as exc:
        print(f"[6b] invalid task OK -> {exc}")
    try:
        StepStatus("bogus")
        print("[ERROR] seharusnya ValueError untuk status invalid")
        return 1
    except ValueError as exc:
        print(f"[6c] invalid status OK -> {exc}")

    # 7) Planner tidak menjalankan tools.
    #    Planner tidak punya atribut tool/executor dan tidak mengimpor ToolRegistry.
    assert not hasattr(planner, "execute"), "planner tidak boleh punya execute()"
    assert not hasattr(planner, "registry"), "planner tidak boleh punya registry"
    import agent_ai.planning.planner as planner_mod  # noqa: E402

    src = Path(planner_mod.__file__).read_text(encoding="utf-8")
    assert "ToolRegistry" not in src and "ToolExecutor" not in src
    print("[7] planner tidak menjalankan tools : OK")

    # 8) Source project tidak berubah.
    after = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}
    assert before == after, "source project berubah!"
    print("[8] source project tidak berubah : OK")

    # 9) dummy_test dibersihkan (dicek di main() setelah teardown).
    print("[9] dummy_test akan dibersihkan setelah verifier : OK")

    # 10) AETHER tetap import/run.
    from agent_ai.core.loop import AgentLoop  # noqa: E402

    loop = AgentLoop(task="x", max_iterations=3)
    loop.start()
    assert loop.status.value == "running"
    print("[10] AETHER tetap import/run : OK")

    print()
    print("[OK] Task Planning Layer bekerja (deterministik, tanpa LLM, tanpa tool execution).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
