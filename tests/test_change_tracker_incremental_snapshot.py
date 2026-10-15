"""Deterministic tests: snapshot inkremental (fingerprint) untuk workspace besar.

Yang dikunci di sini (optimasi TASK 1):

  1. Snapshot pertama tetap menghitung SHA-1 dari ISI file (hash = SHA-1 nyata).
  2. Snapshot BERULANG pada workspace yang tidak berubah TIDAK membaca ulang
     isi file (cache fingerprint dipakai) — hash tetap benar & konsisten.
  3. Perubahan metadata (size/mtime) -> SHA-1 dihitung ulang dan perubahan
     terdeteksi (created/modified/deleted tetap akurat).
  4. Cache terisolasi per canonical project root (dua workspace tidak campur).
  5. Kontrak `snapshot()` tidak berubah: `{rel_path: (hash, size)}`.
  6. Batas akurasi didokumentasikan: perubahan isi yang TIDAK mengubah size
     DAN mtime dapat tidak terdeteksi (heuristik metadata) — dikunci sebagai
     perilaku eksplisit, bukan bug tersembunyi.
  7. Self-aware sync: mutasi oleh PROSES INI (`mark_mutated`/`mark_moved`/
     `mark_deleted`, termasuk lewat `ToolRegistry.execute`) SELALU membuat
     snapshot berikutnya menghitung ulang SHA-1 — bahkan bila size & mtime
     identik. Mutasi dari LUAR proses tetap menjadi batas akurasi.
  8. Aturan eksklusi eksplisit untuk rekursi direktori (dir/ekstensi cache),
     sementara root project AETHER tetap dilacak penuh.

Semua test tanpa network dan tanpa runtime (unit murni).
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.changes.tracker import (  # noqa: E402
    ChangeTracker,
    clear_fingerprint_cache,
    mark_deleted,
    mark_moved,
    mark_mutated,
)
from agent_ai.changes.models import ChangeType  # noqa: E402
from agent_ai.tools.registry import build_registry  # noqa: E402


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# Kontrak & kebenaran hash
# --------------------------------------------------------------------------- #
def test_first_snapshot_hashes_real_content(tmp_path: Path) -> None:
    """Snapshot pertama mengembalikan SHA-1 ISI file yang sebenarnya."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("halo dunia\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    snap = tracker.snapshot(".")

    assert set(snap) == {"a.txt"}
    digest, size = snap["a.txt"]
    assert digest == _sha1(root / "a.txt")
    assert size == len((root / "a.txt").read_bytes())


def test_repeated_snapshot_reuses_cache_without_rehash(tmp_path: Path, monkeypatch) -> None:
    """Snapshot kedua pada file tak berubah TIDAK menghitung ulang SHA-1."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("isi stabil\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("lain\n", encoding="utf-8")

    import agent_ai.changes.tracker as mod

    calls = {"n": 0}
    real_hash = mod._hash_file

    def _spy(path: Path):
        calls["n"] += 1
        return real_hash(path)

    monkeypatch.setattr(mod, "_hash_file", _spy)

    tracker = ChangeTracker(root=root)
    first = tracker.snapshot(".")
    assert calls["n"] == 2  # dua file di-hash pada snapshot pertama
    assert first["a.txt"][0] == _sha1(root / "a.txt")

    calls["n"] = 0
    second = tracker.snapshot(".")
    assert calls["n"] == 0, "snapshot berulang tidak boleh re-hash file tak berubah"
    assert second == first  # kontrak & nilai identik


def test_metadata_change_triggers_rehash_and_detection(tmp_path: Path) -> None:
    """size/mtime berubah -> SHA-1 dihitung ulang & 'modified' terdeteksi."""
    root = tmp_path / "proj"
    root.mkdir()
    f = root / "app.py"
    f.write_text("value = 1\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    tracker.start("t1")
    tracker.snapshot("app.py", task_id="t1")

    f.write_text("value = 2 (lebih panjang)\n", encoding="utf-8")

    records = tracker.detect_changes("t1", path="app.py")
    assert [r.path for r in records] == ["app.py"]
    assert records[0].change_type is ChangeType.MODIFIED
    assert records[0].before_hash == _sha1(f) or records[0].before_hash is not None
    assert records[0].after_hash == _sha1(f)


def test_created_and_deleted_still_detected(tmp_path: Path) -> None:
    """File baru & file terhapus tetap terdeteksi dengan cache fingerprint."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "keep.txt").write_text("keep\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    tracker.start("t2")
    tracker.snapshot(".", task_id="t2")

    (root / "baru.txt").write_text("baru\n", encoding="utf-8")

    records = tracker.detect_changes("t2", path=".")
    kinds = {r.path: r.change_type for r in records}
    assert kinds.get("baru.txt") is ChangeType.CREATED

    # Sekarang hapus file: deteksi berikutnya harus melaporkan deleted.
    tracker.start("t3")
    tracker.snapshot(".", task_id="t3")
    (root / "baru.txt").unlink()

    records3 = tracker.detect_changes("t3", path=".")
    kinds3 = {r.path: r.change_type for r in records3}
    assert kinds3.get("baru.txt") is ChangeType.DELETED


def test_same_content_new_mtime_reports_no_change(tmp_path: Path, monkeypatch) -> None:
    """mtime berubah tapi isi identik -> tidak dilaporkan 'modified' (akurat).

    Guard terhadap false positive: hash tetap sama, jadi `detect_changes`
    TIDAK melaporkan perubahan walau metadata berubah.
    """
    root = tmp_path / "proj"
    root.mkdir()
    f = root / "stable.txt"
    f.write_text("isi sama\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    tracker.start("t4")
    tracker.snapshot(".", task_id="t4")

    # Ubah mtime tanpa mengubah isi (set timestamp berbeda).
    import os
    import time

    st = f.stat()
    os.utime(f, (st.st_atime, st.st_mtime + 10))

    records = tracker.detect_changes("t4", path=".")
    assert records == [], f"isi identik tidak boleh dilaporkan berubah: {records}"


def test_cache_isolated_per_root(tmp_path: Path) -> None:
    """Dua workspace berbeda tidak berbagi cache fingerprint."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    # Path relatif SAMA, isi BERBEDA.
    (root_a / "same.txt").write_text("AAA\n", encoding="utf-8")
    (root_b / "same.txt").write_text("BBB\n", encoding="utf-8")

    ta = ChangeTracker(root=root_a)
    tb = ChangeTracker(root=root_b)

    snap_a = ta.snapshot("same.txt")
    snap_b = tb.snapshot("same.txt")

    assert snap_a["same.txt"][0] == _sha1(root_a / "same.txt")
    assert snap_b["same.txt"][0] == _sha1(root_b / "same.txt")
    assert snap_a["same.txt"][0] != snap_b["same.txt"][0]


def test_snapshot_contract_shape(tmp_path: Path) -> None:
    """`snapshot()` tetap mengembalikan {rel_path: (hash, size)} (2-tuple)."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "x.txt").write_text("x\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    snap = tracker.snapshot(".")
    entry = snap["x.txt"]
    assert isinstance(entry, tuple) and len(entry) == 2
    digest, size = entry
    assert isinstance(digest, str) and len(digest) == 40
    assert isinstance(size, int)


def test_documented_limitation_same_size_and_mtime_is_missed(tmp_path: Path) -> None:
    """BATAS AKURASI (dokumentasi eksplisit, bukan bug tersembunyi).

    Bila isi file berubah TAPI `size` dan `mtime_ns` dipertahankan persis
    sama, cache fingerprint menganggap file tidak berubah sehingga perubahan
    tidak terdeteksi. Ini konsekuensi heuristik metadata yang didokumentasikan
    di docstring modul tracker. Test ini mengunci perilaku tersebut agar
    perubahan strategi di masa depan disadari.
    """
    import os

    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    f = root / "tricky.txt"
    f.write_text("AAAA\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    tracker.start("t5")
    tracker.snapshot(".", task_id="t5")

    st = f.stat()
    # Isi berubah, ukuran SAMA (4 byte + newline) dan mtime dikembalikan persis.
    f.write_text("BBBB\n", encoding="utf-8")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))

    records = tracker.detect_changes("t5", path=".")
    # Perilaku terdokumentasi: perubahan tidak terdeteksi karena metadata sama.
    assert records == []

    # Bukti bahwa dengan metadata BERUBAH, perubahan yang sama TERDETEKSI:
    clear_fingerprint_cache(root)
    tracker2 = ChangeTracker(root=root)
    tracker2.start("t6")
    tracker2.snapshot(".", task_id="t6")
    f.write_text("CCCC\n", encoding="utf-8")
    records2 = tracker2.detect_changes("t6", path=".")
    assert any(r.path == "tricky.txt" for r in records2)


def test_clear_fingerprint_cache_forces_rehash(tmp_path: Path, monkeypatch) -> None:
    """`clear_fingerprint_cache` mengosongkan cache -> snapshot menghitung ulang."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("data\n", encoding="utf-8")

    import agent_ai.changes.tracker as mod

    calls = {"n": 0}
    real_hash = mod._hash_file

    def _spy(path: Path):
        calls["n"] += 1
        return real_hash(path)

    monkeypatch.setattr(mod, "_hash_file", _spy)

    tracker = ChangeTracker(root=root)
    tracker.snapshot(".")
    assert calls["n"] == 1

    calls["n"] = 0
    tracker.snapshot(".")
    assert calls["n"] == 0  # cache dipakai

    clear_fingerprint_cache(root)
    tracker.snapshot(".")
    assert calls["n"] == 1  # cache bersih -> hash ulang


# --------------------------------------------------------------------------- #
# Self-aware sync: mutasi oleh PROSES INI terdeteksi walau metadata identik.
#
# Ini menutup celah yang sebelumnya dikunci
# `test_documented_limitation_same_size_and_mtime_is_missed` (mutasi dari LUAR
# proses tetap menjadi batas akurasi yang terdokumentasi).
# --------------------------------------------------------------------------- #
def _same_metadata(path: Path, st) -> None:
    """Kembalikan mtime_ns persis (agar metadata fingerprint identik)."""
    import os

    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_self_aware_sync_detects_same_size_and_mtime_content_change(tmp_path: Path) -> None:
    """`mark_mutated` -> perubahan isi terdeteksi walau size & mtime identik."""
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    f = root / "tricky.txt"
    f.write_text("AAAA\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    tracker.start("t-sync")
    tracker.snapshot(".", task_id="t-sync")

    st = f.stat()
    f.write_text("BBBB\n", encoding="utf-8")
    _same_metadata(f, st)
    assert f.stat().st_size == st.st_size
    assert f.stat().st_mtime_ns == st.st_mtime_ns

    # Tanpa penanda mutasi: metadata identik -> terlewat (perilaku lama).
    assert tracker.detect_changes("t-sync", path=".") == []

    # Dengan penanda mutasi (jalur tool nyata): hash dihitung ulang -> terdeteksi.
    mark_mutated(root, "tricky.txt")
    records = tracker.detect_changes("t-sync", path=".")
    assert [r.path for r in records] == ["tricky.txt"]
    assert records[0].change_type is ChangeType.MODIFIED
    assert records[0].after_hash == _sha1(f)


def test_tool_mutation_shared_sync_cache_detects_same_size_and_mtime(tmp_path: Path) -> None:
    """Jalur tool NYATA: registry menandai cache -> metadata identik terdeteksi.

    Mengunci wiring produksi: `ToolRegistry.execute` menandai cache fingerprint
    ChangeTracker untuk `edit_file`/`write_file`, sehingga snapshot berikutnya
    menghitung ulang SHA-1 dari isi nyata.
    """
    import os

    root = tmp_path / "proj"
    root.mkdir()
    f = root / "app.py"
    f.write_text("AAAAAAAA\n", encoding="utf-8")
    clear_fingerprint_cache(root)

    tracker = ChangeTracker(root=root)
    tracker.start("t-tool")
    tracker.snapshot(".", task_id="t-tool")
    st = f.stat()

    registry = build_registry(root=root)
    result = registry.execute(
        "edit_file", {"path": "app.py", "old_text": "AAAAAAAA", "new_text": "BBBBBBBB"}
    )
    assert result.get("edited") is True
    # Metadata dipulihkan persis (kasus tersulit).
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert f.stat().st_size == st.st_size
    assert f.stat().st_mtime_ns == st.st_mtime_ns

    records = tracker.detect_changes("t-tool", path=".")
    assert [r.path for r in records] == ["app.py"]
    assert records[0].change_type is ChangeType.MODIFIED
    assert records[0].after_hash == _sha1(f)
    assert records[0].after_hash != records[0].before_hash


def test_mark_deleted_and_moved_invalidate_cache(tmp_path: Path) -> None:
    """`mark_deleted` / `mark_moved` membuang entri cache path terkait."""
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    (root / "a.txt").write_text("isi a\n", encoding="utf-8")
    (root / "c.txt").write_text("isi c\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    tracker.start("t-mv")
    tracker.snapshot(".", task_id="t-mv")

    # Pindahkan file di luar jalur tool, lalu tandai seperti tool nyata.
    marker = root / "a.txt"
    new_path = root / "b.txt"
    marker.rename(new_path)
    mark_moved(root, "a.txt", "b.txt")

    records = tracker.detect_changes("t-mv", path=".")
    kinds = {r.path: r.change_type for r in records}
    # Move: source dilaporkan deleted; destination created. Pada jalur runtime
    # nyata, source di-`unregister_path` agar tidak dilaporkan deleted palsu.
    assert kinds.get("a.txt") is ChangeType.DELETED
    assert kinds.get("b.txt") is ChangeType.CREATED

    # mark_deleted membuang entri cache; file yang ADA di baseline tetap
    # dilaporkan deleted (hilangnya file adalah fakta).
    (root / "c.txt").unlink()
    mark_deleted(root, "c.txt")
    kinds2 = {
        r.path: r.change_type for r in tracker.detect_changes("t-mv", path=".")
    }
    assert kinds2.get("c.txt") is ChangeType.DELETED


def test_sync_disabled_tracker_keeps_isolated_cache(tmp_path: Path, monkeypatch) -> None:
    """`sync=False` -> cache thread-local; tetap benar & tidak terpengaruh mark."""
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    f = root / "a.txt"
    f.write_text("AAAA\n", encoding="utf-8")

    tracker = ChangeTracker(root=root, sync=False)
    first = tracker.snapshot(".")
    assert first["a.txt"][0] == _sha1(f)

    import agent_ai.changes.tracker as mod

    calls = {"n": 0}
    real_hash = mod._hash_file

    def _spy(path):
        calls["n"] += 1
        return real_hash(path)

    monkeypatch.setattr(mod, "_hash_file", _spy)
    tracker.snapshot(".")
    # Cache lokal dipakai (tanpa re-hash) walau cache sync global dimutasi.
    assert calls["n"] == 0
    assert tracker.snapshot(".")["a.txt"][0] == _sha1(f)


def test_snapshot_excludes_irrelevant_dirs_and_suffixes(tmp_path: Path) -> None:
    """Aturan eksklusi eksplisit: source tetap di-hash, artefak besar tidak."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    for ignored_dir in ("__pycache__", "node_modules", "venv", "dist"):
        d = root / ignored_dir
        d.mkdir()
        (d / "artifact.py").write_text("y = 2\n", encoding="utf-8")
    (root / "debug.log").write_text("log\n", encoding="utf-8")
    (root / "module.pyc").write_bytes(b"\x00\x01")

    tracker = ChangeTracker(root=root)
    snap = tracker.snapshot(".")

    assert "app.py" in snap
    for ignored_dir in ("__pycache__", "node_modules", "venv", "dist"):
        assert not any(p.startswith(ignored_dir + "/") for p in snap), snap
    assert "debug.log" not in snap
    assert "module.pyc" not in snap


def test_root_project_still_tracked_without_exclusion(tmp_path: Path) -> None:
    """Root project AETHER TIDAK memakai aturan eksklusi (source + state aman)."""
    import agent_ai.changes.tracker as mod

    assert mod._allowed_in_snapshot(mod._DEFAULT_ROOT) is True
    # File .log di root project AETHER tetap layak masuk snapshot.
    assert mod._allowed_in_snapshot(mod._DEFAULT_ROOT / "should_not_exist.log") is True


def test_command_created_file_in_task_scope_is_detected(tmp_path: Path) -> None:
    """File baru di SCOPE kandidat task (mis. dari run_command) tetap terdeteksi.

    Mengunci bahwa eksklusi dengan aturan eksplisit TIDAK menghilangkan
    kemampuan mendeteksi file baru yang relevan.
    """
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    clear_fingerprint_cache(root)

    tracker = ChangeTracker(root=root)
    tracker.start("t-cmd")
    tracker.snapshot("app.py", task_id="t-cmd")

    (root / "cmd_made.py").write_text("y = 2\n", encoding="utf-8")

    records = tracker.detect_changes("t-cmd", path=".")
    kinds = {r.path: r.change_type for r in records}
    assert kinds.get("cmd_made.py") is ChangeType.CREATED


def test_snapshot_cache_is_thread_safe(tmp_path: Path) -> None:
    """Snapshot paralel (lintas thread) konsisten & tidak merusak baseline."""
    import threading

    root = tmp_path / "proj"
    root.mkdir()
    for i in range(30):
        (root / f"f{i}.txt").write_text(f"isi {i}\n", encoding="utf-8")
    clear_fingerprint_cache(root)

    tracker = ChangeTracker(root=root)
    results = []
    errors = []

    def worker():
        try:
            results.append(tracker.snapshot("."))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(results) == 8
    first = results[0]
    assert len(first) == 30
    for snap in results:
        assert snap == first


