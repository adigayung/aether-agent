"""Permission / Safety Policy Layer (#54).

Layer kebijakan terpusat yang menentukan apakah sebuah action/tool boleh
dijalankan. Ini adalah *policy layer*, BUKAN sistem permission baru di
Django/UI, dan BUKAN policy engine kedua di ToolRegistry.

    Task / Runtime
        -> Permission Policy   (layer ini: memutuskan)
        -> Tool Executor       (menjalankan bila diizinkan)
        -> Tool                (implementasi tool)

Provider-agnostic, deterministik, tanpa dependency ke core/runtime/providers.
"""

from agent_ai.permission.approval import (
    ApprovalCoordinator,
    ApprovalRequest,
    ApprovalStatus,
    DEFAULT_APPROVAL_TIMEOUT,
    make_approval_gate,
    new_approval_id,
)
from agent_ai.permission.classifier import ActionClassifier
from agent_ai.permission.manager import PermissionManager
from agent_ai.permission.matrix import (
    DEFAULT_MATRIX_RULES,
    MATRIX_ACTIONS,
    MATRIX_SCOPES,
    TERMINAL_MUTATE,
    TERMINAL_READ,
    PermissionMatrix,
    classify_terminal_command,
    describe_target,
    is_path_within_root,
    matrix_mode_value,
    resolve_matrix_action,
    resolve_scope,
)
from agent_ai.permission.models import (
    ActionClass,
    ActionScope,
    MatrixAction,
    PermissionConfig,
    PermissionDecision,
    PermissionRequest,
    PolicyMode,
)
from agent_ai.permission.policy import PermissionPolicy

__all__ = [
    "ActionClass",
    "ActionScope",
    "MatrixAction",
    "PolicyMode",
    "PermissionRequest",
    "PermissionDecision",
    "PermissionConfig",
    "ActionClassifier",
    "PermissionPolicy",
    "PermissionManager",
    "PermissionMatrix",
    "ApprovalCoordinator",
    "ApprovalRequest",
    "ApprovalStatus",
    "DEFAULT_APPROVAL_TIMEOUT",
    "DEFAULT_MATRIX_RULES",
    "MATRIX_ACTIONS",
    "MATRIX_SCOPES",
    "TERMINAL_READ",
    "TERMINAL_MUTATE",
    "classify_terminal_command",
    "describe_target",
    "is_path_within_root",
    "make_approval_gate",
    "matrix_mode_value",
    "new_approval_id",
    "resolve_matrix_action",
    "resolve_scope",
]
