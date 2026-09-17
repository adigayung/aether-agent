"""Verifikasi Change/Diff Tracking Subsystem.

Deterministik. Fixture di J:\\Agent_Ai\\dummy_test dan dibersihkan setelah test.

Menguji:
    1. create file detection
    2. modify file detection
    3. delete file detection
    4. unchanged file
    5. before/after hash
    6. size tracking
    7. ChangeSet task isolation
    8. multiple file changes
    9. unified diff modified file
   10. unified diff created file
   11. unified diff deleted file
   12. binary handling
   13. snapshot consistency
   14. run_command-induced filesystem change dapat terdeteksi
   15. no source mutation oleh tracker
   16. provider-agnostic
   17. tidak ada Git dependency
   18. tidak ada duplicate filesystem engine

Jalankan:
    python scripts/check_changes.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.changes import ChangeTracker, ChangeType, diff  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "changes_fixture"


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    # Tulis bytes langsung agar ukuran deterministik lintas platform.
    (FIXTURE / "keep.txt").write_bytes(b"unchanged\n")
    (FIXTURE / "mod.txt").write_bytes(b"line1\nline2\n")
    (FIXTURE / "del.txt").write_bytes(b"to be deleted\n")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Change/Diff Tracking Subsystem ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    tracker = ChangeTracker(root=FIXTURE)

    # Snapshot awal.
    tracker.start("task-1")
    tracker.snapshot(".", task_id="task-1")

    # 1) create file detection.
    (FIXTURE / "new.txt").write_bytes(b"brand new\n")
    # 2) modify file detection.
    (FIXTURE / "mod.txt").write_bytes(b"line1\nCHANGED\n")
    # 3) delete file detection.
    (FIXTURE / "del.txt").unlink()

    records = tracker.detect_changes("task-1")
    by_path = {r.path: r for r in records}

    assert "new.txt" in by_path and by_path["new.txt"].change_type == ChangeType.CREATED
    print("[1] create file detection OK")

    assert "mod.txt" in by_path and by_path["mod.txt"].change_type == ChangeType.MODIFIED
    print("[2] modify file detection OK")

    assert "del.txt" in by_path and by_path["del.txt"].change_type == ChangeType.DELETED
    print("[3] delete file detection OK")

    # 4) unchanged file.
    assert "keep.txt" not in by_path, "file tidak berubah tidak boleh tercatat"
    print("[4] unchanged file OK")

    # 5) before/after hash.
    mod = by_path["mod.txt"]
    assert mod.before_hash is not None and mod.after_hash is not None
    assert mod.before_hash != mod.after_hash
    created = by_path["new.txt"]
    assert created.before_hash is None and created.after_hash is not None
    deleted = by_path["del.txt"]
    assert deleted.before_hash is not None and deleted.after_hash is None
    print("[5] before/after hash OK")

    # 6) size tracking (bandingkan dengan ukuran file aktual di disk).
    assert created.before_size is None
    assert created.after_size == (FIXTURE / "new.txt").stat().st_size
    assert deleted.after_size is None
    assert deleted.before_size == len(b"to be deleted\n")
    assert mod.after_size == (FIXTURE / "mod.txt").stat().st_size
    assert mod.before_size == len(b"line1\nline2\n")
    print("[6] size tracking OK")

    # 7) ChangeSet task isolation.
    tracker.start("task-2")
    tracker.snapshot(".", task_id="task-2")
    (FIXTURE / "task2_only.txt").write_bytes(b"only task2\n")
    tracker.track("task-2")
    set1 = tracker.get_changes("task-1")
    set2 = tracker.get_changes("task-2")
    assert set1.task_id == "task-1" and set2.task_id == "task-2"
    assert set1 is not set2
    # task-1 tidak boleh mencampur perubahan task-2.
    assert all("task2_only.txt" not in c.path for c in set1.changes)
    assert any(c.path == "task2_only.txt" for c in set2.changes)
    print("[7] ChangeSet task isolation OK")

    # 8) multiple file changes.
    tracker.track("task-1")
    paths1 = {c.path for c in tracker.get_changes("task-1").changes}
    assert {"new.txt", "mod.txt", "del.txt"}.issubset(paths1)
    print(f"[8] multiple file changes OK -> {sorted(paths1)}")

    # 9) unified diff modified file.
    d_mod = diff.generate(b"line1\nline2\n", b"line1\nCHANGED\n", "mod.txt")
    assert d_mod.startswith("--- a/mod.txt") and "+++ b/mod.txt" in d_mod
    assert "-line2" in d_mod and "+CHANGED" in d_mod
    print("[9] unified diff modified file OK")

    # 10) unified diff created file.
    d_new = diff.generate(None, b"brand new\n", "new.txt")
    assert "/dev/null" in d_new and "+brand new" in d_new
    print("[10] unified diff created file OK")

    # 11) unified diff deleted file.
    d_del = diff.generate(b"to be deleted\n", None, "del.txt")
    assert "/dev/null" in d_del and "-to be deleted" in d_del
    print("[11] unified diff deleted file OK")

    # 12) binary handling.
    d_bin = diff.generate(b"\x00\x01\x02binary", b"\x00\x03\x04binary", "img.bin")
    assert "binary" in d_bin.lower() and "+++" not in d_bin, "binary tidak boleh dibuat diff teks palsu"
    print("[12] binary handling OK")

    # 13) snapshot consistency.
    snap_a = tracker.snapshot(".")
    snap_b = tracker.snapshot(".")
    assert snap_a == snap_b, "snapshot harus konsisten bila filesystem tidak berubah"
    print("[13] snapshot consistency OK")

    # 14) run_command-induced filesystem change dapat terdeteksi.
    tracker.start("task-3")
    tracker.snapshot(".", task_id="task-3")
    subprocess.run(
        [sys.executable, "-c", "open('cmd_created.txt','w').write('via command\\n')"],
        cwd=str(FIXTURE),
        check=True,
    )
    tracker.track("task-3")
    set3 = tracker.get_changes("task-3")
    assert any(c.path == "cmd_created.txt" and c.change_type == ChangeType.CREATED for c in set3.changes)
    print("[14] run_command-induced filesystem change terdeteksi OK")

    # 15) no source mutation oleh tracker.
    #     Snapshot + detect tidak boleh mengubah file.
    before_bytes = (FIXTURE / "mod.txt").read_bytes()
    tracker.snapshot(".")
    tracker.detect_changes("task-1")
    after_bytes = (FIXTURE / "mod.txt").read_bytes()
    assert before_bytes == after_bytes, "tracker tidak boleh mengubah file source"
    print("[15] no source mutation oleh tracker OK")

    # 16) provider-agnostic.
    changes_dir = SRC_DIR / "agent_ai" / "changes"
    for p in changes_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8").lower()
        for name in ("openrouter", "deepseek", "ollama"):
            assert name not in text, f"{p.name} tidak boleh hardcode provider '{name}'"
    print("[16] provider-agnostic OK")

    # 17) tidak ada Git dependency.
    for p in changes_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "import git" not in text and "subprocess" not in text, f"{p.name} tidak boleh pakai Git/subprocess"
    print("[17] tidak ada Git dependency OK")

    # 18) tidak ada duplicate filesystem engine.
    files = {p.name for p in changes_dir.glob("*.py")}
    assert files == {"__init__.py", "models.py", "tracker.py", "diff.py"}, files
    # tracker memakai helper filesystem yang sudah ada.
    tracker_src = (changes_dir / "tracker.py").read_text(encoding="utf-8")
    assert "from agent_ai.tools.filesystem import" in tracker_src, "harus reuse filesystem helper"
    print("[18] tidak ada duplicate filesystem engine OK")

    print()
    print("[OK] Change/Diff Tracking bekerja (deteksi, isolasi task, unified diff, binary-safe).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
