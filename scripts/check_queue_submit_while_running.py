"""Verifikasi: task submission/enqueue TETAP boleh saat Agent RUNNING.

Bug yang diperbaiki: "UI Architect ikut menjadi disabled ketika Agent RUNNING".
Root cause = gating FRONTEND (Consultant Task Proposal) yang memakai SATU
task_id global (runDisabled) sampai task itu terminal, sehingga selama Agent
RUNNING SELURUH tombol Run Task (termasuk proposal baru) ikut mati. Ini
mencampur `can_execute` (slot eksekusi, ditentukan scheduler) dengan
`can_enqueue` (submission, harus selalu boleh).

Verifier ini membuktikan (end-to-end):
    1. Tidak ada task aktif -> submit -> task RUNNING/eligible (slot bebas).
    2. Task A RUNNING -> submit Task B -> B QUEUED (pending), BUKAN running.
    3. Enqueue saat RUNNING diterima lewat jalur HTTP yang sama (POST /api/tasks).
    4. A selesai -> scheduler menjalankan B (FIFO, 1 slot).
    5. Dependency/aturan queue tetap bekerja (task disabled dilewati).
    6. Tidak ada regresi: concurrency tetap 1 (tidak paralel).
    7. Frontend: Run Task Consultant TIDAK di-disable oleh status running
       global; gating anti-double-submit bersifat PER-PROPOSAL.
    8. Frontend: Agent Input (TaskComposer) tidak di-disable oleh `running`.

Deterministik, tanpa API cloud: memakai TaskExecutor palsu yang bisa
di-"gate" (blocking) untuk mengamati berapa task RUNNING dalam satu waktu.

Jalankan:
    python scripts/check_queue_submit_while_running.py
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
FRONTEND_SRC = PROJECT_ROOT / "web" / "frontend" / "src"
for _p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from api.project_store import ProjectStore  # noqa: E402
from api.services import GatewayService  # noqa: E402


# --------------------------------------------------------------------------- #
# TaskExecutor palsu (deterministik) dengan "gate" untuk mengamati concurrency.
# --------------------------------------------------------------------------- #
class GatedExecutor:
    """TaskExecutor palsu yang memblokir sampai di-release (mengamati slot)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.started: List[str] = []
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
            while not self._event(task_id).is_set():
                if cancel_token is not None and cancel_token.is_cancelled():
                    if on_status is not None:
                        on_status("cancelled", None, None)
                    return {"status": "cancelled", "result": None, "error": None, "iterations": 0}
                time.sleep(0.01)
            if on_status is not None:
                on_status("completed", f"done:{task_id}", None)
            return {
                "status": "completed",
                "result": f"done:{task_id}",
                "error": None,
                "iterations": 1,
            }
        finally:
            with self._lock:
                self.active -= 1


TMP_DIR = PROJECT_ROOT / "dummy_test" / "queue_submit_tmp"


def _new_service(executor: GatedExecutor) -> GatewayService:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    store = ProjectStore(db_path=TMP_DIR / "q.db")
    return GatewayService(task_executor=executor, auto_execute=True, project_store=store)


def _wait_queue_state(service: GatewayService, task_id: str, state: str, timeout: float = 5.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = service.get_task(task_id)
        if rec["queue_state"] == state:
            return rec
        time.sleep(0.01)
    raise AssertionError(
        f"task {task_id} tidak mencapai queue_state={state}; "
        f"terakhir={service.get_task(task_id)['queue_state']}"
    )


def _running_count(service: GatewayService) -> int:
    return sum(1 for r in service.list_tasks() if r["queue_state"] == "running")


# --------------------------------------------------------------------------- #
# Backend: enqueue saat RUNNING (langsung lewat service).
# --------------------------------------------------------------------------- #
def scenario_enqueue_while_running_service() -> None:
    ex = GatedExecutor()
    svc = _new_service(ex)

    # [1] Tidak ada task aktif -> submit -> RUNNING (slot bebas).
    a = svc.create_task("task A")["task_id"]
    ex.wait_started(a)
    _wait_queue_state(svc, a, "running")
    assert _running_count(svc) == 1

    # [2] A RUNNING -> submit B -> B tetap boleh dibuat (enqueue) & QUEUED.
    b = svc.create_task("task B")
    assert b["queue_state"] == "pending", b
    _wait_queue_state(svc, b["task_id"], "pending")
    assert _running_count(svc) == 1, "hanya A yang boleh RUNNING"
    assert ex.started == [a], f"B tidak boleh langsung running: {ex.started}"

    # [4] A selesai -> scheduler menjalankan B (FIFO, 1 slot).
    ex.release(a)
    ex.wait_started(b["task_id"], timeout=5.0)
    _wait_queue_state(svc, b["task_id"], "running")
    # [6] Tidak ada regresi: concurrency tetap 1.
    assert ex.max_active == 1, f"concurrency harus 1, dapat {ex.max_active}"
    ex.release(b["task_id"])
    _wait_queue_state(svc, b["task_id"], "done")
    print("[1-4,6] Backend: enqueue saat RUNNING -> QUEUED -> RUNNING setelah A selesai (concurrency=1) OK")


def scenario_dependency_still_enforced() -> None:
    """B RUNNING, C & D pending; disable C -> B selesai -> D jalan (C dilewati)."""
    ex = GatedExecutor()
    svc = _new_service(ex)
    b = svc.create_task("B")["task_id"]
    ex.wait_started(b)
    c = svc.create_task("C")["task_id"]
    d = svc.create_task("D")["task_id"]
    svc.set_queue_state(c, "disabled")
    assert svc.get_task(c)["queue_state"] == "disabled"
    ex.release(b)
    ex.wait_started(d, timeout=5.0)  # D (bukan C) berikutnya.
    _wait_queue_state(svc, d, "running")
    _wait_queue_state(svc, c, "disabled")
    assert c not in ex.started, f"C harus dilewati: {ex.started}"
    ex.release(d)
    _wait_queue_state(svc, d, "done")
    assert ex.started == [b, d], ex.started
    print("[5] Dependency/aturan queue (disabled dilewati) tetap bekerja OK")


# --------------------------------------------------------------------------- #
# Backend: enqueue saat RUNNING lewat jalur HTTP (POST /api/tasks) — jalur yang
# dipakai Workbench maupun Consultant.
# --------------------------------------------------------------------------- #
def scenario_enqueue_while_running_http() -> None:
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"

    import django

    django.setup()
    from django.test import Client

    import api.services as services_mod

    ex = GatedExecutor()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    store = ProjectStore(db_path=TMP_DIR / "http.db")
    svc = GatewayService(task_executor=ex, auto_execute=True, project_store=store)
    services_mod._default_service = svc
    client = Client()

    def post_task(text: str) -> Dict[str, Any]:
        resp = client.post("/api/tasks", data=json.dumps({"task": text}), content_type="application/json")
        assert resp.status_code == 201, (resp.status_code, resp.content)
        return resp.json()

    a = post_task("A")["task_id"]
    ex.wait_started(a)
    _wait_queue_state(svc, a, "running")

    # Submit B & C saat A RUNNING -> 201 + pending (bukan ditolak, bukan running).
    rb = post_task("B")
    rc = post_task("C")
    assert rb["queue_state"] == "pending", rb
    assert rc["queue_state"] == "pending", rc
    _wait_queue_state(svc, rb["task_id"], "pending")
    _wait_queue_state(svc, rc["task_id"], "pending")
    assert _running_count(svc) == 1
    assert ex.started == [a], f"hanya A: {ex.started}"

    # Queue API (dibaca QueuePanel) merepresentasikan RUNNING + QUEUED bersamaan.
    q = {t["task_id"]: t["queue_state"] for t in client.get("/api/tasks/queue").json()["tasks"]}
    assert q[a] == "running" and q[rb["task_id"]] == "pending" and q[rc["task_id"]] == "pending", q

    # A selesai -> B jalan -> B selesai -> C jalan (FIFO serial).
    ex.release(a)
    ex.wait_started(rb["task_id"], timeout=5.0)
    _wait_queue_state(svc, rb["task_id"], "running")
    ex.release(rb["task_id"])
    ex.wait_started(rc["task_id"], timeout=5.0)
    _wait_queue_state(svc, rc["task_id"], "running")
    ex.release(rc["task_id"])
    _wait_queue_state(svc, rc["task_id"], "done")
    assert ex.started == [a, rb["task_id"], rc["task_id"]], ex.started
    assert ex.max_active == 1, f"concurrency harus 1, dapat {ex.max_active}"
    print("[3] HTTP POST /api/tasks: RUNNING + QUEUED bersamaan, FIFO serial OK")


# --------------------------------------------------------------------------- #
# Frontend: gating submission TIDAK boleh berarti "Agent RUNNING -> diblokir".
# --------------------------------------------------------------------------- #
def scenario_frontend_gating() -> None:
    app = (FRONTEND_SRC / "App.vue").read_text(encoding="utf-8")
    chat = (FRONTEND_SRC / "components" / "ConsultantChat.vue").read_text(encoding="utf-8")
    composer = (FRONTEND_SRC / "components" / "TaskComposer.vue").read_text(encoding="utf-8")

    # Run Task Consultant: TIDAK ada lagi gating global (runDisabled) yang
    # men-disable SEMUA proposal dari satu task_id. Gating harus PER-PROPOSAL.
    assert "runDisabled" not in chat, "gating Run Task global (runDisabled) harus dihapus"
    assert "isProposalBusy" in chat, "Run Task harus di-gate per Task Proposal"
    assert "runTaskId" in chat, "state anti-double-submit harus per message (runTaskId)"
    assert ':disabled="running"' not in chat, "Run Task TIDAK boleh di-disable oleh status running global"
    assert "runTask(msg.taskProposal, i)" in chat, "runTask harus menerima indeks proposal"
    # Indikator per-proposal memakai runningTaskId (queue API), bukan SSE task
    # terminal (SSE difilter per task sehingga terminal task lain tak terlihat).
    assert "runningTaskId" in chat, "Consultant harus punya prop runningTaskId (per-proposal)"
    assert ':running-task-id="runningTaskId"' in app, "App harus meneruskan runningTaskId ke Consultant"

    # TaskComposer (Agent Input): submit hanya di-gate oleh in-flight request
    # (`disabled`=submitting), BUKAN oleh `running`.
    assert ':disabled="disabled || !text.trim()"' in composer, "Run Task composer harus hanya anti double-submit"
    assert "if (!value || props.disabled) return;" in composer, "submit hanya diblokir oleh double-submit"
    assert "runDisabled" not in composer
    print("[7,8] Frontend: gating per-proposal (Consultant) + Agent Input bebas running OK")


def main() -> int:
    print("=== Verifikasi: enqueue tetap boleh saat Agent RUNNING ===")
    scenario_frontend_gating()
    scenario_enqueue_while_running_service()
    scenario_dependency_still_enforced()
    scenario_enqueue_while_running_http()
    print(
        "\n[OK] Architect tetap dapat membuat task baru saat Agent RUNNING; "
        "task baru masuk Queue dan dieksekusi serial oleh Scheduler."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
