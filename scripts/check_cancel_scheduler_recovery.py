"""Verifikasi: Cancel TIDAK memblokir scheduler + semantik cancel/resume/queue.

Regresi untuk audit "task dihentikan lalu pengguna mengirim task lanjutan".

Membuktikan (deterministik, tanpa API cloud; executor palsu yang memblokir hingga
di-release dan sengaja "linger" setelah cancel agar lifecycle terlihat belum
selesai):

    1. Task kedua berjalan SETELAH task pertama dibatalkan dan eksekusi pertama
       benar-benar melepas slot (serial FIFO, concurrency=1).
    2. Task yang MASIH dalam proses pembatalan TIDAK dianggap slot bebas:
       task berikutnya tidak boleh mulai, dan queue API tetap melaporkan slot
       terpakai (queue_state="cancelling", bukan hilang/done) selama lifecycle
       eksekusi belum benar-benar selesai.
    3. Task PENDING dapat dibatalkan TANPA dijalankan (keluar dari antrean).
    4. Task DISABLED tidak berubah semantik menjadi cancelled (disable != cancel);
       disable pada task running ditolak.
    5. Status akhir & event terminal konsisten: status cancelled + queue_state
       menjadi done HANYA setelah slot dilepas; cancel ulang = no-op; tidak ada
       event task_completed untuk task yang di-cancel.
    6. Mode serial DAN parallel tidak mengalami regresi (parallel tetap
       concurrent, cancel melepas slot, task berikutnya dipromosikan).
    7. Respons API cancel_task mencerminkan apakah pembatalan benar-benar
       diterima (cancel_accepted/cancel_pending) atau task sudah terminal.
    8. UI Stop tetap menunjuk task yang benar setelah pembatalan (queue truth:
       isQueueSlotOccupied() -> satu-satunya slot-occupying task terbaru).

Jalankan:
    python scripts/check_cancel_scheduler_recovery.py
"""

from __future__ import annotations

import json
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

TMP_DIR = PROJECT_ROOT / "dummy_test" / "cancel_sched_tmp"


# --------------------------------------------------------------------------- #
# Executor palsu: memblokir sampai di-release; saat cancel diminta ia "linger"
# beberapa saat (mensimulasikan lifecycle eksekusi yang belum benar-benar
# selesai padahal permintaan pembatalan sudah diterima).
# --------------------------------------------------------------------------- #
class LingerExecutor:
    def __init__(self, linger: float = 0.0) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.started: List[str] = []
        self.finished: List[str] = []
        self._release: Dict[str, threading.Event] = {}
        self.linger = linger

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
            time.sleep(0.005)
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
            while True:
                if cancel_token is not None and cancel_token.is_cancelled():
                    if self.linger:
                        time.sleep(self.linger)
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
                if self._event(task_id).is_set():
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
                time.sleep(0.005)
        finally:
            with self._lock:
                self.active -= 1


def _new_service(ex: LingerExecutor, *, auto: bool = True, parallel: int = 4) -> GatewayService:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    store = ProjectStore(db_path=TMP_DIR / "cancel_sched.db")
    return GatewayService(
        task_executor=ex,
        auto_execute=auto,
        project_store=store,
        parallel_concurrency=parallel,
    )


def _wait_state(svc: GatewayService, task_id: str, state: str, timeout: float = 5.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = svc.get_task(task_id)
        if rec["queue_state"] == state:
            return rec
        time.sleep(0.005)
    raise AssertionError(
        f"{task_id} tidak mencapai queue_state={state}; terakhir="
        f"{svc.get_task(task_id)['queue_state']}"
    )


def _slot_owners(svc: GatewayService) -> List[str]:
    return [
        t["task_id"]
        for t in svc.list_queue()
        if t["queue_state"] in ("running", "cancelling")
    ]


# --------------------------------------------------------------------------- #
# Skenario
# --------------------------------------------------------------------------- #
def scenario_1_second_runs_after_cancel() -> None:
    """[1] Task kedua berjalan setelah task pertama dibatalkan & slot dilepas."""
    ex = LingerExecutor()
    svc = _new_service(ex)
    a = svc.create_task("A")["task_id"]
    ex.wait_started(a)
    b = svc.create_task("B")["task_id"]
    _wait_state(svc, b, "pending")
    assert ex.started == [a], ex.started

    out = svc.cancel_task(a)
    assert out["status"] == "cancelled", out
    # Slot benar-benar dilepas -> B dipromosikan (FIFO, concurrency=1).
    ex.wait_started(b, timeout=5.0)
    _wait_state(svc, b, "running")
    assert ex.max_active == 1, f"terjadi overlap eksekusi: {ex.max_active}"
    ex.release(b)
    _wait_state(svc, b, "done")
    assert ex.started == [a, b], ex.started
    print("[1] cancel A -> slot bebas -> B RUNNING (serial, tanpa deadlock) OK")


def scenario_2_cancelling_not_free() -> None:
    """[2] Task dalam proses pembatalan TIDAK dianggap slot bebas."""
    ex = LingerExecutor(linger=0.6)
    svc = _new_service(ex)
    a = svc.create_task("A")["task_id"]
    ex.wait_started(a)
    b = svc.create_task("B")["task_id"]
    _wait_state(svc, b, "pending")

    svc.cancel_task(a)
    # Status lifecycle langsung cancelled (responsif), tetapi slot tetap
    # terpakai (cancelling) dan API queue TIDAK melaporkan "tidak ada running".
    rec = svc.get_task(a)
    assert rec["status"] == "cancelled", rec
    assert rec["queue_state"] == "cancelling", rec
    assert _slot_owners(svc) == [a], _slot_owners(svc)
    time.sleep(0.15)
    assert b not in ex.started, f"B mulai saat A masih membatalkan: {ex.started}"

    ex.wait_started(b, timeout=5.0)
    assert ex.max_active == 1, f"overlap eksekusi: {ex.max_active}"
    ex.release(b)
    _wait_state(svc, b, "done")
    _wait_state(svc, a, "done")
    print("[2] task dalam proses pembatalan tetap menempati slot (cancelling) OK")


def scenario_3_cancel_pending() -> None:
    """[3] Task pending dapat dibatalkan tanpa dijalankan."""
    ex = LingerExecutor()
    svc = _new_service(ex)
    a = svc.create_task("A")["task_id"]
    ex.wait_started(a)
    b = svc.create_task("B")["task_id"]
    c = svc.create_task("C")["task_id"]
    _wait_state(svc, b, "pending")

    out = svc.cancel_task(b)
    assert out["status"] == "cancelled", out
    assert out["queue_state"] == "done", out
    assert b not in _slot_owners(svc), _slot_owners(svc)

    ex.release(a)
    ex.wait_started(c, timeout=5.0)
    ex.release(c)
    _wait_state(svc, c, "done")
    time.sleep(0.15)
    assert b not in ex.started, f"B dieksekusi walau dibatalkan: {ex.started}"
    print("[3] cancel task PENDING -> tidak dijalankan; C tetap jalan OK")


def scenario_4_disabled_not_cancelled() -> None:
    """[4] Disable != cancel; disable task running ditolak."""
    ex = LingerExecutor()
    svc = _new_service(ex)
    a = svc.create_task("A")["task_id"]
    ex.wait_started(a)
    b = svc.create_task("B")["task_id"]
    svc.set_queue_state(b, "disabled")
    rec = svc.get_task(b)
    assert rec["queue_state"] == "disabled", rec
    assert rec["status"] != "cancelled", rec

    try:
        svc.set_queue_state(a, "disabled")
        raise AssertionError("disable task RUNNING seharusnya ditolak")
    except ValidationError:
        pass
    assert svc.get_task(a)["status"] != "cancelled", svc.get_task(a)

    # Task cancelling juga tidak dapat di-disable/di-remove (bukan disable).
    ex2 = LingerExecutor(linger=0.6)
    svc2 = _new_service(ex2)
    a2 = svc2.create_task("A2")["task_id"]
    ex2.wait_started(a2)
    svc2.cancel_task(a2)
    assert svc2.get_task(a2)["queue_state"] == "cancelling"
    for action in ("disable", "remove"):
        try:
            if action == "disable":
                svc2.set_queue_state(a2, "disabled")
            else:
                svc2.remove_task(a2)
            raise AssertionError(f"{action} pada task cancelling harus ditolak")
        except ValidationError:
            pass
    ex.release(a)
    _wait_state(svc, a, "done")
    print("[4] disable != cancel; disable/remove task running & cancelling ditolak OK")


def scenario_5_terminal_consistency() -> None:
    """[5] Status akhir & event terminal konsisten (slot dilepas sekali)."""
    ex = LingerExecutor()
    svc = _new_service(ex)
    a = svc.create_task("A")["task_id"]
    ex.wait_started(a)
    svc.cancel_task(a)
    immediate = svc.get_task(a)
    assert immediate["status"] == "cancelled", immediate
    # Slot belum dilepas -> belum "done".
    assert immediate["queue_state"] == "cancelling", immediate
    final = _wait_state(svc, a, "done", timeout=5.0)
    assert final["status"] == "cancelled", final

    # Cancel ulang = no-op idempotent (tidak resurrect).
    again = svc.cancel_task(a)
    assert again["status"] == "cancelled", again
    assert again.get("cancel_accepted") is False, again
    time.sleep(0.2)
    assert svc.get_task(a)["status"] == "cancelled", svc.get_task(a)

    types: List[str] = []
    for rec in svc.list_tasks():
        sid = rec["session_id"]
        try:
            evs = svc.sessions.get_events(session_id=sid, task_id=a)
        except TypeError:
            evs = svc.sessions.get_events(task_id=a)
        types.extend(e.event_type.value for e in evs)
    assert "task_completed" not in types, types
    print(f"[5] status akhir cancelled + queue_state done setelah slot dilepas; "
          f"cancel ulang no-op (event: {types}) OK")


def scenario_6_serial_and_parallel_no_regression() -> None:
    """[6] Serial tetap 1 slot; parallel tetap concurrent + cancel melepas slot."""
    # Serial: 3 task -> max concurrency 1, FIFO.
    ex = LingerExecutor()
    svc = _new_service(ex)
    ids = [svc.create_task(f"S{i}")["task_id"] for i in range(3)]
    ex.wait_started(ids[0])
    _wait_state(svc, ids[1], "pending")
    _wait_state(svc, ids[2], "pending")
    for i, tid in enumerate(ids):
        ex.release(tid)
        if i + 1 < len(ids):
            ex.wait_started(ids[i + 1], timeout=5.0)
            _wait_state(svc, ids[i + 1], "running")
    _wait_state(svc, ids[-1], "done")
    assert ex.started == ids, ex.started
    assert ex.max_active == 1, f"serial concurrency harus 1: {ex.max_active}"

    # Parallel: 2 task concurrent (limit 2), lalu cancel satu -> slot bebas.
    ex2 = LingerExecutor(linger=0.6)
    svc2 = _new_service(ex2, parallel=2)
    p1 = svc2.create_task("P1", execution_mode="parallel")["task_id"]
    p2 = svc2.create_task("P2", execution_mode="parallel")["task_id"]
    p3 = svc2.create_task("P3", execution_mode="parallel")["task_id"]
    ex2.wait_started(p1)
    ex2.wait_started(p2)
    _wait_state(svc2, p3, "pending")
    assert svc2.get_task(p3)["queue_state"] == "pending", svc2.get_task(p3)
    svc2.cancel_task(p1)
    # Pembatalan melepas slot -> P3 dipromosikan tanpa melebihi limit.
    ex2.wait_started(p3, timeout=5.0)
    assert ex2.max_active <= 2, f"parallel melebihi limit: {ex2.max_active}"
    ex2.release(p2)
    ex2.release(p3)
    _wait_state(svc2, p2, "done")
    _wait_state(svc2, p3, "done")
    print("[6] serial=1 slot FIFO & parallel concurrent (limit dipatuhi, "
          "cancel melepas slot) OK")


def scenario_7_cancel_response_semantics() -> None:
    """[7] Respons API mencerminkan pembatalan diterima / task sudah terminal."""
    ex = LingerExecutor()
    svc = _new_service(ex)
    a = svc.create_task("A")["task_id"]
    ex.wait_started(a)
    b = svc.create_task("B")["task_id"]
    _wait_state(svc, b, "pending")

    # (a) Task running: diterima + pending (slot belum dilepas).
    out_a = svc.cancel_task(a)
    assert out_a["cancel_accepted"] is True, out_a
    assert out_a["cancel_pending"] is True, out_a

    # (b) Task pending: diterima + slot tidak terpakai (langsung done).
    out_b = svc.cancel_task(b)
    assert out_b["cancel_accepted"] is True, out_b
    assert out_b["cancel_pending"] is False, out_b
    assert out_b["queue_state"] == "done", out_b

    # (c) Task sudah terminal: pembatalan TIDAK diterima (no-op).
    out_b2 = svc.cancel_task(b)
    assert out_b2["cancel_accepted"] is False, out_b2
    assert out_b2["cancel_pending"] is False, out_b2
    assert out_b2["status"] == "cancelled", out_b2

    out_a2 = svc.cancel_task(a)
    assert out_a2["cancel_accepted"] is False, out_a2
    print("[7] cancel_task: accepted/pending/no-op sesuai keadaan task OK")


def scenario_8_stop_target_points_to_right_task() -> None:
    """[8] UI Stop tetap menunjuk task yang benar setelah pembatalan.

    Sumber: queue truth (GET /api/tasks/queue). Setelah A dibatalkan dan slot
    dilepas, SATU-SATUNYA slot-occupying task adalah B -> tombol Stop harus
    menunjuk B (bukan A yang sudah terminal, bukan task pending lain).
    """
    ex = LingerExecutor(linger=0.4)
    svc = _new_service(ex)
    a = svc.create_task("A")["task_id"]
    ex.wait_started(a)
    b = svc.create_task("B")["task_id"]
    c = svc.create_task("C")["task_id"]
    _wait_state(svc, b, "pending")

    svc.cancel_task(a)
    # Selama proses pembatalan, slot masih dipegang A -> Stop tetap ke A.
    owners = _slot_owners(svc)
    assert owners == [a], owners

    ex.wait_started(b, timeout=5.0)
    _wait_state(svc, b, "running")
    owners = _slot_owners(svc)
    assert owners == [b], f"Stop harus menunjuk B, dapat {owners}"
    assert c not in owners, owners

    ex.release(b)
    _wait_state(svc, b, "done")
    _wait_state(svc, c, "running", timeout=5.0)
    assert _slot_owners(svc) == [c], _slot_owners(svc)
    ex.release(c)
    _wait_state(svc, c, "done")
    assert _slot_owners(svc) == [], _slot_owners(svc)
    print("[8] Stop target mengikuti queue truth (A->B->C) tanpa salah tunjuk OK")


def main() -> int:
    print("=== Verifikasi Cancel vs Scheduler (recovery + semantik) ===")
    scenario_1_second_runs_after_cancel()
    scenario_2_cancelling_not_free()
    scenario_3_cancel_pending()
    scenario_4_disabled_not_cancelled()
    scenario_5_terminal_consistency()
    scenario_6_serial_and_parallel_no_regression()
    scenario_7_cancel_response_semantics()
    scenario_8_stop_target_points_to_right_task()
    print(
        "\n[OK] Cancel tidak memblokir scheduler; slot dilepas hanya setelah "
        "lifecycle selesai; cancel/disabel/pending/serial/parallel konsisten."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
