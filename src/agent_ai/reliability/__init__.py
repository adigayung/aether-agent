"""Reliability Subsystem AETHER.

Provider-agnostic. Menangani model/provider yang kadang gagal, mengulang
action, timeout, malformed tool call, atau menghabiskan iteration tanpa progres.

    from agent_ai.reliability import (
        ReliabilityManager,
        Detector,
        RetryPolicy,
        ReliabilityEvent,
        ReliabilityDecision,
        ProgressSnapshot,
        EventType,
        DecisionAction,
    )

Extension point (belum diimplementasikan, sengaja disiapkan):
    - fallback provider/model routing (tahap berikutnya).
"""

from agent_ai.reliability.detector import Detector
from agent_ai.reliability.manager import ReliabilityManager
from agent_ai.reliability.models import (
    DecisionAction,
    EventType,
    ProgressSnapshot,
    ReliabilityDecision,
    ReliabilityEvent,
    RetryPolicy,
)
from agent_ai.reliability.retry import RetryController

__all__ = [
    "Detector",
    "ReliabilityManager",
    "RetryController",
    "RetryPolicy",
    "ReliabilityEvent",
    "ReliabilityDecision",
    "ProgressSnapshot",
    "EventType",
    "DecisionAction",
]
