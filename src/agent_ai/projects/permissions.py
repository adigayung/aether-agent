"""Project-local permission policy: `<root>/.aether/permissions.json`.

Ini BUKAN sistem permission kedua. Modul ini hanya *persistence project-local*
untuk konsep policy yang SUDAH ADA di `agent_ai.permission`:

    PolicyMode.ALLOW            <-> "allow"   (UI: ALLOW)
    PolicyMode.REQUIRE_APPROVAL <-> "ask"     (UI: ASK)
    PolicyMode.DENY             <-> "deny"    (UI: DENY)

Konfigurasi disimpan independen untuk setiap project di dalam root project
target:

    <root project target>/
        .aether/
            permissions.json

Bentuk KANONIK (Project Permission Matrix: aksi x inside/outside workspace):

    {
      "read_files":        {"inside": "allow", "outside": "allow"},
      "modify_files":      {"inside": "allow", "outside": "deny"},
      "delete_files":      {"inside": "allow", "outside": "deny"},
      "move_files":        {"inside": "allow", "outside": "deny"},
      "terminal_read":     {"inside": "allow", "outside": "allow"},
      "terminal_mutating": {"inside": "ask",   "outside": "deny"}
    }

Prinsip:
    - Project-local: konfigurasi project A TIDAK boleh tercampur project B.
    - Reuse: memakai model `PermissionConfig`/`PolicyMode` yang sudah ada
      (lihat `to_permission_config()`), sehingga tidak ada policy engine baru.
    - Backward compatible: file LAMA berisi `{"mode": ..., "scope": ...}`
      tetap dibaca dan dipetakan ke matrix default (tanpa merusak project).
    - Idempotent + atomic write: tidak ada file parsial.
    - Tidak ada dependency baru; tanpa DB/vektor/embeddings.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

from agent_ai.permission.matrix import (
    MATRIX_ACTIONS,
    MATRIX_SCOPES,
    MODE_LABELS,
    PermissionMatrix,
    matrix_mode_value,
    normalize_matrix_mode,
)
from agent_ai.permission.models import (
    ActionScope,
    MatrixAction,
    PermissionConfig,
    PolicyMode,
)

#: Nama folder root metadata project (sama dengan aether_store/github_backup).
AETHER_DIR_NAME = ".aether"
#: Nama file policy permission project-local.
PERMISSIONS_FILE_NAME = "permissions.json"

#: Scope policy (legacy, dipertahankan untuk kompatibilitas pemanggil lama).
SCOPE_WORKSPACE = "workspace"
SCOPE_OUTSIDE = "outside"

#: Default Project Policy (baseline) yang berlaku untuk project BARU.
#:
#: Nilai LEGACY (mode/scope) dipertahankan sebagai konstanta agar pemanggil lama
#: tetap bekerja; SUMBER KEBENARAN policy sekarang adalah Project Permission
#: Matrix (lihat `ProjectPolicy.matrix` + `PermissionMatrix.default()`).
DEFAULT_PROJECT_POLICY_MODE = PolicyMode.ALLOW.value
DEFAULT_PROJECT_POLICY_SCOPE = SCOPE_WORKSPACE

#: Alias mode -> PolicyMode kanonik (menerima "ask" seperti tampilan UI).
_MODE_ALIASES = {
    "allow": PolicyMode.ALLOW,
    "ask": PolicyMode.REQUIRE_APPROVAL,
    "require_approval": PolicyMode.REQUIRE_APPROVAL,
    "deny": PolicyMode.DENY,
}

#: Alias scope -> nilai kanonik.
_SCOPE_ALIASES = {
    "workspace": SCOPE_WORKSPACE,
    "inside": SCOPE_WORKSPACE,
    "inside_workspace": SCOPE_WORKSPACE,
    "outside": SCOPE_OUTSIDE,
    "outside_workspace": SCOPE_OUTSIDE,
}

#: Set nilai input yang diterima (validasi request; tanpa menurunkan diam-diam).
MODE_ALIASES_SET = frozenset(_MODE_ALIASES)
SCOPE_ALIASES_SET = frozenset(_SCOPE_ALIASES)
#: Set nilai matrix yang diterima (allow/ask/deny).
MATRIX_MODE_VALUES_SET = frozenset(MODE_LABELS)

#: Label tampilan mode (UI). "ASK" = PolicyMode.REQUIRE_APPROVAL.
_MODE_LABELS = MODE_LABELS

#: Label tampilan scope matrix (UI) — kanonik: inside/outside.
_MATRIX_SCOPE_LABELS = {
    "inside": "Inside workspace",
    "outside": "Outside workspace",
}

#: Label tampilan scope LEGACY (kompatibilitas pemanggil lama).
_SCOPE_LABELS = {
    SCOPE_WORKSPACE: _MATRIX_SCOPE_LABELS["inside"],
    SCOPE_OUTSIDE: _MATRIX_SCOPE_LABELS["outside"],
}

#: Urutan opsi yang ditampilkan ke UI (deterministik).
MODE_OPTIONS: List[Dict[str, str]] = [
    {"value": "allow", "label": "ALLOW"},
    {"value": "ask", "label": "ASK"},
    {"value": "deny", "label": "DENY"},
]

SCOPE_OPTIONS: List[Dict[str, str]] = [
    {"value": SCOPE_WORKSPACE, "label": _SCOPE_LABELS[SCOPE_WORKSPACE]},
    {"value": SCOPE_OUTSIDE, "label": _SCOPE_LABELS[SCOPE_OUTSIDE]},
]

#: Opsi untuk permission matrix: aksi x scope (untuk UI; tanpa hardcode di
#: frontend). Setiap aksi menyertakan label + scope yang tersedia.
ACTION_LABELS: Dict[str, str] = {
    MatrixAction.READ_FILES.value: "Read files",
    MatrixAction.MODIFY_FILES.value: "Modify files",
    MatrixAction.DELETE_FILES.value: "Delete files",
    MatrixAction.MOVE_FILES.value: "Move / rename files",
    MatrixAction.TERMINAL_READ.value: "Terminal read-only",
    MatrixAction.TERMINAL_MUTATING.value: "Terminal mutating",
}

ACTION_OPTIONS: List[Dict[str, Any]] = [
    {
        "value": action,
        "label": ACTION_LABELS[action],
        "scopes": [
            {"value": scope, "label": _MATRIX_SCOPE_LABELS[scope]}
            for scope in MATRIX_SCOPES
        ],
    }
    for action in MATRIX_ACTIONS
]


def normalize_mode(value: Any) -> str:
    """Normalisasi mode -> nilai kanonik PolicyMode (fallback aman: allow)."""
    if isinstance(value, PolicyMode):
        return value.value
    text = str(value or "").strip().lower()
    mode = _MODE_ALIASES.get(text)
    if mode is None:
        return PolicyMode.ALLOW.value
    return mode.value


def normalize_scope(value: Any) -> str:
    """Normalisasi scope -> nilai kanonik (fallback aman: workspace)."""
    text = str(value or "").strip().lower()
    return _SCOPE_ALIASES.get(text, SCOPE_WORKSPACE)


def mode_label(mode: Any) -> str:
    """Label tampilan mode (ALLOW/ASK/DENY)."""
    return _MODE_LABELS.get(matrix_mode_value(mode), "ALLOW")


def scope_label(scope: Any) -> str:
    """Label tampilan scope (Inside workspace/Outside workspace)."""
    return _SCOPE_LABELS.get(normalize_scope(scope), _SCOPE_LABELS[SCOPE_WORKSPACE])


def _is_matrix_payload(data: Mapping[str, Any]) -> bool:
    """True bila dict memuat key aksi matrix (bentuk kanonik project policy)."""
    return any(action in data for action in MATRIX_ACTIONS)


@dataclass
class ProjectPolicy:
    """Policy permission sebuah project (PROJECT-LOCAL).

    Attributes:
        matrix: Project Permission Matrix (SUMBER KEBENARAN): mode per aksi x
            scope (inside/outside workspace).
        mode: Mode LEGACY opsional (kompatibilitas). Bila diisi saat
            konstruksi, dipetakan ke matrix (`PermissionMatrix.from_mode_scope`).
        scope: Scope LEGACY opsional (kompatibilitas), lihat `mode`.
    """

    matrix: PermissionMatrix = field(default_factory=PermissionMatrix.default)
    mode: Optional[str] = None
    scope: Optional[str] = None

    def __post_init__(self) -> None:
        # Legacy (mode/scope) diberikan -> turunkan matrix dari nilai itu agar
        # konstruksi lama `ProjectPolicy(mode="deny", scope="outside")` tetap
        # bermakna (tanpa merusak pemanggil existing).
        if self.mode is not None or self.scope is not None:
            self.matrix = PermissionMatrix.from_mode_scope(
                self.mode if self.mode is not None else DEFAULT_PROJECT_POLICY_MODE,
                self.scope if self.scope is not None else DEFAULT_PROJECT_POLICY_SCOPE,
            )
        elif not isinstance(self.matrix, PermissionMatrix):
            self.matrix = PermissionMatrix.from_dict(self.matrix)

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    @classmethod
    def default(cls) -> "ProjectPolicy":
        """Default Project Policy (baseline) untuk project BARU.

        Satu sumber kebenaran untuk inisialisasi `<root>/.aether/permissions.json`
        saat project dibuat. Setelah file ada, policy menjadi milik project
        tersebut dan default ini tidak berubah.
        """
        return cls(matrix=PermissionMatrix.default())

    @classmethod
    def from_dict(cls, data: Any) -> "ProjectPolicy":
        """Bangun policy dari dict (menerima bentuk matrix KANONIK & LEGACY)."""
        if not isinstance(data, Mapping):
            return cls.default()
        # Bentuk kanonik (matrix) -> langsung.
        if _is_matrix_payload(data):
            return cls(matrix=PermissionMatrix.from_dict(data))
        # Bentuk dibungkus {"matrix"/"rules": {...}}.
        for key in ("matrix", "rules"):
            inner = data.get(key)
            if isinstance(inner, Mapping):
                return cls(matrix=PermissionMatrix.from_dict(inner))
        # Bentuk LEGACY {"mode": ..., "scope": ...}.
        if "mode" in data or "scope" in data:
            return cls(
                mode=data.get("mode", DEFAULT_PROJECT_POLICY_MODE),
                scope=data.get("scope", DEFAULT_PROJECT_POLICY_SCOPE),
            )
        # Tidak dikenal -> default aman.
        return cls.default()

    # ------------------------------------------------------------------ #
    # Access
    # ------------------------------------------------------------------ #
    def mode_for(self, action: Any, scope: Any) -> PolicyMode:
        """Mode matrix untuk aksi x scope."""
        return self.matrix.mode_for(action, scope)

    def to_permission_config(self) -> PermissionConfig:
        """Konversi ke `PermissionConfig` existing (reuse, bukan policy baru).

        Dipakai untuk aksi yang TIDAK tercakup matrix (network/unknown) dan
        sebagai ringkasan policy project. Pemetaan deterministik dari matrix:
            - read_only         <- read_files.inside
            - workspace_write   <- modify_files.inside
            - delete_move       <- paling ketat(delete_files.inside, move_files.inside)
            - command_execution <- paling ketat(terminal_read.inside, terminal_mutating.inside)
            - external_network  <- ALLOW (tidak tercakup matrix)
            - unknown           <- REQUIRE_APPROVAL (aman)
        """
        read_inside = self.matrix.mode_for(MatrixAction.READ_FILES, ActionScope.INSIDE)
        modify_inside = self.matrix.mode_for(MatrixAction.MODIFY_FILES, ActionScope.INSIDE)
        delete_inside = self.matrix.mode_for(MatrixAction.DELETE_FILES, ActionScope.INSIDE)
        move_inside = self.matrix.mode_for(MatrixAction.MOVE_FILES, ActionScope.INSIDE)
        term_read = self.matrix.mode_for(MatrixAction.TERMINAL_READ, ActionScope.INSIDE)
        term_mut = self.matrix.mode_for(MatrixAction.TERMINAL_MUTATING, ActionScope.INSIDE)
        return PermissionConfig(
            enabled=True,
            read_only=read_inside,
            workspace_write=modify_inside,
            delete_move=_most_restrictive(delete_inside, move_inside),
            command_execution=_most_restrictive(term_read, term_mut),
            external_network=PolicyMode.ALLOW,
            unknown=PolicyMode.REQUIRE_APPROVAL,
        )

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        """Representasi kanonik (nilai tersimpan di permissions.json)."""
        return self.matrix.to_dict()

    def to_ui_dict(self) -> Dict[str, Any]:
        """Representasi + label untuk UI (tanpa hardcode di frontend)."""
        return {
            "matrix": self.matrix.to_dict(),
            "actions": ACTION_OPTIONS,
            "scopes": list(MATRIX_SCOPES),
            "modes": MODE_OPTIONS,
        }


def _most_restrictive(*modes: PolicyMode) -> PolicyMode:
    """Mode paling ketat dari beberapa mode (deny > ask > allow)."""
    if PolicyMode.DENY in modes:
        return PolicyMode.DENY
    if PolicyMode.REQUIRE_APPROVAL in modes:
        return PolicyMode.REQUIRE_APPROVAL
    return PolicyMode.ALLOW


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Tulis bytes secara atomik (temp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".aether_tmp_", suffix=".swp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class ProjectPermissionStore:
    """Store project-local `<root>/.aether/permissions.json`.

    Args:
        root: root project target.
    """

    def __init__(self, root: Union[str, Path]) -> None:
        self.root = Path(root)

    @property
    def path(self) -> Path:
        return self.root / AETHER_DIR_NAME / PERMISSIONS_FILE_NAME

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> ProjectPolicy:
        """Muat policy project (default aman/bisa-dipakai bila belum ada).

        Project LAMA tanpa file policy -> Default Project Policy (matrix default)
        tanpa merusak/mengubah project.
        """
        path = self.path
        if not path.is_file():
            return ProjectPolicy.default()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ProjectPolicy.default()
        return ProjectPolicy.from_dict(raw)

    def save(self, policy: ProjectPolicy) -> ProjectPolicy:
        """Simpan policy (atomic write) -> `<root>/.aether/permissions.json`."""
        data = json.dumps(policy.to_dict(), indent=2, ensure_ascii=False) + "\n"
        _atomic_write_bytes(self.path, data.encode("utf-8"))
        return policy

    def ensure_default(self) -> ProjectPolicy:
        """Inisialisasi policy untuk project BARU dari Default Project Policy.

        Dipakai HANYA saat project baru dibuat: bila `permissions.json` BELUM
        ada -> tulis Default Project Policy (matrix default). Bila file SUDAH
        ada -> file dipertahankan apa adanya (perubahan user TIDAK ditimpa).

        Returns:
            ProjectPolicy yang berlaku setelah operasi (default atau existing).
        """
        if self.exists():
            return self.load()
        return self.save(ProjectPolicy.default())
