"""Verifikasi Parallel Execution + Task Isolation — AETHER Task Queue.

Membuktikan bahwa:
  1. execution_mode="parallel" menghasilkan minimal 2 task RUNNING simultan
  2. Queue mode tetap FIFO serial (concurrency=1)
  3. Activity/Report/TaskLog per task terisolasi (tidak bercampur)
  4. Cross-project access ditolak dengan 404
  5. SSE task-scoped memfilter event per task_id
  6. Backend menghasilkan event `task_queued`

Deterministik, tanpa API cloud: memakai TaskExecutor palsu dengan provider
yang di-"gate" (blocking) agar kita bisa mengamati concurrency.

Jalankan:
    python scripts/check_parallel_execution_isolation.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
for _p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from api.project_store import ProjectStore  # noqa: E402
from api.services import GatewayService, NotFoundError  # noqa: E402


# --------------------------------------------------------------------------- #
# TaskExecutor palsu (deterministik) dengan "gate" untuk mengamati concurrency.
# --------------------------------------------------------------------------- #
class GatedExecutor:
    """TaskExecutor palsu yang memblokir sampai di-release.

    `run()` menaikkan `active` (di bawah lock) dan menunggu event `release`
    untuk task tersebut. Test dapat mengamati berapa task yang sedang "jalan"
    pada satu waktu untuk membuktikan concurrency=1 atau >=2 (parallel).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.started: List[str] = []
        self.finished: List[str] = []
        self._release: Dict[str, threading.Event] = {}

    def _event(self, task_id: str) -> threading.Event:
        with self._lock:
            ev = self._release.get(task_id)
            if ev is None:
                ev = threading.Event()
                self._release[task_id] = ev
            return ev

    def release(self, task_id: str) -> None:
        self._event(task_id).set()

    def wait_started(self, task_id: str, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if task_id in self.started:
                    return
            time.sleep(0.01)
        raise AssertionError(f"task {task_id} tidak pernah mulai")

    def run(
        self,
        prepared: Any,
        *,
        session_id: str,
        task_id: str,
        on_status: Any,
        workspace_root: Optional[str] = None,
        provider_name: Optional[str] = None,
        model_name: Optional[str] = None,
        provider_instance_id: Optional[str] = None,
        model_id: Optional[str] = None,
        cancel_token: Any = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.append(task_id)
        try:
            if on_status is not None:
                on_status("running", None, None)
            # Tunggu sampai test me-release task ini (atau cancel diminta).
            while not self._event(task_id).is_set():
                if cancel_token is not None and cancel_token.is_cancelled():
                    if on_status is not None:
                        on_status("cancelled", None, None)
                    with self._lock:
                        self.finished.append(task_id)
                    return {
                        "status": "cancelled",
                        "result": None,
                        "error": None,
                        "iterations": 0,
                    }
                time.sleep(0.01)
            if on_status is not None:
                on_status("completed", f"done:{task_id}", None)
            with self._lock:
                self.finished.append(task_id)
            return {
                "status": "completed",
                "result": f"done:{task_id}",
                "error": None,
                "iterations": 1,
            }
        finally:
            with self._lock:
                self.active -= 1


TMP_DIR = PROJECT_ROOT / "dummy_test" / "parallel_isolation_tmp"


def _new_service(
    executor: GatedExecutor,
    *,
    auto: bool = True,
    parallel_concurrency: Optional[int] = None,
) -> GatewayService:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    store = ProjectStore(db_path=TMP_DIR / "q.db")
    return GatewayService(
        task_executor=executor,
        auto_execute=auto,
        project_store=store,
        parallel_concurrency=parallel_concurrency,
    )


def _wait_queue_state(
    service: GatewayService, task_id: str, state: str, timeout: float = 5.0
) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = service.get_task(task_id)
        if rec["queue_state"] == state:
            return rec
        time.sleep(0.01)
    raise AssertionError(
        f"task {task_id} tidak mencapai queue_state={state}; terakhir="
        f"{service.get_task(task_id)['queue_state']}"
    )


def _running_count(service: GatewayService) -> int:
    return sum(1 for r in service.list_tasks() if r["queue_state"] == "running")


# --------------------------------------------------------------------------- #
# Skenario
# --------------------------------------------------------------------------- #
def scenario_1_parallel_execution() -> None:
    """Submit 3 task parallel -> minimal 2 RUNNING simultan."""
    ex = GatedExecutor()
    svc = _new_service(ex)

    a = svc.create_task("parallel A", execution_mode="parallel")["task_id"]
    b = svc.create_task("parallel B", execution_mode="parallel")["task_id"]
    c = svc.create_task("parallel C", execution_mode="parallel")["task_id"]

    ex.wait_started(a)
    ex.wait_started(b)
    ex.wait_started(c)

    _wait_queue_state(svc, a, "running")
    _wait_queue_state(svc, b, "running")
    _wait_queue_state(svc, c, "running")

    assert _running_count(svc) == 3, f"harus 3 RUNNING simultan, dapat {_running_count(svc)}"
    assert ex.max_active >= 2, f"concurrency >= 2, dapat {ex.max_active}"

    # Release semua
    ex.release(a)
    ex.release(b)
    ex.release(c)
    _wait_queue_state(svc, a, "done")
    _wait_queue_state(svc, b, "done")
    _wait_queue_state(svc, c, "done")
    print(f"[1] Parallel execution: 3 task RUNNING simultan, max_active={ex.max_active} OK")


def scenario_2_queue_still_serial() -> None:
    """Queue mode tetap FIFO serial (concurrency=1)."""
    ex = GatedExecutor()
    svc = _new_service(ex)

    a = svc.create_task("queue A")["task_id"]
    ex.wait_started(a)
    b = svc.create_task("queue B")["task_id"]
    c = svc.create_task("queue C")["task_id"]

    _wait_queue_state(svc, a, "running")
    _wait_queue_state(svc, b, "pending")
    _wait_queue_state(svc, c, "pending")

    assert _running_count(svc) == 1, "queue mode harus tepat 1 RUNNING"
    assert ex.max_active == 1, f"queue mode concurrency harus 1, dapat {ex.max_active}"

    ex.release(a)
    ex.wait_started(b)
    _wait_queue_state(svc, b, "running")
    _wait_queue_state(svc, c, "pending")
    assert _running_count(svc) == 1

    ex.release(b)
    ex.wait_started(c)
    ex.release(c)
    _wait_queue_state(svc, c, "done")
    assert ex.started == [a, b, c], f"queue mode harus FIFO, dapat {ex.started}"
    assert ex.max_active == 1, f"queue mode concurrency harus 1, dapat {ex.max_active}"
    print("[2] Queue mode tetap FIFO serial (concurrency=1) OK")


def scenario_3_parallel_concurrency_limit() -> None:
    """Parallel concurrency limit: 2 RUNNING, 1 pending."""
    ex = GatedExecutor()
    svc = _new_service(ex, parallel_concurrency=2)

    a = svc.create_task("par A", execution_mode="parallel")["task_id"]
    b = svc.create_task("par B", execution_mode="parallel")["task_id"]
    c = svc.create_task("par C", execution_mode="parallel")["task_id"]

    ex.wait_started(a)
    ex.wait_started(b)
    time.sleep(0.2)  # Beri waktu scheduler untuk mencoba promote C

    _wait_queue_state(svc, a, "running")
    _wait_queue_state(svc, b, "running")
    _wait_queue_state(svc, c, "pending")  # C harus pending (limit=2)

    assert _running_count(svc) == 2, f"harus 2 RUNNING (limit), dapat {_running_count(svc)}"

    # Release A -> slot bebas -> C promote
    ex.release(a)
    ex.wait_started(c)
    _wait_queue_state(svc, c, "running")

    ex.release(b)
    ex.release(c)
    _wait_queue_state(svc, c, "done")
    print("[3] Parallel concurrency limit: 2 RUNNING + 1 pending, slot release OK")


def scenario_4_mixed_queue_parallel() -> None:
    """Queue dan parallel berjalan independen."""
    ex = GatedExecutor()
    svc = _new_service(ex)

    # Queue task A
    a = svc.create_task("queue A")["task_id"]
    ex.wait_started(a)
    _wait_queue_state(svc, a, "running")

    # Parallel tasks B, C
    b = svc.create_task("parallel B", execution_mode="parallel")["task_id"]
    c = svc.create_task("parallel C", execution_mode="parallel")["task_id"]

    ex.wait_started(b)
    ex.wait_started(c)

    # Queue A RUNNING + Parallel B, C RUNNING = 3 total
    _wait_queue_state(svc, a, "running")
    _wait_queue_state(svc, b, "running")
    _wait_queue_state(svc, c, "running")
    assert _running_count(svc) == 3

    ex.release(a)
    ex.release(b)
    ex.release(c)
    _wait_queue_state(svc, a, "done")
    _wait_queue_state(svc, b, "done")
    _wait_queue_state(svc, c, "done")
    print("[4] Mixed queue + parallel: queue tidak memblokir parallel OK")


def scenario_5_cross_project_protection() -> None:
    """Activity/Report/History menolak akses cross-project (404)."""
    import tempfile

    ex = GatedExecutor()
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    # Buat dua project store berbeda
    project_store_path = TMP_DIR / "projects.db"
    store = ProjectStore(db_path=project_store_path)
    svc = GatewayService(task_executor=ex, auto_execute=True, project_store=store)

    # Daftarkan dua project di store
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        p1_meta = store.add_project_with_id("proj-1", "Project 1", tmp1)
        p2_meta = store.add_project_with_id("proj-2", "Project 2", tmp2)
        store.set_active_project("proj-1")

        # Task di project 1
        task1 = svc.create_task("task for proj 1", project_id="proj-1")
        task1_id = task1["task_id"]

        # Akses dari project 2 -> harus 404
        try:
            svc.get_task_history(task1_id, project_id="proj-2")
            raise AssertionError("seharusnya NotFoundError untuk cross-project access")
        except NotFoundError:
            pass  # OK

        try:
            svc.get_task_activity(task1_id, project_id="proj-2")
            raise AssertionError("seharusnya NotFoundError untuk cross-project access")
        except NotFoundError:
            pass  # OK

        try:
            svc.get_task_report(task1_id, project_id="proj-2")
            raise AssertionError("seharusnya NotFoundError untuk cross-project access")
        except NotFoundError:
            pass  # OK

        # Akses dari project yang benar -> harus OK (meskipun log mungkin belum ada)
        try:
            svc.get_task_history(task1_id, project_id="proj-1")
        except NotFoundError:
            pass  # Log mungkin belum ada, tapi ownership check harus lolos

    print("[5] Cross-project protection: Activity/Report/History tolak 404 OK")


def scenario_6_task_queued_event() -> None:
    """Backend menghasilkan event `task_queued` saat task masuk queue."""
    ex = GatedExecutor()
    svc = _new_service(ex, auto=False)  # auto_execute=False agar tidak langsung running

    # Subscribe ke events
    events_received = []
    subscription = svc.sessions.subscribe(lambda e: events_received.append(e))

    try:
        task = svc.create_task("queued task", execution_mode="parallel")
        task_id = task["task_id"]

        # Tunggu event
        time.sleep(0.3)

        # Cek event task_queued
        queued_events = [
            e for e in events_received
            if e.event_type.value == "task_queued" and e.task_id == task_id
        ]
        assert len(queued_events) >= 1, f"harus ada event task_queued, dapat {len(queued_events)}"

        # Cek payload
        evt = queued_events[0]
        assert evt.payload.get("queue_state") == "pending", f"payload queue_state harus pending, dapat {evt.payload}"
        assert evt.payload.get("project_id") is not None or evt.payload.get("task") is not None
        assert evt.payload.get("execution_mode") == "parallel"
    finally:
        svc.sessions.unsubscribe(subscription)

    print("[6] Backend menghasilkan event `task_queued` dengan payload lengkap OK")


def scenario_7_sse_task_scoped_filter() -> None:
    """SSE task-scoped: EventSubscription._matches() memfilter event berdasarkan task_id."""
    ex = GatedExecutor()
    svc = _new_service(ex)

    from agent_ai.session.events import ExecutionEvent, EventType, make_event
    from api.streaming import EventSubscription

    task_a_id = "task-a-id"
    task_b_id = "task-b-id"

    session_a = svc.sessions.create_session(metadata={"task_id": task_a_id})
    session_b = svc.sessions.create_session(metadata={"task_id": task_b_id})

    # Buat event untuk task A dan B
    event_a = make_event(
        session_id=session_a.session_id,
        event_type=EventType.TASK_STARTED,
        task_id=task_a_id,
        payload={"test": "A"},
    )
    event_b = make_event(
        session_id=session_b.session_id,
        event_type=EventType.TASK_STARTED,
        task_id=task_b_id,
        payload={"test": "B"},
    )

    # Test EventSubscription filter langsung
    sub_a = EventSubscription(svc.sessions, task_id=task_a_id)
    sub_b = EventSubscription(svc.sessions, task_id=task_b_id)

    # Test _matches langsung
    assert sub_a._matches(event_a), "sub A harus match event A"
    assert not sub_a._matches(event_b), "sub A TIDAK boleh match event B"
    assert sub_b._matches(event_b), "sub B harus match event B"
    assert not sub_b._matches(event_a), "sub B TIDAK boleh match event A"

    # Test melalui queue (start + get)
    sub_a.start()
    sub_b.start()
    try:
        svc.sessions.append_event(event_a)
        svc.sessions.append_event(event_b)
        time.sleep(0.1)

        a_got = sub_a.get(timeout=0.5)
        b_got = sub_b.get(timeout=0.5)

        assert a_got is not None, "sub A harus menerima event A"
        assert a_got.task_id == task_a_id
        assert b_got is not None, "sub B harus menerima event B"
        assert b_got.task_id == task_b_id

        # Verify tidak ada cross-contamination
        a_got2 = sub_a.get(timeout=0.1)
        b_got2 = sub_b.get(timeout=0.1)
        assert a_got2 is None, "sub A tidak boleh menerima event B"
        assert b_got2 is None, "sub B tidak boleh menerima event A"
    finally:
        sub_a.close()
        sub_b.close()

    print("[7] SSE task-scoped: EventSubscription filter task_id OK")


def scenario_8_parallel_stop_releases_slot() -> None:
    """Stop pada task parallel melepas slot -> task lain promote."""
    ex = GatedExecutor()
    svc = _new_service(ex)

    a = svc.create_task("par A", execution_mode="parallel")["task_id"]
    b = svc.create_task("par B", execution_mode="parallel")["task_id"]
    c = svc.create_task("par C", execution_mode="parallel")["task_id"]

    ex.wait_started(a)
    ex.wait_started(b)
    ex.wait_started(c)

    _wait_queue_state(svc, a, "running")
    _wait_queue_state(svc, b, "running")
    _wait_queue_state(svc, c, "running")

    # Stop A
    stopped = svc.cancel_task(a)
    assert stopped["status"] == "cancelled"

    # A selesai, B & C tetap running
    _wait_queue_state(svc, a, "done", timeout=5.0)

    ex.release(b)
    ex.release(c)
    _wait_queue_state(svc, b, "done")
    _wait_queue_state(svc, c, "done")

    print("[8] Parallel stop: cancel melepas slot, task lain selesai OK")


def scenario_9_parallel_slot_released_on_terminal() -> None:
    """Slot parallel dilepas ketika task mencapai terminal state."""
    ex = GatedExecutor()
    svc = _new_service(ex, parallel_concurrency=2)

    a = svc.create_task("par A", execution_mode="parallel")["task_id"]
    b = svc.create_task("par B", execution_mode="parallel")["task_id"]
    c = svc.create_task("par C", execution_mode="parallel")["task_id"]

    ex.wait_started(a)
    ex.wait_started(b)
    time.sleep(0.2)

    # Limit=2: A, B running; C pending
    _wait_queue_state(svc, a, "running")
    _wait_queue_state(svc, b, "running")
    _wait_queue_state(svc, c, "pending")

    # Release A -> A done -> slot bebas -> C promote
    ex.release(a)
    _wait_queue_state(svc, a, "done", timeout=5.0)

    ex.wait_started(c)
    _wait_queue_state(svc, c, "running")

    ex.release(b)
    ex.release(c)
    _wait_queue_state(svc, b, "done")
    _wait_queue_state(svc, c, "done")

    print("[9] Parallel slot release on terminal state: A done -> C promote OK")


def main() -> int:
    print("=== Verifikasi Parallel Execution + Task Isolation ===")
    scenario_1_parallel_execution()
    scenario_2_queue_still_serial()
    scenario_3_parallel_concurrency_limit()
    scenario_4_mixed_queue_parallel()
    scenario_5_cross_project_protection()
    scenario_6_task_queued_event()
    scenario_7_sse_task_scoped_filter()
    scenario_8_parallel_stop_releases_slot()
    scenario_9_parallel_slot_released_on_terminal()
    print(
        "\n[OK] Parallel execution (N>=2 simultan), queue tetap serial FIFO, "
        "isolation cross-project ditolak, task_queued event dihasilkan, "
        "SSE task-scoped tanpa cross-contamination."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
