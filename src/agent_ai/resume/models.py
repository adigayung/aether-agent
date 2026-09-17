"""Model untuk Resume / Continue Task.

Provider-agnostic. Model di sini plain data (dataclass) dan persistence-ready:
semua input resume dapat direkonstruksi dari data serializable (TaskState
snapshot + daftar ExecutionEvent + metadata workspace).

    ResumeStatus   -> keputusan resume
    WorkspaceSnapshot -> kondisi workspace saat inspeksi
    ResumePlan     -> rencana resume (status, alasan, langkah)
    ResumeResult   -> hasil eksekusi resume
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ResumeStatus(str, Enum):
    """Keputusan apakah task dapat dilanjutkan."""

    RESUMABLE = "resumable"          # aman dilanjutkan
    NOT_RESUMABLE = "not_resumable"  # tidak boleh dilanjutkan (mis. sudah selesai)
    DIVERGED = "diverged"            # workspace berubah sejak task berhenti
    ALREADY_RESUMED = "already_resumed"  # double-resume dicegah
    UNKNOWN = "unknown"              # state tidak cukup untuk memutuskan


@dataclass
class WorkspaceSnapshot:
    """Kondisi workspace saat inspeksi sebelum resume.

    Attributes:
        root: root workspace.
        is_git_repository: True bila workspace adalah repo Git.
        git_branch: branch Git saat ini (None bila bukan repo).
        git_clean: True bila working tree bersih (None bila bukan repo).
        changed_files: daftar path yang berubah (dari Git status, bila ada).
        fingerprint: hash ringkas kondisi workspace (untuk deteksi divergence).
    """

    root: str = ""
    is_git_repository: bool = False
    git_branch: Optional[str] = None
    git_clean: Optional[bool] = None
    changed_files: List[str] = field(default_factory=list)
    fingerprint: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "is_git_repository": self.is_git_repository,
            "git_branch": self.git_branch,
            "git_clean": self.git_clean,
            "changed_files": list(self.changed_files),
            "fingerprint": self.fingerprint,
        }


@dataclass
class ResumePlan:
    """Rencana resume untuk sebuah task.

    Attributes:
        task_id: identifier task.
        session_id: identifier session (opsional).
        status: keputusan resume.
        reason: alasan keputusan.
        last_status: status lifecycle terakhir yang diketahui.
        last_phase: fase terakhir yang diketahui.
        resume_from_phase: fase yang disarankan untuk dilanjutkan.
        workspace: snapshot workspace saat inspeksi.
        warnings: peringatan (mis. divergence, perubahan tak terduga).
        metadata: info tambahan bebas.
    """

    task_id: str
    session_id: Optional[str] = None
    status: ResumeStatus = ResumeStatus.UNKNOWN
    reason: str = ""
    last_status: Optional[str] = None
    last_phase: Optional[str] = None
    resume_from_phase: Optional[str] = None
    workspace: Optional[WorkspaceSnapshot] = None
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def can_resume(self) -> bool:
        return self.status == ResumeStatus.RESUMABLE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "status": self.status.value,
            "reason": self.reason,
            "last_status": self.last_status,
            "last_phase": self.last_phase,
            "resume_from_phase": self.resume_from_phase,
            "workspace": self.workspace.to_dict() if self.workspace else None,
            "warnings": list(self.warnings),
            "metadata": self.metadata,
        }


@dataclass
class ResumeResult:
    """Hasil eksekusi resume.

    Attributes:
        task_id: identifier task.
        resumed: True bila task benar-benar dilanjutkan.
        plan: ResumePlan yang dipakai.
        error: pesan error bila resume gagal.
        metadata: info tambahan bebas.
    """

    task_id: str
    resumed: bool = False
    plan: Optional[ResumePlan] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "resumed": self.resumed,
            "plan": self.plan.to_dict() if self.plan else None,
            "error": self.error,
            "metadata": self.metadata,
        }
