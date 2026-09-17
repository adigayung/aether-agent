"""Advanced Recovery Subsystem AETHER (#43).

Recovery adalah *orchestration/recovery policy*, BUKAN executor baru. Ia
mengorkestrasi tindakan berdasarkan subsystem yang sudah ada:

    ReliabilityManager  -> keputusan retry/repeated-action/no-progress
    ValidationResult    -> hasil validation
    Replanner           -> revisi plan
    ChangeTracker       -> apakah workspace berubah
    Git (read-only)     -> kondisi repository
    SessionStore        -> event recovery_started/recovery_completed

    from agent_ai.recovery import RecoveryManager, FailureSignal, RecoveryAction

Recovery TIDAK membuat engine kedua untuk reliability/validation/replanning/
execution, dan tidak melakukan operasi Git destruktif.
"""

from agent_ai.recovery.classifier import FailureClassifier
from agent_ai.recovery.manager import RecoveryManager
from agent_ai.recovery.models import (
    FailureKind,
    FailureSignal,
    RecoveryAction,
    RecoveryConfig,
    RecoveryDecision,
)
from agent_ai.recovery.policy import RecoveryPolicy

__all__ = [
    "RecoveryManager",
    "RecoveryPolicy",
    "FailureClassifier",
    "FailureKind",
    "FailureSignal",
    "RecoveryAction",
    "RecoveryDecision",
    "RecoveryConfig",
]
