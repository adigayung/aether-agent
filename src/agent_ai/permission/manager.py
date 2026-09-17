"""PermissionManager: facade tipis untuk Permission Policy Layer (#54).

Menyatukan classifier + policy menjadi satu entry point yang dipakai executor/
runtime. Manager TIDAK mengeksekusi tool dan TIDAK menduplikasi ToolRegistry:
ia hanya mengembalikan keputusan.

    from agent_ai.permission import PermissionManager

    manager = PermissionManager()
    decision = manager.check("write_file", {"path": "a.txt", "content": "x"})
    if not decision.allowed:
        ...  # jangan jalankan tool
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent_ai.permission.classifier import ActionClassifier
from agent_ai.permission.models import (
    ActionClass,
    PermissionConfig,
    PermissionDecision,
    PermissionRequest,
    PolicyMode,
)
from agent_ai.permission.policy import PermissionPolicy


def _to_mode(value: object, default: PolicyMode) -> PolicyMode:
    """Konversi nilai config (str/PolicyMode) -> PolicyMode (fallback aman)."""
    if isinstance(value, PolicyMode):
        return value
    try:
        return PolicyMode(str(value).strip().lower())
    except (ValueError, TypeError):
        return default


class PermissionManager:
    """Entry point policy: klasifikasi + evaluasi action/tool.

    Args:
        policy: PermissionPolicy opsional (default: dari config settings).
        classifier: ActionClassifier opsional.
    """

    def __init__(
        self,
        policy: Optional[PermissionPolicy] = None,
        classifier: Optional[ActionClassifier] = None,
    ) -> None:
        self.classifier = classifier or ActionClassifier()
        self.policy = policy or PermissionPolicy(
            config=self._config_from_settings(),
            classifier=self.classifier,
        )

    @staticmethod
    def _config_from_settings() -> PermissionConfig:
        """Ambil PermissionConfig dari settings (fallback aman bila gagal).

        Settings menyimpan mode sebagai string (dari .env); di sini dikonversi
        ke PolicyMode. Bila settings tidak tersedia, pakai default aman.
        """
        try:
            from agent_ai.config.settings import settings

            cfg = settings.permission
            return PermissionConfig(
                enabled=bool(cfg.enabled),
                read_only=_to_mode(cfg.read_only, PolicyMode.ALLOW),
                workspace_write=_to_mode(cfg.workspace_write, PolicyMode.ALLOW),
                delete_move=_to_mode(cfg.delete_move, PolicyMode.ALLOW),
                command_execution=_to_mode(cfg.command_execution, PolicyMode.ALLOW),
                external_network=_to_mode(cfg.external_network, PolicyMode.ALLOW),
                unknown=_to_mode(cfg.unknown, PolicyMode.REQUIRE_APPROVAL),
            )
        except Exception:  # noqa: BLE001 - config error tidak boleh crash
            return PermissionConfig()

    @property
    def enabled(self) -> bool:
        """True bila enforcement policy aktif."""
        return bool(self.policy.config.enabled)

    def classify(self, action: str, arguments: Optional[Dict[str, Any]] = None) -> ActionClass:
        """Klasifikasi action/tool -> ActionClass."""
        return self.classifier.classify(action, arguments)

    def check(
        self,
        action: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        project_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PermissionDecision:
        """Evaluasi action/tool -> PermissionDecision.

        Args:
            action: nama action/tool.
            arguments: argumen action.
            project_id: project terkait (opsional).
            metadata: info tambahan bebas.

        Returns:
            PermissionDecision.
        """
        request = PermissionRequest(
            action=action,
            arguments=dict(arguments or {}),
            project_id=project_id,
            metadata=dict(metadata or {}),
        )
        return self.policy.evaluate(request)

    def is_allowed(
        self,
        action: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """True bila action boleh dijalankan (tanpa approval)."""
        return self.check(action, arguments).allowed
