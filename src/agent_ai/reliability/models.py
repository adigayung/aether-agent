"""Model untuk Reliability Subsystem.

Provider-agnostic. Tidak menyimpan chain-of-thought; hanya data terstruktur
untuk mendeteksi pola kegagalan dan memutuskan tindakan.

    ReliabilityEvent    -> apa yang terjadi
    ReliabilityDecision -> apa yang harus dilakukan (retry/recover/stop/fail)
    RetryPolicy         -> kebijakan retry (max, backoff, retryable)
    ProgressSnapshot    -> ringkasan progres antar iterasi
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class EventType(str, Enum):
    """Jenis event yang dikenali detector."""

    REPEATED_ACTION = "repeated_action"            # action identik berulang
    REPEATED_ARGUMENTS = "repeated_arguments"      # argumen tool identik berulang
    REPEATED_FAILED_ACTION = "repeated_failed_action"  # action gagal berulang
    NO_PROGRESS = "no_progress"                    # iterasi tanpa progres
    ITERATION_LIMIT = "iteration_limit"            # mendekati/mencapai batas iterasi
    PROVIDER_ERROR = "provider_error"              # error dari provider
    TIMEOUT = "timeout"                            # operasi melewati batas waktu
    MALFORMED_TOOL_RESPONSE = "malformed_tool_response"  # tool call tidak valid


class DecisionAction(str, Enum):
    """Tindakan yang diputuskan ReliabilityManager."""

    RETRY = "retry"      # ulangi operasi (mungkin dengan backoff)
    RECOVER = "recover"  # lanjutkan dengan strategi pemulihan (mis. reset progres)
    STOP = "stop"        # hentikan loop dengan aman
    FAIL = "fail"        # tandai gagal


@dataclass
class ReliabilityEvent:
    """Satu event yang terdeteksi.

    Attributes:
        type: jenis event.
        message: deskripsi singkat.
        severity: tingkat keparahan (0..1, opsional).
        metadata: info tambahan bebas.
    """

    type: EventType
    message: str = ""
    severity: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type.value,
            "message": self.message,
            "severity": self.severity,
            "metadata": self.metadata,
        }


@dataclass
class ReliabilityDecision:
    """Keputusan yang dihasilkan ReliabilityManager.

    Attributes:
        action: retry/recover/stop/fail.
        reason: alasan keputusan.
        retryable: apakah operasi boleh diulang.
        delay: jeda sebelum retry (detik), bila relevan.
        events: event yang memicu keputusan.
        metadata: info tambahan bebas.
    """

    action: DecisionAction
    reason: str = ""
    retryable: bool = False
    delay: float = 0.0
    events: List[ReliabilityEvent] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "retryable": self.retryable,
            "delay": self.delay,
            "events": [e.to_dict() for e in self.events],
            "metadata": self.metadata,
        }


@dataclass
class RetryPolicy:
    """Kebijakan retry.

    Attributes:
        max_retries: jumlah maksimum percobaan ulang.
        base_delay: delay dasar (detik) untuk backoff.
        backoff_factor: faktor pengali eksponensial.
        max_delay: batas atas delay (detik).
        retryable_outcomes: outcome yang boleh di-retry.
        non_retryable_outcomes: outcome yang jelas tidak boleh di-retry.
    """

    max_retries: int = 3
    base_delay: float = 0.5
    backoff_factor: float = 2.0
    max_delay: float = 30.0
    retryable_outcomes: List[str] = field(
        default_factory=lambda: ["provider_error", "timeout", "malformed_tool_response"]
    )
    non_retryable_outcomes: List[str] = field(
        default_factory=lambda: ["command_failure", "execution_error"]
    )

    def is_retryable(self, outcome: str) -> bool:
        """True bila outcome boleh di-retry.

        Non-retryable menang atas retryable (lebih eksplisit).
        """
        if outcome in self.non_retryable_outcomes:
            return False
        return outcome in self.retryable_outcomes

    def delay_for(self, attempt: int) -> float:
        """Hitung delay backoff eksponensial untuk attempt ke-`attempt` (0-based).

        delay = min(base_delay * backoff_factor**attempt, max_delay)
        """
        if attempt < 0:
            attempt = 0
        delay = self.base_delay * (self.backoff_factor ** attempt)
        return min(delay, self.max_delay)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_retries": self.max_retries,
            "base_delay": self.base_delay,
            "backoff_factor": self.backoff_factor,
            "max_delay": self.max_delay,
            "retryable_outcomes": self.retryable_outcomes,
            "non_retryable_outcomes": self.non_retryable_outcomes,
        }


@dataclass
class ProgressSnapshot:
    """Ringkasan progres pada satu titik waktu.

    Attributes:
        iteration: nomor iterasi.
        action_signature: identitas action (nama + argumen) untuk deteksi repetisi.
        outcome: outcome action (mis. success/command_failure/timeout).
        observation_signature: ringkasan observation (untuk deteksi perubahan).
        made_progress: apakah iterasi ini dianggap menghasilkan progres.
    """

    iteration: int = 0
    action_signature: Optional[str] = None
    outcome: Optional[str] = None
    observation_signature: Optional[str] = None
    made_progress: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "iteration": self.iteration,
            "action_signature": self.action_signature,
            "outcome": self.outcome,
            "observation_signature": self.observation_signature,
            "made_progress": self.made_progress,
        }
