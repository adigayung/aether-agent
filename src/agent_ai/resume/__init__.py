"""Resume / Continue Task AETHER.

Mekanisme melanjutkan task yang berhenti/gagal/interrupted dengan aman,
berdasarkan fondasi yang sudah ada (Task Lifecycle, Session & Execution Event,
Change Tracking, Git Awareness, Runtime).

    from agent_ai.resume import TaskResumer, ResumeStatus

    resumer = TaskResumer(git_facade=repo, workspace_root=root)
    plan = resumer.plan_resume(lifecycle.snapshot(), events=store.get_events(task_id=tid))
    if plan.can_resume:
        ...

TIDAK membuat state engine kedua, UI, atau database. Persistence-ready.
"""

from agent_ai.resume.models import (
    ResumePlan,
    ResumeResult,
    ResumeStatus,
    WorkspaceSnapshot,
)
from agent_ai.resume.resume import TaskResumer

__all__ = [
    "ResumeStatus",
    "WorkspaceSnapshot",
    "ResumePlan",
    "ResumeResult",
    "TaskResumer",
]
