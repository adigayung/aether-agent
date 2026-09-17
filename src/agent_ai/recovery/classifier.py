"""FailureClassifier: klasifikasi kegagalan terstruktur (bukan string exception).

Provider-agnostic. Mengklasifikasi `FailureSignal` (yang berasal dari subsystem
yang sudah ada: Reliability, Validation, ChangeTracker, Runtime) menjadi
`FailureKind`.

Prinsip:
    - TIDAK mengklasifikasi berdasarkan pesan exception mentah.
    - Memakai outcome/event terstruktur dari subsystem.
    - Deterministik dan bounded.
"""

from __future__ import annotations

from typing import Optional

from agent_ai.recovery.models import FailureKind, FailureSignal

# Outcome terstruktur -> FailureKind (dari Reliability/Validation/Runtime).
_OUTCOME_MAP = {
    "command_failure": FailureKind.COMMAND_FAILURE,
    "tool_failure": FailureKind.TOOL_FAILURE,
    "execution_error": FailureKind.TOOL_FAILURE,
    "timeout": FailureKind.TIMEOUT,
    "provider_error": FailureKind.PROVIDER_ERROR,
    "malformed_tool_response": FailureKind.PROVIDER_ERROR,
    "validation_failure": FailureKind.VALIDATION_FAILURE,
    "repeated_action": FailureKind.REPEATED_ACTION,
    "repeated_failed_action": FailureKind.REPEATED_ACTION,
    "no_progress": FailureKind.NO_PROGRESS,
    "workspace_changed": FailureKind.WORKSPACE_CHANGED,
}

# Event reliability -> FailureKind.
_EVENT_MAP = {
    "repeated_action": FailureKind.REPEATED_ACTION,
    "repeated_arguments": FailureKind.REPEATED_ACTION,
    "repeated_failed_action": FailureKind.REPEATED_ACTION,
    "no_progress": FailureKind.NO_PROGRESS,
    "timeout": FailureKind.TIMEOUT,
    "provider_error": FailureKind.PROVIDER_ERROR,
    "malformed_tool_response": FailureKind.PROVIDER_ERROR,
}

# Outcome validation -> FailureKind.
_VALIDATION_MAP = {
    "command_failure": FailureKind.VALIDATION_FAILURE,
    "timeout": FailureKind.TIMEOUT,
    "execution_error": FailureKind.VALIDATION_FAILURE,
}


class FailureClassifier:
    """Mengklasifikasi FailureSignal menjadi FailureKind (deterministik)."""

    def classify(self, signal: FailureSignal) -> FailureKind:
        """Klasifikasi kegagalan dari sinyal terstruktur.

        Prioritas (paling spesifik dulu):
            1. workspace berubah tak terduga
            2. repeated action / no-progress (dari reliability events)
            3. timeout
            4. provider error
            5. validation failure
            6. command failure
            7. tool failure
            8. unknown
        """
        # 1) Workspace berubah tak terduga.
        if signal.workspace_changed:
            return FailureKind.WORKSPACE_CHANGED

        # 2) Repeated action / no-progress dari reliability events.
        for event in signal.reliability_events:
            kind = _EVENT_MAP.get(event)
            if kind in (FailureKind.REPEATED_ACTION, FailureKind.NO_PROGRESS):
                return kind

        # 3) Timeout (outcome atau event).
        if signal.outcome == "timeout" or "timeout" in signal.reliability_events:
            return FailureKind.TIMEOUT

        # 4) Provider error (outcome atau event).
        if signal.outcome in ("provider_error", "malformed_tool_response"):
            return FailureKind.PROVIDER_ERROR
        if "provider_error" in signal.reliability_events:
            return FailureKind.PROVIDER_ERROR

        # 5) Validation failure.
        if signal.validation_outcome is not None:
            mapped = _VALIDATION_MAP.get(signal.validation_outcome)
            if mapped is not None:
                return mapped
            return FailureKind.VALIDATION_FAILURE

        # 6) Command failure (outcome terstruktur atau flag is_command).
        if signal.outcome == "command_failure":
            return FailureKind.COMMAND_FAILURE
        if signal.is_command and signal.outcome not in (None, "success"):
            return FailureKind.COMMAND_FAILURE

        # 7) Tool failure (outcome terstruktur atau flag is_tool).
        if signal.outcome in ("tool_failure", "execution_error"):
            return FailureKind.TOOL_FAILURE
        if signal.is_tool and signal.outcome not in (None, "success"):
            return FailureKind.TOOL_FAILURE

        # 8) Fallback dari outcome map umum.
        if signal.outcome is not None:
            mapped = _OUTCOME_MAP.get(signal.outcome)
            if mapped is not None:
                return mapped

        return FailureKind.UNKNOWN
