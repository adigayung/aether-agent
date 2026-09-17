"""RecoveryPolicy: memetakan FailureKind + konteks -> RecoveryAction.

Provider-agnostic, deterministik, bounded. Policy TIDAK mengeksekusi apa pun;
ia hanya memutuskan tindakan. Keputusan retry/repeated-action/no-progress
tetap bersumber dari ReliabilityManager (tidak diduplikasi di sini).

Aturan (default):
    TOOL_FAILURE       -> RETRY (bila recoverable & kuota ada) / RECOVER
    COMMAND_FAILURE    -> RECOVER (ubah pendekatan) / REPLAN bila berulang
    VALIDATION_FAILURE -> REPLAN (perbaiki pekerjaan) / FAIL bila kuota habis
    TIMEOUT            -> RETRY (bila kuota ada) / RECOVER
    PROVIDER_ERROR     -> RETRY (bila kuota ada) / FAIL
    REPEATED_ACTION    -> RECOVER (ubah pendekatan) / REPLAN
    NO_PROGRESS        -> RECOVER / REPLAN
    WORKSPACE_CHANGED  -> REPLAN (sesuaikan plan) / STOP
    UNKNOWN            -> STOP (aman) / FAIL bila kuota habis
"""

from __future__ import annotations

from typing import Optional

from agent_ai.recovery.models import (
    FailureKind,
    FailureSignal,
    RecoveryAction,
    RecoveryConfig,
    RecoveryDecision,
)


class RecoveryPolicy:
    """Memutuskan tindakan recovery berdasarkan jenis kegagalan + konteks.

    Args:
        config: RecoveryConfig (bounded policy). Default: RecoveryConfig().
    """

    def __init__(self, config: Optional[RecoveryConfig] = None) -> None:
        self.config = config or RecoveryConfig()

    def decide(
        self,
        kind: FailureKind,
        signal: FailureSignal,
        *,
        attempts: int = 0,
        replans: int = 0,
        total_cycles: int = 0,
        retry_allowed: bool = True,
        replan_allowed: bool = True,
    ) -> RecoveryDecision:
        """Putuskan tindakan recovery.

        Args:
            kind: jenis kegagalan (dari classifier).
            signal: sinyal kegagalan terstruktur.
            attempts: jumlah percobaan recovery yang sudah dilakukan.
            replans: jumlah replan yang sudah dipicu recovery.
            total_cycles: total siklus recovery yang sudah berjalan.
            retry_allowed: apakah retry masih diizinkan (dari reliability).
            replan_allowed: apakah replan masih diizinkan (replanner tersedia).

        Returns:
            RecoveryDecision (bounded).
        """
        # Bounded: bila total cycle habis -> FAIL (anti infinite loop).
        if total_cycles >= self.config.max_total_cycles:
            return RecoveryDecision(
                action=RecoveryAction.FAIL,
                kind=kind,
                reason=f"Batas total siklus recovery ({self.config.max_total_cycles}) tercapai.",
                bounded=True,
            )

        # Bounded: bila attempts habis -> FAIL.
        if attempts >= self.config.max_attempts:
            return RecoveryDecision(
                action=RecoveryAction.FAIL,
                kind=kind,
                reason=f"Batas percobaan recovery ({self.config.max_attempts}) tercapai.",
                bounded=True,
            )

        if kind == FailureKind.WORKSPACE_CHANGED:
            return self._workspace_changed(signal, replans, replan_allowed)

        if kind == FailureKind.VALIDATION_FAILURE:
            return self._validation_failure(signal, replans, replan_allowed)

        if kind in (FailureKind.REPEATED_ACTION, FailureKind.NO_PROGRESS):
            return self._repeated_or_no_progress(kind, replans, replan_allowed)

        if kind == FailureKind.TIMEOUT:
            return self._retry_or_recover(kind, signal, retry_allowed)

        if kind == FailureKind.PROVIDER_ERROR:
            return self._provider_error(signal, retry_allowed)

        if kind == FailureKind.COMMAND_FAILURE:
            return self._command_failure(signal, replans, replan_allowed)

        if kind == FailureKind.TOOL_FAILURE:
            return self._tool_failure(signal, retry_allowed)

        # UNKNOWN -> STOP (aman) agar tidak loop tanpa arah.
        return RecoveryDecision(
            action=RecoveryAction.STOP,
            kind=kind,
            reason="Kegagalan tidak terklasifikasi; berhenti dengan aman.",
        )

    # ------------------------------------------------------------------ #
    # Per-kind rules
    # ------------------------------------------------------------------ #
    def _retry_or_recover(
        self,
        kind: FailureKind,
        signal: FailureSignal,
        retry_allowed: bool,
    ) -> RecoveryDecision:
        if retry_allowed:
            return RecoveryDecision(
                action=RecoveryAction.RETRY,
                kind=kind,
                reason=f"Kegagalan '{kind.value}' dapat diulang.",
                delay=self.config.retry_delay,
            )
        return RecoveryDecision(
            action=RecoveryAction.RECOVER,
            kind=kind,
            reason=f"Retry tidak tersedia untuk '{kind.value}'; ubah pendekatan.",
        )

    def _provider_error(self, signal: FailureSignal, retry_allowed: bool) -> RecoveryDecision:
        if retry_allowed:
            return RecoveryDecision(
                action=RecoveryAction.RETRY,
                kind=FailureKind.PROVIDER_ERROR,
                reason="Provider error; coba ulang.",
                delay=self.config.retry_delay,
            )
        return RecoveryDecision(
            action=RecoveryAction.FAIL,
            kind=FailureKind.PROVIDER_ERROR,
            reason="Provider error dan retry habis.",
            bounded=True,
        )

    def _tool_failure(self, signal: FailureSignal, retry_allowed: bool) -> RecoveryDecision:
        if signal.recoverable and retry_allowed:
            return RecoveryDecision(
                action=RecoveryAction.RETRY,
                kind=FailureKind.TOOL_FAILURE,
                reason="Tool failure recoverable; coba ulang.",
                delay=self.config.retry_delay,
            )
        return RecoveryDecision(
            action=RecoveryAction.RECOVER,
            kind=FailureKind.TOOL_FAILURE,
            reason="Tool failure; ubah pendekatan.",
        )

    def _command_failure(
        self,
        signal: FailureSignal,
        replans: int,
        replan_allowed: bool,
    ) -> RecoveryDecision:
        # Command gagal berulang -> replan (ubah strategi), bukan retry buta.
        if signal.attempts >= 1 and replan_allowed and replans < self.config.max_replans:
            return RecoveryDecision(
                action=RecoveryAction.REPLAN,
                kind=FailureKind.COMMAND_FAILURE,
                reason="Command gagal berulang; revisi plan.",
            )
        return RecoveryDecision(
            action=RecoveryAction.RECOVER,
            kind=FailureKind.COMMAND_FAILURE,
            reason="Command failure; ubah pendekatan.",
        )

    def _validation_failure(
        self,
        signal: FailureSignal,
        replans: int,
        replan_allowed: bool,
    ) -> RecoveryDecision:
        if replan_allowed and replans < self.config.max_replans:
            return RecoveryDecision(
                action=RecoveryAction.REPLAN,
                kind=FailureKind.VALIDATION_FAILURE,
                reason="Validation gagal; revisi plan untuk perbaikan.",
            )
        return RecoveryDecision(
            action=RecoveryAction.FAIL,
            kind=FailureKind.VALIDATION_FAILURE,
            reason="Validation gagal dan replan habis.",
            bounded=True,
        )

    def _repeated_or_no_progress(
        self,
        kind: FailureKind,
        replans: int,
        replan_allowed: bool,
    ) -> RecoveryDecision:
        if replan_allowed and replans < self.config.max_replans:
            return RecoveryDecision(
                action=RecoveryAction.REPLAN,
                kind=kind,
                reason=f"Terdeteksi '{kind.value}'; revisi plan.",
            )
        return RecoveryDecision(
            action=RecoveryAction.RECOVER,
            kind=kind,
            reason=f"Terdeteksi '{kind.value}'; ubah pendekatan.",
        )

    def _workspace_changed(
        self,
        signal: FailureSignal,
        replans: int,
        replan_allowed: bool,
    ) -> RecoveryDecision:
        if replan_allowed and replans < self.config.max_replans:
            return RecoveryDecision(
                action=RecoveryAction.REPLAN,
                kind=FailureKind.WORKSPACE_CHANGED,
                reason="Workspace berubah tak terduga; sesuaikan plan.",
                metadata={"changed_paths": signal.changed_paths},
            )
        return RecoveryDecision(
            action=RecoveryAction.STOP,
            kind=FailureKind.WORKSPACE_CHANGED,
            reason="Workspace berubah dan replan tidak tersedia; berhenti dengan aman.",
            metadata={"changed_paths": signal.changed_paths},
        )
