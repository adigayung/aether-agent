"""Detector: mengenali pola kegagalan dari riwayat ProgressSnapshot.

Provider-agnostic. Detector TIDAK memutuskan tindakan; ia hanya menghasilkan
ReliabilityEvent. Keputusan (retry/recover/stop/fail) dibuat ReliabilityManager.

Prinsip penting:
    Repeated action TIDAK otomatis dianggap failure. Agent boleh membaca file
    yang sama atau memvalidasi ulang selama ada progress. Karena itu deteksi
    repetisi menggabungkan:
        - action identity (nama + argumen)
        - outcome
        - perubahan observation
        - progress state
"""

from __future__ import annotations

from typing import List, Optional

from agent_ai.reliability.models import (
    EventType,
    ProgressSnapshot,
    ReliabilityEvent,
)


class Detector:
    """Mendeteksi pola kegagalan dari riwayat snapshot.

    Args:
        repeat_threshold: jumlah pengulangan identik sebelum dianggap repetisi.
        no_progress_threshold: jumlah iterasi tanpa progres sebelum dianggap
            no-progress.
        iteration_limit: batas iterasi; mendekati batas memicu ITERATION_LIMIT.
        iteration_warn_margin: jarak dari batas untuk memicu peringatan.
    """

    def __init__(
        self,
        repeat_threshold: int = 3,
        no_progress_threshold: int = 3,
        iteration_limit: int = 10,
        iteration_warn_margin: int = 2,
    ) -> None:
        self.repeat_threshold = max(2, repeat_threshold)
        self.no_progress_threshold = max(1, no_progress_threshold)
        self.iteration_limit = iteration_limit
        self.iteration_warn_margin = max(0, iteration_warn_margin)

    # ------------------------------------------------------------------ #
    # Deteksi dari riwayat
    # ------------------------------------------------------------------ #
    def detect(self, history: List[ProgressSnapshot]) -> List[ReliabilityEvent]:
        """Deteksi semua event dari riwayat snapshot."""
        events: List[ReliabilityEvent] = []
        events.extend(self._detect_repeated_action(history))
        events.extend(self._detect_repeated_arguments(history))
        events.extend(self._detect_repeated_failed_action(history))
        events.extend(self._detect_no_progress(history))
        events.extend(self._detect_iteration_limit(history))
        return events

    def _detect_repeated_action(
        self, history: List[ProgressSnapshot]
    ) -> List[ReliabilityEvent]:
        """Action identik berulang TANPA perubahan observation.

        Repetisi hanya dianggap event bila observation tidak berubah (tidak ada
        informasi baru). Bila observation berubah, itu progress yang sah.
        """
        if len(history) < self.repeat_threshold:
            return []
        tail = history[-self.repeat_threshold:]
        signatures = [s.action_signature for s in tail]
        obs = [s.observation_signature for s in tail]
        if (
            signatures[0] is not None
            and all(sig == signatures[0] for sig in signatures)
            and all(o == obs[0] for o in obs)
        ):
            return [
                ReliabilityEvent(
                    type=EventType.REPEATED_ACTION,
                    message=f"Action identik '{signatures[0]}' berulang {len(tail)}x tanpa perubahan observation.",
                    severity=0.6,
                    metadata={"signature": signatures[0], "count": len(tail)},
                )
            ]
        return []

    def _detect_repeated_arguments(
        self, history: List[ProgressSnapshot]
    ) -> List[ReliabilityEvent]:
        """Argumen tool identik berulang (action_signature sama) dengan outcome sama."""
        if len(history) < self.repeat_threshold:
            return []
        tail = history[-self.repeat_threshold:]
        signatures = [s.action_signature for s in tail]
        outcomes = [s.outcome for s in tail]
        if (
            signatures[0] is not None
            and all(sig == signatures[0] for sig in signatures)
            and all(o == outcomes[0] for o in outcomes)
        ):
            return [
                ReliabilityEvent(
                    type=EventType.REPEATED_ARGUMENTS,
                    message=f"Argumen tool identik berulang {len(tail)}x dengan outcome '{outcomes[0]}'.",
                    severity=0.5,
                    metadata={"signature": signatures[0], "outcome": outcomes[0]},
                )
            ]
        return []

    def _detect_repeated_failed_action(
        self, history: List[ProgressSnapshot]
    ) -> List[ReliabilityEvent]:
        """Action yang gagal (outcome bukan success) berulang identik."""
        if len(history) < self.repeat_threshold:
            return []
        tail = history[-self.repeat_threshold:]
        signatures = [s.action_signature for s in tail]
        failed = [s.outcome is not None and s.outcome != "success" for s in tail]
        if (
            signatures[0] is not None
            and all(sig == signatures[0] for sig in signatures)
            and all(failed)
        ):
            return [
                ReliabilityEvent(
                    type=EventType.REPEATED_FAILED_ACTION,
                    message=f"Action gagal '{signatures[0]}' berulang {len(tail)}x.",
                    severity=0.8,
                    metadata={"signature": signatures[0], "count": len(tail)},
                )
            ]
        return []

    def _detect_no_progress(
        self, history: List[ProgressSnapshot]
    ) -> List[ReliabilityEvent]:
        """Iterasi berturut-turut tanpa progres."""
        if len(history) < self.no_progress_threshold:
            return []
        tail = history[-self.no_progress_threshold:]
        if all(not s.made_progress for s in tail):
            return [
                ReliabilityEvent(
                    type=EventType.NO_PROGRESS,
                    message=f"{len(tail)} iterasi berturut-turut tanpa progres.",
                    severity=0.7,
                    metadata={"count": len(tail)},
                )
            ]
        return []

    def _detect_iteration_limit(
        self, history: List[ProgressSnapshot]
    ) -> List[ReliabilityEvent]:
        """Mendekati atau mencapai batas iterasi."""
        if not history:
            return []
        current = history[-1].iteration
        if current >= self.iteration_limit:
            return [
                ReliabilityEvent(
                    type=EventType.ITERATION_LIMIT,
                    message=f"Iterasi {current} mencapai batas {self.iteration_limit}.",
                    severity=1.0,
                    metadata={"iteration": current, "limit": self.iteration_limit},
                )
            ]
        if current >= self.iteration_limit - self.iteration_warn_margin:
            return [
                ReliabilityEvent(
                    type=EventType.ITERATION_LIMIT,
                    message=f"Iterasi {current} mendekati batas {self.iteration_limit}.",
                    severity=0.5,
                    metadata={"iteration": current, "limit": self.iteration_limit},
                )
            ]
        return []

    # ------------------------------------------------------------------ #
    # Deteksi dari error tunggal
    # ------------------------------------------------------------------ #
    @staticmethod
    def detect_error(error: BaseException) -> ReliabilityEvent:
        """Klasifikasi error menjadi event provider_error/timeout/malformed."""
        name = type(error).__name__.lower()
        text = str(error).lower()
        if "timeout" in name or "timeout" in text or "timed out" in text:
            return ReliabilityEvent(
                type=EventType.TIMEOUT,
                message=f"Timeout: {type(error).__name__}",
                severity=0.7,
                metadata={"error": type(error).__name__},
            )
        if "malformed" in text or "invalid" in text or "json" in text:
            return ReliabilityEvent(
                type=EventType.MALFORMED_TOOL_RESPONSE,
                message=f"Tool response tidak valid: {type(error).__name__}",
                severity=0.6,
                metadata={"error": type(error).__name__},
            )
        return ReliabilityEvent(
            type=EventType.PROVIDER_ERROR,
            message=f"Provider error: {type(error).__name__}",
            severity=0.7,
            metadata={"error": type(error).__name__},
        )
