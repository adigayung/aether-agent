"""GitClient: abstraction untuk operasi Git read-only.

Provider-agnostic. Menggunakan executable `git` melalui subprocess (TANPA
library Git pihak ketiga). Read-only: tidak ada operasi mutasi Git.

    GitClient (ABC)
      └── SubprocessGitClient
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

from agent_ai.git.models import (
    GitCommit,
    GitDiffSummary,
    GitFileStatus,
    GitRepository,
    GitStatus,
)


class GitError(Exception):
    """Base error untuk operasi Git."""


class GitNotAvailableError(GitError):
    """Executable `git` tidak tersedia."""


class GitCommandError(GitError):
    """Perintah git mengembalikan error."""


class GitClient(ABC):
    """Interface operasi Git read-only."""

    @abstractmethod
    def is_repository(self, path: Path) -> bool:
        """True bila `path` berada di dalam repository Git."""
        raise NotImplementedError

    @abstractmethod
    def current_branch(self, path: Path) -> Optional[str]:
        """Nama branch saat ini (None bila detached/tidak ada)."""
        raise NotImplementedError

    @abstractmethod
    def status(self, path: Path) -> GitStatus:
        """Status repository (branch, clean, files)."""
        raise NotImplementedError

    @abstractmethod
    def diff(self, path: Path) -> List[GitDiffSummary]:
        """Ringkasan diff per file (working tree vs HEAD)."""
        raise NotImplementedError

    @abstractmethod
    def log(self, path: Path, limit: int = 10) -> List[GitCommit]:
        """Daftar commit terakhir (terbaru dulu)."""
        raise NotImplementedError


class SubprocessGitClient(GitClient):
    """Implementasi GitClient via executable `git` (subprocess).

    Args:
        git_executable: nama/path executable git (default "git").
        timeout: batas waktu tiap perintah git (detik).
    """

    def __init__(self, git_executable: str = "git", timeout: float = 30.0) -> None:
        self.git_executable = git_executable
        self.timeout = timeout

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    def _run(self, args: List[str], cwd: Path) -> str:
        """Jalankan perintah git dan kembalikan stdout.

        Raises:
            GitNotAvailableError: bila executable git tidak ditemukan.
            GitCommandError: bila perintah gagal.
        """
        try:
            completed = subprocess.run(
                [self.git_executable, *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise GitNotAvailableError(
                f"Executable git tidak ditemukan: {self.git_executable}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitCommandError(f"Perintah git timeout: {' '.join(args)}") from exc
        except OSError as exc:
            raise GitCommandError(f"Gagal menjalankan git: {exc}") from exc

        if completed.returncode != 0:
            raise GitCommandError(
                f"git {' '.join(args)} gagal (exit {completed.returncode}): "
                f"{completed.stderr.strip()}"
            )
        return completed.stdout

    # ------------------------------------------------------------------ #
    # Interface
    # ------------------------------------------------------------------ #
    def is_repository(self, path: Path) -> bool:
        try:
            out = self._run(["rev-parse", "--is-inside-work-tree"], path)
        except GitError:
            return False
        return out.strip() == "true"

    def current_branch(self, path: Path) -> Optional[str]:
        try:
            out = self._run(["rev-parse", "--abbrev-ref", "HEAD"], path)
        except GitError:
            return None
        branch = out.strip()
        if not branch or branch == "HEAD":  # detached HEAD
            return None
        return branch

    def status(self, path: Path) -> GitStatus:
        branch = self.current_branch(path)
        # --porcelain=v1 -z: format stabil & machine-readable.
        out = self._run(["status", "--porcelain=v1", "-z"], path)
        files = self._parse_status(out)
        return GitStatus(branch=branch, clean=len(files) == 0, files=files)

    @staticmethod
    def _parse_status(raw: str) -> List[GitFileStatus]:
        """Parse output `git status --porcelain=v1 -z`."""
        files: List[GitFileStatus] = []
        # -z memisahkan entri dengan NUL; rename punya dua field.
        entries = [e for e in raw.split("\x00") if e]
        i = 0
        while i < len(entries):
            entry = entries[i]
            if len(entry) < 3:
                i += 1
                continue
            x = entry[0]  # status index (staged)
            y = entry[1]  # status working tree (unstaged)
            file_path = entry[3:]
            # Rename/copy: entri berikutnya adalah path asal -> lewati.
            if x in ("R", "C"):
                i += 1
            untracked = x == "?" and y == "?"
            files.append(
                GitFileStatus(
                    path=file_path,
                    status=(x + y).strip() or "??",
                    staged=x not in (" ", "?"),
                    unstaged=y not in (" ", "?"),
                    untracked=untracked,
                )
            )
            i += 1
        return files

    def diff(self, path: Path) -> List[GitDiffSummary]:
        # --numstat: additions/deletions per file (working tree vs HEAD).
        out = self._run(["diff", "--numstat", "HEAD"], path)
        summaries: List[GitDiffSummary] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            add_raw, del_raw, file_path = parts[0], parts[1], parts[2]
            additions = int(add_raw) if add_raw.isdigit() else 0
            deletions = int(del_raw) if del_raw.isdigit() else 0
            summaries.append(
                GitDiffSummary(
                    path=file_path,
                    additions=additions,
                    deletions=deletions,
                    status="modified",
                )
            )
        return summaries

    def log(self, path: Path, limit: int = 10) -> List[GitCommit]:
        # Format: hash<US>short<US>author<US>timestamp<US>subject
        fmt = "%H%x1f%h%x1f%an%x1f%at%x1f%s"
        out = self._run(["log", f"-n{int(limit)}", f"--pretty=format:{fmt}"], path)
        commits: List[GitCommit] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\x1f")
            if len(parts) < 5:
                continue
            full, short, author, ts, subject = parts[0], parts[1], parts[2], parts[3], parts[4]
            try:
                timestamp = float(ts)
            except ValueError:
                timestamp = 0.0
            commits.append(
                GitCommit(
                    hash=full,
                    short_hash=short,
                    author=author,
                    timestamp=timestamp,
                    subject=subject,
                )
            )
        return commits
