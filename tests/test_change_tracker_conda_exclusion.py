"""Deterministic tests: pengecualian environment Conda dari ChangeTracker.

Yang dikunci di sini:

  1. Environment Conda terdeteksi lewat STRUKTUR (`conda-meta`), bukan nama
     folder — nama direktori arbitrer (`conda`, `myenv-xyz`, `envs/whatever`).
  2. Seluruh subtree environment BERHENTI ditelusuri SEBELUM rekursi: file di
     dalamnya tidak di-`stat` dan tidak di-hash (dibuktikan lewat spy
     `Path.stat` + `_hash_file`).
  3. Folder biasa bernama `Lib`, `Scripts`, `Library`, `DLLs` TETAP dilacak.
  4. Source code proyek di luar environment tetap dilacak.
  5. Entri environment yang STALE (dari snapshot/cache eksekusi lama atau
     jalur kandidat eksplisit) dibuang dari hasil, state task, dan cache.
  6. Deteksi perubahan (created/modified/deleted) & isolasi antarproyek tetap
     berfungsi.
  7. Root workspace tidak dikecualikan walau strukturnya menyerupai environment.
  8. Akses EXPLISIT ke path di dalam environment tetap diizinkan (general
     filesystem tools tidak diubah).

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
    _is_conda_environment,
    clear_fingerprint_cache,
    iter_trackable_files,
    mark_mutated,
)
from agent_ai.changes.models import ChangeType  # noqa: E402


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _make_conda_env(path: Path, *, with_history: bool = True, with_json: bool = True) -> Path:
    """Buat struktur environment Conda minimal (marker saja, tanpa isi nyata).

    Mengikuti bentuk environment Conda NYATA: `conda-meta/history` adalah
    FILE (riwayat transaksi), bukan direktori.
    """
    meta = path / "conda-meta"
    meta.mkdir(parents=True, exist_ok=True)
    if with_json:
        (meta / "python-3.10.18-h8c5b53a_0_cpython.json").write_text("{}", encoding="utf-8")
    if with_history:
        (meta / "history").write_text("==> 2025-01-01 <==\n", encoding="utf-8")
    # Isi environment yang lazim (binary env — bukan source).
    for sub in ("Lib", "Scripts", "DLLs", "Library", "Library/bin"):
        (path / sub).mkdir(parents=True, exist_ok=True)
    (path / "Lib" / "site.py").write_text("x = 1\n", encoding="utf-8")
    (path / "Scripts" / "pip.exe").write_bytes(b"MZ\x00\x00binary")
    (path / "python.exe").write_bytes(b"MZ\x00\x00binary")
    return path


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


def _spy_stat(monkeypatch) -> dict:
    """Hitung berapa kali `Path.stat` dipanggil pada path tertentu."""
    calls = {"n": 0, "paths": []}
    real_stat = Path.stat

    def _spy(self, *args, **kwargs):
        calls["n"] += 1
        calls["paths"].append(str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _spy)
    return calls


# --------------------------------------------------------------------------- #
# 1. Deteksi struktural, nama folder arbitrer.
# --------------------------------------------------------------------------- #
def test_conda_environment_detected_by_structure_not_name(tmp_path: Path) -> None:
    """Nama folder arbitrer tetap terdeteksi sebagai environment Conda."""
    for name in ("conda", "myenv-xyz", "env", "anaconda", "some/other/env4"):
        env = _make_conda_env(tmp_path / name.replace("/", "_"))
        assert _is_conda_environment(env) is True, name

    # Proyek biasa (tanpa conda-meta) bukan environment.
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text("x = 1\n", encoding="utf-8")
    assert _is_conda_environment(plain) is False


def test_conda_detection_does_not_read_metadata_content(tmp_path: Path, monkeypatch) -> None:
    """Deteksi murni keberadaan path: isi file metadata tidak pernah dibaca."""
    import builtins

    env = _make_conda_env(tmp_path / "condee")
    real_open = builtins.open
    opened = []

    def _spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _spy_open)
    assert _is_conda_environment(env) is True
    assert opened == [], f"tidak boleh ada pembacaan file: {opened}"


def test_directory_named_conda_meta_without_history_is_not_an_environment(
    tmp_path: Path,
) -> None:
    """`conda-meta` tanpa struktur lengkap TIDAK dianggap environment."""
    fake = tmp_path / "fake"
    (fake / "conda-meta").mkdir(parents=True)
    (fake / "conda-meta" / "notes.json").write_text("{}", encoding="utf-8")
    assert _is_conda_environment(fake) is False


def test_conda_history_accepted_as_file_or_directory(tmp_path: Path) -> None:
    """`conda-meta/history` diterima sebagai FILE (nyata) maupun direktori."""
    as_file = _make_conda_env(tmp_path / "as_file")
    assert (as_file / "conda-meta" / "history").is_file()
    assert _is_conda_environment(as_file) is True

    as_dir = tmp_path / "as_dir"
    (as_dir / "conda-meta" / "history").mkdir(parents=True)
    (as_dir / "conda-meta" / "pkg-1.json").write_text("{}", encoding="utf-8")
    assert _is_conda_environment(as_dir) is True


# --------------------------------------------------------------------------- #
# 2. Prune SEBELUM rekursi (tidak di-stat, tidak di-hash).
# --------------------------------------------------------------------------- #
def test_conda_subtree_pruned_before_recursion(tmp_path: Path, monkeypatch) -> None:
    """Seluruh subtree environment berhenti ditelusuri sebelum hashing/stat."""
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    env = _make_conda_env(root / "conda")
    # Banyak file di dalam environment untuk membuktikan tidak ada yang disentuh.
    deep = env / "Lib" / "site-packages" / "numpy"
    deep.mkdir(parents=True)
    for i in range(50):
        (deep / f"mod_{i}.py").write_text(f"v = {i}\n", encoding="utf-8")
    (root / "app.py").write_text("main = 1\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "lib.py").write_text("lib = 2\n", encoding="utf-8")

    hash_calls = _spy_hash(monkeypatch)
    stat_calls = _spy_stat(monkeypatch)

    tracker = ChangeTracker(root=root)
    snap = tracker.snapshot(".")

    assert set(snap) == {"app.py", "src/lib.py"}, snap
    assert "conda/Lib/site.py" not in snap
    assert hash_calls["n"] == 2, hash_calls["paths"]
    assert all("conda/" not in p.replace("\\", "/") for p in hash_calls["paths"])
    # File di dalam environment tidak di-stat oleh jalur snapshot manapun.
    # Direktori environment ROOT sendiri boleh di-stat sekali saat deteksi
    # (murah; satu `stat` untuk membaca `conda-meta`, bukan traversal subtree).
    stat_inside = [
        p
        for p in stat_calls["paths"]
        if Path(p) != env and "conda" in Path(p).relative_to(root).parts
    ]
    assert stat_inside == [], f"file environment di-stat: {stat_inside[:5]}"


def test_conda_env_arbitrary_name_pruned_without_name_rule(tmp_path: Path) -> None:
    """Environment bernama arbitrer tetap dipangkas (tanpa aturan nama folder)."""
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    _make_conda_env(root / "totally-unrelated-name")
    (root / "app.py").write_text("a = 1\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    snap = tracker.snapshot(".")
    assert set(snap) == {"app.py"}, snap


# --------------------------------------------------------------------------- #
# 3-4. Folder biasa & source code tetap dilacak.
# --------------------------------------------------------------------------- #
def test_plain_folders_named_Lib_Scripts_Library_still_tracked(tmp_path: Path) -> None:
    """Folder `Lib`/`Scripts`/`Library`/`DLLs` biasa TIDAK dikecualikan."""
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    (root / "Lib").mkdir()
    (root / "Lib" / "helper.py").write_text("h = 1\n", encoding="utf-8")
    (root / "Library").mkdir()
    (root / "Library" / "data.txt").write_text("d\n", encoding="utf-8")
    (root / "Scripts").mkdir()
    (root / "Scripts" / "deploy.sh").write_text("echo hi\n", encoding="utf-8")
    (root / "DLLs").mkdir()
    (root / "DLLs" / "native.py").write_text("n = 1\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    snap = tracker.snapshot(".")
    assert set(snap) == {
        "DLLs/native.py",
        "Lib/helper.py",
        "Library/data.txt",
        "Scripts/deploy.sh",
    }, snap


def test_project_source_outside_env_still_tracked_and_changes_detected(
    tmp_path: Path,
) -> None:
    """Source code proyek tetap dilacak; created/modified/deleted tetap akurat."""
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    _make_conda_env(root / "conda")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    (root / "del.py").write_text("del = True\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    tracker.start("t1")
    before = tracker.snapshot(".", task_id="t1")
    assert set(before) == {"app.py", "del.py"}

    (root / "app.py").write_text("value = 2 (panjang)\n", encoding="utf-8")
    (root / "new.py").write_text("baru\n", encoding="utf-8")
    (root / "del.py").unlink()
    # Perubahan DI DALAM environment tidak boleh muncul.
    (root / "conda" / "Lib" / "site.py").write_text("changed\n", encoding="utf-8")

    records = tracker.detect_changes("t1", path=".")
    kinds = {r.path: r.change_type for r in records}
    assert kinds.get("app.py") is ChangeType.MODIFIED
    assert kinds.get("new.py") is ChangeType.CREATED
    assert kinds.get("del.py") is ChangeType.DELETED
    assert not any(r.path.startswith("conda/") for r in records), kinds


# --------------------------------------------------------------------------- #
# 5. Entri STALE environment dibuang dari hasil/state/cache.
# --------------------------------------------------------------------------- #
def test_stale_conda_entries_dropped_from_snapshot_state_and_cache(
    tmp_path: Path,
) -> None:
    """Snapshot/sate/cache lama yang memuat entri environment tidak bertahan."""
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    env = _make_conda_env(root / "conda")
    stale_file = env / "Lib" / "site.py"
    (root / "app.py").write_text("a = 1\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    tracker.start("t1")
    # Simulasi eksekusi LAMA: entri environment sudah tercatat di state task.
    digest = _sha1(stale_file)
    tracker._snapshots["t1"]["conda/Lib/site.py"] = (digest, stale_file.stat().st_size)
    tracker._fingerprint_cache()["conda/Lib/site.py"] = (
        digest,
        stale_file.stat().st_size,
        stale_file.stat().st_mtime_ns,
    )

    snap = tracker.snapshot(".", task_id="t1")

    assert "conda/Lib/site.py" not in snap
    assert "conda/Lib/site.py" not in tracker.tracked_paths("t1")
    assert "conda/Lib/site.py" not in tracker._fingerprint_cache()
    assert tracker.last_snapshot_stats()["excluded_conda"] == 1

    # Environment tidak dilaporkan 'deleted' palsu setelah entri dibuang.
    records = tracker.detect_changes("t1", path=".")
    assert not any(r.path.startswith("conda/") for r in records)


def test_explicit_conda_path_access_still_allowed(tmp_path: Path) -> None:
    """Akses eksplisit ke path di dalam environment tetap diizinkan (hash)."""
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    env = _make_conda_env(root / "conda")
    target = env / "Lib" / "site.py"

    tracker = ChangeTracker(root=root)
    snap = tracker.snapshot("conda/Lib/site.py")
    digest, size = snap["conda/Lib/site.py"]
    assert digest == _sha1(target)
    assert size == target.stat().st_size


# --------------------------------------------------------------------------- #
# 6. Isolasi antarproyek + deteksi iteratif aman.
# --------------------------------------------------------------------------- #
def test_conda_rule_isolated_between_projects(tmp_path: Path) -> None:
    """Dua workspace: environment di satu proyek tidak memengaruhi proyek lain."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_b.mkdir(parents=True)
    clear_fingerprint_cache(root_a)
    clear_fingerprint_cache(root_b)
    _make_conda_env(root_a / "conda")
    (root_a / "same.txt").write_text("AAA\n", encoding="utf-8")
    (root_b / "same.txt").write_text("BBB\n", encoding="utf-8")

    ta = ChangeTracker(root=root_a)
    tb = ChangeTracker(root=root_b)

    snap_a = ta.snapshot(".")
    snap_b = tb.snapshot(".")

    assert set(snap_a) == {"same.txt"}
    assert set(snap_b) == {"same.txt"}
    assert snap_a["same.txt"][0] != snap_b["same.txt"][0]


def test_repeated_snapshots_stay_consistent_with_conda_env(tmp_path: Path) -> None:
    """Snapshot berulang stabil dan tetap konsisten walau ada environment."""
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    _make_conda_env(root / "conda")
    (root / "a.txt").write_text("stabil\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    first = tracker.snapshot(".")
    second = tracker.snapshot(".")
    size = (root / "a.txt").stat().st_size
    assert first == second == {"a.txt": (_sha1(root / "a.txt"), size)}


def test_iter_trackable_files_yields_no_conda_paths(tmp_path: Path) -> None:
    """Iterator mentah tidak pernah menghasilkan path di dalam environment."""
    root = tmp_path / "proj"
    root.mkdir()
    _make_conda_env(root / "condee")
    (root / "a.py").write_text("a = 1\n", encoding="utf-8")

    found = {p.relative_to(root).as_posix() for p in iter_trackable_files(root, root=root)}
    assert found == {"a.py"}, found


# --------------------------------------------------------------------------- #
# 7. Root workspace tidak dikecualikan.
# --------------------------------------------------------------------------- #
def test_workspace_root_resembling_environment_is_not_excluded(tmp_path: Path) -> None:
    """Root yang strukturnya menyerupai environment tetap dipindai penuh."""
    root = _make_conda_env(tmp_path / "proj")
    # Tambahkan environment LAIN di dalamnya untuk memastikan hanya anak yang dipangkas.
    _make_conda_env(root / "inner_env")
    (root / "app.py").write_text("app = 1\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    snap = tracker.snapshot(".")

    # File root sendiri (di luar marker dir) tetap dilacak...
    assert "app.py" in snap
    assert "Lib/site.py" in snap
    # ...sementara environment ANAK tetap dipangkas.
    assert not any(k.startswith("inner_env/") for k in snap)


# --------------------------------------------------------------------------- #
# 8. Mutasi tool pada source tetap terdeteksi (tidak ada regresi).
# --------------------------------------------------------------------------- #
def test_tool_mutation_outside_env_still_detected(tmp_path: Path) -> None:
    """Mutasi self-aware pada source di luar environment tetap terdeteksi."""
    root = tmp_path / "proj"
    root.mkdir()
    clear_fingerprint_cache(root)
    _make_conda_env(root / "conda")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    tracker.start("t9")
    tracker.snapshot(".", task_id="t9")

    (root / "app.py").write_text("value = 22222\n", encoding="utf-8")
    mark_mutated(root, "app.py")

    records = tracker.detect_changes("t9", path=".")
    kinds = {r.path: r.change_type for r in records}
    assert kinds.get("app.py") is ChangeType.MODIFIED
