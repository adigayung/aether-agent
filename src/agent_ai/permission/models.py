"""Model untuk Permission / Safety Policy Layer (#54).

Provider-agnostic. Layer ini adalah *policy layer*: ia hanya memutuskan apakah
sebuah action/tool boleh dijalankan. Ia TIDAK mengeksekusi apa pun dan TIDAK
menduplikasi tanggung jawab Runtime, ToolRegistry, Validation, Recovery, atau
Django.

    ActionClass        -> klasifikasi action (read-only, write, delete, command, ...)
    ActionScope        -> lokasi target relatif workspace (inside / outside)
    MatrixAction       -> aksi Project Permission Matrix (read_files, modify_files, ...)
    PolicyMode         -> mode kebijakan (allow / deny / require_approval)
    PermissionRequest  -> input terstruktur untuk evaluasi
    PermissionDecision -> keputusan lengkap + alasan
    PermissionConfig   -> policy default per ActionClass (dari config, tidak di-hardcode)

Tidak menyimpan chain-of-thought.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ActionClass(str, Enum):
    """Klasifikasi action/tool berdasarkan dampaknya.

    Klasifikasi bersifat deklaratif dan deterministik (lihat classifier.py).
    """

    READ_ONLY = "read_only"                  # baca/inspeksi, tanpa efek samping
    WORKSPACE_WRITE = "workspace_write"      # tulis/edit di dalam workspace
    DELETE_MOVE = "delete_move"              # hapus/pindah/rename
    COMMAND_EXECUTION = "command_execution"  # menjalankan command/terminal
    EXTERNAL_NETWORK = "external_network"    # aksi keluar/network (bila relevan)
    UNKNOWN = "unknown"                      # tidak terklasifikasi


class ActionScope(str, Enum):
    """Lokasi target sebuah action relatif terhadap workspace project.

    Dipakai oleh Project Permission Matrix untuk membedakan policy di dalam dan
    di luar workspace project.

        INSIDE  -> path/aksi berada DI DALAM workspace project.
        OUTSIDE -> path/aksi berada DI LUAR workspace project.
    """

    INSIDE = "inside"
    OUTSIDE = "outside"


class MatrixAction(str, Enum):
    """Aksi Project Permission Matrix (baris matrix).

    Setiap aksi dipetakan ke satu ActionClass existing, sehingga matrix tetap
    memakai klasifikasi action existing (bukan classifier kedua).
    """

    READ_FILES = "read_files"                # baca file (read-only)
    MODIFY_FILES = "modify_files"            # tulis/edit file
    DELETE_FILES = "delete_files"            # hapus file/directory
    MOVE_FILES = "move_files"                # pindah/rename file/directory
    TERMINAL_READ = "terminal_read"          # command terminal read-only
    TERMINAL_MUTATING = "terminal_mutating"  # command terminal yang mengubah state


class PolicyMode(str, Enum):
    """Mode kebijakan untuk sebuah ActionClass."""

    ALLOW = "allow"                        # boleh dijalankan
    DENY = "deny"                          # dilarang
    REQUIRE_APPROVAL = "require_approval"  # butuh persetujuan (belum ada UI)


@dataclass
class PermissionRequest:
    """Input terstruktur untuk evaluasi policy.

    Attributes:
        action: nama action/tool (mis. "write_file", "run_command").
        arguments: argumen action (dict).
        action_class: klasifikasi action (bila sudah diketahui; None = klasifikasi
            otomatis oleh policy).
        project_id: project terkait (opsional, untuk konteks).
        metadata: info tambahan bebas.

    Matrix (`project_matrix`) & scope (`scope`/`workspace_root`) bersifat
    OPSIONAL dan ADDITIVE: bila tidak diisi, policy existing berperilaku persis
    seperti sebelumnya (backward compatible).
    """

    action: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    action_class: Optional[ActionClass] = None
    project_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    #: Root workspace project (untuk menentukan INSIDE/OUTSIDE). None = tidak
    #: dapat dibedakan (diperlakukan INSIDE; boundary tool tetap berlaku).
    workspace_root: Optional[str] = None
    #: Project Permission Matrix efektif (bila project punya
    #: `.aether/settings/permissions.json`). None = pakai PermissionConfig existing.
    project_matrix: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "arguments": self.arguments,
            "action_class": self.action_class.value if self.action_class else None,
            "project_id": self.project_id,
            "metadata": self.metadata,
            "workspace_root": self.workspace_root,
        }


@dataclass
class PermissionDecision:
    """Keputusan policy (struktural, bukan chain-of-thought).

    Attributes:
        allowed: True bila action boleh dijalankan.
        mode: mode kebijakan yang diterapkan.
        action_class: klasifikasi action yang mendasari keputusan.
        reason: alasan singkat.
        requires_approval: True bila butuh persetujuan (mode require_approval).
        matrix_action: aksi Project Permission Matrix (bila matrix dipakai).
        scope: INSIDE/OUTSIDE workspace (bila dapat ditentukan).
        metadata: info tambahan bebas.
    """

    allowed: bool
    mode: PolicyMode = PolicyMode.ALLOW
    action_class: ActionClass = ActionClass.UNKNOWN
    reason: str = ""
    requires_approval: bool = False
    matrix_action: Optional[MatrixAction] = None
    scope: Optional[ActionScope] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "mode": self.mode.value,
            "action_class": self.action_class.value,
            "matrix_action": (
                self.matrix_action.value if self.matrix_action is not None else None
            ),
            "scope": self.scope.value if self.scope is not None else None,
            "reason": self.reason,
            "requires_approval": self.requires_approval,
            "metadata": self.metadata,
        }


@dataclass
class PermissionConfig:
    """Policy default per ActionClass (dibaca dari config, tidak di-hardcode).

    Default aman & backward-compatible: operasi read-only dan workspace write
    diizinkan (sesuai perilaku workspace existing), sedangkan delete/move dan
    command execution juga diizinkan secara default agar perilaku existing
    tidak berubah. Mode dapat diubah lewat config (.env) tanpa mengubah kode.

    Attributes:
        enabled: apakah enforcement policy aktif.
        read_only: mode untuk ActionClass.READ_ONLY.
        workspace_write: mode untuk ActionClass.WORKSPACE_WRITE.
        delete_move: mode untuk ActionClass.DELETE_MOVE.
        command_execution: mode untuk ActionClass.COMMAND_EXECUTION.
        external_network: mode untuk ActionClass.EXTERNAL_NETWORK.
        unknown: mode untuk ActionClass.UNKNOWN (default aman: require_approval).
    """

    enabled: bool = True
    read_only: PolicyMode = PolicyMode.ALLOW
    workspace_write: PolicyMode = PolicyMode.ALLOW
    delete_move: PolicyMode = PolicyMode.ALLOW
    command_execution: PolicyMode = PolicyMode.ALLOW
    external_network: PolicyMode = PolicyMode.ALLOW
    unknown: PolicyMode = PolicyMode.REQUIRE_APPROVAL

    def mode_for(self, action_class: ActionClass) -> PolicyMode:
        """Mode kebijakan untuk sebuah ActionClass."""
        return {
            ActionClass.READ_ONLY: self.read_only,
            ActionClass.WORKSPACE_WRITE: self.workspace_write,
            ActionClass.DELETE_MOVE: self.delete_move,
            ActionClass.COMMAND_EXECUTION: self.command_execution,
            ActionClass.EXTERNAL_NETWORK: self.external_network,
            ActionClass.UNKNOWN: self.unknown,
        }.get(action_class, self.unknown)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "read_only": self.read_only.value,
            "workspace_write": self.workspace_write.value,
            "delete_move": self.delete_move.value,
            "command_execution": self.command_execution.value,
            "external_network": self.external_network.value,
            "unknown": self.unknown.value,
        }
