"""Verifikasi AETHER Code Editor (Monaco) — File Explorer -> "Open with Editor".

Deterministik, tanpa model/API cloud dan tanpa browser. Menguji:

Backend (API file existing, ADDITIVE):
    1. GET  /api/files/content?path=...  -> baca isi file project aktif
    2. POST /api/files/content           -> simpan isi file project aktif
    3. Workspace boundary: path keluar root ditolak
    4. File tidak ditemukan -> error jelas (bukan 500)
    5. Tanpa active project -> 404

Frontend (statis + mapping language via Node):
    6. Model dependency monaco-editor + dependency frontend terdaftar
    7. api.js memakai endpoint /files/content (read + write)
    8. Explorer: context menu "Open with Editor" + emit open-file-editor
    9. App.vue menghubungkan emit Explorer ke modal CodeEditor (:key per file)
   10. CodeEditor.vue: Monaco lazy, dirty state, Save/Discard/Cancel, dispose
   11. Deteksi language per extension (semua extension wajib)
   12. Style modal editor ada di styles.css (kontrak visual AETHER)

Jalankan:
    python scripts/check_code_editor.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
FRONTEND_DIR = PROJECT_ROOT / "web" / "frontend"
SRC_FRONTEND = FRONTEND_DIR / "src"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "code_editor_fixture"

JS_CONTENT = "// fixture\nconst value = 1;\n"
PY_CONTENT = 'print("hello")\n'


def setup_fixture() -> None:
    """Fixture project nyata: file JS + file Python + subfolder."""
    shutil.rmtree(FIXTURE, ignore_errors=True)
    (FIXTURE / "src").mkdir(parents=True, exist_ok=True)
    (FIXTURE / "main.js").write_text(JS_CONTENT, encoding="utf-8")
    (FIXTURE / "src" / "app.py").write_text(PY_CONTENT, encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------
def check_backend(tmp: Path) -> None:
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
    django.setup()

    from django.test import Client

    import api.views as views
    from api.project_store import ProjectStore
    from api.services import GatewayService

    # Store terpisah (temp DB) + active project = fixture. Tidak menyentuh
    # data/aether.db maupun project user.
    service = GatewayService(
        auto_execute=False,
        project_store=ProjectStore(db_path=tmp / "check_code_editor.db"),
    )
    service.get_active_project = lambda: {  # type: ignore[assignment]
        "id": "fixture",
        "name": "fixture",
        "path": str(FIXTURE),
        "root": str(FIXTURE),
    }
    views.get_service = lambda: service  # type: ignore[assignment]

    client = Client()

    # [1] Baca file via API (root + nested).
    resp = client.get("/api/files/content", {"path": "main.js"})
    assert resp.status_code == 200, f"GET read gagal: {resp.status_code} {resp.content}"
    payload = resp.json()
    assert payload["content"] == JS_CONTENT, f"isi file salah: {payload['content']!r}"
    assert payload["path"] == "main.js"
    nested = client.get("/api/files/content", {"path": "src/app.py"})
    assert nested.status_code == 200, nested.status_code
    assert nested.json()["content"] == PY_CONTENT
    print("[1] GET /api/files/content membaca file (root + subfolder): OK")

    # [2] Simpan file via API -> isi di disk benar-benar berubah.
    new_content = "// edited by code editor\nconst value = 2;\n"
    resp = client.post(
        "/api/files/content",
        data=json.dumps({"path": "main.js", "content": new_content}),
        content_type="application/json",
    )
    assert resp.status_code == 200, f"POST save gagal: {resp.status_code} {resp.content}"
    assert resp.json().get("written") is True, resp.json()
    assert (FIXTURE / "main.js").read_text(encoding="utf-8") == new_content, (
        "file di disk tidak berubah setelah save"
    )
    # Save file baru (path belum ada) juga memakai jalur yang sama.
    resp = client.post(
        "/api/files/content",
        data=json.dumps({"path": "src/baru.txt", "content": "baru\n"}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert (FIXTURE / "src" / "baru.txt").read_text(encoding="utf-8") == "baru\n"
    print("[2] POST /api/files/content menyimpan file ke disk (existing + baru): OK")
    (FIXTURE / "src" / "baru.txt").unlink()

    # [3] Workspace boundary: path keluar root DITOLAK (read & write).
    for bad in ("../outside.txt", "src/../../outside.txt"):
        r = client.get("/api/files/content", {"path": bad})
        assert r.status_code == 400, f"path traversal read harus 400: {bad} -> {r.status_code}"
        w = client.post(
            "/api/files/content",
            data=json.dumps({"path": bad, "content": "x"}),
            content_type="application/json",
        )
        assert w.status_code == 400, f"path traversal write harus 400: {bad} -> {w.status_code}"
    print("[3] Workspace boundary: path di luar root ditolak (read & write): OK")

    # [4] File tidak ditemukan / field kosong -> 400 dengan pesan jelas.
    r = client.get("/api/files/content", {"path": "tidak-ada.js"})
    assert r.status_code == 400, r.status_code
    assert r.json()["error"]["message"], "pesan error harus jelas"
    r = client.get("/api/files/content")
    assert r.status_code == 400, "path kosong harus 400"
    r = client.post(
        "/api/files/content",
        data=json.dumps({"path": "main.js"}),
        content_type="application/json",
    )
    assert r.status_code == 400, "content kosong harus 400"
    print("[4] File tidak ditemukan / input tidak lengkap -> 400 (pesan jelas): OK")

    # [5] Tanpa active project -> 404.
    service.get_active_project = lambda: None  # type: ignore[assignment]
    r = client.get("/api/files/content", {"path": "main.js"})
    assert r.status_code == 404, f"tanpa active project harus 404: {r.status_code}"
    print("[5] Tanpa active project -> 404: OK")


# ---------------------------------------------------------------------------
# Frontend (statis)
# ---------------------------------------------------------------------------
def check_frontend_static() -> None:
    pkg = json.loads(_read(FRONTEND_DIR / "package.json"))
    assert "monaco-editor" in pkg.get("dependencies", {}), (
        "monaco-editor harus terdaftar sebagai dependency frontend"
    )

    api_src = _read(SRC_FRONTEND / "api.js")
    assert "/files/content" in api_src, "api.js harus memakai endpoint /files/content"
    assert "readFileContent" in api_src and "writeFileContent" in api_src, (
        "api.js harus menyediakan readFileContent + writeFileContent"
    )
    print("[6] Dependency monaco-editor + API file (read/write) terdaftar: OK")

    explorer_src = _read(SRC_FRONTEND / "components" / "FileExplorer.vue")
    assert "Open with Editor" in explorer_src, "context menu harus punya 'Open with Editor'"
    assert 'emit("open-file-editor"' in explorer_src, (
        "Explorer harus emit 'open-file-editor' (action tambahan, tidak mengubah action lain)"
    )
    for keep in ("ctxOpen(", "ctxCopyPath(", "ctxReveal(", "ctxDelete("):
        assert keep in explorer_src, f"action Explorer existing tidak boleh hilang: {keep}"
    print("[7] Explorer: 'Open with Editor' + emit open-file-editor (action lain utuh): OK")

    app_src = _read(SRC_FRONTEND / "App.vue")
    assert 'import CodeEditor from "./components/CodeEditor.vue"' in app_src
    assert '@open-file-editor="openFileInEditor"' in app_src, (
        "App harus menghubungkan emit Explorer ke handler editor"
    )
    assert ":key=\"editorFile.path\"" in app_src, (
        "editor harus di-key per path (instance Monaco tidak bocor antar file)"
    )
    assert "@open-file=\"openFileInEditor\"" in app_src, (
        "klik file / Edit di CHANGES tetap membuka editor (dummy lama diganti)"
    )
    print("[8] App.vue: emit Explorer -> modal CodeEditor (:key per file): OK")

    editor_src = _read(SRC_FRONTEND / "components" / "CodeEditor.vue")
    for needle, label in (
        ('import("../monacoSetup.js")', "Monaco dimuat lazy (dynamic import)"),
        ("readFileContent(", "load file lewat API backend"),
        ("writeFileContent(", "save lewat API backend"),
        ("getAlternativeVersionId", "dirty state dari versi model Monaco"),
        ("editor.dispose()", "Monaco di-dispose saat unmount"),
        ("model.dispose()", "model Monaco di-dispose saat unmount"),
        ("ResizeObserver", "resize handler di-cleanup"),
        ("removeEventListener", "event listener di-cleanup"),
        ("Unsaved Changes", "confirmation modal unsaved changes"),
        ("confirmSave", "perilaku Save pada konfirmasi"),
        ("confirmDiscard", "perilaku Discard pada konfirmasi"),
        ("confirmCancel", "perilaku Cancel pada konfirmasi"),
        ("requestClose", "semua jalur close tunduk aturan unsaved changes"),
        ("e.key === \"Escape\"", "Escape memakai aturan unsaved changes"),
        ("btn-primary", "tombol Save"),
        ("close-x", "tombol Close"),
    ):
        assert needle in editor_src, f"CodeEditor.vue harus memuat {label} ({needle})"
    assert "automaticLayout" not in editor_src, (
        "layout editor diurus resize handler sendiri (bukan automaticLayout)"
    )
    print("[9] CodeEditor.vue: lazy Monaco, dirty, save/discard/cancel, cleanup: OK")

    setup_src = _read(SRC_FRONTEND / "monacoSetup.js")
    for needle in (
        "monaco-editor",
        "?worker",
        "MonacoEnvironment",
        "defineTheme",
        "minimap",
        "folding",
    ):
        assert needle in setup_src, f"monacoSetup.js harus memuat '{needle}'"
    print("[10] monacoSetup.js: worker Monaco + theme AETHER + opsi editor: OK")

    css_src = _read(SRC_FRONTEND / "styles.css")
    for needle in (".modal.editor-m", ".ed-head", ".ed-monaco", ".ed-status", ".ed-confirm"):
        assert needle in css_src, f"styles.css harus memuat style editor '{needle}'"
    assert ".explorer {" in css_src, "kontrak scroll Explorer harus tetap ada"
    print("[11] styles.css: style modal editor + kontrak Explorer tetap: OK")


# ---------------------------------------------------------------------------
# Language detection (jalankan mapping frontend lewat Node)
# ---------------------------------------------------------------------------
def check_language_detection() -> None:
    node = shutil.which("node")
    assert node, "node tidak tersedia untuk verifikasi mapping language"
    expected = {
        "a.js": "javascript",
        "a.jsx": "javascript",
        "a.ts": "typescript",
        "a.tsx": "typescript",
        "a.vue": "html",
        "a.html": "html",
        "a.css": "css",
        "a.scss": "scss",
        "a.json": "json",
        "a.md": "markdown",
        "a.py": "python",
        "a.php": "php",
        "a.java": "java",
        "a.c": "c",
        "a.h": "c",
        "a.cpp": "cpp",
        "a.hpp": "cpp",
        "a.xml": "xml",
        "a.yaml": "yaml",
        "a.yml": "yaml",
        "a.toml": "ini",
        "a.sql": "sql",
        # tidak dikenal / tanpa extension -> plaintext
        "a.unknownext": "plaintext",
        "Dockerfile": "plaintext",
    }
    script = (
        "import { languageForFile } from './src/editorLanguages.js';\n"
        f"const expected = {json.dumps(expected)};\n"
        "const out = {};\n"
        "for (const k of Object.keys(expected)) out[k] = languageForFile(k);\n"
        "// path Windows + nested harus tetap terdeteksi dari basename\n"
        "out['nested'] = languageForFile('src\\\\\\\\web/frontend/main.js');\n"
        "process.stdout.write(JSON.stringify({ expected, out }));\n"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(FRONTEND_DIR),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"node gagal:\n{result.stdout}\n{result.stderr}"
    data = json.loads(result.stdout)
    for name, want in data["expected"].items():
        got = data["out"][name]
        assert got == want, f"language untuk {name}: {got} != {want}"
    assert data["out"]["nested"] == "javascript", (
        f"deteksi dari basename (path Windows) salah: {data['out']['nested']}"
    )
    print(f"[12] Deteksi language {len(data['expected'])} nama file (+ path Windows): OK")


def main() -> int:
    print("=== Verifikasi AETHER Code Editor (Monaco) ===")
    tmp = Path(tempfile.mkdtemp(prefix="aether_editor_check_"))
    setup_fixture()
    try:
        check_backend(tmp)
        check_frontend_static()
        check_language_detection()
    finally:
        teardown_fixture()
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("[OK] Code Editor (Monaco) terintegrasi dengan Explorer + API file existing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
