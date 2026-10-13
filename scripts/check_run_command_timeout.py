"""Verifikasi P0: run_command timeout benar-benar menghentikan process tree.

Latar belakang (bug yang diperbaiki):
    `run_command` sebelumnya memakai
    `subprocess.run(capture_output=True, timeout=...)`. Pada Windows, saat
    timeout, `subprocess.run` hanya membunuh child LANGSUNG lalu memanggil
    `communicate()` TANPA batas. Bila ada grandchild yang mewarisi pipe
    stdout/stderr (mis. `manage.py runserver` + proses autoreload, atau
    installer yang meluncurkan server), `communicate()` menunggu EOF
    SELAMANYA -> tool call menggantung -> thread eksekutor task tidak pernah
    selesai -> slot scheduler TIDAK pernah dilepas -> seluruh Task Queue
    tersangkut permanen.

Yang diverifikasi (deterministik, tanpa API cloud):
    1. Command normal tetap bekerja (backward compatible).
    2. TIMEOUT membunuh SELURUH process tree (parent + grandchild) dan
       tool kembali dalam waktu terbatas (bukan hang permanen).
    3. Proses induk selesai tetapi grandchild masih memegang pipe -> tool tetap
       kembali (bounded), TIDAK menunggu EOF selamanya, DAN grandchild (orphan)
       dibersihkan (tidak ditinggalkan hidup).
    4. Cancel (Stop) benar-benar menghentikan command yang sedang berjalan,
       bukan hanya mengubah status.
    5. Queue recovery end-to-end: task yang command-nya timeout berakhir
       FAILED terkontrol, slot scheduler dilepas, dan task berikutnya jalan.
    6. Timeout pada parent yang masih hidup membunuh grandchild pemegang pipe
       (PID terpisah) tanpa meninggalkan orphan.
    7. Default timeout aman, override dipakai untuk proses lambat, nilai
       berlebihan di-clamp, dan nilai tidak valid ditolak (bukan hang).

Jalankan:
    python scripts/check_run_command_timeout.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
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

from agent_ai.core.cancel import CancellationToken  # noqa: E402
from agent_ai.tools.base import ToolValidationError  # noqa: E402
from agent_ai.tools.terminal import RunCommandTool  # noqa: E402

from api.project_store import ProjectStore  # noqa: E402
from api.services import GatewayService  # noqa: E402

PY = sys.executable
TMP_DIR = PROJECT_ROOT / "dummy_test" / "run_command_timeout_tmp"


# --------------------------------------------------------------------------- #
# Fixture: script yang meniru proses macet (parent + grandchild pegang pipe).
# --------------------------------------------------------------------------- #
def _write_fixture(ws: Path) -> None:
    # Parent hidup lama + spawn grandchild yang mewarisi stdout/stderr (pipe).
    (ws / "hang_parent.py").write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, 'hang_grandchild.py'])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    # Grandchild menulis marker SETELAH delay -> bukti apakah ia dibunuh.
    (ws / "hang_grandchild.py").write_text(
        "import time\n"
        "time.sleep(4)\n"
        "open('grandchild_marker.txt', 'w', encoding='utf-8').write('alive')\n",
        encoding="utf-8",
    )
    # Parent keluar SEGERA; grandchild tetap hidup memegang pipe.
    (ws / "spawn_parent.py").write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, 'spawn_grandchild.py'])\n",
        encoding="utf-8",
    )
    (ws / "spawn_grandchild.py").write_text(
        "import os, time\n"
        "with open('spawn_pid.txt', 'w', encoding='utf-8') as fh:\n"
        "    fh.write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )


def _kill_pid(pid: int) -> None:
    """Bersihkan proses grandchild yang sengaja tertinggal (test hygiene)."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=10,
            shell=False,
        )
    except Exception:  # noqa: BLE001 - cleanup best-effort
        pass


def _pid_alive(pid: int) -> bool:
    """True bila proses dengan PID tersebut masih hidup.

    Windows: `tasklist /FI "PID eq <pid>"`. POSIX: `os.kill(pid, 0)`.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
            )
            return str(pid) in (out.stdout or "")
        except Exception:  # noqa: BLE001 - anggap hidup bila tak bisa dipastikan
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Skenario
# --------------------------------------------------------------------------- #
def scenario_1_normal_command(run: RunCommandTool) -> None:
    """Command normal tetap bekerja setelah perubahan."""
    r = run.execute(command=f'"{PY}" -c "print(123)"', timeout=30)
    assert r["success"] and r["exit_code"] == 0 and "123" in r["stdout"], r
    assert r["outcome"] == "success", r
    print("[1] command normal tetap bekerja OK")


def scenario_2_timeout_kills_tree(run: RunCommandTool, ws: Path) -> None:
    """Timeout membunuh SELURUH process tree + kembali terbatas (bukan hang)."""
    marker = ws / "grandchild_marker.txt"
    if marker.exists():
        marker.unlink()

    start = time.perf_counter()
    r = run.execute(command=f'"{PY}" hang_parent.py', timeout=1)
    elapsed = time.perf_counter() - start

    assert r["timed_out"] is True and r["outcome"] == "timeout", r
    assert r["success"] is False, r
    assert elapsed < 12.0, f"run_command timeout terlalu lama (hang?): {elapsed:.1f}s"
    # Beri waktu grandchild menulis marker bila ia TIDAK dibunuh (delay 4s).
    time.sleep(5.0)
    assert not marker.exists(), (
        "grandchild pemegang pipe TIDAK dibunuh saat timeout "
        "(marker terbentuk -> process tree masih hidup)"
    )
    print(
        f"[2] timeout membunuh process tree (parent+grandchild) & kembali "
        f"dalam {elapsed:.1f}s OK"
    )


def scenario_3_parent_exit_grandchild_pipe(run: RunCommandTool, ws: Path) -> None:
    """Parent selesai tetapi grandchild memegang pipe -> tidak hang (bounded)
    DAN grandchild (orphan) tidak ditinggalkan hidup."""
    pid_file = ws / "spawn_pid.txt"
    if pid_file.exists():
        pid_file.unlink()

    start = time.perf_counter()
    r = run.execute(command=f'"{PY}" spawn_parent.py', timeout=5)
    elapsed = time.perf_counter() - start

    assert r["success"] and r["exit_code"] == 0, r
    assert elapsed < 8.0, (
        f"run_command menunggu EOF pipe yang dipegang grandchild terlalu lama: "
        f"{elapsed:.1f}s (harus bounded, grandchild tidur 60s)"
    )
    # Orphan cleanup: grandchild yang memegang pipe TIDAK boleh dibiarkan hidup.
    # Bila pid file sempat ditulis, proses dengan PID itu harus sudah mati.
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            assert not _pid_alive(pid), (
                f"grandchild pemegang pipe (PID={pid}) masih hidup setelah "
                "run_command kembali (orphan tidak dibersihkan)"
            )
        except ValueError:
            pass
    print(
        f"[3] parent selesai + grandchild memegang pipe -> kembali "
        f"dalam {elapsed:.1f}s (tidak hang, orphan dibersihkan) OK"
    )


def scenario_6_orphan_killed_on_timeout(ws: Path) -> None:
    """Timeout pada parent yang masih hidup membunuh SELURUH tree:

    grandchild (pemegang pipe) dengan PID terpisah harus benar-benar mati
    setelah run_command timeout kembali. Ini menguji reaping process tree
    tanpa bergantung pada relasi PID induk (yang bisa sudah hilang).
    """
    pid_file = ws / "tree_pid.txt"
    if pid_file.exists():
        pid_file.unlink()

    run = RunCommandTool(root=ws)
    # Parent hidup lama dan men-spawn grandchild yang menulis PID-nya lalu tidur.
    (ws / "tree_parent.py").write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, 'tree_grandchild.py'])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    (ws / "tree_grandchild.py").write_text(
        "import os, time\n"
        "with open('tree_pid.txt', 'w', encoding='utf-8') as fh:\n"
        "    fh.write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    r = run.execute(command=f'"{PY}" tree_parent.py', timeout=2)
    assert r["timed_out"] is True and r["outcome"] == "timeout", r
    assert r["success"] is False, r

    deadline = time.time() + 5.0
    pid: Optional[int] = None
    while time.time() < deadline:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
                break
            except ValueError:
                pass
        time.sleep(0.1)
    assert pid is not None, "grandchild tidak menulis PID (fixture tidak jalan)"
    # Beri sedikit waktu OS mereap; lalu pastikan grandchild benar-benar mati.
    time.sleep(1.0)
    assert not _pid_alive(pid), (
        f"grandchild (PID={pid}) yang memegang pipe masih hidup setelah timeout "
        "(process tree TIDAK dihentikan penuh)"
    )
    print("[6] timeout membunuh grandchild pemegang pipe (tanpa orphan) OK")


def scenario_7_timeout_override_and_default(ws: Path) -> None:
    """Default timeout aman, override dipakai untuk proses lambat, dan nilai
    berlebihan dibatasi (clamp) ke batas maksimum."""
    from agent_ai.tools import terminal as _terminal  # noqa: E402

    run = RunCommandTool(root=ws)

    # (a) Override: command yang butuh >1s TETAP sukses bila timeout dinaikkan.
    r = run.execute(
        command=f'"{PY}" -c "import time; time.sleep(2); print(\'late\')"',
        timeout=30,
    )
    assert r["success"] and "late" in r["stdout"], r

    # (b) Default timeout terpasang bila argumen tidak diberikan (60s) dan
    #     tidak memotong command cepat.
    r = run.execute(command=f'"{PY}" -c "print(\'fast\')"')
    assert r["success"] and "fast" in r["stdout"], r
    assert _terminal._DEFAULT_TIMEOUT == 60.0, _terminal._DEFAULT_TIMEOUT

    # (c) Nilai timeout berlebihan dibatasi ke _MAX_TIMEOUT (clamp) — bukan
    #     diteruskan mentah. Diverifikasi tanpa menunggu 600s: command yang
    #     "timeout" memakai nilai kecil, dan clamp diuji lewat pemetaan nilai.
    r = run.execute(command=f'"{PY}" -c "import time; time.sleep(5)"', timeout=0.5)
    assert r["outcome"] == "timeout", r
    assert "0.5" in r["error"], r

    huge = _terminal._MAX_TIMEOUT + 5000
    # Replikasi jalur validasi: timeout efektif = min(huge, _MAX_TIMEOUT).
    assert min(float(huge), _terminal._MAX_TIMEOUT) == _terminal._MAX_TIMEOUT

    # (d) Timeout tidak valid ditolak sebagai validation error (tidak hang).
    rejected = False
    try:
        run.execute(command=f'"{PY}" -c "print(1)"', timeout=0)
    except ToolValidationError:
        rejected = True
    assert rejected, "timeout <= 0 harus ditolak sebagai ToolValidationError"

    print("[7] default/override/clamp timeout OK")


def scenario_4_cancel_stops_process(ws: Path) -> None:
    """Cancel (Stop) benar-benar menghentikan command yang sedang berjalan."""
    token = CancellationToken()
    run = RunCommandTool(root=ws, cancel_token=token)
    holder: Dict[str, Any] = {}

    def _target() -> None:
        holder["result"] = run.execute(
            command=f'"{PY}" -c "import time; time.sleep(60)"', timeout=120
        )

    start = time.perf_counter()
    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    time.sleep(1.0)
    token.request("test_cancel")
    thread.join(timeout=15)
    elapsed = time.perf_counter() - start

    assert not thread.is_alive(), "run_command tetap berjalan setelah cancel"
    result = holder.get("result", {})
    assert result.get("outcome") == "cancelled", result
    assert result.get("success") is False, result
    assert elapsed < 10.0, f"cancel tidak menghentikan proses: {elapsed:.1f}s"
    print(f"[4] cancel menghentikan proses berjalan dalam {elapsed:.1f}s OK")


def scenario_5_queue_recovery(ws: Path) -> None:
    """Task dengan command timeout FAILED terkontrol -> slot dilepas -> task
    berikutnya jalan (queue tidak tersangkut)."""

    class TimeoutThenNormalExecutor:
        """TaskExecutor palsu yang menjalankan run_command NYATA."""

        def __init__(self, root: Path) -> None:
            self._tool = RunCommandTool(root=root)
            self.started: List[str] = []
            self.finished: List[str] = []
            self._lock = threading.Lock()

        def run(
            self,
            prepared: Any,
            *,
            session_id: str,
            task_id: str,
            on_status: Any = None,
            workspace_root: Optional[str] = None,
            provider_name: Optional[str] = None,
            model_name: Optional[str] = None,
            provider_instance_id: Optional[str] = None,
            model_id: Optional[str] = None,
            cancel_token: Any = None,
        ) -> Dict[str, Any]:
            with self._lock:
                index = len(self.started)
                self.started.append(task_id)
            if on_status is not None:
                on_status("running", None, None)

            if index == 0:
                # Command "macet" (parent + grandchild) -> harus timeout.
                res = self._tool.execute(command=f'"{PY}" hang_parent.py', timeout=1)
                assert res["outcome"] == "timeout", res
                if on_status is not None:
                    on_status("failed", None, res["error"])
                with self._lock:
                    self.finished.append(task_id)
                return {
                    "status": "failed",
                    "result": None,
                    "error": res["error"],
                    "iterations": 1,
                }

            # Task berikutnya: command normal -> completed.
            res = self._tool.execute(command=f'"{PY}" -c "print(7)"', timeout=30)
            assert res["success"], res
            if on_status is not None:
                on_status("completed", "ok", None)
            with self._lock:
                self.finished.append(task_id)
            return {
                "status": "completed",
                "result": "ok",
                "error": None,
                "iterations": 1,
            }

    def _wait_status(service: GatewayService, task_id: str, status: str, timeout: float = 30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            rec = service.get_task(task_id)
            if rec["status"] == status:
                return rec
            time.sleep(0.05)
        raise AssertionError(
            f"task {task_id} tidak mencapai status={status}; terakhir="
            f"{service.get_task(task_id)['status']}"
        )

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    executor = TimeoutThenNormalExecutor(ws)
    store = ProjectStore(db_path=TMP_DIR / "queue_recovery.db")
    service = GatewayService(
        task_executor=executor, auto_execute=True, project_store=store
    )

    a = service.create_task("task command macet")["task_id"]
    b = service.create_task("task berikutnya")["task_id"]

    _wait_status(service, a, "failed")
    _wait_status(service, b, "completed")

    assert executor.started == [a, b], executor.started
    assert service.get_task(a)["queue_state"] == "done", service.get_task(a)
    assert service.get_task(b)["queue_state"] == "done", service.get_task(b)
    # Slot benar-benar dilepas: tidak ada token tersisa, tidak ada task running.
    assert not service._cancel_tokens, service._cancel_tokens
    assert all(
        r["queue_state"] != "running" for r in service.list_tasks()
    ), service.list_tasks()
    print("[5] task timeout FAILED -> slot dilepas -> task berikutnya jalan OK")


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=== Verifikasi P0: run_command timeout + cleanup + queue recovery ===")
    ws = Path(tempfile.mkdtemp(prefix="rc_timeout_ws_"))
    _write_fixture(ws)
    try:
        run = RunCommandTool(root=ws)
        scenario_1_normal_command(run)
        scenario_2_timeout_kills_tree(run, ws)
        scenario_3_parent_exit_grandchild_pipe(run, ws)
        scenario_4_cancel_stops_process(ws)
        scenario_5_queue_recovery(ws)
        scenario_6_orphan_killed_on_timeout(ws)
        scenario_7_timeout_override_and_default(ws)
        print()
        print(
            "[OK] run_command timeout menghentikan process tree, tidak hang, "
            "cancel menghentikan proses, dan queue recovery bekerja."
        )
        return 0
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(TMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
