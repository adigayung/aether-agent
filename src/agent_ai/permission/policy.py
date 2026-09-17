"""PermissionPolicy: memutuskan apakah action/tool boleh dijalankan (#54).

Provider-agnostic, deterministik. Policy TIDAK mengeksekusi apa pun; ia hanya
memutuskan (allow / deny / require_approval) berdasarkan ActionClass dan
PermissionConfig.

Prinsip:
    - Default aman & backward-compatible: bila policy tidak diaktifkan
      (config.enabled=False), semua action diizinkan (perilaku existing).
    - Klasifikasi action memakai ActionClassifier (tidak mengubah ToolRegistry).
    - Keputusan dapat dipakai executor/runtime sebelum eksekusi tool.
"""

from __future__ import annotations

from typing import Optional

from agent_ai.permission.classifier import ActionClassifier
from agent_ai.permission.models import (
    ActionClass,
    PermissionConfig,
    PermissionDecision,
    PermissionRequest,
    PolicyMode,
)


class PermissionPolicy:
    """Memutuskan apakah sebuah action/tool boleh dijalankan.

    Args:
        config: PermissionConfig (policy default per ActionClass). Default:
            PermissionConfig() (semua diizinkan kecuali UNKNOWN -> require_approval).
        classifier: ActionClassifier opsional (default: ActionClassifier()).
    """

    def __init__(
        self,
        config: Optional[PermissionConfig] = None,
        classifier: Optional[ActionClassifier] = None,
    ) -> None:
        self.config = config or PermissionConfig()
        self.classifier = classifier or ActionClassifier()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def evaluate(self, request: PermissionRequest) -> PermissionDecision:
        """Evaluasi sebuah PermissionRequest -> PermissionDecision.

        Args:
            request: PermissionRequest (action + arguments + konteks).

        Returns:
            PermissionDecision (allowed/mode/action_class/reason).
        """
        # Klasifikasi action (pakai yang sudah ada bila diberikan).
        action_class = request.action_class or self.classifier.classify(
            request.action, request.arguments
        )

        # Policy tidak aktif -> izinkan (backward compatible).
        if not self.config.enabled:
            return PermissionDecision(
                allowed=True,
                mode=PolicyMode.ALLOW,
                action_class=action_class,
                reason="Permission policy tidak aktif; action diizinkan.",
                metadata={"enforced": False},
            )

        mode = self.config.mode_for(action_class)
        return self._decide(mode, action_class, request)

    def is_allowed(self, request: PermissionRequest) -> bool:
        """True bila action boleh dijalankan (tanpa approval)."""
        return self.evaluate(request).allowed

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    @staticmethod
    def _decide(
        mode: PolicyMode,
        action_class: ActionClass,
        request: PermissionRequest,
    ) -> PermissionDecision:
        """Bangun PermissionDecision dari mode + action_class."""
        if mode == PolicyMode.ALLOW:
            return PermissionDecision(
                allowed=True,
                mode=mode,
                action_class=action_class,
                reason=f"Action '{request.action}' ({action_class.value}) diizinkan.",
                metadata={"enforced": True},
            )
        if mode == PolicyMode.DENY:
            return PermissionDecision(
                allowed=False,
                mode=mode,
                action_class=action_class,
                reason=f"Action '{request.action}' ({action_class.value}) ditolak oleh policy.",
                metadata={"enforced": True},
            )
        # REQUIRE_APPROVAL: belum ada UI approval -> tidak dijalankan (aman).
        return PermissionDecision(
            allowed=False,
            mode=mode,
            action_class=action_class,
            reason=(
                f"Action '{request.action}' ({action_class.value}) butuh persetujuan "
                "dan belum disetujui."
            ),
            requires_approval=True,
            metadata={"enforced": True},
        )
