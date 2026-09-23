"""Verifikasi: UI Workbench TIDAK berpindah ke task yang masih PENDING.

Bug yang diperbaiki (laporan user, frontend-only):
    Saat Task A sedang RUNNING, user submit Task B dari Agent Workbench.
    Backend queue sudah benar (A tetap running, B masuk pending), TETAPI UI
    langsung berpindah menampilkan B -> Agent seolah berhenti/pindah dari A.

Root cause (frontend): `App.vue::submitTask()` SELALU meng-adopsi task yang
baru dibuat sebagai task yang dipantau (`task.id = record.task_id`,
`resetWorkspace()`, `connectStream()` dengan filter per-task), walaupun backend
mengembalikan `queue_state="pending"` (B belum dieksekusi). Konsep "task yang
dipantau (viewed/running)" tercampur dengan "task yang baru di-submit".

Perbaikan: pisahkan kedua konsep (helper murni `web/frontend/src/taskView.js`):
    - submit B saat memantau task A yang benar-benar RUNNING -> JANGAN adopsi B;
      B hanya masuk antrian (pending), stream A tidak di-rebind,
    - saat A selesai dan scheduler BENAR-BENAR menjalankan B (event
      `task_started` / queue truth) -> UI mengikuti B.

Verifier ini membuktikan:
    1. Logika keputusan (taskView.js) via Node (uji perilaku, tanpa framework).
    2. Wiring App.vue: keputusan dilakukan SEBELUM meng-overwrite task.id;
       stream SSE global (tidak difilter per-task lagi); guard event task lain;
       Run Task Consultant memakai task_id yang baru dibuat (bukan task.id).
    3. Premis backend (tanpa cloud): A running -> submit B -> B pending ->
       A selesai -> B running (scheduler serial, 1 slot).

Deterministik, offline, Windows-friendly.

Jalankan:
    python scripts/check_queue_view_follows_running.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
FRONTEND_DIR = PROJECT_ROOT / "web" / "frontend"
SRC_FRONTEND = FRONTEND_DIR / "src"

for _p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from api.project_store import ProjectStore  # noqa: E402
from api.services import GatewayService  # noqa: E402


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1) Uji perilaku logika keputusan via Node (tanpa framework/dependency baru).
# --------------------------------------------------------------------------- #
def scenario_task_view_logic() -> None:
    node = shutil.which("node")
    assert node, "node tidak tersedia"
    test_file = SRC_FRONTEND / "taskView.test.mjs"
    assert test_file.exists(), "web/frontend/src/taskView.test.mjs tidak ada"
    proc = subprocess.run(
        [node, str(test_file)],
        cwd=str(FRONTEND_DIR),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"uji logika taskView gagal:\n{proc.stdout}\n{proc.stderr}"
    print("[1] Logika taskView (viewed/running vs newly-submitted) via Node OK")


# --------------------------------------------------------------------------- #
# 2) Wiring App.vue.
# --------------------------------------------------------------------------- #
def scenario_app_wiring() -> None:
    app = _read(SRC_FRONTEND / "App.vue")
    helper = _read(SRC_FRONTEND / "taskView.js")

    # Helper murni diekspor dan dipakai.
    for fn in ("isViewedTaskRunning", "shouldAdoptSubmittedTask", "shouldFollowStartedTask"):
        assert f"export function {fn}" in helper, f"taskView.js harus mengekspor {fn}"
        assert fn in app, f"App.vue harus memakai {fn}"

    # Keputusan adopsi dijalankan SEBELUM meng-overwrite task yang dipantau.
    sub_idx = app.find("async function submitTask(")
    assert sub_idx != -1, "submitTask tidak ditemukan"
    sub_block = app[sub_idx : app.find("\nasync function stopTask(", sub_idx)]
    adopt_idx = sub_block.find("shouldAdoptSubmittedTask(")
    overwrite_idx = sub_block.find("task.id = record.task_id;")
    assert adopt_idx != -1, "submitTask harus memakai shouldAdoptSubmittedTask"
    assert overwrite_idx != -1, "submitTask harus meng-assign task.id (di cabang adopsi)"
    assert adopt_idx < overwrite_idx, (
        "keputusan shouldAdoptSubmittedTask HARUS sebelum meng-overwrite task.id "
        "(kalau tidak, task pending menimpa task running yang dipantau)"
    )
    # Task yang diantrikan dicatat untuk di-follow saat benar-benar running.
    assert "deferredTaskIds.set(record.task_id" in sub_block, (
        "submitTask harus mencatat task yang diantrikan (deferredTaskIds)"
    )
    assert "function adoptRunningTask(" in app, "harus ada adoptRunningTask (mengikuti task yang mulai running)"

    # Stream SSE TIDAK lagi difilter per-task: kalau difilter, event task antrian
    # berikutnya yang mulai running tak akan pernah terlihat.
    assert "openEventStream({ onEvent: handleEvent })" in app, (
        "connectStream harus membuka stream global (tanpa filter per-task)"
    )
    assert "taskId: task.id" not in app, (
        "stream TIDAK boleh difilter per task.id (itu penyebab stream A ter-rebind)"
    )

    # Guard: event task LAIN tidak boleh mengubah tampilan task yang dipantau.
    assert "evtTaskId !== viewedId" in app, (
        "handleEvent harus mengabaikan event milik task lain (guard task_id)"
    )

    # Run Task Consultant memakai task_id yang BARU dibuat (bisa pending), bukan
    # task.id yang sedang dipantau.
    assert 'submittedTaskId.value = (record && record.task_id) || "";' in app, (
        "runConsultantTask harus memakai task_id dari respons createTask"
    )

    # Kompatibilitas kontrak lama (tidak diubah): input tidak di-disable oleh running.
    assert ':disabled="submitting"' in app, "composer tetap anti double-submit (submitting)"
    assert ':disabled="isRunning"' not in app, "submission TIDAK boleh di-disable oleh isRunning"
    assert "setInterval" not in app, "App.vue tidak boleh menambah polling/setInterval"
    print("[2] Wiring App.vue: adopsi tertunda + stream global + guard event OK")


# --------------------------------------------------------------------------- #
# 3) Premis backend: A running -> B pending -> A selesai -> B running.
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
                time.sleep(0.01)
            if on_status is not None:
                on_status("completed", f"done:{task_id}", None)
            return {"status": "completed", "result": f"done:{task_id}", "error": None, "iterations": 1}
        finally:
            with self._lock:
                self.active -= 1


def scenario_backend_premise() -> None:
    tmp = PROJECT_ROOT / "dummy_test" / "queue_view_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    ex = GatedExecutor()
    store = ProjectStore(db_path=tmp / "q.db")
    svc = GatewayService(task_executor=ex, auto_execute=True, project_store=store)

    a = svc.create_task("task A")["task_id"]
    ex.wait_started(a)
    assert svc.get_task(a)["queue_state"] == "running"

    b = svc.create_task("task B")
    assert b["queue_state"] == "pending", b
    assert svc.get_task(b["task_id"])["queue_state"] == "pending"
    assert ex.started == [a], f"B tidak boleh langsung running: {ex.started}"

    ex.release(a)
    ex.wait_started(b["task_id"], timeout=5.0)
    assert svc.get_task(b["task_id"])["queue_state"] == "running"
    assert ex.max_active == 1, f"concurrency harus 1, dapat {ex.max_active}"
    ex.release(b["task_id"])
    print("[3] Premis backend: A running -> B pending -> A selesai -> B running OK")


def main() -> int:
    print("=== Verifikasi: UI tetap memantau task RUNNING (bukan task pending) ===")
    scenario_task_view_logic()
    scenario_app_wiring()
    scenario_backend_premise()
    print(
        "\n[OK] Submit B saat A RUNNING: A tetap tampil, stream A tidak terputus, "
        "B pending di antrian; setelah A selesai & B running, UI dapat mengikuti B."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)
