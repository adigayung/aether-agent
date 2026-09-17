"""Verifikasi Session & Execution Event Architecture.

Deterministik, tanpa API cloud. Fixture (bila perlu) di J:\\Agent_Ai\\dummy_test
dan dibersihkan setelah test.

Menguji:
    1. session creation
    2. unique session_id
    3. task reference
    4. event creation
    5. unique event_id
    6. event immutability
    7. chronological ordering
    8. append-only behavior
    9. filtering by session
   10. filtering by task
   11. store interface compatibility
   12. provider/tool agnostic
   13. compatibility dengan TaskLifecycle

Jalankan:
    python scripts/check_session.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.session import (  # noqa: E402
    EventType,
    ExecutionEvent,
    InMemorySessionStore,
    SessionStore,
    SessionStatus,
    event_from_lifecycle_snapshot,
    make_event,
)
from agent_ai.tasks import TaskLifecycle, TaskStatus  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "session_fixture"


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Session & Execution Event Architecture ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    store = InMemorySessionStore()

    # 1) session creation.
    session = store.create_session(project_id="proj-1", metadata={"user": "dev"})
    assert session.session_id
    assert session.project_id == "proj-1"
    assert session.status == SessionStatus.ACTIVE
    assert store.get_session(session.session_id) is session
    print("[1] session creation OK")

    # 2) unique session_id.
    ids = {store.create_session().session_id for _ in range(50)}
    assert len(ids) == 50, "session_id harus unik"
    print("[2] unique session_id OK")

    # 3) task reference.
    ref = store.create_task_reference(session.session_id, "task-1", metadata={"desc": "x"})
    assert ref is not None and ref.task_id == "task-1"
    # Idempotent: task_id sama tidak diduplikasi.
    ref2 = store.create_task_reference(session.session_id, "task-1")
    assert ref2 is ref
    assert len(store.get_session(session.session_id).tasks) == 1
    # Session tidak ada -> None.
    assert store.create_task_reference("tidak-ada", "task-x") is None
    print("[3] task reference OK")

    # 4) event creation.
    ev = make_event(session.session_id, EventType.TASK_CREATED, task_id="task-1", payload={"k": "v"})
    stored = store.append_event(ev)
    assert stored.event_type == EventType.TASK_CREATED
    assert stored.session_id == session.session_id
    assert stored.task_id == "task-1"
    assert stored.payload == {"k": "v"}
    print("[4] event creation OK")

    # 5) unique event_id.
    eids = {make_event(session.session_id, EventType.PHASE_CHANGED).event_id for _ in range(50)}
    assert len(eids) == 50, "event_id harus unik"
    print("[5] unique event_id OK")

    # 6) event immutability.
    try:
        stored.event_type = EventType.TASK_FAILED  # frozen dataclass
        raise AssertionError("event seharusnya immutable")
    except Exception as exc:
        assert "FrozenInstanceError" in type(exc).__name__ or isinstance(exc, AttributeError), type(exc).__name__
    print("[6] event immutability OK")

    # 7) chronological ordering (sequence deterministik).
    store.append_event(make_event(session.session_id, EventType.TASK_STARTED, task_id="task-1"))
    store.append_event(make_event(session.session_id, EventType.TOOL_CALLED, task_id="task-1", payload={"tool": "read_file"}))
    store.append_event(make_event(session.session_id, EventType.TOOL_COMPLETED, task_id="task-1"))
    events = store.get_events(session_id=session.session_id)
    seqs = [e.sequence for e in events]
    assert seqs == sorted(seqs), "event harus terurut berdasarkan sequence"
    assert len(set(seqs)) == len(seqs), "sequence harus unik"
    print("[7] chronological ordering OK")

    # 8) append-only behavior.
    before = len(store.get_events(session_id=session.session_id))
    store.append_event(make_event(session.session_id, EventType.OBSERVATION_RECEIVED, task_id="task-1"))
    after = len(store.get_events(session_id=session.session_id))
    assert after == before + 1, "append harus menambah, bukan mengubah"
    # Tidak ada API untuk menghapus/mengubah event.
    assert not hasattr(store, "delete_event") and not hasattr(store, "update_event")
    print("[8] append-only behavior OK")

    # 9) filtering by session.
    other = store.create_session()
    store.append_event(make_event(other.session_id, EventType.TASK_CREATED, task_id="task-2"))
    only_session = store.get_events(session_id=session.session_id)
    assert all(e.session_id == session.session_id for e in only_session)
    assert all(e.session_id != other.session_id for e in only_session)
    print("[9] filtering by session OK")

    # 10) filtering by task.
    only_task = store.get_events(task_id="task-1")
    assert only_task and all(e.task_id == "task-1" for e in only_task)
    # Filter by event_type juga bekerja.
    only_type = store.get_events(event_type=EventType.TOOL_CALLED)
    assert only_type and all(e.event_type == EventType.TOOL_CALLED for e in only_type)
    print("[10] filtering by task OK")

    # 11) store interface compatibility.
    assert isinstance(store, SessionStore)
    for method in (
        "create_session", "get_session", "update_session",
        "append_event", "get_events", "create_task_reference",
    ):
        assert callable(getattr(store, method)), f"SessionStore harus punya {method}"
    # update_session bekerja.
    updated = store.update_session(session.session_id, status=SessionStatus.COMPLETED, metadata={"done": True})
    assert updated.status == SessionStatus.COMPLETED and updated.metadata["done"] is True
    assert store.update_session("tidak-ada", status=SessionStatus.FAILED) is None
    print("[11] store interface compatibility OK")

    # 12) provider/tool agnostic.
    session_dir = SRC_DIR / "agent_ai" / "session"
    files = {p.name for p in session_dir.glob("*.py")}
    assert files == {"__init__.py", "models.py", "events.py", "store.py"}, files
    for p in session_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8").lower()
        for bad in ("openrouter", "deepseek", "ollama", "openai", "requests", "subprocess", "sqlite"):
            assert bad not in text, f"{p.name} tidak boleh menyebut '{bad}'"
    print("[12] provider/tool agnostic OK")

    # 13) compatibility dengan TaskLifecycle.
    lc = TaskLifecycle("task dengan lifecycle", task_id="task-lc-1")
    store.create_task_reference(session.session_id, lc.task_id)
    # Bridge: snapshot lifecycle -> event (tanpa coupling besar).
    store.append_event(event_from_lifecycle_snapshot(session.session_id, lc.snapshot()))
    lc.transition(TaskStatus.PREPARING)
    lc.transition(TaskStatus.RUNNING)
    store.append_event(event_from_lifecycle_snapshot(session.session_id, lc.snapshot()))
    lc.complete(result="selesai")
    store.append_event(event_from_lifecycle_snapshot(session.session_id, lc.snapshot()))

    lc_events = store.get_events(task_id="task-lc-1")
    types = [e.event_type for e in lc_events]
    assert EventType.TASK_CREATED in types
    assert EventType.TASK_COMPLETED in types
    # task_id pada event sama dengan TaskLifecycle.task_id.
    assert all(e.task_id == lc.task_id for e in lc_events)
    # Session tidak mengambil alih responsibility TaskLifecycle.
    assert lc.status == TaskStatus.COMPLETED
    print("[13] compatibility dengan TaskLifecycle OK")

    print()
    print("[OK] Session & Execution Event Architecture bekerja (append-only, ordering, filtering).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
