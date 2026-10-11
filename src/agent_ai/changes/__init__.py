"""Change/Diff Tracking Subsystem AETHER.

Provider-agnostic. Melacak perubahan filesystem selama satu task tanpa
Git/database.

    from agent_ai.changes import (
        ChangeType,
        ChangeRecord,
        ChangeSet,
        ChangeTracker,
        TaskChangeEvidence,
        diff,
    )

Read-only terhadap source project. Isolasi per task_id.
`TaskChangeEvidence` = adapter tipis di atas ChangeTracker (bukan tracker
kedua) untuk menyediakan bukti perubahan ringkas pada continuous runtime.
"""

from agent_ai.changes import diff
from agent_ai.changes.models import ChangeRecord, ChangeSet, ChangeType
from agent_ai.changes.task_evidence import TaskChangeEvidence
from agent_ai.changes.tracker import ChangeTracker

__all__ = [
    "ChangeType",
    "ChangeRecord",
    "ChangeSet",
    "ChangeTracker",
    "TaskChangeEvidence",
    "diff",
]
