"""ReliabilityManager: komponen observasi & keputusan untuk Runtime/Orchestrator.

Provider-agnostic. Dapat dipakai tanpa mengubah Provider interface.

Alur:
    observe(...)          -> catat event/error
    record_progress(...)  -> catat snapshot progres
    should_retry(...)     -> boleh retry?
    should_recover(...)   -> perlu pemulihan?
    should_stop(...)      -> harus berhenti?
    decide(...)           -> keputusan lengkap (retry/recover/stop/fail)

Extension point (belum diimplementasikan, sengaja disiapkan):
    - fallback provider/model routing (tahap berikutnya).
"""

from __future__ import annotations

from typing import List, Optional

from agent_ai.reliability.detector import Detector
from agent_ai.reliability.models import (
    DecisionAction,
    EventType,
    ProgressSnapshot,
    ReliabilityDecision,
    ReliabilityEvent,
    RetryPolicy,
)
from agent_ai.reliability.retry import RetryController


class ReliabilityManager:
    """Mengelola observasi kegagalan dan keputusan pemulihan.

    Args:
        detector: Detector opsional.
        retry_policy: RetryPolicy opsional.
    """

    def __init__(
        self,
        detector: Optional[Detector] = None,
        retry_policy: Optional[RetryPolicy] = None,
    ) -> None:
        self.detector = detector or Detector()
        self.retry = RetryController(retry_policy)
        self._history: List[ProgressSnapshot] = []
        self._events: List[ReliabilityEvent] = []

    # ------------------------------------------------------------------ #
    # Observasi
    # ------------------------------------------------------------------ #
    def observe(self, event: ReliabilityEvent) -> None:
        """Catat satu event."""
        self._events.append(event)

    def observe_error(self, error: BaseException) -> ReliabilityEvent:
        """Klasifikasi error menjadi event dan catat."""
        event = self.detector.detect_error(error)
        self._events.append(event)
        return event

    def record_progress(self, snapshot: ProgressSnapshot) -> List[ReliabilityEvent]:
        """Catat snapshot progres dan deteksi event dari riwayat.

        Returns:
            Event yang terdeteksi dari riwayat (juga dicatat internal).
        """
        self._history.append(snapshot)
        detected = self.detector.detect(self._history)
        self._events.extend(detected)
        return detected

    # ------------------------------------------------------------------ #
    # Query
    # ------------------------------------------------------------------ #
    @property
    def history(self) -> List[ProgressSnapshot]:
        return list(self._history)

    @property
    def events(self) -> List[ReliabilityEvent]:
        return list(self._events)

    def latest_events(self) -> List[ReliabilityEvent]:
        """Event yang terdeteksi pada snapshot terakhir."""
        if not self._history:
            return []
        return self.detector.detect(self._history)

    def _has(self, event_type: EventType) -> bool:
        return any(e.type == event_type for e in self._events)

    # ------------------------------------------------------------------ #
    # Keputusan
    # ------------------------------------------------------------------ #
    def should_retry(self, outcome: str) -> bool:
        """True bila outcome boleh di-retry (kuota + retryable)."""
        return self.retry.can_retry(outcome)

    def should_recover(self) -> bool:
        """True bila perlu strategi pemulihan (repetisi/no-progress)."""
        return self._has(EventType.REPEATED_ACTION) or self._has(EventType.NO_PROGRESS)

    def should_stop(self) -> bool:
        """True bila loop harus berhenti dengan aman.

        Iteration limit TIDAK lagi menjadi alasan berhenti: autonomous task
        berjalan selama diperlukan dan berhenti murni dari keputusan LLM
        (final), user cancel, atau fatal error nyata.
        """
        return False

    def decide(self, outcome: Optional[str] = None) -> ReliabilityDecision:
        """Buat keputusan lengkap berdasarkan event + outcome.

        Prioritas:
            1. outcome retryable      -> RETRY (dengan backoff)
            2. repeated failed action -> RECOVER (sinyal ke LLM, bukan FAIL)
            3. repeated/no-progress   -> RECOVER
            4. selain itu             -> RECOVER (bila ada event) / STOP

        CATATAN: iteration limit BUKAN lagi alasan menghentikan autonomous
        task. Berhenti murni dari keputusan LLM (final), user cancel, atau
        fatal error nyata.
        """
        events = self.latest_events() or self._events

        # 1) Outcome retryable -> retry.
        if outcome is not None and self.should_retry(outcome):
            delay = self.retry.next_delay(outcome)
            return ReliabilityDecision(
                action=DecisionAction.RETRY,
                reason=f"Outcome '{outcome}' retryable.",
                retryable=True,
                delay=delay or 0.0,
                events=events,
                metadata={"attempt": self.retry.attempts},
            )

        # 2) Repeated failed action -> RECOVER: kirim sinyal ke LLM agar
        #    mengubah pendekatan. JANGAN mematikan seluruh task hanya karena
        #    action identik gagal berulang.
        if self._has(EventType.REPEATED_FAILED_ACTION):
            return ReliabilityDecision(
                action=DecisionAction.RECOVER,
                reason="Action gagal berulang identik; minta LLM ubah pendekatan.",
                retryable=False,
                events=events,
            )

        # 3) Repetisi / no-progress -> recover.
        if self.should_recover():
            return ReliabilityDecision(
                action=DecisionAction.RECOVER,
                reason="Terdeteksi repetisi/no-progress; perlu pemulihan.",
                retryable=False,
                events=events,
            )

        # 4) Default: bila ada event -> recover, jika tidak -> stop.
        if events:
            return ReliabilityDecision(
                action=DecisionAction.RECOVER,
                reason="Ada event yang perlu ditangani.",
                retryable=False,
                events=events,
            )
        return ReliabilityDecision(
            action=DecisionAction.STOP,
            reason="Tidak ada event; tidak ada tindakan.",
            retryable=False,
            events=[],
        )

    def reset(self) -> None:
        """Reset seluruh state (history, events, retry)."""
        self._history.clear()
        self._events.clear()
        self.retry.reset()
