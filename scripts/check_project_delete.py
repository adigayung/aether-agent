"""Verifikasi Delete Project (Project Launcher) + Close Project.

Menguji titik yang diperbaiki:
    - Delete menghapus RECORD project dari SQLite AETHER (persistence nyata).
    - Delete TIDAK menghapus/mengubah folder/filesystem project (target maupun
      metadata registry AETHER).
    - Setelah delete, daftar launcher langsung tidak memuat project tersebut.
    - Bila project yang dihapus sedang aktif, active project dibersihkan.
    - Delete tetap bekerja walau folder registry project tidak ada.
    - Close Project (clear active) tidak menghapus record project.

Deterministik, tanpa model/API cloud. Fixture project dibuat di
J:\\Agent_Ai\\dummy_test\\project_delete_fixture dan dibersihkan setelah test.
Store SQLite memakai file sementara (tidak menyentuh data produksi).

Jalankan:
    python scripts/check_project_delete.py
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
FIXTURE = DUMMY_ROOT / "project_delete_fixture"


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "keep_me.txt").write_text("jangan dihapus\n", encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def _run() -> int:
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
    import django

    django.setup()

    from django.test import Client

    import api.services as services_mod
    from api.project_store import ProjectStore
    from agent_ai.projects.registry import ProjectRegistry

    # Isolasi: registry + store sementara (tidak menyentuh data produksi).
    workspace = DUMMY_ROOT / "project_delete_registry"
    shutil.rmtree(workspace, ignore_errors=True)
    registry = ProjectRegistry(workspace=workspace)
    tmp_store_dir = tempfile.mkdtemp(prefix="delete_store_")
    store = ProjectStore(db_path=Path(tmp_store_dir) / "t.db")
    service = services_mod.GatewayService(
        project_registry=registry, project_store=store, auto_execute=False
    )
    services_mod._default_service = service
    client = Client()

    # 1) Buat project via jalur resmi (registry AETHER + record SQLite).
    project = service.create_project(name="DeleteFixture", path=str(FIXTURE))
    pid = project["id"]
    assert store.get_project(pid) is not None, "record SQLite harus ada"
    assert registry.project_dir(pid).exists(), "folder registry harus ada"
    print(f"[1] project dibuat OK -> {pid[:8]}... (record SQLite + folder registry)")

    # 2) Delete via HTTP -> record SQLite terhapus (persistence nyata).
    resp = client.delete(f"/api/projects/{pid}")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    assert resp.json().get("deleted") is True, resp.content
    assert store.get_project(pid) is None, "record SQLite harus terhapus"
    print("[2] DELETE /api/projects/<id> menghapus RECORD SQLite OK")

    # 3) Filesystem TIDAK disentuh (folder project target tetap ada).
    assert FIXTURE.exists(), "folder project target TIDAK boleh dihapus"
    assert (FIXTURE / "keep_me.txt").exists(), "file project target TIDAK boleh dihapus"
    # Folder metadata registry AETHER juga TIDAK dihapus (delete = record saja).
    assert registry.project_dir(pid).exists(), "folder registry TIDAK boleh dihapus"
    print("[3] filesystem project TIDAK dihapus/diubah OK")

    # 4) Daftar launcher langsung tidak memuat project tersebut.
    resp = client.get("/api/projects")
    projects = resp.json()["projects"]
    assert all(p["id"] != pid for p in projects), projects
    print("[4] daftar launcher langsung diperbarui (project hilang) OK")

    # 5) Delete project yang sedang aktif -> active project dibersihkan.
    p2 = service.create_project(name="ActiveFixture", path=str(FIXTURE))
    pid2 = p2["id"]
    assert service.get_active_project()["id"] == pid2
    resp = client.delete(f"/api/projects/{pid2}")
    assert resp.status_code == 200, resp.content
    assert service.get_active_project() is None, "active project harus dibersihkan"
    print("[5] delete project aktif -> active project dibersihkan OK")

    # 6) Delete tetap bekerja walau folder registry tidak ada.
    p3 = service.create_project(name="NoRegistryFixture", path=str(FIXTURE))
    pid3 = p3["id"]
    shutil.rmtree(registry.project_dir(pid3), ignore_errors=True)
    assert not registry.project_dir(pid3).exists()
    resp = client.delete(f"/api/projects/{pid3}")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    assert store.get_project(pid3) is None, "record SQLite harus terhapus"
    print("[6] delete tetap bekerja walau folder registry tidak ada OK")

    # 7) Delete project yang tidak ada -> 404 (bukan crash).
    resp = client.delete("/api/projects/tidak_ada")
    assert resp.status_code == 404, resp.status_code
    assert resp.json()["error"]["code"] == "not_found", resp.content
    print("[7] delete project tidak ada -> 404 terstruktur OK")

    # 8) Close Project: clear active, record project TETAP ada.
    p4 = service.create_project(name="CloseFixture", path=str(FIXTURE))
    pid4 = p4["id"]
    assert service.get_active_project()["id"] == pid4
    resp = client.delete("/api/active-project")
    assert resp.status_code == 200, resp.content
    assert service.get_active_project() is None, "active harus clear"
    assert store.get_project(pid4) is not None, "record project harus TETAP ada"
    print("[8] Close Project clear active, record project tetap ada OK")

    # Cleanup.
    shutil.rmtree(workspace, ignore_errors=True)
    shutil.rmtree(tmp_store_dir, ignore_errors=True)

    print()
    print("[OK] Delete Project + Close Project bekerja (record SQLite, filesystem aman).")
    return 0


def main() -> int:
    print("=== Verifikasi Delete Project + Close Project ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


if __name__ == "__main__":
    raise SystemExit(main())
