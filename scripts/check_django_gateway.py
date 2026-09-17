"""Verifikasi Django API / Gateway (#50).

Deterministik, tanpa API key/model/API cloud. Memakai Django test client.
Fixture project dibuat di J:\\Agent_Ai\\dummy_test\\gateway_fixture dan
dibersihkan setelah test.

Menguji:
    1. Django project dapat di-load
    2. health endpoint
    3. project endpoint memakai Project Registry AETHER
    4. task creation
    5. task lookup
    6. request validation
    7. error response
    8. boundary: tidak ada Agent Runtime/Loop/Planning/Tool implementation baru
    9. boundary: gateway memakai komponen AETHER yang sudah ada
   10. dependency: tanpa DRF/Celery/Redis/Channels/ORM

Jalankan:
    python scripts/check_django_gateway.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"

# Pastikan AETHER (src/) dan Django app dapat diimpor.
for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "gateway_fixture"


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "app.py").write_text("print('hello')\n", encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Django API / Gateway (#50) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    # Django test client mengirim Host: testserver. Tambahkan ke ALLOWED_HOSTS
    # (hardening #57 membatasi host; ini hanya untuk verifier, bukan produksi).
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

    # 1) Django project dapat di-load.
    import django

    django.setup()
    from django.test import Client

    client = Client()
    print("[1] Django project load OK -> settings + urls")

    # 2) health endpoint.
    resp = client.get("/api/health")
    assert resp.status_code == 200, resp.status_code
    body = resp.json()
    assert body["status"] == "ok", body
    assert body["service"] == "aether-gateway"
    print(f"[2] health endpoint OK -> {body}")

    # 3) project endpoint memakai Project Registry AETHER.
    #    Daftarkan project uji via ProjectRegistry AETHER (bukan registry Django).
    from agent_ai.projects.registry import ProjectRegistry

    workspace = DUMMY_ROOT / "gateway_projects"
    shutil.rmtree(workspace, ignore_errors=True)
    registry = ProjectRegistry(workspace=workspace)

    # Override service default agar memakai registry uji (tanpa menyentuh
    # registry produksi). Ini hanya untuk verifier.
    # auto_execute=False: verifier ini fokus pada gateway HTTP (bukan eksekusi
    # runtime). Eksekusi nyata diuji terpisah di check_web_runtime.py.
    # project_store terisolasi (SQLite sementara) agar tidak menyentuh data
    # produksi; daftar launcher dibaca dari store ini.
    import tempfile

    import api.services as services_mod
    from api.project_store import ProjectStore

    tmp_store_dir = tempfile.mkdtemp(prefix="gateway_store_")
    store = ProjectStore(db_path=Path(tmp_store_dir) / "t.db")
    services_mod._default_service = services_mod.GatewayService(
        project_registry=registry, project_store=store, auto_execute=False
    )
    service = services_mod._default_service

    # Daftarkan project uji lewat jalur resmi (registry AETHER + record SQLite).
    project = service.create_project(name="GatewayFixture", path=str(FIXTURE))

    resp = client.get("/api/projects")
    assert resp.status_code == 200, resp.status_code
    projects = resp.json()["projects"]
    assert any(p["id"] == project["id"] for p in projects), projects
    # Pastikan data konsisten (field khas launcher: id/name/path/root).
    sample = [p for p in projects if p["id"] == project["id"]][0]
    assert "root" in sample and "path" in sample, sample
    print(f"[3] project endpoint (ProjectRegistry AETHER) OK -> {len(projects)} project")

    # 4) task creation.
    resp = client.post(
        "/api/tasks",
        data=json.dumps({"task": "buat fungsi add(a, b)", "project_id": project["id"]}),
        content_type="application/json",
    )
    assert resp.status_code == 201, (resp.status_code, resp.content)
    created = resp.json()
    assert created["task"] == "buat fungsi add(a, b)"
    assert created["project_id"] == project["id"]
    assert created["status"] == "prepared"
    assert created["task_id"]
    assert "prepared" in created
    task_id = created["task_id"]
    print(f"[4] task creation OK -> task_id={task_id[:8]}..., status={created['status']}")

    # 5) task lookup.
    resp = client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200, resp.status_code
    fetched = resp.json()
    assert fetched["task_id"] == task_id
    assert fetched["task"] == created["task"]
    print(f"[5] task lookup OK -> {fetched['task_id'][:8]}...")

    # 6) request validation.
    #    - task kosong -> 400
    resp = client.post("/api/tasks", data=json.dumps({"task": ""}), content_type="application/json")
    assert resp.status_code == 400, resp.status_code
    assert resp.json()["error"]["code"] == "validation_error"
    #    - task hilang -> 400
    resp = client.post("/api/tasks", data=json.dumps({}), content_type="application/json")
    assert resp.status_code == 400, resp.status_code
    #    - body bukan JSON -> 400
    resp = client.post("/api/tasks", data="bukan json", content_type="application/json")
    assert resp.status_code == 400, resp.status_code
    #    - metadata bukan object -> 400
    resp = client.post(
        "/api/tasks",
        data=json.dumps({"task": "x", "metadata": "bukan object"}),
        content_type="application/json",
    )
    assert resp.status_code == 400, resp.status_code
    #    - project_id tidak ada -> 404
    resp = client.post(
        "/api/tasks",
        data=json.dumps({"task": "x", "project_id": "tidak_ada"}),
        content_type="application/json",
    )
    assert resp.status_code == 404, resp.status_code
    print("[6] request validation OK -> 400/404 untuk input tidak valid")

    # 7) error response.
    #    - task tidak ditemukan -> 404 terstruktur
    resp = client.get("/api/tasks/tidak_ada")
    assert resp.status_code == 404, resp.status_code
    err = resp.json()
    assert err["error"]["code"] == "not_found", err
    assert "message" in err["error"]
    #    - method tidak diizinkan -> 405
    resp = client.delete("/api/health")
    assert resp.status_code == 405, resp.status_code
    print("[7] error response OK -> struktur {error:{code,message}}")

    # 8) boundary: tidak ada Agent Runtime/Loop/Planning/Tool implementation baru.
    api_dir = DJANGO_APP_DIR / "api"
    forbidden_classes = (
        "class AgentRuntime",
        "class AgentLoop",
        "class AgentOrchestrator",
        "class TaskPlanner",
        "class Replanner",
        "class ToolExecutor",
        "class RecoveryManager",
        "class FallbackManager",
        "class ValidationRunner",
        "class ProjectIntelligence",
        "class SessionStore",
    )
    for p in api_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        for bad in forbidden_classes:
            assert bad not in text, f"{p.name} tidak boleh mendefinisikan '{bad}'"
    print("[8] boundary OK -> tanpa implementasi Agent/Runtime/Planning/Tool baru")

    # 9) boundary: gateway memakai komponen AETHER yang sudah ada.
    services_src = (api_dir / "services.py").read_text(encoding="utf-8")
    assert "from agent_ai.projects.registry import" in services_src, "harus pakai ProjectRegistry AETHER"
    assert "from agent_ai.task.preparation import" in services_src, "harus pakai TaskPreparation AETHER"
    # Gateway boleh MEMANGGIL AETHER Runtime (via execution bridge #55), tetapi
    # TIDAK boleh mendefinisikan ulang Runtime/Orchestrator/Loop (sudah dicek di [8]).
    # services.py sendiri tetap tipis: import runtime hanya lewat api.execution.
    for bad in ("agent_ai.core.orchestrator", "agent_ai.core.loop"):
        assert bad not in services_src, f"services.py tidak boleh impor '{bad}'"
    print("[9] boundary OK -> memakai ProjectRegistry + TaskPreparation AETHER")

    # 10) dependency: tanpa DRF/Celery/Redis/Channels/ORM.
    settings_src = (DJANGO_APP_DIR / "config" / "settings.py").read_text(encoding="utf-8")
    for bad in ("rest_framework", "celery", "channels", "redis"):
        assert bad not in settings_src, f"settings tidak boleh memakai '{bad}'"
    assert "DATABASES = {}" in settings_src, "gateway tidak boleh memakai database ORM"
    # Views memakai Django views biasa, bukan DRF.
    views_src = (api_dir / "views.py").read_text(encoding="utf-8")
    assert "rest_framework" not in views_src, "views tidak boleh memakai DRF"
    assert "JsonResponse" in views_src, "views harus memakai JsonResponse (Django biasa)"
    print("[10] dependency OK -> Django views biasa, tanpa DRF/Celery/Redis/ORM")

    # Cleanup registry uji + store sementara.
    shutil.rmtree(workspace, ignore_errors=True)
    shutil.rmtree(tmp_store_dir, ignore_errors=True)

    print()
    print("[OK] Django Gateway bekerja (HTTP tipis -> AETHER, tanpa logic agent).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
