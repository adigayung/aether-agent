"""Verifikasi tombol Delete Project Launcher (klik nyata -> modal -> delete).

Bug yang diperbaiki: tombol `.project-delete` terasa "tidak bekerja" karena
modal konfirmasi TIDAK PERNAH terlihat. Penyebabnya: Bootstrap 5 juga
mendefinisikan `.modal { display: none }` dan `.modal-backdrop`. Custom CSS
AETHER untuk `.modal` TIDAK men-set `display`, sehingga `display:none` dari
Bootstrap tetap berlaku -> modal konfirmasi tidak muncul saat tombol diklik.

Verifier ini memeriksa BUNDLE CSS yang benar-benar disajikan (dist), bukan
hanya source, sehingga regresi cascade Bootstrap vs custom terdeteksi.

Menguji:
    1. Bundle CSS memuat custom `.modal` AETHER dengan `display` eksplisit
       (menimpa `display:none` Bootstrap).
    2. Custom `.modal-backdrop` AETHER punya `display:flex` + z-index tinggi.
    3. Custom rule datang SETELAH rule Bootstrap (cascade menang).
    4. Bundle JS memuat handler delete (class `.project-delete`, endpoint
       DELETE `/api/projects/<id>`, refresh daftar).
    5. Alur nyata via HTTP: create -> DELETE -> record SQLite hilang ->
       filesystem project TETAP ada.

Deterministik, tanpa model/API cloud. Fixture di
J:\\Agent_Ai\\dummy_test\\delete_button_fixture dan dibersihkan setelah test.

Jalankan:
    python scripts/check_delete_button.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
FRONTEND_DIST = PROJECT_ROOT / "web" / "frontend" / "dist"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "delete_button_fixture"


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "keep_me.txt").write_text("jangan dihapus\n", encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def _read_bundle_css() -> str:
    css_files = sorted(FRONTEND_DIST.glob("assets/*.css"))
    assert css_files, f"bundle CSS tidak ditemukan di {FRONTEND_DIST}"
    return "\n".join(p.read_text(encoding="utf-8") for p in css_files)


def _read_bundle_js() -> str:
    js_files = sorted(FRONTEND_DIST.glob("assets/*.js"))
    assert js_files, f"bundle JS tidak ditemukan di {FRONTEND_DIST}"
    return "\n".join(p.read_text(encoding="utf-8") for p in js_files)


def _rule_body(css: str, selector: str) -> str | None:
    """Ambil body rule terakhir untuk selector (tanpa pseudo/varian)."""
    # Cocokkan `selector{...}` persis (bukan `.modal-backdrop` saat cari `.modal`).
    pattern = re.compile(re.escape(selector) + r"\{([^}]*)\}")
    matches = pattern.findall(css)
    return matches[-1] if matches else None


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

    css = _read_bundle_css()
    js = _read_bundle_js()

    # 1) Custom `.modal` AETHER harus menimpa layout Bootstrap.
    #    Bootstrap mendefinisikan `.modal{...display:none; position:fixed;
    #    top:0; left:0; width:100%; height:100%...}`. Bila custom tidak
    #    menimpa SEMUA properti itu, modal konfirmasi tidak terlihat atau
    #    salah posisi (menempel kiri-atas full-height) sehingga tombol Delete
    #    terasa "tidak bekerja".
    modal_body = _rule_body(css, ".modal")
    assert modal_body is not None, "custom .modal tidak ditemukan di bundle CSS"
    modal_compact = modal_body.replace(" ", "")
    assert "display:block" in modal_compact, (
        "custom .modal HARUS men-set display (menimpa display:none Bootstrap): "
        f"{modal_body}"
    )
    # Netralkan layout fixed full-screen Bootstrap.
    assert "position:static" in modal_compact, (
        "custom .modal HARUS position:static (menimpa position:fixed Bootstrap): "
        f"{modal_body}"
    )
    assert "width:auto" in modal_compact, (
        "custom .modal HARUS width:auto (menimpa width:100% Bootstrap): "
        f"{modal_body}"
    )
    assert "height:auto" in modal_compact, (
        "custom .modal HARUS height:auto (menimpa height:100% Bootstrap): "
        f"{modal_body}"
    )
    print("[1] custom .modal menimpa layout Bootstrap (display/position/size) OK")

    # 2) Custom `.modal-backdrop` AETHER: display:flex + z-index tinggi.
    backdrop_body = _rule_body(css, ".modal-backdrop")
    assert backdrop_body is not None, "custom .modal-backdrop tidak ditemukan"
    compact = backdrop_body.replace(" ", "")
    assert "display:flex" in compact, f".modal-backdrop harus display:flex: {backdrop_body}"
    assert "align-items:center" in compact, (
        f".modal-backdrop harus align-items:center: {backdrop_body}"
    )
    assert "justify-content:center" in compact, (
        f".modal-backdrop harus justify-content:center: {backdrop_body}"
    )
    assert "z-index:1050" in compact or "z-index:1040" in compact, (
        f".modal-backdrop harus z-index tinggi (di atas konten): {backdrop_body}"
    )
    print("[2] custom .modal-backdrop display:flex + centering + z-index OK")

    # 3) Custom rule datang SETELAH rule Bootstrap (cascade menang).
    bs_modal = css.find(".modal{--bs-modal-zindex")
    custom_modal = css.find(".modal{position:static")
    assert bs_modal >= 0, "rule Bootstrap .modal tidak ditemukan (bundle berubah?)"
    assert custom_modal >= 0, "custom .modal{position:static} tidak ditemukan"
    assert custom_modal > bs_modal, (
        "custom .modal harus SETELAH Bootstrap agar cascade menang"
    )
    print("[3] custom rule setelah Bootstrap (cascade menang) OK")

    # 4) Bundle JS memuat handler delete + endpoint + refresh.
    assert "project-delete" in js, "bundle JS harus memuat class .project-delete"
    assert "Delete project record" in js, "bundle JS harus memuat tombol delete"
    assert "modal-backdrop" in js, "bundle JS harus memuat modal konfirmasi"
    assert "btn-danger" in js, "bundle JS harus memuat tombol konfirmasi Delete"
    assert "projects/" in js, "bundle JS harus memuat endpoint /api/projects/<id>"
    assert "DELETE" in js, "bundle JS harus memakai HTTP method DELETE"
    assert "Failed to delete project" in js, "bundle JS harus menangani error delete"
    print("[4] bundle JS memuat handler delete + endpoint + error handling OK")

    # 5) Alur nyata via HTTP: create -> DELETE -> SQLite hilang, filesystem aman.
    workspace = DUMMY_ROOT / "delete_button_registry"
    shutil.rmtree(workspace, ignore_errors=True)
    registry = ProjectRegistry(workspace=workspace)
    tmp_store_dir = tempfile.mkdtemp(prefix="delete_button_store_")
    store = ProjectStore(db_path=Path(tmp_store_dir) / "t.db")
    service = services_mod.GatewayService(
        project_registry=registry, project_store=store, auto_execute=False
    )
    services_mod._default_service = service
    client = Client()

    project = service.create_project(name="DeleteButtonFixture", path=str(FIXTURE))
    pid = project["id"]
    assert store.get_project(pid) is not None

    # Simulasi klik tombol: DELETE /api/projects/<id>.
    resp = client.delete(f"/api/projects/{pid}")
    assert resp.status_code == 200, (resp.status_code, resp.content)
    assert resp.json().get("deleted") is True, resp.content

    # Record SQLite benar-benar hilang.
    assert store.get_project(pid) is None, "record SQLite harus terhapus"
    # Daftar launcher langsung tidak memuat project (UI refresh).
    resp = client.get("/api/projects")
    assert all(p["id"] != pid for p in resp.json()["projects"])
    # Filesystem project TIDAK ikut terhapus.
    assert FIXTURE.exists() and (FIXTURE / "keep_me.txt").exists(), (
        "filesystem project TIDAK boleh terhapus"
    )
    print("[5] alur nyata create->DELETE: SQLite hilang, filesystem aman OK")

    shutil.rmtree(workspace, ignore_errors=True)
    shutil.rmtree(tmp_store_dir, ignore_errors=True)

    print()
    print("[OK] Tombol Delete Project Launcher bekerja (modal tampil + record terhapus).")
    return 0


def main() -> int:
    print("=== Verifikasi Tombol Delete Project Launcher ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


if __name__ == "__main__":
    raise SystemExit(main())
