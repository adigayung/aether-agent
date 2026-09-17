"""Model untuk Advanced Recovery Subsystem (#43).

Provider-agnostic. Recovery adalah *orchestration/recovery policy*, BUKAN
executor baru. Model di sini hanya data terstruktur untuk mengklasifikasi
kegagalan dan memutuskan tindakan (retry/recover/replan/stop/fail).

    FailureKind       -> jenis kegagalan terstruktur (bukan string exception)
    FailureSignal     -> input terstruktur untuk klasifikasi
    RecoveryAction    -> tindakan recovery
    RecoveryDecision  -> keputusan lengkap + alasan
    RecoveryConfig    -> policy bounded (dari config, tidak di-hardcode)

Tidak menyimpan chain-of-thought.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class FailureKind(str, Enum):
    """Jenis kegagalan yang dikenali recovery (terstruktur)."""

    TOOL_FAILURE = "tool_failure"                # tool gagal (bukan command)
    COMMAND_FAILURE = "command_failure"          # command/terminal gagal
    VALIDATION_FAILURE = "validation_failure"    # validation tidak lulus
    TIMEOUT = "timeout"                          # operasi melewati batas waktu
    PROVIDER_ERROR = "provider_error"            # error dari provider
    REPEATED_ACTION = "repeated_action"          # action identik berulang
    NO_PROGRESS = "no_progress"                  # iterasi tanpa progres
    WORKSPACE_CHANGED = "workspace_changed"      # workspace berubah tak terduga
    UNKNOWN = "unknown"                          # tidak terklasifikasi


class RecoveryAction(str, Enum):
    """Tindakan recovery yang diputuskan."""

    RETRY = "retry"      # ulangi operasi (mungkin dengan backoff)
    RECOVER = "recover"  # lanjutkan dengan strategi pemulihan (ubah pendekatan)
    REPLAN = "replan"    # minta replanner merevisi plan
    STOP = "stop"        # hentikan dengan aman (tanpa menandai gagal permanen)
    FAIL = "fail"        # tandai gagal


@dataclass
class FailureSignal:
    """Input terstruktur untuk klasifikasi kegagalan.

    Recovery TIDAK mengklasifikasi berdasarkan string exception. Semua field
    di sini berasal dari subsystem yang sudah ada (Reliability, Validation,
    ChangeTracker, Git, Runtime).

    Attributes:
        source: asal sinyal (mis. "runtime", "reliability", "validation").
        outcome: outcome terstruktur (mis. "command_failure", "timeout").
        error_type: nama tipe error (bukan pesan mentah).
        recoverable: apakah kegagalan dapat dipulihkan (dari subsystem).
        is_command: apakah kegagalan berasal dari command/terminal.
        is_tool: apakah kegagalan berasal dari tool.
        validation_outcome: outcome validation (bila relevan).
        reliability_events: jenis event reliability (bila relevan).
        workspace_changed: apakah workspace berubah tak terduga.
        changed_paths: path yang berubah (dari ChangeTracker).
        attempts: jumlah percobaan yang sudah dilakukan.
        metadata: info tambahan bebas.
    """

    source: str = "runtime"
    outcome: Optional[str] = None
    error_type: Optional[str] = None
    recoverable: bool = False
    is_command: bool = False
    is_tool: bool = False
    validation_outcome: Optional[str] = None
    reliability_events: List[str] = field(default_factory=list)
    workspace_changed: bool = False
    changed_paths: List[str] = field(default_factory=list)
    attempts: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "outcome": self.outcome,
            "error_type": self.error_type,
            "recoverable": self.recoverable,
            "is_command": self.is_command,
            "is_tool": self.is_tool,
            "validation_outcome": self.validation_outcome,
            "reliability_events": self.reliability_events,
            "workspace_changed": self.workspace_changed,
            "changed_paths": self.changed_paths,
            "attempts": self.attempts,
            "metadata": self.metadata,
        }


@dataclass
class RecoveryDecision:
    """Keputusan recovery (struktural, bukan chain-of-thought).

    Attributes:
        action: retry/recover/replan/stop/fail.
        kind: jenis kegagalan yang mendasari keputusan.
        reason: alasan singkat.
        delay: jeda sebelum retry (detik), bila relevan.
        bounded: apakah keputusan dibatasi oleh policy (mis. kuota habis).
        metadata: info tambahan bebas.
    """

    action: RecoveryAction
    kind: FailureKind = FailureKind.UNKNOWN
    reason: str = ""
    delay: float = 0.0
    bounded: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "kind": self.kind.value,
            "reason": self.reason,
            "delay": self.delay,
            "bounded": self.bounded,
            "metadata": self.metadata,
        }


@dataclass
class RecoveryConfig:
    """Policy recovery (bounded). Dibaca dari config, tidak di-hardcode.

    Attributes:
        enabled: apakah recovery aktif.
        max_attempts: batas total percobaan recovery.
        max_replans: batas replan yang dipicu recovery.
        max_total_cycles: batas total siklus recovery (anti infinite loop).
        retry_delay: delay dasar sebelum retry (detik).
    """

    enabled: bool = True
    max_attempts: int = 3
    max_replans: int = 2
    max_total_cycles: int = 5
    retry_delay: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_attempts": self.max_attempts,
            "max_replans": self.max_replans,
            "max_total_cycles": self.max_total_cycles,
            "retry_delay": self.retry_delay,
        }
