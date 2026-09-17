"""Verifikasi Resume / Continue Task.

Deterministik. Fixture Git di J:\\Agent_Ai\\dummy_test\\resume_fixture dan
dibersihkan setelah test.

Menguji:
    1. rekonstruksi state dari lifecycle + events
    2. task terminal tidak resumable
    3. task berhenti (running) resumable
    4. inspeksi workspace (git)
    5. deteksi divergence (fingerprint berubah)
    6. cegah double-resume
    7. resume mencatat fingerprint
    8. reset memungkinkan resume ulang
    9. tidak membuat state engine kedua (read-only)
   10. persistence-ready (input serializable)

Jalankan:
    python scripts/check_resume.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.git import GitRepositoryFacade  # noqa: E402
from agent_ai.resume import ResumeStatus, TaskResumer  # noqa: E402
from agent_ai.session import EventType, InMemorySessionStore, make_event  # noqa: E402
from agent_ai.tasks import TaskLifecycle, TaskStatus  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "resume_fixture"


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], FIXTURE)
    _git(["config", "user.email", "test@example.com"], FIXTURE)
    _git(["config", "user.name", "Test User"], FIXTURE)
    (FIXTURE / "a.txt").write_bytes(b"hello\n")
    _git(["add", "a.txt"], FIXTURE)
    _git(["commit", "-m", "init"], FIXTURE)


def teardown_fixture() -> None:
    if not FIXTURE.exists():
        return
    for p in FIXTURE.rglob("*"):
        try:
            p.chmod(0o777)
        except OSError:
            pass
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Resume / Continue Task ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    store = InMemorySessionStore()
    session = store.create_session(project_id="proj")
    git_facade = GitRepositoryFacade(root=FIXTURE)
    resumer = TaskResumer(git_facade=git_facade, workspace_root=str(FIXTURE))

    # 1) rekonstruksi state dari lifecycle + events.
    lc = TaskLifecycle("task berjalan", task_id="task-r1")
    store.create_task_reference(session.session_id, lc.task_id)
    lc.transition(TaskStatus.PREPARING)
    lc.transition(TaskStatus.RUNNING)
    store.append_event(make_event(session.session_id, EventType.TASK_CREATED, task_id=lc.task_id))
    store.append_event(make_event(session.session_id, EventType.TASK_STARTED, task_id=lc.task_id))
    events = store.get_events(task_id=lc.task_id)
    state = TaskResumer.reconstruct_state(lc.snapshot(), events)
    assert state["task_id"] == "task-r1"
    assert state["status"] == "running"
    assert state["event_count"] == 2
    assert state["last_event"]["event_type"] == "task_started"
    print("[1] rekonstruksi state OK")

    # 2) task terminal tidak resumable.
    lc_done = TaskLifecycle("task selesai", task_id="task-done")
    lc_done.transition(TaskStatus.PREPARING)
    lc_done.transition(TaskStatus.RUNNING)
    lc_done.complete(result="ok")
    plan_done = resumer.plan_resume(lc_done.snapshot())
    assert plan_done.status == ResumeStatus.NOT_RESUMABLE, plan_done.status
    assert plan_done.can_resume is False
    print("[2] task terminal tidak resumable OK")

    # 3) task berhenti (running) resumable.
    plan = resumer.plan_resume(lc.snapshot(), events=events, session_id=session.session_id)
    assert plan.status == ResumeStatus.RESUMABLE, plan.to_dict()
    assert plan.can_resume is True
    assert plan.resume_from_phase == "execution"
    assert plan.workspace is not None and plan.workspace.is_git_repository is True
    print("[3] task berhenti resumable OK")

    # 4) inspeksi workspace (git).
    ws = resumer.inspect_workspace()
    assert ws.is_git_repository is True
    assert ws.git_branch == "main"
    assert ws.git_clean is True
    assert ws.fingerprint
    print("[4] inspeksi workspace OK")

    # 5) deteksi divergence (fingerprint berubah).
    plan_div = resumer.plan_resume(
        lc.snapshot(), events=events, expected_fingerprint="fingerprint-palsu"
    )
    assert plan_div.status == ResumeStatus.DIVERGED, plan_div.status
    assert plan_div.warnings
    print("[5] deteksi divergence OK")

    # 6) cegah double-resume.
    result1 = resumer.resume(lc.snapshot(), events=events, session_id=session.session_id)
    assert result1.resumed is True
    result2 = resumer.resume(lc.snapshot(), events=events, session_id=session.session_id)
    assert result2.resumed is False
    assert result2.plan.status == ResumeStatus.ALREADY_RESUMED, result2.plan.status
    print("[6] cegah double-resume OK")

    # 7) resume mencatat fingerprint.
    assert resumer._resumed.get("task-r1") == ws.fingerprint
    print("[7] resume mencatat fingerprint OK")

    # 8) reset memungkinkan resume ulang.
    resumer.reset("task-r1")
    result3 = resumer.resume(lc.snapshot(), events=events, session_id=session.session_id)
    assert result3.resumed is True
    print("[8] reset memungkinkan resume ulang OK")

    # 9) tidak membuat state engine kedua (read-only terhadap lifecycle).
    before = lc.snapshot().to_dict()
    resumer.plan_resume(lc.snapshot(), events=events)
    resumer.inspect_workspace()
    after = lc.snapshot().to_dict()
    assert before == after, "resume tidak boleh memutasi lifecycle"
    # Resume tidak mengubah status lifecycle.
    assert lc.status == TaskStatus.RUNNING
    print("[9] tidak membuat state engine kedua (read-only) OK")

    # 10) persistence-ready: input serializable (TaskState + events to_dict).
    serializable = {
        "state": lc.snapshot().to_dict(),
        "events": [e.to_dict() for e in events],
        "plan": plan.to_dict(),
    }
    import json
    json.dumps(serializable)  # tidak boleh error
    print("[10] persistence-ready (input serializable) OK")

    print()
    print("[OK] Resume / Continue Task bekerja (rekonstruksi, divergence, anti double-resume).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
