"""Verifikasi Git Awareness Foundation.

Deterministik. Menggunakan temporary Git repository di
J:\\Agent_Ai\\dummy_test\\git_fixture dan menghapusnya setelah test.

Menguji:
    1. repository detection
    2. branch detection
    3. clean status
    4. untracked file
    5. modified file
    6. staged file
    7. diff summary
    8. log
    9. repository boundary
   10. non-repository handling
   11. subprocess error handling
   12. no mutation (read-only)
   13. no provider dependency
   14. no Runtime dependency

Jalankan:
    python scripts/check_git.py
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

from agent_ai.git import (  # noqa: E402
    GitCommandError,
    GitNotAvailableError,
    GitRepositoryFacade,
    SubprocessGitClient,
)
from agent_ai.tools.base import ToolError  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "git_fixture"


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], FIXTURE)
    _git(["config", "user.email", "test@example.com"], FIXTURE)
    _git(["config", "user.name", "Test User"], FIXTURE)
    (FIXTURE / "tracked.txt").write_bytes(b"line1\nline2\n")
    _git(["add", "tracked.txt"], FIXTURE)
    _git(["commit", "-m", "initial commit"], FIXTURE)


def teardown_fixture() -> None:
    """Hapus fixture. File .git di Windows read-only -> bersihkan atribut dulu."""
    if not FIXTURE.exists():
        return
    for p in FIXTURE.rglob("*"):
        try:
            p.chmod(0o777)
        except OSError:
            pass
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Git Awareness Foundation ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    repo = GitRepositoryFacade(root=FIXTURE)

    # 1) repository detection.
    assert repo.is_repository() is True
    info = repo.repository()
    assert info.is_repository is True
    print("[1] repository detection OK")

    # 2) branch detection.
    assert repo.current_branch() == "main", repo.current_branch()
    assert info.branch == "main"
    print("[2] branch detection OK")

    # 3) clean status.
    status = repo.status()
    assert status.clean is True, status.to_dict()
    assert status.files == []
    assert status.branch == "main"
    print("[3] clean status OK")

    # 4) untracked file.
    (FIXTURE / "untracked.txt").write_bytes(b"new\n")
    status = repo.status()
    assert status.clean is False
    untracked = [f for f in status.files if f.path == "untracked.txt"]
    assert untracked and untracked[0].untracked is True, status.to_dict()
    print("[4] untracked file OK")

    # 5) modified file.
    (FIXTURE / "tracked.txt").write_bytes(b"line1\nCHANGED\nline3\n")
    status = repo.status()
    modified = [f for f in status.files if f.path == "tracked.txt"]
    assert modified and modified[0].unstaged is True, status.to_dict()
    print("[5] modified file OK")

    # 6) staged file.
    (FIXTURE / "staged.txt").write_bytes(b"staged content\n")
    _git(["add", "staged.txt"], FIXTURE)
    status = repo.status()
    staged = [f for f in status.files if f.path == "staged.txt"]
    assert staged and staged[0].staged is True, status.to_dict()
    print("[6] staged file OK")

    # 7) diff summary.
    diffs = repo.diff()
    tracked_diff = [d for d in diffs if d.path == "tracked.txt"]
    assert tracked_diff, diffs
    assert tracked_diff[0].additions >= 1 and tracked_diff[0].deletions >= 1, tracked_diff[0].to_dict()
    print("[7] diff summary OK")

    # 8) log.
    commits = repo.log(limit=5)
    assert len(commits) >= 1
    assert commits[0].subject == "initial commit"
    assert commits[0].author == "Test User"
    assert commits[0].hash and commits[0].short_hash
    assert commits[0].timestamp > 0
    print("[8] log OK")

    # 9) repository boundary.
    #    Path di luar workspace harus ditolak (ToolError dari _resolve_within_root).
    try:
        repo.status(path="../../etc")
        raise AssertionError("path di luar boundary seharusnya ditolak")
    except ToolError:
        pass
    # Path valid di dalam boundary tetap bekerja.
    assert repo.is_repository(".") is True
    print("[9] repository boundary OK")

    # 10) non-repository handling.
    non_repo_dir = DUMMY_ROOT / "not_a_repo"
    non_repo_dir.mkdir(parents=True, exist_ok=True)
    try:
        non_repo = GitRepositoryFacade(root=non_repo_dir)
        assert non_repo.is_repository() is False
        assert non_repo.current_branch() is None
        st = non_repo.status()
        assert st.clean is True and st.files == [] and st.branch is None
        assert non_repo.diff() == []
        assert non_repo.log() == []
    finally:
        shutil.rmtree(non_repo_dir, ignore_errors=True)
    print("[10] non-repository handling OK")

    # 11) subprocess error handling.
    #     Executable git yang tidak ada -> GitNotAvailableError (via _run).
    bad_client = SubprocessGitClient(git_executable="git_tidak_ada_xyz")
    try:
        bad_client._run(["status"], FIXTURE)
        raise AssertionError("harus raise GitNotAvailableError")
    except GitNotAvailableError:
        pass
    #     is_repository menelan error -> False (bukan crash).
    assert bad_client.is_repository(FIXTURE) is False
    #     Perintah git yang gagal -> GitCommandError.
    try:
        SubprocessGitClient()._run(["log", "--tidak-ada-opsi"], FIXTURE)
        raise AssertionError("harus raise GitCommandError")
    except GitCommandError:
        pass
    print("[11] subprocess error handling OK")

    # 12) no mutation (read-only).
    #     Snapshot status sebelum & sesudah operasi read-only harus sama.
    before = repo.status().to_dict()
    repo.repository()
    repo.current_branch()
    repo.status()
    repo.diff()
    repo.log()
    after = repo.status().to_dict()
    assert before == after, "operasi Git harus read-only (tidak mengubah status)"
    # Tidak ada API mutasi Git di facade.
    for forbidden in ("add", "commit", "push", "pull", "checkout", "reset", "merge", "rebase"):
        assert not hasattr(repo, forbidden), f"facade tidak boleh punya operasi mutasi '{forbidden}'"
    print("[12] no mutation (read-only) OK")

    # 13) no provider dependency.
    git_dir = SRC_DIR / "agent_ai" / "git"
    files = {p.name for p in git_dir.glob("*.py")}
    assert files == {"__init__.py", "models.py", "client.py", "repository.py"}, files
    for p in git_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8").lower()
        for bad in ("openrouter", "deepseek", "ollama", "openai", "requests"):
            assert bad not in text, f"{p.name} tidak boleh menyebut '{bad}'"
        # Tidak ada import ke package providers.
        assert "agent_ai.providers" not in text, f"{p.name} tidak boleh impor providers"
    print("[13] no provider dependency OK")

    # 14) no Runtime dependency.
    for p in git_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "agent_ai.runtime" not in text, f"{p.name} tidak boleh impor runtime"
        assert "agent_ai.core" not in text, f"{p.name} tidak boleh impor core"
    print("[14] no Runtime dependency OK")

    print()
    print("[OK] Git Awareness Foundation bekerja (read-only, boundary-aware, provider/runtime-independent).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
