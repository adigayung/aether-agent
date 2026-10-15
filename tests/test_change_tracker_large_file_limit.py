"""Deterministic tests: pengecualian file besar (>25 MiB) dari ChangeTracker.

Yang dikunci di sini:

  1. Konstanta publik `MAX_TRACKED_FILE_SIZE` = 25 MiB.
  2. File TEPAT 25 MiB masih di-track (batasnya "lebih dari").
  3. File sedikit di atas 25 MiB dilewati dari snapshot DIREKTORI.
  4. File besar TIDAK memanggil fungsi hashing (`_hash_file`).
  5. File source normal tetap di-hash & perubahannya tetap terdeteksi.
  6. File besar tidak diproses ulang (tidak di-hash) pada snapshot berikutnya.
  7. Isolasi cache antarproyek tetap berlaku untuk aturan ukuran.
  8. Aturan berlaku per FILE, bukan per direktori.
  9. Akses eksplisit ke file besar (jalur kandidat task) tetap terdeteksi.

File besar dibuat SPARSE (seek + tulis 1 byte) agar tidak memakan ruang disk
nyata maupun waktu tulis.
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
    MAX_TRACKED_FILE_SIZE,
    ChangeTracker,
    clear_fingerprint_cache,
    mark_mutated,
)
from agent_ai.changes.models import ChangeType  # noqa: E402


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _make_sized_file(path: Path, size: int) -> None:
    """Buat file berukuran `size` byte secara SPARSE (isi tidak di-materialize).

    Hanya 1 byte yang benar-benar ditulis pada offset terakhir; ukuran logis
    tetap `size` sehingga `st_size` sesuai tanpa memakan ruang disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.truncate(size)


def _spy_hash(monkeypatch) -> dict:
    """Ganti `_hash_file` dengan spy penghitung panggilan."""
    import agent_ai.changes.tracker as mod

    calls = {"n": 0, "paths": []}
    real_hash = mod._hash_file

    def _spy(path: Path):
        calls["n"] += 1
        calls["paths"].append(str(path))
        return real_hash(path)

    monkeypatch.setattr(mod, "_hash_file", _spy)
    return calls


# --------------------------------------------------------------------------- #
# 1-2. Konstanta & file tepat pada batas.
# --------------------------------------------------------------------------- #
def test_max_tracked_file_size_constant() -> None:
    """Batas ukuran eksplisit = 25 MiB."""
    assert MAX_TRACKED_FILE_SIZE == 25 * 1024 * 1024
    assert MAX_TRACKED_FILE_SIZE == 26_214_400


def test_file_exactly_at_limit_is_tracked(tmp_path: Path) -> None:
    """File berukuran TEPAT 25 MiB tetap mengikuti pelacakan normal."""
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    exact = root / "checkpoint.bin"
    _make_sized_file(exact, MAX_TRACKED_FILE_SIZE)
    assert exact.stat().st_size == MAX_TRACKED_FILE_SIZE

    tracker = ChangeTracker(root=root)
    snap = tracker.snapshot(".")

    assert set(snap) == {"checkpoint.bin"}
    digest, size = snap["checkpoint.bin"]
    assert size == MAX_TRACKED_FILE_SIZE
    assert digest == _sha1(exact)


# --------------------------------------------------------------------------- #
# 3-4. File sedikit di atas batas: dilewati & tidak di-hash.
# --------------------------------------------------------------------------- #
def test_file_slightly_over_limit_is_skipped_and_not_hashed(
    tmp_path: Path, monkeypatch
) -> None:
    """File >25 MiB dilewati snapshot direktori & tidak memanggil hashing."""
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    big = root / "model.safetensors"
    _make_sized_file(big, MAX_TRACKED_FILE_SIZE + 1)
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")

    calls = _spy_hash(monkeypatch)

    tracker = ChangeTracker(root=root)
    snap = tracker.snapshot(".")

    assert "model.safetensors" not in snap, "file >25 MiB harus dikecualikan"
    assert "app.py" in snap
    assert calls["n"] == 1, "hanya file source yang boleh di-hash"
    assert str(big) not in calls["paths"]
    assert tracker.last_snapshot_stats()["skipped_large"] == 1
    assert tracker.last_snapshot_stats()["files_seen"] == 2
    assert tracker.last_snapshot_stats()["tracked"] == 1


def test_explicit_per_file_large_access_still_hashes(tmp_path: Path) -> None:
    """Akses EXPLISIT ke file besar tetap menghasilkan hash (jalur kandidat).

    Mencegah regresi: mutasi tool yang melaporkan path file besar tidak boleh
    kehilangan jejak perubahan hanya karena batas ukuran snapshot rekursif.
    """
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    big = root / "weights.bin"
    _make_sized_file(big, MAX_TRACKED_FILE_SIZE + 1024)

    tracker = ChangeTracker(root=root)
    snap = tracker.snapshot("weights.bin")

    digest, size = snap["weights.bin"]
    assert size == MAX_TRACKED_FILE_SIZE + 1024
    assert digest == _sha1(big)


# --------------------------------------------------------------------------- #
# 5. File source normal tetap akurat.
# --------------------------------------------------------------------------- #
def test_normal_source_files_still_hashed_and_changes_detected(tmp_path: Path) -> None:
    """File source normal tetap di-hash; created/modified/deleted tetap akurat."""
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    (root / "model.bin").write_bytes(b"")  # kecil, tetap dilacak
    _make_sized_file(root / "dataset.bin", MAX_TRACKED_FILE_SIZE + 5)

    tracker = ChangeTracker(root=root)
    tracker.start("t1")
    before = tracker.snapshot(".", task_id="t1")
    assert "app.py" in before
    assert "dataset.bin" not in before

    # Modify source + create file baru.
    (root / "app.py").write_text("value = 2 (panjang)\n", encoding="utf-8")
    (root / "new.py").write_text("baru\n", encoding="utf-8")

    records = tracker.detect_changes("t1", path=".")
    kinds = {r.path: r.change_type for r in records}
    assert kinds.get("app.py") is ChangeType.MODIFIED
    assert kinds.get("new.py") is ChangeType.CREATED
    assert "dataset.bin" not in kinds


# --------------------------------------------------------------------------- #
# 6. File besar tidak diproses ulang pada snapshot berikutnya.
# --------------------------------------------------------------------------- #
def test_large_file_not_reprocessed_on_next_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    """Snapshot berulang tidak memanggil hashing untuk file besar."""
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    _make_sized_file(root / "big.bin", MAX_TRACKED_FILE_SIZE + 7)
    (root / "small.txt").write_text("kecil\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    tracker.start("t2")
    tracker.snapshot(".", task_id="t2")

    calls = _spy_hash(monkeypatch)
    second = tracker.snapshot(".", task_id="t2")

    assert "big.bin" not in second
    assert calls["n"] == 0, "tidak ada hashing pada snapshot tak berubah"
    assert tracker.last_snapshot_stats()["skipped_large"] == 1


def test_large_file_written_by_tool_is_not_reintroduced_to_recursive_scan(
    tmp_path: Path, monkeypatch
) -> None:
    """File besar yang dimutasi tool tidak masuk ulang ke snapshot rekursif.

    Jalur kandidat eksplisit (`register_path`) menyelesaikan hash per-file;
    snapshot rekursif berikutnya harus membuang entri file besar itu sehingga
    hashing penuh tidak berulang tiap task.
    """
    from agent_ai.changes.task_evidence import TaskChangeEvidence

    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    big = root / "big.bin"
    _make_sized_file(big, MAX_TRACKED_FILE_SIZE + 11)
    (root / "keep.py").write_text("keep = True\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    evidence = TaskChangeEvidence(tracker, task_id="t3")
    evidence.begin("t3")
    evidence.register_path("big.bin", task_id="t3")  # hash per-file eksplisit
    evidence.register_path("keep.py", task_id="t3")
    # File besar TIDAK menjadi bagian scan/tracking otomatis (walau hash
    # eksplisitnya dihitung sekali), sehingga biaya hashing tidak terbawa ke
    # snapshot seluruh workspace.
    assert "big.bin" not in tracker.tracked_paths("t3")
    assert "keep.py" in tracker.tracked_paths("t3")

    calls = _spy_hash(monkeypatch)
    snap = tracker.snapshot(".", task_id="t3")

    assert "big.bin" not in snap, "file besar tidak boleh kembali ke scan rekursif"
    assert "big.bin" not in tracker.tracked_paths("t3")
    assert calls["paths"] == [], f"tidak boleh ada hashing ulang: {calls['paths']}"
    assert tracker.last_snapshot_stats()["skipped_large"] >= 1


# --------------------------------------------------------------------------- #
# 7. Isolasi antarproyek.
# --------------------------------------------------------------------------- #
def test_large_file_rule_isolated_between_projects(tmp_path: Path) -> None:
    """Dua workspace: file besar di satu proyek tidak memengaruhi proyek lain."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    clear_fingerprint_cache(root_a)
    clear_fingerprint_cache(root_b)

    _make_sized_file(root_a / "huge.bin", MAX_TRACKED_FILE_SIZE + 1)
    (root_a / "same.txt").write_text("AAA\n", encoding="utf-8")
    (root_b / "same.txt").write_text("BBB\n", encoding="utf-8")

    ta = ChangeTracker(root=root_a)
    tb = ChangeTracker(root=root_b)

    snap_a = ta.snapshot(".")
    snap_b = tb.snapshot(".")

    assert "huge.bin" not in snap_a
    assert set(snap_a) == {"same.txt"}
    assert set(snap_b) == {"same.txt"}
    assert snap_a["same.txt"][0] == _sha1(root_a / "same.txt")
    assert snap_b["same.txt"][0] == _sha1(root_b / "same.txt")
    assert snap_a["same.txt"][0] != snap_b["same.txt"][0]


# --------------------------------------------------------------------------- #
# 8. Per-file, bukan per-direktori.
# --------------------------------------------------------------------------- #
def test_rule_is_per_file_not_per_directory(tmp_path: Path) -> None:
    """Direktori yang memuat file besar TIDAK dikecualikan seluruhnya."""
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    models = root / "models"
    models.mkdir()
    _make_sized_file(models / "huge.bin", MAX_TRACKED_FILE_SIZE + 1)
    (models / "config.json").write_text('{"a": 1}\n', encoding="utf-8")
    (models / "utils.py").write_text("y = 2\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    snap = tracker.snapshot(".")

    assert "models/config.json" in snap
    assert "models/utils.py" in snap
    assert "models/huge.bin" not in snap


# --------------------------------------------------------------------------- #
# 9. Perubahan file besar yang dilaporkan tool tetap terdeteksi (konsisten).
# --------------------------------------------------------------------------- #
def test_tool_reported_large_file_change_is_detected(tmp_path: Path) -> None:
    """Mutasi eksplisit file besar (register_path + mark_mutated) tetap terdeteksi."""
    from agent_ai.changes.task_evidence import TaskChangeEvidence

    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    big = root / "big.bin"
    _make_sized_file(big, MAX_TRACKED_FILE_SIZE + 3)

    tracker = ChangeTracker(root=root)
    evidence = TaskChangeEvidence(tracker, task_id="t4")
    evidence.begin("t4")
    evidence.register_path("big.bin", task_id="t4")

    # Mutasi pada byte terakhir (ukuran tetap) + penanda mutasi dari jalur tool.
    with big.open("r+b") as fh:
        fh.seek(MAX_TRACKED_FILE_SIZE + 2)
        fh.write(b"\xff")
    mark_mutated(root, "big.bin")

    records = evidence.detect("t4")
    kinds = {r.path: r.change_type for r in records}
    assert kinds.get("big.bin") is ChangeType.MODIFIED
