"""Session & Execution Event Architecture AETHER.

Fondasi untuk session history, execution history, resume/recovery, dan UI
event stream di masa depan — tanpa database/UI.

    from agent_ai.session import (
        Session, SessionStatus, TaskReference,
        ExecutionEvent, EventType,
        SessionStore, InMemorySessionStore,
    )

Relasi:

    Session
      └── Task (reference: task_id)
            └── Execution (events)

Session TIDAK menggantikan TaskLifecycle. Store tidak menjalankan logic agent
dan tidak mengetahui provider/tool tertentu.
"""

from agent_ai.session.events import (
    EventType,
    ExecutionEvent,
    event_from_lifecycle_snapshot,
    make_event,
    new_event_id,
)
from agent_ai.session.models import (
    Session,
    SessionStatus,
    TaskReference,
    new_session_id,
)
from agent_ai.session.store import InMemorySessionStore, SessionStore

__all__ = [
    "Session",
    "SessionStatus",
    "TaskReference",
    "new_session_id",
    "ExecutionEvent",
    "EventType",
    "make_event",
    "new_event_id",
    "event_from_lifecycle_snapshot",
    "SessionStore",
    "InMemorySessionStore",
]

