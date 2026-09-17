"""FallbackPolicy: klasifikasi kegagalan provider + keputusan fallback.

Provider-agnostic, deterministik, bounded. Policy TIDAK mengeksekusi apa pun;
ia hanya memutuskan tindakan (FALLBACK / RETRY_CURRENT / STOP).

Prinsip:
    - HANYA menangani kegagalan provider (unavailable/connection/timeout/API/
      malformed). Kegagalan tool/command/validation/workspace/repeated/no-progress
      TIDAK memicu fallback (dikembalikan sebagai STOP dengan reason
      NOT_PROVIDER_FAILURE).
    - Retry current TIDAK menduplikasi ReliabilityManager: keputusan retry
      bersumber dari `retry_allowed` (hasil Reliability) yang diberikan.
    - Bounded oleh FallbackConfig.
"""

from __future__ import annotations

from typing import Optional

from agent_ai.fallback.models import (
    FallbackAction,
    FallbackConfig,
    FallbackDecision,
    FallbackReason,
    FallbackRequest,
)

# Alasan yang dianggap kegagalan provider (memicu fallback).
_PROVIDER_REASONS = frozenset({
    FallbackReason.PROVIDER_UNAVAILABLE,
    FallbackReason.CONNECTION_FAILURE,
    FallbackReason.TIMEOUT,
    FallbackReason.PROVIDER_API_ERROR,
    FallbackReason.MALFORMED_RESPONSE,
})

# Alasan transient (boleh retry current sebelum fallback).
_TRANSIENT_REASONS = frozenset({
    FallbackReason.CONNECTION_FAILURE,
    FallbackReason.TIMEOUT,
    FallbackReason.PROVIDER_API_ERROR,
})


class FallbackPolicy:
    """Memutuskan tindakan fallback berdasarkan kegagalan provider.

    Args:
        config: FallbackConfig (bounded). Default: FallbackConfig().
    """

    def __init__(self, config: Optional[FallbackConfig] = None) -> None:
        self.config = config or FallbackConfig()

    def is_provider_failure(self, reason: FallbackReason) -> bool:
        """True bila reason merupakan kegagalan provider (bukan subsystem lain)."""
        return reason in _PROVIDER_REASONS

    def is_transient(self, reason: FallbackReason) -> bool:
        """True bila reason bersifat transient (boleh retry current)."""
        return reason in _TRANSIENT_REASONS

    def decide(
        self,
        request: FallbackRequest,
        *,
        has_candidate: bool = False,
    ) -> FallbackDecision:
        """Putuskan tindakan fallback (bounded).

        Args:
            request: FallbackRequest terstruktur.
            has_candidate: apakah ada kandidat alternatif yang eligible.

        Returns:
            FallbackDecision (bounded).
        """
        # Bukan kegagalan provider -> STOP (jangan ambil alih subsystem lain).
        if not self.is_provider_failure(request.reason):
            return FallbackDecision(
                action=FallbackAction.STOP,
                reason=request.reason,
                rationale="Bukan kegagalan provider; fallback tidak berlaku.",
            )

        # Fallback tidak aktif -> STOP.
        if not self.config.enabled:
            return FallbackDecision(
                action=FallbackAction.STOP,
                reason=request.reason,
                rationale="Fallback tidak aktif.",
                bounded=True,
            )

        # Bounded: kuota fallback habis -> STOP.
        if request.attempts >= self.config.max_attempts:
            return FallbackDecision(
                action=FallbackAction.STOP,
                reason=request.reason,
                rationale=f"Batas percobaan fallback ({self.config.max_attempts}) tercapai.",
                bounded=True,
            )

        # Transient + retry current diizinkan (dari Reliability) -> RETRY_CURRENT.
        # Retry TIDAK diduplikasi: keputusan retry bersumber dari retry_allowed.
        if (
            self.config.allow_retry_current
            and request.transient
            and request.retry_allowed
        ):
            return FallbackDecision(
                action=FallbackAction.RETRY_CURRENT,
                reason=request.reason,
                rationale=f"Kegagalan transient '{request.reason.value}'; retry provider aktif.",
            )

        # Ada kandidat alternatif -> FALLBACK.
        if has_candidate:
            return FallbackDecision(
                action=FallbackAction.FALLBACK,
                reason=request.reason,
                rationale=f"Kegagalan provider '{request.reason.value}'; pindah ke kandidat alternatif.",
            )

        # Tidak ada kandidat -> STOP (TIDAK fallback ke provider lain).
        return FallbackDecision(
            action=FallbackAction.STOP,
            reason=request.reason,
            rationale="Tidak ada kandidat alternatif yang memenuhi requirement.",
            bounded=True,
        )
