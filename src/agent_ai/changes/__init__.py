"""Change/Diff Tracking Subsystem AETHER.

Provider-agnostic. Melacak perubahan filesystem selama satu task tanpa
Git/database.

    from agent_ai.changes import (
        ChangeType,
        ChangeRecord,
        ChangeSet,
        ChangeTracker,
        diff,
    )

Read-only terhadap source project. Isolasi per task_id.
"""

from agent_ai.changes import diff
from agent_ai.changes.models import ChangeRecord, ChangeSet, ChangeType
from agent_ai.changes.tracker import ChangeTracker

__all__ = [
    "ChangeType",
    "ChangeRecord",
    "ChangeSet",
    "ChangeTracker",
    "diff",
]
