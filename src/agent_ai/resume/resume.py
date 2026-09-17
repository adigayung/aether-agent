"""TaskResumer: mekanisme resume/continue task yang aman.

Provider-agnostic. Merekonstruksi state dari fondasi yang sudah ada:
    - Task Lifecycle (TaskState snapshot)
    - Session & Execution Event System (daftar ExecutionEvent)
    - Change Tracking (ChangeTracker, opsional)
    - Git Awareness (GitRepositoryFacade, opsional)

Prinsip:
    - TIDAK membuat state engine kedua: hanya membaca snapshot/event yang ada.
    - TIDAK menganggap task aman dilanjutkan hanya karena session masih ada.
    - Mencegah double-resume (idempotent).
    - Mendeteksi divergence workspace (fingerprint berubah).
    - Persistence-ready: seluruh input dapat direkonstruksi dari data
      serializable (TaskState + events + metadata workspace).

Tidak ada UI/database. Tidak menjalankan agent logic.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from agent_ai.resume.models import (
    ResumePlan,
    ResumeResult,
    ResumeStatus,
    WorkspaceSnapshot,
)

#: Status lifecycle yang dianggap "berhenti" dan berpotensi dilanjutkan.
_RESUMABLE_STATUSES = frozenset({"created", "preparing", "planning", "running", "validating"})
#: Status lifecycle terminal yang tidak boleh dilanjutkan.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _fingerprint(parts: Dict[str, Any]) -> str:
    """Hash ringkas dari kondisi workspace (deterministik)."""
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


class TaskResumer:
    """Mengelola resume/continue task.

    Args:
        git_facade: GitRepositoryFacade opsional (untuk inspeksi workspace).
        change_tracker: ChangeTracker opsional (untuk melihat perubahan task).
        workspace_root: root workspace (untuk fingerprint & git).
    """

    def __init__(
        self,
        git_facade: Optional[Any] = None,
        change_tracker: Optional[Any] = None,
        workspace_root: Optional[str] = None,
    ) -> None:
        self.git_facade = git_facade
        self.change_tracker = change_tracker
        self.workspace_root = workspace_root
        # task_id -> fingerprint workspace saat terakhir resume (anti double-resume).
        self._resumed: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Workspace inspection
    # ------------------------------------------------------------------ #
    def inspect_workspace(self) -> WorkspaceSnapshot:
        """Inspeksi kondisi workspace (read-only) sebelum resume."""
        snap = WorkspaceSnapshot(root=self.workspace_root or "")
        if self.git_facade is not None:
            try:
                snap.is_git_repository = bool(self.git_facade.is_repository())
                if snap.is_git_repository:
                    snap.git_branch = self.git_facade.current_branch()
                    status = self.git_facade.status()
                    snap.git_clean = status.clean
                    snap.changed_files = [f.path for f in status.files]
            except Exception:  # noqa: BLE001 - inspeksi tidak boleh crash resume
                snap.is_git_repository = False
        snap.fingerprint = _fingerprint({
            "root": snap.root,
            "is_git": snap.is_git_repository,
            "branch": snap.git_branch,
            "clean": snap.git_clean,
            "changed": sorted(snap.changed_files),
        })
        return snap

    # ------------------------------------------------------------------ #
    # State reconstruction
    # ------------------------------------------------------------------ #
    @staticmethod
    def reconstruct_state(
        lifecycle_snapshot: Any,
        events: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """Rekonstruksi state task dari snapshot lifecycle + events.

        Tidak membuat state engine baru: hanya merangkum data yang sudah ada.

        Args:
            lifecycle_snapshot: TaskState (atau objek dengan atribut serupa).
            events: daftar ExecutionEvent (opsional).

        Returns:
            Dict ringkas: task_id, status, phase, last_event, event_count.
        """
        status = getattr(getattr(lifecycle_snapshot, "status", None), "value", None)
        phase = getattr(getattr(lifecycle_snapshot, "phase", None), "value", None)
        events = events or []
        last_event = None
        if events:
            ordered = sorted(events, key=lambda e: getattr(e, "sequence", 0))
            last = ordered[-1]
            last_event = {
                "event_type": getattr(getattr(last, "event_type", None), "value", None),
                "sequence": getattr(last, "sequence", None),
                "timestamp": getattr(last, "timestamp", None),
            }
        return {
            "task_id": getattr(lifecycle_snapshot, "task_id", None),
            "status": status,
            "phase": phase,
            "event_count": len(events),
            "last_event": last_event,
        }

    # ------------------------------------------------------------------ #
    # Planning
    # ------------------------------------------------------------------ #
    def plan_resume(
        self,
        lifecycle_snapshot: Any,
        events: Optional[List[Any]] = None,
        session_id: Optional[str] = None,
        expected_fingerprint: Optional[str] = None,
    ) -> ResumePlan:
        """Bangun ResumePlan (keputusan) tanpa mengeksekusi apa pun.

        Args:
            lifecycle_snapshot: TaskState terakhir.
            events: daftar ExecutionEvent task.
            session_id: session id (opsional).
            expected_fingerprint: fingerprint workspace yang diharapkan
                (untuk deteksi divergence). Bila None, divergence tidak dicek.

        Returns:
            ResumePlan.
        """
        state = self.reconstruct_state(lifecycle_snapshot, events)
        task_id = state["task_id"] or ""
        last_status = state["status"]
        last_phase = state["phase"]

        plan = ResumePlan(
            task_id=task_id,
            session_id=session_id,
            last_status=last_status,
            last_phase=last_phase,
            metadata={"event_count": state["event_count"], "last_event": state["last_event"]},
        )

        # 1) Task sudah terminal -> tidak boleh dilanjutkan.
        if last_status in _TERMINAL_STATUSES:
            plan.status = ResumeStatus.NOT_RESUMABLE
            plan.reason = f"Task sudah terminal (status='{last_status}')."
            return plan

        # 2) Status tidak dikenal -> tidak cukup informasi.
        if last_status not in _RESUMABLE_STATUSES:
            plan.status = ResumeStatus.UNKNOWN
            plan.reason = f"Status lifecycle tidak dikenal: '{last_status}'."
            return plan

        # 3) Inspeksi workspace.
        workspace = self.inspect_workspace()
        plan.workspace = workspace

        # 4) Deteksi divergence (workspace berubah sejak task berhenti).
        if expected_fingerprint is not None and workspace.fingerprint != expected_fingerprint:
            plan.status = ResumeStatus.DIVERGED
            plan.reason = "Workspace berubah sejak task berhenti (fingerprint berbeda)."
            plan.warnings.append(
                f"fingerprint diharapkan={expected_fingerprint}, kini={workspace.fingerprint}"
            )
            return plan

        # 5) Cegah double-resume: task yang sama dengan fingerprint sama.
        if task_id in self._resumed and self._resumed[task_id] == workspace.fingerprint:
            plan.status = ResumeStatus.ALREADY_RESUMED
            plan.reason = "Task sudah pernah di-resume pada kondisi workspace ini."
            return plan

        # 6) Aman dilanjutkan.
        plan.status = ResumeStatus.RESUMABLE
        plan.reason = "Task dapat dilanjutkan."
        plan.resume_from_phase = self._suggest_phase(last_status, last_phase)
        if workspace.is_git_repository and workspace.git_clean is False:
            plan.warnings.append(
                f"Working tree tidak bersih ({len(workspace.changed_files)} file berubah)."
            )
        return plan

    @staticmethod
    def _suggest_phase(last_status: Optional[str], last_phase: Optional[str]) -> Optional[str]:
        """Saran fase untuk melanjutkan berdasarkan status/fase terakhir."""
        mapping = {
            "created": "preparation",
            "preparing": "planning",
            "planning": "execution",
            "running": "execution",
            "validating": "validation",
        }
        return mapping.get(last_status or "", last_phase)

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def resume(
        self,
        lifecycle_snapshot: Any,
        events: Optional[List[Any]] = None,
        session_id: Optional[str] = None,
        expected_fingerprint: Optional[str] = None,
    ) -> ResumeResult:
        """Rencanakan dan (bila aman) tandai task sebagai di-resume.

        Catatan: method ini TIDAK menjalankan agent logic. Ia hanya memutuskan
        dan mencatat bahwa task boleh dilanjutkan (mencegah double-resume).
        Eksekusi nyata tetap tanggung jawab Runtime.

        Returns:
            ResumeResult (resumed=True bila aman dilanjutkan).
        """
        plan = self.plan_resume(
            lifecycle_snapshot,
            events=events,
            session_id=session_id,
            expected_fingerprint=expected_fingerprint,
        )
        if not plan.can_resume:
            return ResumeResult(task_id=plan.task_id, resumed=False, plan=plan, error=plan.reason)

        # Catat fingerprint agar double-resume pada kondisi sama dicegah.
        if plan.workspace is not None:
            self._resumed[plan.task_id] = plan.workspace.fingerprint or ""
        return ResumeResult(task_id=plan.task_id, resumed=True, plan=plan)

    def reset(self, task_id: str) -> None:
        """Hapus catatan resume untuk task (mis. setelah workspace berubah)."""
        self._resumed.pop(task_id, None)
