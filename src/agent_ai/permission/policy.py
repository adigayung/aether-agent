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

from typing import Callable, Optional

from agent_ai.permission.classifier import ActionClassifier
from agent_ai.permission.matrix import (
    PermissionMatrix,
    resolve_matrix_action,
    resolve_scope,
)
from agent_ai.permission.models import (
    ActionClass,
    ActionScope,
    PermissionConfig,
    PermissionDecision,
    PermissionRequest,
    PolicyMode,
)


def _default_extension_tool_resolver(action: str) -> bool:
    """Resolver default: apakah ``action`` adalah tool Extension ENABLED.

    Memakai mekanisme registrasi Extension yang SUDAH ADA — keanggotaan registry
    tool Extension bersama (``agent_ai.tools.registry.is_extension_tool``) —
    BUKAN daftar nama Extension (mis. "playwright"). Import dilakukan lazily agar
    Permission Policy Layer tetap ringan, bebas import-cycle, dan tidak menambah
    dependency keras ke layer tool. Kegagalan apa pun -> False (pakai policy
    normal; aman, tidak pernah memaksa ALLOW).
    """
    try:
        from agent_ai.tools.registry import is_extension_tool

        return bool(is_extension_tool(action))
    except Exception:  # noqa: BLE001 - resolver tidak boleh membuat evaluasi crash
        return False


class PermissionPolicy:
    """Memutuskan apakah sebuah action/tool boleh dijalankan.

    Args:
        config: PermissionConfig (policy default per ActionClass). Default:
            PermissionConfig() (semua diizinkan kecuali UNKNOWN -> require_approval).
        classifier: ActionClassifier opsional (default: ActionClassifier()).
        extension_tool_resolver: callable opsional ``(action: str) -> bool`` yang
            menentukan apakah sebuah action adalah tool dari Extension yang
            ENABLED. Tool Extension yang enabled SELALU diizinkan (ALLOW) tanpa
            approval, terlepas dari PermissionConfig — karena Extension hanya
            bisa mengaktifkan tool-nya bila user ENABLE extension tersebut
            (mekanisme registrasi Extension existing). Default: resolver yang
            membaca registry tool Extension bersama (generik untuk SEMUA
            Extension, bukan logic khusus Extension tertentu).
        matrix: Project Permission Matrix opsional (aksi x inside/outside).
            Bila diberikan, matrix MENJADI sumber keputusan untuk aksi yang
            tercakup matrix (read/modify/delete/move file + terminal
            read/mutating); action lain (network/unknown) tetap memakai
            PermissionConfig. Bila None, perilaku existing tidak berubah.

    Catatan precedence:
        - Tool Extension enabled -> ALLOW (tidak berubah).
        - ``config.enabled`` False -> ALLOW (tidak berubah).
        - ``request.project_matrix`` (project-local) MENANG atas ``self.matrix``
          (global), sehingga policy project selalu berlaku untuk project itu.
    """

    def __init__(
        self,
        config: Optional[PermissionConfig] = None,
        classifier: Optional[ActionClassifier] = None,
        extension_tool_resolver: Optional[Callable[[str], bool]] = None,
        matrix: Optional[PermissionMatrix] = None,
    ) -> None:
        self.config = config or PermissionConfig()
        self.classifier = classifier or ActionClassifier()
        self.matrix = matrix
        self.extension_tool_resolver = (
            extension_tool_resolver or _default_extension_tool_resolver
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def is_extension_tool(self, action: str) -> bool:
        """True bila ``action`` adalah tool dari Extension yang ENABLED.

        Memakai resolver yang dikonfigurasi (default: keanggotaan registry tool
        Extension bersama). Aman: kegagalan resolver -> False.
        """
        try:
            return bool(self.extension_tool_resolver(action))
        except Exception:  # noqa: BLE001 - jangan crash saat evaluasi
            return False

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

        # Tool Extension ENABLED -> SELALU ALLOW (tanpa approval).
        #
        # Alasan: tool Extension hanya bisa aktif bila user ENABLE extension
        # tersebut (mekanisme registrasi/capability Extension existing); begitu
        # enabled, extension itu trusted. Deteksi memakai KEANGGOTAAN registry
        # tool Extension (bukan nama Extension tertentu) sehingga berlaku
        # GENERIC untuk semua Extension. Tool bawaan AETHER TIDAK terdaftar di
        # registry itu, sehingga tetap mengikuti policy normal di bawah.
        if self.is_extension_tool(request.action):
            return PermissionDecision(
                allowed=True,
                mode=PolicyMode.ALLOW,
                action_class=action_class,
                reason=(
                    f"Tool '{request.action}' berasal dari Extension yang enabled; "
                    "diizinkan otomatis."
                ),
                metadata={"enforced": True, "source": "extension"},
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

        # Project Permission Matrix (aksi x inside/outside): berlaku bila matrix
        # diberikan (project-local menang atas global). Aksi yang tercakup
        # matrix diputuskan oleh matrix; aksi lain (network/unknown) memakai
        # PermissionConfig existing.
        matrix = request.project_matrix or self.matrix
        if matrix is not None:
            decision = self._decide_with_matrix(
                matrix, action_class, request
            )
            if decision is not None:
                return decision

        mode = self.config.mode_for(action_class)
        return self._decide(mode, action_class, request)

    def is_allowed(self, request: PermissionRequest) -> bool:
        """True bila action boleh dijalankan (tanpa approval)."""
        return self.evaluate(request).allowed

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    def _decide_with_matrix(
        self,
        matrix: PermissionMatrix,
        action_class: ActionClass,
        request: PermissionRequest,
    ) -> Optional[PermissionDecision]:
        """Keputusan dari Project Permission Matrix, atau None bila tak tercakup.

        Menentukan aksi matrix (read/modify/delete/move file, terminal
        read/mutating) + scope (inside/outside workspace), lalu membaca mode
        dari matrix. Mengembalikan None bila aksi di luar lingkup matrix
        (mis. network/unknown) sehingga pemanggil memakai policy existing.
        """
        matrix_action = resolve_matrix_action(
            request.action, action_class, request.arguments
        )
        if matrix_action is None:
            return None
        scope = resolve_scope(
            request.action, action_class, request.arguments, request.workspace_root
        )
        mode = matrix.mode_for(matrix_action, scope)
        decision = self._decide(mode, action_class, request)
        decision.matrix_action = matrix_action
        decision.scope = scope
        decision.metadata = {
            **decision.metadata,
            "matrix_action": matrix_action.value,
            "scope": scope.value,
            "source": "matrix",
        }
        return decision

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
