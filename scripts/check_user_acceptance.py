"""Acceptance test sesuai deskripsi pengguna:

- Buka project A, buat 3 task → sidebar hanya menampilkan 3 task milik A
- Pindah ke project B → sidebar hanya menampilkan task milik B (bisa kosong)
- Buat task baru di project B → task muncul di sidebar B, tidak di A
- Kembali ke project A → sidebar menampilkan 3 task A, tidak ada task B
- /api/tasks tanpa param → mengembalikan semua task (backward compatible)
- Task lama (project_id=None) tetap terlihat jika filter tidak diset
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

# Pastikan AETHER (src/) dan Django app dapat diimpor.
for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "accept_fixture"


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "app.py").write_text("print('hello')\n", encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Acceptance Test: Sidebar Tasks per Project ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "*")

    import django
    django.setup()
    from django.test import Client

    from django.conf import settings
    settings.ALLOWED_HOSTS.append("testserver")
    client = Client()

    # Setup backend
    import api.services as services_mod
    from agent_ai.projects.registry import ProjectRegistry
    from api.project_store import ProjectStore

    workspace = DUMMY_ROOT / "accept_projects"
    shutil.rmtree(workspace, ignore_errors=True)
    registry = ProjectRegistry(workspace=workspace)

    tmp_store_dir = tempfile.mkdtemp(prefix="accept_store_")
    store = ProjectStore(db_path=Path(tmp_store_dir) / "t.db")
    services_mod._default_service = services_mod.GatewayService(
        project_registry=registry,
        project_store=store,
        auto_execute=False,
    )
    service = services_mod._default_service

    # Buat project A dan B
    proj_a = service.create_project(name="ProjectA", path=str(FIXTURE / "a"))
    proj_b = service.create_project(name="ProjectB", path=str(FIXTURE / "b"))
    print(f"[SETUP] Project A: {proj_a['id']}, Project B: {proj_b['id']}")

    # Helper: buat task dan kembalikan dict
    def mk_task(project_id, text):
        resp = client.post(
            "/api/tasks",
            data=json.dumps({"task": text, "project_id": project_id}),
            content_type="application/json",
        )
        assert resp.status_code == 201, resp.status_code
        return resp.json()

    # Helper: ambil daftar task dengan optional project_id
    def get_tasks(project_id=None):
        params = f"?project_id={project_id}" if project_id else ""
        resp = client.get(f"/api/tasks{params}")
        assert resp.status_code == 200, resp.status_code
        return resp.json()["tasks"]

    # Helper: ambil antrian UI (listTaskQueue)
    def get_queue(project_id=None):
        params = f"?project_id={project_id}" if project_id else ""
        resp = client.get(f"/api/tasks/queue{params}")
        assert resp.status_code == 200, resp.status_code
        return resp.json()["tasks"]

    # 1) Buka project A, buat 3 task → sidebar hanya menampilkan 3 task milik A
    a1 = mk_task(proj_a["id"], "Task A1")
    a2 = mk_task(proj_a["id"], "Task A2")
    a3 = mk_task(proj_a["id"], "Task A3")
    tasks_a = get_tasks(proj_a["id"])
    assert len(tasks_a) == 3, f"Expected 3 tasks for A, got {len(tasks_a)}"
    assert all(t["project_id"] == proj_a["id"] for t in tasks_a)
    print("[PASS] Project A: 3 task dibuat, semua milik A")

    # 2) Pindah ke project B → sidebar hanya menampilkan task milik B (bisa kosong)
    tasks_b_initial = get_tasks(proj_b["id"])
    assert len(tasks_b_initial) == 0, f"Expected 0 tasks for B initially, got {len(tasks_b_initial)}"
    print("[PASS] Project B: awalnya kosong (seperti yang diharapkan)")

    # 3) Buat task baru di project B → task muncul di sidebar B, tidak di A
    b1 = mk_task(proj_b["id"], "Task B1")
    tasks_b_after = get_tasks(proj_b["id"])
    assert len(tasks_b_after) == 1, f"Expected 1 task for B after creation, got {len(tasks_b_after)}"
    assert tasks_b_after[0]["task_id"] == b1["task_id"]
    # Pastikan task B tidak masuk daftar A
    tasks_a_still = get_tasks(proj_a["id"])
    assert len(tasks_a_still) == 3, f"Project A masih harus memiliki 3 task, got {len(tasks_a_still)}"
    assert all(t["project_id"] == proj_a["id"] for t in tasks_a_still)
    task_ids_a = {t["task_id"] for t in tasks_a_still}
    assert b1["task_id"] not in task_ids_a, "Task B tidak boleh muncul di daftar A"
    print("[PASS] Project B: task B1 muncul di sidebar B, tidak ada di A")

    # 4) Kembali ke project A → sidebar menampilkan 3 task A, tidak ada task B
    tasks_a_final = get_tasks(proj_a["id"])
    assert len(tasks_a_final) == 3, f"Expected 3 tasks for A after switch back, got {len(tasks_a_final)}"
    assert all(t["project_id"] == proj_a["id"] for t in tasks_a_final)
    task_ids_a_final = {t["task_id"] for t in tasks_a_final}
    assert a1["task_id"] in task_ids_a_final
    assert a2["task_id"] in task_ids_a_final
    assert a3["task_id"] in task_ids_a_final
    assert b1["task_id"] not in task_ids_a_final
    print("[PASS] Kembali ke Project A: sidebar masih menunjukkan 3 task A, tidak ada task B")

    # 5) /api/tasks tanpa param → mengembalikan semua task (backward compatible)
    all_tasks = get_tasks(None)
    # Task yang dibuat: a1, a2, a3, b1, plus possible legacy task dari verifier sebelumnya?
    # Kita hanya memastikan bahwa 4 task kita ada di dalam hasil.
    all_task_ids = {t["task_id"] for t in all_tasks}
    assert a1["task_id"] in all_task_ids
    assert a2["task_id"] in all_task_ids
    assert a3["task_id"] in all_task_ids
    assert b1["task_id"] in all_task_ids
    print(f"[PASS] /api/tasks tanpa parameter mengembalikan semua task ({len(all_tasks)} task termasuk 4 kita)")

    # 6) Task lama (project_id=None) tetap terlihat jika filter tidak diset
    # Buat task tanpa project_id (legacy)
    legacy_resp = client.post(
        "/api/tasks",
        data=json.dumps({"task": "Legacy task tanpa project_id"}),
        content_type="application/json",
    )
    assert legacy_resp.status_code == 201, legacy_resp.status_code
    legacy = legacy_resp.json()
    assert legacy["project_id"] is None
    # Dalam daftar semua task, legacy harus ada
    all_tasks2 = get_tasks(None)
    assert any(t["task_id"] == legacy["task_id"] for t in all_tasks2), "Legacy task harus muncul saat tidak ada filter"
    # Namun saat filter project_id diset, legacy TIDAK boleh muncul
    for pid in (proj_a["id"], proj_b["id"]):
        filtered = get_tasks(pid)
        assert not any(t["task_id"] == legacy["task_id"] for t in filtered), f"Legacy task tidak boleh muncul saat filter project_id={pid}"
    print("[PASS] Task legacy (project_id=None) terlihat dalam daftar semua, disembunyikan saat filter aktif")

    # Bersihkan
    shutil.rmtree(workspace, ignore_errors=True)
    shutil.rmtree(tmp_store_dir, ignore_errors=True)

    print()
    print("[OK] Semua kriteria acceptansi pengguna terpenuhi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())