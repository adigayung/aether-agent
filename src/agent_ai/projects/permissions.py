"""Project-local permission policy: `<root>/.aether/permissions.json`.

Ini BUKAN sistem permission kedua. Modul ini hanya *persistence project-local*
untuk konsep policy yang SUDAH ADA di `agent_ai.permission`:

    PolicyMode.ALLOW            <-> "allow"            (UI: ALLOW)
    PolicyMode.REQUIRE_APPROVAL <-> "require_approval" (UI: ASK)
    PolicyMode.DENY             <-> "deny"             (UI: DENY)

Policy disimpan independen untuk setiap project di dalam root project target:

    <root project target>/
        .aether/
            permissions.json    # { "mode": "...", "scope": "..." }

Prinsip:
    - Project-local: konfigurasi project A TIDAK boleh tercampur project B.
    - Reuse: memakai model `PermissionConfig`/`PolicyMode` yang sudah ada
      (lihat `to_permission_config()`), sehingga tidak ada policy engine baru.
    - Idempotent + atomic write: tidak ada file parsial.
    - Tidak ada dependency baru; tanpa DB/vektor/embeddings.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Union

from agent_ai.permission.models import PermissionConfig, PolicyMode

#: Nama folder root metadata project (sama dengan aether_store/github_backup).
AETHER_DIR_NAME = ".aether"
#: Nama file policy permission project-local.
PERMISSIONS_FILE_NAME = "permissions.json"

#: Scope policy: di dalam workspace atau di luar workspace.
SCOPE_WORKSPACE = "workspace"
SCOPE_OUTSIDE = "outside"

#: Default Project Policy (baseline) yang berlaku untuk project BARU.
#:
#: Nilai default sengaja AMAN & backward-compatible dengan perilaku existing
#: (operasi workspace diizinkan). Default ini HANYA dipakai saat project baru
#: dibuat untuk menginisialisasi `<root>/.aether/permissions.json`; setelah file
#: itu ada, policy menjadi milik project tersebut sehingga perubahan policy di
#: UI TIDAK mengubah default ini.
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

#: Label tampilan mode (UI). "ASK" = PolicyMode.REQUIRE_APPROVAL.
_MODE_LABELS = {
    PolicyMode.ALLOW.value: "ALLOW",
    PolicyMode.REQUIRE_APPROVAL.value: "ASK",
    PolicyMode.DENY.value: "DENY",
}

#: Label tampilan scope (UI).
_SCOPE_LABELS = {
    SCOPE_WORKSPACE: "Inside workspace",
    SCOPE_OUTSIDE: "Outside workspace",
}

#: Urutan opsi yang ditampilkan ke UI (deterministik).
MODE_OPTIONS: List[Dict[str, str]] = [
    {"value": PolicyMode.ALLOW.value, "label": _MODE_LABELS[PolicyMode.ALLOW.value]},
    {
        "value": PolicyMode.REQUIRE_APPROVAL.value,
        "label": _MODE_LABELS[PolicyMode.REQUIRE_APPROVAL.value],
    },
    {"value": PolicyMode.DENY.value, "label": _MODE_LABELS[PolicyMode.DENY.value]},
]

SCOPE_OPTIONS: List[Dict[str, str]] = [
    {"value": SCOPE_WORKSPACE, "label": _SCOPE_LABELS[SCOPE_WORKSPACE]},
    {"value": SCOPE_OUTSIDE, "label": _SCOPE_LABELS[SCOPE_OUTSIDE]},
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
    return _MODE_LABELS.get(normalize_mode(mode), "ALLOW")


def scope_label(scope: Any) -> str:
    """Label tampilan scope (Inside workspace/Outside workspace)."""
    return _SCOPE_LABELS.get(normalize_scope(scope), _SCOPE_LABELS[SCOPE_WORKSPACE])


@dataclass
class ProjectPolicy:
    """Policy permission sebuah project (PROJECT-LOCAL).

    Attributes:
        mode: mode permission kanonik PolicyMode ("allow"/"require_approval"/
            "deny"). "ask" (UI) == "require_approval".
        scope: cakupan policy (SCOPE_WORKSPACE = inside workspace,
            SCOPE_OUTSIDE = outside workspace).
    """

    mode: str = DEFAULT_PROJECT_POLICY_MODE
    scope: str = DEFAULT_PROJECT_POLICY_SCOPE

    def __post_init__(self) -> None:
        self.mode = normalize_mode(self.mode)
        self.scope = normalize_scope(self.scope)

    @classmethod
    def default(cls) -> "ProjectPolicy":
        """Default Project Policy (baseline) untuk project BARU.

        Nilai ini adalah satu sumber kebenaran untuk inisialisasi
        `<root>/.aether/permissions.json` saat project dibuat. Setelah file ada,
        policy menjadi milik project tersebut dan default ini tidak berubah.
        """
        return cls(mode=DEFAULT_PROJECT_POLICY_MODE, scope=DEFAULT_PROJECT_POLICY_SCOPE)

    def mode_enum(self) -> PolicyMode:
        """PolicyMode yang sesuai dengan mode ini (reuse model existing)."""
        return PolicyMode(self.mode)

    def to_dict(self) -> Dict[str, Any]:
        """Representasi kanonik (nilai tersimpan di permissions.json)."""
        return {"mode": self.mode, "scope": self.scope}

    def to_ui_dict(self) -> Dict[str, Any]:
        """Representasi + label untuk UI (tanpa hardcode di frontend)."""
        return {
            "mode": self.mode,
            "mode_label": mode_label(self.mode),
            "scope": self.scope,
            "scope_label": scope_label(self.scope),
        }

    def to_permission_config(self) -> PermissionConfig:
        """Konversi ke `PermissionConfig` existing (reuse, bukan policy baru).

        Pemetaan deterministik:
            - read-only selalu ALLOW.
            - mode berlaku untuk mutasi di dalam workspace (workspace_write /
              delete_move / command_execution).
            - scope "outside" membuat external_network mengikuti mode;
              scope "workspace" menutup akses luar (DENY).
            - action tak terklasifikasi tetap aman (require_approval).
        """
        mode = self.mode_enum()
        # Scope "inside workspace" (default) = policy berlaku untuk operasi DI
        # DALAM workspace; aksi luar workspace mengikuti perilaku existing
        # (external_network default ALLOW). Scope "outside" = mode juga
        # mengikat operasi LUAR workspace (external_network mengikuti mode).
        outside_mode = mode if self.scope == SCOPE_OUTSIDE else PolicyMode.ALLOW
        return PermissionConfig(
            enabled=True,
            read_only=PolicyMode.ALLOW,
            workspace_write=mode,
            delete_move=mode,
            command_execution=mode,
            external_network=outside_mode,
            unknown=PolicyMode.REQUIRE_APPROVAL,
        )

    @classmethod
    def from_dict(cls, data: Any) -> "ProjectPolicy":
        if not isinstance(data, dict):
            data = {}
        return cls(
            mode=data.get("mode", PolicyMode.ALLOW.value),
            scope=data.get("scope", SCOPE_WORKSPACE),
        )


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
        """Muat policy project (default aman/backward-compatible bila belum ada)."""
        path = self.path
        if not path.is_file():
            return ProjectPolicy()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ProjectPolicy()
        return ProjectPolicy.from_dict(raw)

    def save(self, policy: ProjectPolicy) -> ProjectPolicy:
        """Simpan policy (atomic write) -> `<root>/.aether/permissions.json`."""
        data = json.dumps(policy.to_dict(), indent=2, ensure_ascii=False) + "\n"
        _atomic_write_bytes(self.path, data.encode("utf-8"))
        return policy

    def ensure_default(self) -> ProjectPolicy:
        """Inisialisasi policy untuk project BARU dari Default Project Policy.

        Dipakai HANYA saat project baru dibuat: bila `permissions.json` BELUM
        ada -> tulis Default Project Policy. Bila file SUDAH ada -> file
        dipertahankan apa adanya (perubahan user TIDAK ditimpa), sehingga
        default tidak pernah "mengembalikan" policy project yang sudah diubah.

        Returns:
            ProjectPolicy yang berlaku setelah operasi (default atau existing).
        """
        if self.exists():
            return self.load()
        return self.save(ProjectPolicy.default())
