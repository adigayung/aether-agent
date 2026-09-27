"""Verifikasi Create Project: membuat folder untuk PURE EMPTY project.

Menguji titik yang diperbaiki:
    - Create Project dengan path BARU sederhana -> directory dibuat otomatis.
    - Create Project dengan NESTED path (parent belum ada) -> dibuat recursive.
    - Create Project dengan path DIRECTORY yang sudah ada -> tetap dipakai
      (perilaku existing, tidak ada file yang ditimpa/dihapus).
    - Create Project dengan path menunjuk ke FILE -> DITOLAK dengan error
      jelas (tanpa menghapus/memindahkan/mengubah file).
    - Project baru menjadi ACTIVE project dan workspace (GET /api/files pada
      root active project, jalur yang sama dipakai File Explorer) terbuka.
    - TIDAK ada template aplikasi/source yang dibuat otomatis: selain
      metadata/internal AETHER yang diwajibkan mekanisme registration
      (.aether/, project.json di registry), project TETAP kosong dari sisi
      aplikasi.

Deterministik, tanpa model/API cloud. Fixture dibuat di
J:\AETHER_DI_EDIT\aether-agent\dummy_test\create_project_fixture dan
dibersihkan setelah test. Store SQLite memakai file sementara (tidak
menyentuh data produksi).

Jalankan:
    python scripts/check_create_project_empty.py
"""

from __future__ import annotations

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
FIXTURE_ROOT = DUMMY_ROOT / "create_project_fixture"

# Template/source file yang TIDAK boleh dibuat otomatis oleh Create Project.
FORBIDDEN_AUTO_FILES = (
    "app.py",
    "main.py",
    "README.md",
    "requirements.txt",
    "package.json",
)
# Struktur metadata/internal AETHER yang DIWAJIBKAN mekanisme registration
# (ditulis ProjectRegistry -> ProjectIntelligence -> BibleStore).
REQUIRED_AETHER_DIRS = ("log", "bible")
REQUIRED_AETHER_FILES = ("index.md",)


def _run() -> int:
    import os

    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    # Force-set agar tidak tergantung environment luar (testserver wajib ada
    # untuk Django test client; SECRET_KEY dev aman untuk mode development).
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
    os.environ["AETHER_ENV"] = "development"
    os.environ["DJANGO_SECRET_KEY"] = "aether-gateway-dev-key-not-for-production"
    import django

    django.setup()

    from django.test import Client

    import api.services as services_mod
    from api.project_store import ProjectStore
    from agent_ai.projects.registry import ProjectRegistry

    # Isolasi: registry + store sementara (tidak menyentuh data produksi).
    workspace = DUMMY_ROOT / "create_project_registry"
    shutil.rmtree(workspace, ignore_errors=True)
    registry = ProjectRegistry(workspace=workspace)
    tmp_store_dir = tempfile.mkdtemp(prefix="create_project_store_")
    store = ProjectStore(db_path=Path(tmp_store_dir) / "t.db")
    service = services_mod.GatewayService(
        project_registry=registry, project_store=store, auto_execute=False
    )
    services_mod._default_service = service
    client = Client()

    def assert_no_app_files(root: Path, label: str) -> None:
        """Tidak boleh ada template/source aplikasi di root project target."""
        for fname in FORBIDDEN_AUTO_FILES:
            assert not (root / fname).exists(), (
                f"{label}: file aplikasi TIDAK boleh dibuat otomatis: {root / fname}"
            )

    # ------------------------------------------------------------------ #
    # 1) Path baru sederhana.
    # ------------------------------------------------------------------ #
    simple = FIXTURE_ROOT / "simple_project"
    project = service.create_project(name="SimpleEmpty", path=str(simple))
    pid = project["id"]
    assert simple.is_dir(), "directory baru harus dibuat"
    assert store.get_project(pid) is not None, "record SQLite harus ada"
    assert registry.project_dir(pid).exists(), "folder registry harus ada"
    assert service.get_active_project()["id"] == pid, "harus menjadi active project"
    assert project["root"] == str(simple), "root harus path yang sama dengan yang dibuat"
    assert_no_app_files(simple, "[1] simple")
    print(f"[1] path baru sederhana OK -> dibuat {simple} (active={pid[:8]}...)")

    # Workspace/Explorer: GET /api/files pada root active project harus sukses
    # (jalur yang sama dipakai File Explorer di frontend). ListFilesTool AETHER
    # mengabaikan folder internal .aether (sama seperti FileExplorer yang
    # menyembunyikannya via HIDDEN_NAMES), jadi dari sisi aplikasi project
    # terlihat KOSONG — persis perilaku "pure empty project".
    resp = client.get("/api/files?path=.")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    entries = resp.json().get("entries", [])
    assert isinstance(entries, list), resp.content
    names = {e.get("name") for e in entries}
    assert not names, (
        f"Explorer root TIDAK boleh menampilkan file aplikasi: {names}"
    )
    print("[1b] workspace (GET /api/files) terbuka untuk project baru OK -> kosong (pure empty)")

    # ------------------------------------------------------------------ #
    # 2) Nested path, parent belum ada -> recursive.
    # ------------------------------------------------------------------ #
    nested = FIXTURE_ROOT / "parent" / "mid" / "nested_project"
    assert not nested.exists(), "fixture nested harus belum ada"
    p2 = service.create_project(name="NestedEmpty", path=str(nested))
    pid2 = p2["id"]
    assert nested.is_dir(), "nested directory harus dibuat recursive"
    assert nested.parent.is_dir(), "parent harus dibuat"
    assert nested.parent.parent.is_dir(), "grand-parent harus dibuat"
    assert service.get_active_project()["id"] == pid2, "harus menjadi active project"
    assert_no_app_files(nested, "[2] nested")
    print(f"[2] nested path (parent belum ada) OK -> {nested} dibuat recursive")

    # ------------------------------------------------------------------ #
    # 3) Directory sudah ada + berisi file -> dipertahankan (tanpa timpa).
    # ------------------------------------------------------------------ #
    existing = FIXTURE_ROOT / "existing_dir"
    existing.mkdir(parents=True, exist_ok=True)
    keep_file = existing / "keep_me.txt"
    keep_file.write_text("jangan ditimpa\n", encoding="utf-8")
    p3 = service.create_project(name="ExistingDir", path=str(existing))
    pid3 = p3["id"]
    assert existing.is_dir(), "directory existing harus tetap ada"
    assert keep_file.read_text(encoding="utf-8") == "jangan ditimpa\n", (
        "file existing TIDAK boleh ditimpa"
    )
    assert service.get_active_project()["id"] == pid3
    print(f"[3] directory sudah ada OK -> {existing} dipertahankan, isi aman")

    # ------------------------------------------------------------------ #
    # 4) Path menunjuk ke FILE -> DITOLAK, file tidak diubah.
    # ------------------------------------------------------------------ #
    a_file = FIXTURE_ROOT / "i_am_a_file.txt"
    a_file.write_text("isi asli\n", encoding="utf-8")
    try:
        service.create_project(name="FileTarget", path=str(a_file))
        raise AssertionError("[4] path file harus DITOLAK (ValidationError)")
    except services_mod.ValidationError as exc:
        assert "file" in str(exc).lower() or "directory" in str(exc).lower(), str(exc)
    assert a_file.is_file(), "target harus tetap file (tidak diubah/dihapus)"
    assert a_file.read_text(encoding="utf-8") == "isi asli\n", "isi file harus utuh"
    # Via HTTP juga harus 400.
    resp = client.post(
        "/api/projects", data='{"name":"FileTarget","path":"%s"}' % str(a_file).replace("\\", "\\\\"),
        content_type="application/json",
    )
    assert resp.status_code == 400, (resp.status_code, resp.content)
    assert resp.json()["error"]["code"] == "validation_error", resp.content
    print(f"[4] path menunjuk ke file DITOLAK OK -> 400, file {a_file} utuh")

    # ------------------------------------------------------------------ #
    # 5) Tidak ada template/source aplikasi di SEMUA project yang dibuat.
    # ------------------------------------------------------------------ #
    for root in (simple, nested, existing):
        assert_no_app_files(root, "[5] scan")
    # Struktur metadata AETHER yang memang diwajibkan harus ada.
    aether = simple / ".aether"
    for d in REQUIRED_AETHER_DIRS:
        assert (aether / d).is_dir(), f".aether/{d} harus ada ({aether})"
    for f in REQUIRED_AETHER_FILES:
        assert (aether / "bible" / f).is_file(), f".aether/bible/{f} harus ada"
    print("[5] tanpa template aplikasi, hanya metadata AETHER OK")

    # ------------------------------------------------------------------ #
    # 6) Active project baru BISA langsung dipakai AETHER: GET /api/files
    #    sukses untuk project terakhir (active) + project pertama juga
    #    terdaftar & bisa dijadikan aktif via jalur open existing.
    # ------------------------------------------------------------------ #
    assert service.get_active_project()["id"] == pid3
    resp = client.get("/api/files?path=.")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    # Open project pertama via jalur existing (setActiveProject).
    resp = client.post(
        "/api/active-project",
        data='{"project_id":"%s"}' % pid,
        content_type="application/json",
    )
    assert resp.status_code == 200, (resp.status_code, resp.content)
    assert service.get_active_project()["id"] == pid
    resp = client.get("/api/files?path=.")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    print("[6] project baru bisa dibuka lagi & aktif -> workspace OK")

    # Cleanup.
    shutil.rmtree(workspace, ignore_errors=True)
    shutil.rmtree(tmp_store_dir, ignore_errors=True)

    print()
    print("[OK] Create Project membuat folder untuk PURE EMPTY project.")
    return 0


def main() -> int:
    print("=== Verifikasi Create Project (Pure Empty Project) ===")
    shutil.rmtree(FIXTURE_ROOT, ignore_errors=True)
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        return _run()
    finally:
        shutil.rmtree(FIXTURE_ROOT, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())