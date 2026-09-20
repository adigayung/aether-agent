"""Verifikasi Task Queue API (TAMPILAN/kontrol UI antrian) — TAHAP UI.

Deterministik, tanpa API key/model/API cloud. Memakai service dengan
auto_execute=False (tidak menjalankan runtime) sehingga queue_state tetap
"pending" kecuali diset eksplisit.

Menguji:
    1. task creation -> queue_state="pending", queue_order monotonik
    2. GET /api/tasks/queue -> hanya task aktif (pending/running/disabled)
    3. disable -> queue_state="disabled" (task tetap ada, tidak dihapus)
    4. enable -> kembali "pending"
    5. move up/down -> urutan posisi berubah (hanya non-running)
    6. remove -> task hilang; running tidak dapat di-remove
    7. disable pada task running ditolak (disable != cancel)
    8. queue_state BUKAN bagian dari enum TaskStatus core / TERMINAL_STATUSES
    9. tidak ada endpoint existing yang berubah kontraknya

Jalankan:
    python scripts/check_task_queue.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _run() -> int:
    import os
    import tempfile

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    # Set langsung (bukan setdefault) agar tidak no-op bila env kosong sudah ada.
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"

    import django

    django.setup()
    from django.test import Client

    import api.services as services_mod
    from api.project_store import ProjectStore

    tmp_store_dir = tempfile.mkdtemp(prefix="queue_store_")
    store = ProjectStore(db_path=Path(tmp_store_dir) / "t.db")
    services_mod._default_service = services_mod.GatewayService(
        project_store=store, auto_execute=False
    )
    service = services_mod._default_service
    client = Client()

    def create_task(text):
        resp = client.post(
            "/api/tasks",
            data=json.dumps({"task": text}),
            content_type="application/json",
        )
        assert resp.status_code == 201, (resp.status_code, resp.content)
        return resp.json()

    # [1] task creation -> pending + queue_order monotonik.
    a = create_task("task a")
    b = create_task("task b")
    c = create_task("task c")
    assert a["queue_state"] == "pending", a
    assert b["queue_order"] > a["queue_order"], (a, b)
    assert c["queue_order"] > b["queue_order"], (b, c)
    print("[1] create -> queue_state=pending + urutan FIFO OK")

    # [2] GET /api/tasks/queue -> hanya task aktif.
    resp = client.get("/api/tasks/queue")
    assert resp.status_code == 200, resp.status_code
    q = resp.json()["tasks"]
    ids = [t["task_id"] for t in q]
    assert ids == [a["task_id"], b["task_id"], c["task_id"]], ids
    print(f"[2] GET /api/tasks/queue OK -> {len(q)} task aktif")

    # [3] disable -> disabled, tetap ada di antrian.
    resp = client.post(f"/api/tasks/queue/{b['task_id']}/disable")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    assert resp.json()["queue_state"] == "disabled", resp.json()
    q = client.get("/api/tasks/queue").json()["tasks"]
    assert b["task_id"] in [t["task_id"] for t in q], "disabled task harus tetap tampil"
    print("[3] disable OK -> disabled, task tetap ada (tidak dihapus)")

    # [4] enable -> pending.
    resp = client.post(f"/api/tasks/queue/{b['task_id']}/enable")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    assert resp.json()["queue_state"] == "pending", resp.json()
    print("[4] enable OK -> kembali pending")

    # [5] move down a -> urutan berubah (a bertukar dengan b).
    resp = client.post(
        f"/api/tasks/queue/{a['task_id']}/move",
        data=json.dumps({"direction": "down"}),
        content_type="application/json",
    )
    assert resp.status_code == 200, (resp.status_code, resp.content)
    order = [t["task_id"] for t in resp.json()["tasks"]]
    assert order == [b["task_id"], a["task_id"], c["task_id"]], order
    # move a up kembali.
    resp = client.post(
        f"/api/tasks/queue/{a['task_id']}/move",
        data=json.dumps({"direction": "up"}),
        content_type="application/json",
    )
    order = [t["task_id"] for t in resp.json()["tasks"]]
    assert order == [a["task_id"], b["task_id"], c["task_id"]], order
    print("[5] move up/down OK -> posisi berubah, kembali ke urutan semula")

    # [7] disable pada task running ditolak (disable != cancel).
    rec = service._tasks[c["task_id"]]
    rec.queue_state = "running"
    resp = client.post(f"/api/tasks/queue/{c['task_id']}/disable")
    assert resp.status_code == 400, (resp.status_code, resp.content)
    # running juga tidak dapat di-remove.
    resp = client.post(f"/api/tasks/queue/{c['task_id']}/remove")
    assert resp.status_code == 400, (resp.status_code, resp.content)
    # running tetap ada di queue.
    q = client.get("/api/tasks/queue").json()["tasks"]
    assert c["task_id"] in [t["task_id"] for t in q]
    rec.queue_state = "pending"
    print("[7] running tidak dapat disable/remove OK (disable != cancel)")

    # [6] remove -> task hilang (non-running).
    resp = client.post(f"/api/tasks/queue/{b['task_id']}/remove")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    q = client.get("/api/tasks/queue").json()["tasks"]
    assert b["task_id"] not in [t["task_id"] for t in q], "removed task hilang"
    print("[6] remove OK -> task non-running hilang dari antrian")

    # [8] queue_state BUKAN bagian enum TaskStatus core.
    from agent_ai.tasks.models import TaskStatus, TERMINAL_STATUSES

    core = {s.value for s in TaskStatus}
    assert "pending" not in core, core
    assert "disabled" not in core, core
    assert "running" in core  # RUNNING memang ada di core (lifecycle), tapi
    # nilai gateway queue_state TIDAK dimasukkan ke TERMINAL_STATUSES.
    assert "pending" not in {s.value for s in TERMINAL_STATUSES}
    assert "disabled" not in {s.value for s in TERMINAL_STATUSES}
    print("[8] queue_state TERPISAH dari enum core / TERMINAL_STATUSES OK")

    # [9] endpoint existing tetap ada (kontrak tidak berubah).
    assert client.get("/api/tasks").status_code == 200
    assert client.get("/api/tasks/queue").status_code == 200
    assert client.post("/api/tasks/nonexistent/cancel").status_code == 404
    print("[9] endpoint existing tidak berubah OK")

    print("\n[OK] Task Queue API (UI) bekerja: pending/disabled/remove/move + "
          "terpisah dari lifecycle Agent.")


def main() -> int:
    print("=== Verifikasi Task Queue API (TAMPILAN/kontrol UI antrian) ===")
    return _run()


if __name__ == "__main__":
    sys.exit(main() or 0)
