"""Verifikasi Scheduler Serial GLOBAL (1 execution slot) — Task Queue AETHER.

Membuktikan bahwa satu Global Task Queue benar-benar mengontrol eksekusi:
concurrency=1, FIFO berdasarkan queue_order, disable/enable enforced,
Move Up/Down memengaruhi urutan eksekusi, dan Stop melepas slot.

Deterministik, tanpa API cloud: memakai TaskExecutor palsu dengan provider
yang di-"gate" (blocking) agar kita bisa mengamati berapa task yang RUNNING
dalam satu waktu.

Menguji:
    1. Empty slot: submit A -> A RUNNING -> A selesai -> slot free.
    2. Serial: A,B,C -> hanya SATU RUNNING; B/C PENDING; eksekusi berurutan
       sesuai queue_order. Tidak pernah 2 RUNNING bersamaan (max concurrency=1).
    3. Disabled: B di-disable -> dilewati; A selesai -> C jalan; B tetap disabled.
    4. Enable: B di-enable -> kembali pending -> dieksekusi sesuai urutan.
    5. Move Up/Down: queue_order memengaruhi task berikutnya.
    6. Stop: A RUNNING, B PENDING -> Stop A -> A CANCELLED -> slot free ->
       B RUNNING. Tidak ada eksekusi paralel.
    7. disable != cancel: disable pada task running ditolak.
    8. auto_execute=False: TIDAK ada eksekusi (kontrak verifier lain utuh).
    9. Jalur submit Workbench (POST /api/tasks via HTTP) memakai GLOBAL queue
       yang sama: RUNNING saat slot kosong, PENDING saat slot terpakai,
       disable/enable pada pending, promosi FIFO saat slot bebas (concurrency=1).

Jalankan:
    python scripts/check_task_queue_scheduler.py
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
from api.services import GatewayService, ValidationError  # noqa: E402


# --------------------------------------------------------------------------- #
# TaskExecutor palsu (deterministik) dengan "gate" untuk mengamati concurrency.
# --------------------------------------------------------------------------- #
class GatedExecutor:
    """TaskExecutor palsu yang memblokir sampai di-release.

    `run()` menaikkan `running` (di bawah lock) dan menunggu event `release`
    untuk task tersebut. Test dapat mengamati berapa task yang sedang "jalan"
    pada satu waktu untuk membuktikan concurrency=1.
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


TMP_DIR = PROJECT_ROOT / "dummy_test" / "queue_sched_tmp"


def _new_service(executor: GatedExecutor, *, auto: bool = True) -> GatewayService:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    store = ProjectStore(db_path=TMP_DIR / "q.db")
    return GatewayService(
        task_executor=executor,
        auto_execute=auto,
        project_store=store,
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
def scenario_1_empty_slot() -> None:
    """Submit A -> A RUNNING -> A selesai -> slot free."""
    ex = GatedExecutor()
    svc = _new_service(ex)
    a = svc.create_task("task A")["task_id"]
    ex.wait_started(a)
    _wait_queue_state(svc, a, "running")
    assert _running_count(svc) == 1
    ex.release(a)
    _wait_queue_state(svc, a, "done")
    assert _running_count(svc) == 0
    print("[1] empty slot: A RUNNING -> selesai -> slot free OK")


def scenario_2_serial() -> None:
    """A,B,C -> hanya satu RUNNING; eksekusi berurutan FIFO."""
    ex = GatedExecutor()
    svc = _new_service(ex)
    a = svc.create_task("task A")["task_id"]
    ex.wait_started(a)
    b = svc.create_task("task B")["task_id"]
    c = svc.create_task("task C")["task_id"]

    # A running, B & C pending (B/C tidak boleh mulai).
    _wait_queue_state(svc, a, "running")
    _wait_queue_state(svc, b, "pending")
    _wait_queue_state(svc, c, "pending")
    assert _running_count(svc) == 1, "harus tepat 1 RUNNING"
    assert ex.started == [a], f"hanya A yang boleh mulai: {ex.started}"

    # Selesaikan A -> B harus jalan, C tetap pending.
    ex.release(a)
    ex.wait_started(b, timeout=5.0)
    _wait_queue_state(svc, b, "running")
    _wait_queue_state(svc, c, "pending")
    assert _running_count(svc) == 1
    assert ex.max_active == 1, f"concurrency harus 1, dapat {ex.max_active}"

    # Selesaikan B -> C jalan.
    ex.release(b)
    ex.wait_started(c, timeout=5.0)
    _wait_queue_state(svc, c, "running")
    ex.release(c)
    _wait_queue_state(svc, c, "done")
    assert ex.started == [a, b, c], ex.started
    assert ex.max_active == 1, f"concurrency harus 1, dapat {ex.max_active}"
    print("[2] serial FIFO A->B->C, max concurrency=1 OK")


def scenario_3_disabled_skipped() -> None:
    """A running, B pending, C pending; disable B -> A selesai -> C jalan."""
    ex = GatedExecutor()
    svc = _new_service(ex)
    a = svc.create_task("A")["task_id"]
    ex.wait_started(a)
    b = svc.create_task("B")["task_id"]
    c = svc.create_task("C")["task_id"]
    svc.set_queue_state(b, "disabled")
    assert svc.get_task(b)["queue_state"] == "disabled"

    ex.release(a)
    ex.wait_started(c, timeout=5.0)  # C (bukan B) yang jalan berikutnya.
    _wait_queue_state(svc, c, "running")
    _wait_queue_state(svc, b, "disabled")  # B tetap disabled, tidak dieksekusi
    assert b not in ex.started, f"B tidak boleh dieksekusi: {ex.started}"
    ex.release(c)
    _wait_queue_state(svc, c, "done")
    assert ex.started == [a, c], ex.started
    print("[3] disable B -> dilewati, C jalan, B tetap DISABLED OK")


def scenario_4_enable() -> None:
    """B disabled -> enable -> kembali pending -> dieksekusi sesuai urutan."""
    ex = GatedExecutor()
    svc = _new_service(ex)
    a = svc.create_task("A")["task_id"]
    ex.wait_started(a)
    b = svc.create_task("B")["task_id"]
    svc.set_queue_state(b, "disabled")
    # Enable B kembali -> pending.
    svc.set_queue_state(b, "pending")
    _wait_queue_state(svc, b, "pending")
    assert b not in ex.started, "B tidak jalan selama A masih running"

    ex.release(a)
    ex.wait_started(b, timeout=5.0)
    _wait_queue_state(svc, b, "running")
    ex.release(b)
    _wait_queue_state(svc, b, "done")
    assert ex.started == [a, b], ex.started
    print("[4] enable B -> pending -> dieksekusi (urutan) OK")


def scenario_5_move_up_down() -> None:
    """Move Up/Down mengubah queue_order -> mengubah task berikutnya."""
    ex = GatedExecutor()
    svc = _new_service(ex)
    a = svc.create_task("A")["task_id"]
    ex.wait_started(a)
    b = svc.create_task("B")["task_id"]
    c = svc.create_task("C")["task_id"]

    # Geser C ke atas B -> urutan pending: C lalu B.
    svc.move_task(c, "up")
    # Selesaikan A -> C harus jalan lebih dulu.
    ex.release(a)
    ex.wait_started(c, timeout=5.0)
    _wait_queue_state(svc, c, "running")
    assert b not in ex.started, "B belum boleh jalan (C lebih awal)"
    ex.release(c)
    ex.wait_started(b, timeout=5.0)
    ex.release(b)
    _wait_queue_state(svc, b, "done")
    assert ex.started == [a, c, b], f"urutan Move Up harus [a,c,b]: {ex.started}"

    # Kembalikan urutan: A2,B2,C2 -> move B2 down -> A2,C2,B2.
    ex2 = GatedExecutor()
    svc2 = _new_service(ex2)
    a2 = svc2.create_task("A2")["task_id"]
    ex2.wait_started(a2)
    b2 = svc2.create_task("B2")["task_id"]
    c2 = svc2.create_task("C2")["task_id"]
    svc2.move_task(b2, "down")
    ex2.release(a2)
    ex2.wait_started(c2, timeout=5.0)
    ex2.release(c2)
    ex2.wait_started(b2, timeout=5.0)
    ex2.release(b2)
    _wait_queue_state(svc2, b2, "done")
    assert ex2.started == [a2, c2, b2], f"Move Down harus [a2,c2,b2]: {ex2.started}"
    print("[5] Move Up/Down mengubah urutan eksekusi OK")


def scenario_6_stop() -> None:
    """A RUNNING, B PENDING -> Stop A -> A CANCELLED -> slot free -> B RUNNING."""
    ex = GatedExecutor()
    svc = _new_service(ex)
    a = svc.create_task("A")["task_id"]
    ex.wait_started(a)
    b = svc.create_task("B")["task_id"]
    _wait_queue_state(svc, b, "pending")

    stopped = svc.cancel_task(a)
    assert stopped["status"] == "cancelled", stopped
    _wait_queue_state(svc, a, "done", timeout=5.0)
    # Slot dilepas -> B mulai.
    ex.wait_started(b, timeout=5.0)
    _wait_queue_state(svc, b, "running")
    assert ex.max_active == 1, f"tidak boleh paralel, dapat {ex.max_active}"
    ex.release(b)
    _wait_queue_state(svc, b, "done")
    print("[6] Stop A -> CANCELLED -> slot free -> B RUNNING (tanpa paralel) OK")


def scenario_7_disable_running_rejected() -> None:
    """disable pada task running ditolak (disable != cancel)."""
    ex = GatedExecutor()
    svc = _new_service(ex)
    a = svc.create_task("A")["task_id"]
    ex.wait_started(a)
    _wait_queue_state(svc, a, "running")
    try:
        svc.set_queue_state(a, "disabled")
        raise AssertionError("disable task running seharusnya ditolak")
    except ValidationError:
        pass
    ex.release(a)
    _wait_queue_state(svc, a, "done")
    print("[7] disable pada task RUNNING ditolak (disable != cancel) OK")


def scenario_8_auto_execute_false() -> None:
    """auto_execute=False -> TIDAK ada eksekusi (kontrak verifier lain utuh)."""
    ex = GatedExecutor()
    svc = _new_service(ex, auto=False)
    a = svc.create_task("A")["task_id"]
    time.sleep(0.3)
    assert ex.started == [], f"auto_execute=False tidak boleh mengeksekusi: {ex.started}"
    rec = svc.get_task(a)
    assert rec["queue_state"] == "pending", rec
    print("[8] auto_execute=False -> tidak ada eksekusi (kontrak utuh) OK")


def scenario_9_workbench_http_path() -> None:
    """Jalur submit Workbench (POST /api/tasks) memakai GLOBAL queue yang sama.

    Agent Input (Workbench) dan Run Task (Consultant) memakai endpoint +
    service yang SAMA: POST /api/tasks -> GatewayService.create_task. Skenario
    ini membuktikan lewat layer HTTP (bukan hanya memanggil service langsung)
    bahwa task dari Workbench:
        (i)   RUNNING bila slot kosong,
        (ii)  PENDING bila ada task lain RUNNING (tidak langsung dieksekusi),
        (iii) PENDING dapat di-disable lalu di-enable (kembali mengantre),
        (iv)  dipromosikan RUNNING saat slot bebas, tetap FIFO + concurrency=1.
    """
    import json
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"

    import django

    django.setup()
    from django.test import Client

    import api.services as services_mod

    ex = GatedExecutor()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    store = ProjectStore(db_path=TMP_DIR / "wb_http.db")
    svc = GatewayService(task_executor=ex, auto_execute=True, project_store=store)
    # Views memakai get_service() -> instance default ini (jalur HTTP nyata).
    services_mod._default_service = svc
    client = Client()

    def post_task(text: str) -> Dict[str, Any]:
        resp = client.post(
            "/api/tasks",
            data=json.dumps({"task": text}),
            content_type="application/json",
        )
        assert resp.status_code == 201, (resp.status_code, resp.content)
        return resp.json()

    # (i) Submit Workbench saat tidak ada task -> RUNNING (slot kosong).
    a = post_task("workbench A")["task_id"]
    ex.wait_started(a)
    _wait_queue_state(svc, a, "running")
    assert _running_count(svc) == 1

    # (ii) Submit Workbench saat task lain RUNNING -> PENDING.
    b = post_task("workbench B")
    c = post_task("workbench C")
    assert b["queue_state"] == "pending", b
    assert c["queue_state"] == "pending", c
    _wait_queue_state(svc, b["task_id"], "pending")
    _wait_queue_state(svc, c["task_id"], "pending")
    assert _running_count(svc) == 1, "harus tepat 1 RUNNING"
    assert ex.started == [a], f"hanya A yang boleh mulai: {ex.started}"

    # (iii) Pending dari Workbench dapat di-disable lalu di-enable.
    resp = client.post(f"/api/tasks/queue/{b['task_id']}/disable")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    assert resp.json()["queue_state"] == "disabled", resp.json()
    resp = client.post(f"/api/tasks/queue/{b['task_id']}/enable")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    assert resp.json()["queue_state"] == "pending", resp.json()
    assert b["task_id"] not in ex.started

    # (iv) Slot bebas -> task pending berikutnya (dari Workbench) RUNNING.
    ex.release(a)
    ex.wait_started(b["task_id"], timeout=5.0)
    _wait_queue_state(svc, b["task_id"], "running")
    _wait_queue_state(svc, c["task_id"], "pending")
    ex.release(b["task_id"])
    ex.wait_started(c["task_id"], timeout=5.0)
    ex.release(c["task_id"])
    _wait_queue_state(svc, c["task_id"], "done")
    assert ex.started == [a, b["task_id"], c["task_id"]], ex.started
    assert ex.max_active == 1, f"concurrency harus 1, dapat {ex.max_active}"

    # Queue API (dibaca QueuePanel Workbench) melihat state antrian yang sama;
    # semua task sudah terminal -> antrian aktif kosong.
    assert client.get("/api/tasks/queue").json()["tasks"] == []
    print(
        "[9] Workbench POST /api/tasks -> GLOBAL queue sama "
        "(running/pending/disable/enable/FIFO) OK"
    )


def main() -> int:
    print("=== Verifikasi Scheduler Serial GLOBAL (1 execution slot) ===")
    scenario_1_empty_slot()
    scenario_2_serial()
    scenario_3_disabled_skipped()
    scenario_4_enable()
    scenario_5_move_up_down()
    scenario_6_stop()
    scenario_7_disable_running_rejected()
    scenario_8_auto_execute_false()
    scenario_9_workbench_http_path()
    print(
        "\n[OK] Global Task Queue + scheduler serial (1 slot, FIFO queue_order, "
        "disable/enable enforced, Stop aman, jalur Workbench HTTP) bekerja."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
