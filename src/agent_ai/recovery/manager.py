"""RecoveryManager: orkestrasi recovery policy (BUKAN executor).

Provider-agnostic. RecoveryManager mengorkestrasi tindakan berdasarkan hasil
subsystem yang sudah ada:
    - ReliabilityManager  -> keputusan retry/repeated-action/no-progress
    - ValidationResult    -> hasil validation
    - Replanner           -> revisi plan
    - ChangeTracker       -> apakah workspace berubah
    - Git (read-only)     -> kondisi repository (branch/status/diff)
    - SessionStore        -> event recovery_started/recovery_completed

RecoveryManager TIDAK:
    - mengeksekusi tool/command (itu Runtime/ToolExecutor),
    - membuat planner/validator/executor kedua,
    - menduplikasi retry/repeated-action/no-progress detection (milik Reliability),
    - melakukan operasi Git destruktif.

Recovery loop bounded oleh RecoveryConfig (max_attempts/max_replans/max_total_cycles).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from agent_ai.recovery.classifier import FailureClassifier
from agent_ai.recovery.models import (
    FailureKind,
    FailureSignal,
    RecoveryAction,
    RecoveryConfig,
    RecoveryDecision,
)
from agent_ai.recovery.policy import RecoveryPolicy

if TYPE_CHECKING:  # pragma: no cover - hindari import cycle
    from agent_ai.changes.tracker import ChangeTracker
    from agent_ai.git.repository import GitRepositoryFacade
    from agent_ai.planning.replanner import Replanner
    from agent_ai.reliability.manager import ReliabilityManager
    from agent_ai.session.store import SessionStore


class RecoveryManager:
    """Mengorkestrasi recovery berdasarkan subsystem yang sudah ada.

    Args:
        config: RecoveryConfig (bounded). Default dari settings.recovery.
        classifier: FailureClassifier opsional.
        policy: RecoveryPolicy opsional.
        reliability: ReliabilityManager opsional (sumber keputusan reliability).
        replanner: Replanner opsional (revisi plan).
        change_tracker: ChangeTracker opsional (deteksi perubahan workspace).
        git: GitRepositoryFacade opsional (read-only).
        session_store: SessionStore opsional (event recovery).
        session_id: session id untuk event.
    """

    def __init__(
        self,
        config: Optional[RecoveryConfig] = None,
        classifier: Optional[FailureClassifier] = None,
        policy: Optional[RecoveryPolicy] = None,
        reliability: Optional["ReliabilityManager"] = None,
        replanner: Optional["Replanner"] = None,
        change_tracker: Optional["ChangeTracker"] = None,
        git: Optional["GitRepositoryFacade"] = None,
        session_store: Optional["SessionStore"] = None,
        session_id: Optional[str] = None,
    ) -> None:
        self.config = config or self._config_from_settings()
        self.classifier = classifier or FailureClassifier()
        self.policy = policy or RecoveryPolicy(self.config)
        self.reliability = reliability
        self.replanner = replanner
        self.change_tracker = change_tracker
        self.git = git
        self.session_store = session_store
        self.session_id = session_id

        # State bounded (per instance).
        self._attempts = 0
        self._replans = 0
        self._total_cycles = 0
        self._history: List[Dict[str, Any]] = []

    @staticmethod
    def _config_from_settings() -> RecoveryConfig:
        """Ambil RecoveryConfig dari settings (fallback aman)."""
        try:
            from agent_ai.config.settings import settings

            return settings.recovery
        except Exception:  # noqa: BLE001 - config error tidak boleh crash
            return RecoveryConfig()

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def replans(self) -> int:
        return self._replans

    @property
    def total_cycles(self) -> int:
        return self._total_cycles

    @property
    def history(self) -> List[Dict[str, Any]]:
        return list(self._history)

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def reset(self) -> None:
        """Reset state recovery (untuk task baru)."""
        self._attempts = 0
        self._replans = 0
        self._total_cycles = 0
        self._history.clear()

    # ------------------------------------------------------------------ #
    # Workspace / Git awareness (read-only)
    # ------------------------------------------------------------------ #
    def detect_workspace_change(self, task_id: Optional[str]) -> List[str]:
        """Deteksi perubahan workspace via ChangeTracker (read-only).

        Returns:
            Daftar path yang berubah (kosong bila tidak ada/tidak tersedia).
        """
        if self.change_tracker is None or not task_id:
            return []
        try:
            records = self.change_tracker.detect_changes(task_id)
        except Exception:  # noqa: BLE001 - deteksi tidak boleh crash
            return []
        return [r.path for r in records]

    def git_snapshot(self) -> Dict[str, Any]:
        """Ambil kondisi Git read-only (branch/status/diff). Tidak ada mutasi."""
        if self.git is None:
            return {}
        try:
            if not self.git.is_repository():
                return {"is_repository": False}
            status = self.git.status()
            diff = self.git.diff()
            return {
                "is_repository": True,
                "branch": status.branch,
                "clean": status.clean,
                "changed_files": [f.path for f in status.files],
                "diff_files": [d.path for d in diff],
            }
        except Exception:  # noqa: BLE001 - git read-only tidak boleh crash
            return {"is_repository": False, "error": "git_unavailable"}

    # ------------------------------------------------------------------ #
    # Decision
    # ------------------------------------------------------------------ #
    def decide(self, signal: FailureSignal) -> RecoveryDecision:
        """Klasifikasi + putuskan tindakan recovery (bounded).

        Menggabungkan sinyal dengan konteks subsystem:
            - reliability: apakah retry diizinkan (tidak menduplikasi policy).
            - replanner: apakah replan tersedia.
            - change_tracker: apakah workspace berubah.
        """
        if not self.enabled:
            return RecoveryDecision(
                action=RecoveryAction.FAIL,
                kind=FailureKind.UNKNOWN,
                reason="Recovery tidak aktif.",
                bounded=True,
            )

        # Lengkapi sinyal dengan konteks subsystem (tanpa menduplikasi logic).
        signal = self._enrich_signal(signal)

        kind = self.classifier.classify(signal)

        retry_allowed = self._retry_allowed(signal)
        replan_allowed = self.replanner is not None

        decision = self.policy.decide(
            kind,
            signal,
            attempts=self._attempts,
            replans=self._replans,
            total_cycles=self._total_cycles,
            retry_allowed=retry_allowed,
            replan_allowed=replan_allowed,
        )
        self._history.append(
            {
                "signal": signal.to_dict(),
                "decision": decision.to_dict(),
                "attempts": self._attempts,
                "replans": self._replans,
                "total_cycles": self._total_cycles,
            }
        )
        return decision

    def _enrich_signal(self, signal: FailureSignal) -> FailureSignal:
        """Lengkapi sinyal dengan konteks reliability/change tracker."""
        # Reliability: ambil event terbaru (tidak menduplikasi deteksi).
        if self.reliability is not None and not signal.reliability_events:
            try:
                events = self.reliability.latest_events() or self.reliability.events
                signal.reliability_events = [e.type.value for e in events]
            except Exception:  # noqa: BLE001
                pass
        # ChangeTracker: deteksi perubahan workspace (read-only).
        if not signal.workspace_changed and self.change_tracker is not None:
            task_id = signal.metadata.get("task_id")
            changed = self.detect_workspace_change(task_id)
            if changed:
                signal.changed_paths = changed
                # Workspace berubah dianggap tak terduga hanya bila sinyal
                # menandainya (mis. expected_state_ok=False dari replanner).
                if signal.metadata.get("expected_state_ok") is False:
                    signal.workspace_changed = True
        return signal

    def _retry_allowed(self, signal: FailureSignal) -> bool:
        """Apakah retry diizinkan (bersumber dari ReliabilityManager).

        Bila reliability tersedia, pakai policy-nya (tidak menduplikasi).
        Bila tidak, izinkan retry selama kuota recovery belum habis.
        """
        if self.reliability is not None and signal.outcome is not None:
            try:
                return self.reliability.should_retry(signal.outcome)
            except Exception:  # noqa: BLE001
                pass
        return self._attempts < self.config.max_attempts

    # ------------------------------------------------------------------ #
    # Orchestration (bounded)
    # ------------------------------------------------------------------ #
    def recover(
        self,
        signal: FailureSignal,
        plan: Any = None,
        observation: Any = None,
    ) -> RecoveryDecision:
        """Orkestrasi satu langkah recovery (bounded).

        RecoveryManager TIDAK mengeksekusi; ia:
            - memutuskan tindakan,
            - memicu replanner (bila REPLAN) untuk merevisi plan,
            - mencatat state & event.

        Args:
            signal: sinyal kegagalan terstruktur.
            plan: TaskPlan (untuk replan).
            observation: Observation replanner (untuk replan).

        Returns:
            RecoveryDecision final (bounded).
        """
        self._total_cycles += 1
        self._emit("recovery_started", {"cycle": self._total_cycles})

        decision = self.decide(signal)

        if decision.action == RecoveryAction.RETRY:
            self._attempts += 1
        elif decision.action == RecoveryAction.RECOVER:
            self._attempts += 1
        elif decision.action == RecoveryAction.REPLAN:
            self._attempts += 1
            self._replans += 1
            self._trigger_replan(plan, observation, decision)

        self._emit(
            "recovery_completed",
            {
                "cycle": self._total_cycles,
                "action": decision.action.value,
                "kind": decision.kind.value,
                "bounded": decision.bounded,
            },
        )
        return decision

    def _trigger_replan(self, plan: Any, observation: Any, decision: RecoveryDecision) -> None:
        """Picu replanner (bila tersedia). Replanner tetap pemilik revisi plan."""
        if self.replanner is None or plan is None or observation is None:
            return
        try:
            self.replanner.replan(plan, observation)
        except Exception:  # noqa: BLE001 - replan error tidak boleh crash recovery
            return

    # ------------------------------------------------------------------ #
    # Events (memakai SessionStore yang sudah ada)
    # ------------------------------------------------------------------ #
    def _emit(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Emit event recovery via SessionStore (bukan event bus baru)."""
        if self.session_store is None or not self.session_id:
            return
        try:
            from agent_ai.session.events import EventType, make_event

            try:
                et = EventType(event_type)
            except ValueError:
                et = EventType.PHASE_CHANGED
            event = make_event(
                session_id=self.session_id,
                event_type=et,
                task_id=payload.get("task_id"),
                payload=payload,
            )
            self.session_store.append_event(event)
        except Exception:  # noqa: BLE001 - event emission tidak boleh crash
            return
