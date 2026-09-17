"""Model untuk Git Awareness Foundation.

Provider-agnostic, read-only. Model di sini plain data (dataclass) dan TIDAK
menyimpan credential.

    GitRepository   -> info repository (root, is_repository, branch)
    GitFileStatus   -> status satu file
    GitStatus       -> status keseluruhan (branch, clean, files)
    GitCommit       -> satu commit (hash, author, timestamp, subject)
    GitDiffSummary  -> ringkasan diff satu file (additions, deletions, status)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class GitRepository:
    """Info sebuah repository Git.

    Attributes:
        root: path root repository (workspace root).
        is_repository: True bila path adalah repository Git.
        branch: nama branch saat ini (None bila detached/tidak ada).
    """

    root: str
    is_repository: bool = False
    branch: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "is_repository": self.is_repository,
            "branch": self.branch,
        }


@dataclass
class GitFileStatus:
    """Status satu file dalam repository.

    Attributes:
        path: path relatif terhadap repository root.
        status: kode status ringkas (mis. "M", "A", "??", "D").
        staged: True bila ada perubahan di index (staged).
        unstaged: True bila ada perubahan di working tree (unstaged).
        untracked: True bila file belum dilacak Git.
    """

    path: str
    status: str = ""
    staged: bool = False
    unstaged: bool = False
    untracked: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "staged": self.staged,
            "unstaged": self.unstaged,
            "untracked": self.untracked,
        }


@dataclass
class GitStatus:
    """Status keseluruhan repository.

    Attributes:
        branch: nama branch saat ini.
        clean: True bila tidak ada perubahan (working tree bersih).
        files: daftar GitFileStatus.
    """

    branch: Optional[str] = None
    clean: bool = True
    files: List[GitFileStatus] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "branch": self.branch,
            "clean": self.clean,
            "files": [f.to_dict() for f in self.files],
        }


@dataclass
class GitCommit:
    """Satu commit Git.

    Attributes:
        hash: hash lengkap commit.
        short_hash: hash singkat.
        author: nama author.
        timestamp: waktu commit (epoch detik).
        subject: baris subjek commit.
    """

    hash: str
    short_hash: str = ""
    author: str = ""
    timestamp: float = 0.0
    subject: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hash": self.hash,
            "short_hash": self.short_hash,
            "author": self.author,
            "timestamp": self.timestamp,
            "subject": self.subject,
        }


@dataclass
class GitDiffSummary:
    """Ringkasan diff satu file.

    Attributes:
        path: path file.
        additions: jumlah baris ditambahkan.
        deletions: jumlah baris dihapus.
        status: status perubahan (mis. "modified", "added", "deleted").
    """

    path: str
    additions: int = 0
    deletions: int = 0
    status: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "additions": self.additions,
            "deletions": self.deletions,
            "status": self.status,
        }
