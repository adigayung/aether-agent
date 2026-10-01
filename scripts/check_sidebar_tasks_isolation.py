"""Verifikasi isolasi project untuk Sidebar -> Tasks.

Sidebar -> Tasks memakai DUA sumber data (keduanya endpoint Gateway):
    1. GET /api/tasks/queue    -> QueuePanel (mode QUEUE, live) — TaskRecord.
    2. GET /api/tasks/history  -> tabel HISTORY (mode History) — .aether/log/.

Verifier ini memastikan KEDUA jalur yang benar-benar dipakai Sidebar Tasks
ter-isolasi per project (bukan hanya /api/tasks, yang TIDAK dipakai sidebar):

    - Project A shows only A tasks.
    - Switching to Project B shows only B tasks.
    - Switching back to A restores only A tasks.
    - Tidak ada task "stale" dari project sebelumnya.
    - /api/tasks & /api/tasks/queue/history tanpa project_id tetap "semua"
      (backward compatible).

Deterministik, tanpa API key/model (auto_execute=False + log fixture).

Jalankan:
    python scripts/check_sidebar_tasks_isolation.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "sidebar_iso"


def _write_task_log(root: Path, task_id: str, prompt: str, ts: str) -> None:
    """Tulis minimal task log JSONL di <root>/.aether/log/<task_id>.log."""
    log_dir = root / ".aether" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    events = [
        {"timestamp": ts, "event": "task_requested", "data": {"prompt": prompt}},
        {"timestamp": ts, "event": "task_completed", "data": {"result": "ok"}},
    ]
    (log_dir / f"{task_id}.log").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )


def _run() -> int:
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"

    import django

    django.setup()
    from django.test import Client

    import api.services as services_mod
    from api.project_store import ProjectStore
    from agent_ai.projects.registry import ProjectRegistry

    shutil.rmtree(FIXTURE, ignore_errors=True)
    root_a = FIXTURE / "a"
    root_b = FIXTURE / "b"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)
    # Log fixture: satu task selesai di masing-masing project.
    _write_task_log(root_a, "logA1", "Logged A task", "2026-01-01T00:00:00+00:00")
    _write_task_log(root_b, "logB1", "Logged B task", "2026-01-02T00:00:00+00:00")

    workspace = DUMMY_ROOT / "sidebar_iso_projects"
    shutil.rmtree(workspace, ignore_errors=True)
    registry = ProjectRegistry(workspace=workspace)
    tmp_store_dir = tempfile.mkdtemp(prefix="sidebar_iso_store_")
    store = ProjectStore(db_path=Path(tmp_store_dir) / "t.db")
    services_mod._default_service = services_mod.GatewayService(
        project_registry=registry,
        project_store=store,
        auto_execute=False,
    )
    service = services_mod._default_service
    client = Client()

    proj_a = service.create_project(name="ProjectA", path=str(root_a))
    proj_b = service.create_project(name="ProjectB", path=str(root_b))
    aid, bid = proj_a["id"], proj_b["id"]

    def mk_task(project_id, text):
        resp = client.post(
            "/api/tasks",
            data=json.dumps({"task": text, "project_id": project_id}),
            content_type="application/json",
        )
        assert resp.status_code == 201, (resp.status_code, resp.content)
        return resp.json()

    def queue(project_id=None):
        qs = f"?project_id={project_id}" if project_id else ""
        resp = client.get(f"/api/tasks/queue{qs}")
        assert resp.status_code == 200, resp.status_code
        return resp.json()["tasks"]

    def history(project_id=None):
        qs = f"?project_id={project_id}" if project_id else ""
        resp = client.get(f"/api/tasks/history{qs}")
        assert resp.status_code == 200, resp.status_code
        return resp.json()["tasks"]

    # --- Project A: buat 2 task QUEUE + 1 task HISTORY (log) ---------------
    a1 = mk_task(aid, "Task A1")
    a2 = mk_task(aid, "Task A2")

    q_a = queue(aid)
    assert [t["task_id"] for t in q_a] == [a1["task_id"], a2["task_id"]], q_a
    assert all(t["project_id"] == aid for t in q_a), "Queue A memuat task non-A"
    h_a = history(aid)
    assert "logA1" in [t["task_id"] for t in h_a], h_a
    assert all(t["task_id"] != "logB1" for t in h_a), "History A memuat task B (bocor)"
    print("[PASS] Project A: Queue + History hanya menampilkan task A")

    # --- Switch ke Project B ---------------------------------------------
    assert queue(bid) == [], "Queue B harus kosong di awal"
    b1 = mk_task(bid, "Task B1")

    q_b = queue(bid)
    assert [t["task_id"] for t in q_b] == [b1["task_id"]], q_b
    assert all(t["project_id"] == bid for t in q_b), "Queue B memuat task non-B"
    h_b = history(bid)
    ids_b = [t["task_id"] for t in h_b]
    assert "logB1" in ids_b, h_b
    assert "logA1" not in ids_b, "History B memuat task A (stale/bocor)"
    assert a1["task_id"] not in ids_b and a2["task_id"] not in ids_b, (
        "History B memuat task queue A (stale)"
    )
    print("[PASS] Switch ke Project B: Queue + History hanya menampilkan task B")

    # --- Kembali ke Project A: task A pulih, task B TIDAK ada ------------
    q_a2 = queue(aid)
    assert [t["task_id"] for t in q_a2] == [a1["task_id"], a2["task_id"]], q_a2
    assert b1["task_id"] not in [t["task_id"] for t in q_a2], "Task B muncul di A"
    h_a2 = history(aid)
    ids_a2 = [t["task_id"] for t in h_a2]
    assert "logA1" in ids_a2 and "logB1" not in ids_a2, "History A stale memuat task B"
    print("[PASS] Kembali ke Project A: Queue + History hanya task A (tidak ada B)")

    # --- Backward compatible: tanpa project_id -> daftar semua -----------
    all_q = queue(None)
    all_q_ids = {t["task_id"] for t in all_q}
    assert {a1["task_id"], a2["task_id"], b1["task_id"]} <= all_q_ids, all_q_ids
    all_h_ids = {t["task_id"] for t in history(None)}
    assert {"logA1", "logB1"} <= all_h_ids, all_h_ids
    print("[PASS] Tanpa project_id: Queue + History mengembalikan semua task")

    print()
    print("[OK] Sidebar -> Tasks (Queue + History) ter-isolasi per project.")
    return 0


def main() -> int:
    print("=== Verifikasi Isolasi Project: Sidebar -> Tasks ===")
    try:
        return _run()
    finally:
        shutil.rmtree(FIXTURE, ignore_errors=True)
        shutil.rmtree(DUMMY_ROOT / "sidebar_iso_projects", ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
