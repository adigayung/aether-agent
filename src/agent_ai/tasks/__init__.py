"""Task Lifecycle & Execution State AETHER.

Satu identitas dan state lifecycle yang konsisten dari task masuk sampai
selesai. Ini orchestration metadata, BUKAN pengganti AgentLoop/Runtime/
Reliability/Validation/Changes/Planning.

    from agent_ai.tasks import TaskLifecycle, TaskStatus, TaskPhase

    lc = TaskLifecycle("Perbaiki bug login")
    lc.transition(TaskStatus.PREPARING)
    ...
    lc.complete(result="selesai")

Tidak ada database/persistence/UI.
"""

from agent_ai.tasks.lifecycle import InvalidTransitionError, TaskLifecycle
from agent_ai.tasks.models import (
    TERMINAL_STATUSES,
    TaskPhase,
    TaskState,
    TaskStatus,
    new_task_id,
)

__all__ = [
    "TaskLifecycle",
    "InvalidTransitionError",
    "TaskStatus",
    "TaskPhase",
    "TaskState",
    "TERMINAL_STATUSES",
    "new_task_id",
]
