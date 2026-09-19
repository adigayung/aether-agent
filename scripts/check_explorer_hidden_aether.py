"""Verifikasi folder .aether TIDAK muncul di File Explorer AETHER.

Masalah: File Explorer (web/frontend/src/components/FileExplorer.vue) merender
daftar file/folder dari endpoint GET /api/files, yang di backend memakai
ListFilesTool AETHER (src/agent_ai/tools/filesystem.py). Folder internal
AETHER `.aether` ikut terdaftar sehingga tampil di UI.

Perbaikan (HANYA menyembunyikan dari tampilan, TIDAK menghapus/mengubah
filesystem):
    1. Backend: `.aether` ditambahkan ke `_IGNORED_DIRS` pada
       ListFilesTool/SearchCodeTool -> entri `.aether` tidak dikembalikan API.
    2. Frontend: FileExplorer.vue memfilter nama `.aether` (HIDDEN_NAMES)
       sebagai pengaman di UI.

Verifier ini:
    - Membuat fixture nyata (folder `.aether` + folder biasa) di
      J:\\Agent_Ai\\dummy_test\\explorer_fixture.
    - Menjalankan ListFilesTool terhadap fixture dan memastikan:
        * `.aether` TIDAK ada di daftar entri (hidden),
        * folder/file biasa (src, docs, README.md) TETAP muncul.
    - Memastikan search_code tidak menelusuri isi `.aether`.
    - Memastikan filesystem TIDAK berubah (folder `.aether` masih ada di disk).
    - Cek statis sumber frontend memuat filter `.aether`.
    Fixture dibersihkan setelah test.

Jalankan:
    python scripts/check_explorer_hidden_aether.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.tools.filesystem import ListFilesTool, SearchCodeTool  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "explorer_fixture"
FRONTEND_SRC = PROJECT_ROOT / "web" / "frontend" / "src" / "components" / "FileExplorer.vue"


def setup_fixture() -> None:
    """Buat fixture project dummy: punya folder `.aether` + folder biasa."""
    shutil.rmtree(FIXTURE, ignore_errors=True)
    (FIXTURE / ".aether" / "brain").mkdir(parents=True, exist_ok=True)
    (FIXTURE / "src").mkdir(parents=True, exist_ok=True)
    (FIXTURE / "docs").mkdir(parents=True, exist_ok=True)

    (FIXTURE / "README.md").write_text("# dummy fixture\n", encoding="utf-8")
    (FIXTURE / ".aether" / "project.json").write_text(
        '{ "note": "internal aether - harus hidden" }\n', encoding="utf-8"
    )
    (FIXTURE / ".aether" / "brain" / "facts.md").write_text(
        "- dummy fact\n", encoding="utf-8"
    )
    (FIXTURE / "src" / "main.py").write_text('print("hi")\n', encoding="utf-8")
    (FIXTURE / "docs" / "guide.md").write_text("# guide\n", encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def _run() -> int:
    assert FIXTURE.is_dir(), f"fixture tidak ada: {FIXTURE}"
    aether_dir = FIXTURE / ".aether"
    assert aether_dir.is_dir(), "fixture harus punya folder .aether di disk"

    # 1) ListFilesTool root = fixture -> `.aether` HARUS hidden, lain tampil.
    tool = ListFilesTool(root=FIXTURE)
    listing = tool.execute(path=".")
    names = {e["name"] for e in listing["entries"]}

    assert ".aether" not in names, f".aether masih muncul di list_files: {names}"
    for expected in ("README.md", "src", "docs"):
        assert expected in names, f"entri normal '{expected}' hilang dari listing: {names}"
    print(f"[1] list_files('.') menyembunyikan .aether, entri normal tampil: OK -> {sorted(names)}")

    # 2) ListFilesTool di dalam folder biasa tetap normal.
    sub = tool.execute(path="src")
    sub_names = {e["name"] for e in sub["entries"]}
    assert "main.py" in sub_names, f"isi src/ harus tampil: {sub_names}"
    print(f"[2] list_files('src') tetap normal: OK -> {sorted(sub_names)}")

    # 3) search_code TIDAK menelusuri isi `.aether`.
    search = SearchCodeTool(root=FIXTURE)
    result = search.execute(query="dummy fact", path=".")
    hit_files = {m["file"].replace("\\", "/") for m in result["matches"]}
    assert not any(".aether" in f for f in hit_files), (
        f"search_code menembus .aether: {hit_files}"
    )
    print("[3] search_code tidak menelusuri .aether: OK")

    # 4) Filesystem TIDAK berubah: folder `.aether` masih ada di disk.
    assert aether_dir.is_dir(), "folder .aether di disk TIDAK boleh hilang"
    assert (aether_dir / "project.json").exists()
    print("[4] folder .aether tetap ada di disk (tidak dihapus): OK")

    # 5) Cek statis: sumber frontend memuat filter `.aether`.
    src_text = FRONTEND_SRC.read_text(encoding="utf-8")
    assert ".aether" in src_text, "FileExplorer.vue tidak memuat filter .aether"
    assert "HIDDEN_NAMES" in src_text and "visibleEntries" in src_text, (
        "FileExplorer.vue tidak menerapkan filter HIDDEN_NAMES/visibleEntries"
    )
    print("[5] FileExplorer.vue memuat filter .aether (HIDDEN_NAMES/visibleEntries): OK")

    print()
    print("[OK] Folder .aether tersembunyi dari File Explorer; filesystem tak berubah.")
    return 0


def main() -> int:
    print("=== Verifikasi File Explorer: folder .aether disembunyikan ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


if __name__ == "__main__":
    raise SystemExit(main())
