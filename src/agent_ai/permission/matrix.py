"""Project Permission Matrix: policy per aksi x lokasi (inside/outside).

Modul ini melengkapi Permission Policy Layer existing (#54) dengan representasi
MATRIX yang dipakai project (`<root>/.aether/settings/permissions.json`). Ini BUKAN
sistem permission kedua: ia hanya *model + resolver* yang tetap menghasilkan
keputusan lewat `PermissionPolicy`/`PermissionManager` yang sudah ada.

Matrix (default untuk project baru)::

    {
      "read_files":       {"inside": "allow", "outside": "allow"},
      "modify_files":     {"inside": "allow", "outside": "deny"},
      "delete_files":     {"inside": "allow", "outside": "deny"},
      "move_files":       {"inside": "allow", "outside": "deny"},
      "terminal_read":    {"inside": "allow", "outside": "allow"},
      "terminal_mutating": {"inside": "ask",   "outside": "deny"}
    }

Setiap sel bernilai mode kanonik ``allow`` / ``ask`` / ``deny`` (``ask`` ==
:attr:`PolicyMode.REQUIRE_APPROVAL`).

Isi modul:
    - `PermissionMatrix`: model matrix (rules per aksi x scope).
    - `resolve_matrix_action`: map action/tool -> aksi matrix.
    - `resolve_scope`: map action + argumen -> inside/outside workspace.
    - `classify_terminal_command`: terminal read-only vs mutating.

Deterministik, tanpa dependency ke Django/runtime/tool registry.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from agent_ai.permission.models import (
    ActionClass,
    ActionScope,
    MatrixAction,
    PolicyMode,
)

#: Aksi terminal read-only (hasil `classify_terminal_command`).
TERMINAL_READ = "read"
#: Aksi terminal mutating (hasil `classify_terminal_command`).
TERMINAL_MUTATE = "mutate"

#: Matrix default AETHER (berlaku untuk project BARU, dan untuk project lama
#: yang belum punya `.aether/settings/permissions.json`).
DEFAULT_MATRIX_RULES: Dict[str, Dict[str, str]] = {
    MatrixAction.READ_FILES.value: {"inside": "allow", "outside": "allow"},
    MatrixAction.MODIFY_FILES.value: {"inside": "allow", "outside": "deny"},
    MatrixAction.DELETE_FILES.value: {"inside": "allow", "outside": "deny"},
    MatrixAction.MOVE_FILES.value: {"inside": "allow", "outside": "deny"},
    MatrixAction.TERMINAL_READ.value: {"inside": "allow", "outside": "allow"},
    MatrixAction.TERMINAL_MUTATING.value: {"inside": "ask", "outside": "deny"},
}

#: Urutan aksi matrix (deterministik untuk UI/serialisasi).
MATRIX_ACTIONS: List[str] = [action.value for action in MatrixAction]
#: Urutan scope matrix (deterministik untuk UI/serialisasi).
MATRIX_SCOPES: List[str] = [scope.value for scope in ActionScope]

#: Alias mode -> PolicyMode kanonik. "ask" (UI) == require_approval.
_MODE_ALIASES = {
    "allow": PolicyMode.ALLOW,
    "ask": PolicyMode.REQUIRE_APPROVAL,
    "require_approval": PolicyMode.REQUIRE_APPROVAL,
    "deny": PolicyMode.DENY,
}

#: Nilai mode kanonik yang ditulis ke file (bentuk pendek seperti spec).
_MODE_CANONICAL = {
    PolicyMode.ALLOW: "allow",
    PolicyMode.REQUIRE_APPROVAL: "ask",
    PolicyMode.DENY: "deny",
}

#: Label tampilan mode (UI).
MODE_LABELS: Dict[str, str] = {"allow": "ALLOW", "ask": "ASK", "deny": "DENY"}


def normalize_matrix_mode(value: Any, default: PolicyMode = PolicyMode.ALLOW) -> PolicyMode:
    """Normalisasi nilai mode matrix -> PolicyMode (fallback aman)."""
    if isinstance(value, PolicyMode):
        return value
    text = str(value or "").strip().lower()
    return _MODE_ALIASES.get(text, default)


def matrix_mode_value(mode: Any, default: str = "allow") -> str:
    """Nilai mode kanonik (allow/ask/deny) untuk serialisasi/UI."""
    if isinstance(mode, PolicyMode):
        return _MODE_CANONICAL.get(mode, default)
    text = str(mode or "").strip().lower()
    if text in _MODE_CANONICAL.values():
        return text
    resolved = _MODE_ALIASES.get(text)
    if resolved is not None:
        return _MODE_CANONICAL[resolved]
    return default


def _most_restrictive(modes: List[PolicyMode]) -> PolicyMode:
    """Mode paling ketat dari sekumpulan mode (deny > ask > allow)."""
    if PolicyMode.DENY in modes:
        return PolicyMode.DENY
    if PolicyMode.REQUIRE_APPROVAL in modes:
        return PolicyMode.REQUIRE_APPROVAL
    return PolicyMode.ALLOW


@dataclass
class PermissionMatrix:
    """Policy permission per aksi x scope (inside/outside workspace).

    Attributes:
        rules: ``{action: {"inside": PolicyMode, "outside": PolicyMode}}`` untuk
            setiap aksi matrix (`MatrixAction`).
    """

    rules: Dict[str, Dict[str, PolicyMode]] = field(
        default_factory=lambda: PermissionMatrix.rules_from_default()
    )

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    @staticmethod
    def rules_from_default() -> Dict[str, Dict[str, PolicyMode]]:
        """Rules default AETHER (salinan baru; tidak pernah dibagi antar-instance)."""
        return {
            action: {
                scope: normalize_matrix_mode(mode)
                for scope, mode in scopes.items()
            }
            for action, scopes in DEFAULT_MATRIX_RULES.items()
        }

    def __post_init__(self) -> None:
        self.rules = self._normalize_rules(self.rules)

    @classmethod
    def _normalize_rules(cls, rules: Any) -> Dict[str, Dict[str, PolicyMode]]:
        """Lengkapi/normalisasi rules agar selalu memuat seluruh aksi & scope."""
        normalized = cls.rules_from_default()
        if not isinstance(rules, Mapping):
            return normalized
        for action in MATRIX_ACTIONS:
            raw_scopes = rules.get(action)
            if not isinstance(raw_scopes, Mapping):
                continue
            for scope in MATRIX_SCOPES:
                if scope in raw_scopes:
                    normalized[action][scope] = normalize_matrix_mode(
                        raw_scopes[scope], normalized[action][scope]
                    )
        return normalized

    @classmethod
    def default(cls) -> "PermissionMatrix":
        """Matrix default AETHER (project baru / project tanpa file policy)."""
        return cls(rules=cls.rules_from_default())

    @classmethod
    def from_dict(cls, data: Any) -> "PermissionMatrix":
        """Bangun matrix dari dict (menerima bentuk pendek maupun penuh)."""
        if not isinstance(data, Mapping):
            return cls.default()
        # Terima payload yang dibungkus {"matrix": {...}} atau {"rules": {...}}.
        for key in ("matrix", "rules"):
            inner = data.get(key)
            if isinstance(inner, Mapping):
                return cls(rules=inner)
        return cls(rules=dict(data))

    @classmethod
    def from_mode_scope(cls, mode: Any, scope: Any) -> "PermissionMatrix":
        """Konversi policy lama (mode + scope tunggal) -> matrix.

        Semantik legacy: ``mode`` berlaku untuk aksi MUTASI pada ``scope`` yang
        dipilih (workspace -> inside, outside -> outside); read selalu ALLOW dan
        sel lain tetap ALLOW. Ini menjaga project lama (file
        ``{"mode","scope"}``) tetap berjalan tanpa merusak konfigurasi.
        """
        resolved = normalize_matrix_mode(mode)
        scope_value = str(scope or "workspace").strip().lower()
        outside_scope = scope_value in ("outside", "outside_workspace")
        target_scope = ActionScope.OUTSIDE.value if outside_scope else ActionScope.INSIDE.value
        mutating = {
            MatrixAction.MODIFY_FILES.value,
            MatrixAction.DELETE_FILES.value,
            MatrixAction.MOVE_FILES.value,
            MatrixAction.TERMINAL_MUTATING.value,
        }
        rules: Dict[str, Dict[str, PolicyMode]] = {}
        for action in MATRIX_ACTIONS:
            rules[action] = {
                ActionScope.INSIDE.value: PolicyMode.ALLOW,
                ActionScope.OUTSIDE.value: PolicyMode.ALLOW,
            }
            if action in mutating:
                rules[action][target_scope] = resolved
        return cls(rules=rules)

    # ------------------------------------------------------------------ #
    # Access
    # ------------------------------------------------------------------ #
    def mode_for(self, action: Any, scope: Any) -> PolicyMode:
        """Mode untuk aksi matrix + scope (fallback aman: aksi tanpa aturan -> ALLOW)."""
        action_key = action.value if isinstance(action, MatrixAction) else str(action)
        scope_key = scope.value if isinstance(scope, ActionScope) else str(scope)
        scopes = self.rules.get(action_key)
        if not isinstance(scopes, Mapping):
            return PolicyMode.ALLOW
        return normalize_matrix_mode(scopes.get(scope_key), PolicyMode.ALLOW)

    def with_mode(self, action: Any, scope: Any, mode: Any) -> "PermissionMatrix":
        """Salinan matrix dengan satu sel diubah (immutable-friendly)."""
        action_key = action.value if isinstance(action, MatrixAction) else str(action)
        scope_key = scope.value if isinstance(scope, ActionScope) else str(scope)
        rules = {a: dict(scopes) for a, scopes in self.rules.items()}
        rules.setdefault(action_key, {})
        rules[action_key][scope_key] = normalize_matrix_mode(mode)
        return PermissionMatrix(rules=rules)

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Dict[str, str]]:
        """Representasi kanonik (nilai tersimpan di `permissions.json`)."""
        return {
            action: {
                scope: matrix_mode_value(self.rules[action][scope])
                for scope in MATRIX_SCOPES
            }
            for action in MATRIX_ACTIONS
        }

    def to_ui_dict(self) -> Dict[str, Any]:
        """Representasi + label untuk UI (tanpa hardcode di frontend)."""
        return {
            "actions": [
                {
                    "value": action,
                    "scopes": {
                        scope: {
                            "value": matrix_mode_value(self.rules[action][scope]),
                            "label": MODE_LABELS[
                                matrix_mode_value(self.rules[action][scope])
                            ],
                        }
                        for scope in MATRIX_SCOPES
                    },
                }
                for action in MATRIX_ACTIONS
            ],
            "scopes": list(MATRIX_SCOPES),
        }


# --------------------------------------------------------------------------- #
# Resolusi aksi matrix
# --------------------------------------------------------------------------- #
def resolve_matrix_action(
    action: str,
    action_class: ActionClass,
    arguments: Optional[Mapping[str, Any]] = None,
) -> Optional[MatrixAction]:
    """Map action/tool -> aksi matrix, atau None bila di luar lingkup matrix.

    Read-only -> read_files, write/edit -> modify_files, delete -> delete_files,
    move/rename -> move_files, command -> terminal_read/terminal_mutating.
    Action lain (network/unknown) -> None (memakai policy existing).
    """
    if action_class == ActionClass.READ_ONLY:
        return MatrixAction.READ_FILES
    if action_class == ActionClass.WORKSPACE_WRITE:
        return MatrixAction.MODIFY_FILES
    if action_class == ActionClass.DELETE_MOVE:
        name = (action or "").strip().lower()
        if any(keyword in name for keyword in ("move", "rename")):
            return MatrixAction.MOVE_FILES
        return MatrixAction.DELETE_FILES
    if action_class == ActionClass.COMMAND_EXECUTION:
        command = (arguments or {}).get("command")
        if classify_terminal_command(command) == TERMINAL_READ:
            return MatrixAction.TERMINAL_READ
        return MatrixAction.TERMINAL_MUTATING
    return None


# --------------------------------------------------------------------------- #
# Resolusi scope (inside/outside workspace)
# --------------------------------------------------------------------------- #
#: Nama argumen yang menunjuk path pada tool file AETHER.
_PATH_ARGUMENT_KEYS = (
    "path",
    "file",
    "filepath",
    "file_path",
    "target",
    "source",
    "src",
    "destination",
    "dest",
    "dst",
    "old_path",
    "new_path",
    "dir",
    "directory",
    "folder",
)

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def is_path_within_root(path: Any, workspace_root: Any) -> bool:
    """True bila `path` berada di dalam `workspace_root` (workspace boundary).

    Path relatif di-resolve terhadap root. Path dengan `..` yang keluar root
    dianggap OUTSIDE. Kegagalan resolve -> dianggap INSIDE (aman: boundary tool
    tetap berlaku dan policy tidak menambah penolakan palsu).
    """
    if workspace_root in (None, ""):
        return True
    text = str(path or "").strip().strip("'\"")
    if not text:
        return True
    try:
        root_abs = os.path.abspath(str(workspace_root))
        expanded = os.path.expanduser(text)
        if not os.path.isabs(expanded):
            expanded = os.path.join(root_abs, expanded)
        candidate = os.path.abspath(expanded)
    except Exception:  # noqa: BLE001 - path aneh tidak boleh crash evaluasi
        return True
    if candidate == root_abs:
        return True
    return candidate.startswith(root_abs + os.sep)


def _argument_paths(arguments: Mapping[str, Any]) -> List[str]:
    """Kumpulkan nilai path dari argumen tool (tanpa duplikat)."""
    found: List[str] = []
    for key in _PATH_ARGUMENT_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value)
    return found


def _command_path_tokens(command: Any) -> List[str]:
    """Token path di dalam string command (untuk deteksi path luar workspace)."""
    text = str(command or "")
    tokens = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', text)
    result: List[str] = []
    for token in tokens:
        value = token.strip().strip("'\"")
        # Nilai flag `--file=..\\x` -> ambil bagian setelah '='.
        if value.startswith("-") and "=" in value:
            value = value.split("=", 1)[1]
        if not value:
            continue
        looks_like_path = (
            "/" in value
            or "\\" in value
            or value.startswith("~")
            or value.startswith("..")
            or bool(_DRIVE_RE.match(value))
        )
        if looks_like_path:
            result.append(value)
    return result


def resolve_scope(
    action: str,
    action_class: ActionClass,
    arguments: Optional[Mapping[str, Any]],
    workspace_root: Any,
) -> ActionScope:
    """Tentukan scope action (INSIDE/OUTSIDE workspace).

    - Aksi file: OUTSIDE bila ADA argumen path yang keluar root.
    - Aksi command: OUTSIDE bila `cwd` atau token path di command keluar root.
    - Tanpa workspace_root: INSIDE (tidak dapat dibedakan; boundary tool tetap
      berlaku).
    """
    if workspace_root in (None, ""):
        return ActionScope.INSIDE
    args: Mapping[str, Any] = arguments or {}

    if action_class == ActionClass.COMMAND_EXECUTION:
        cwd = args.get("cwd")
        if isinstance(cwd, str) and cwd.strip() and not is_path_within_root(cwd, workspace_root):
            return ActionScope.OUTSIDE
        for token in _command_path_tokens(args.get("command")):
            if not is_path_within_root(token, workspace_root):
                return ActionScope.OUTSIDE
        return ActionScope.INSIDE

    for path in _argument_paths(args):
        if not is_path_within_root(path, workspace_root):
            return ActionScope.OUTSIDE
    return ActionScope.INSIDE


# --------------------------------------------------------------------------- #
# Klasifikasi terminal (read-only vs mutating)
# --------------------------------------------------------------------------- #
#: Executable yang dinilai READ-ONLY (tidak mengubah workspace).
_READ_ONLY_EXECUTABLES = frozenset(
    {
        # Windows CMD builtins read-only.
        "dir", "type", "echo", "set", "ver", "vol", "date", "time", "path",
        "where", "chcp", "tree", "more", "findstr", "tasklist", "systeminfo",
        "ipconfig", "hostname", "whoami", "assoc", "ftype", "cls", "verify",
        # POSIX read-only (bila tersedia).
        "cat", "ls", "pwd", "head", "tail", "wc", "grep", "env", "printenv",
        "which", "uname", "id", "df", "du", "stat", "file", "printf",
    }
)

#: Subcommand git yang read-only (tidak mengubah working tree).
_GIT_READ_SUBCOMMANDS = frozenset(
    {
        "status", "log", "diff", "show", "rev-parse", "describe", "ls-files",
        "blame", "shortlog", "rev-list", "cat-file", "symbolic-ref", "grep",
    }
)

#: Suffix flag yang menandakan permintaan versi/help (tidak mengubah apa pun).
_VERSION_FLAGS = frozenset({"--version", "-version", "-v", "-V", "--help", "-h", "/?"})

#: Operator shell yang menandakan kemungkinan mutasi (redirect/pipe).
_SHELL_MUTATION_RE = re.compile(r"[<>|]")


def _basename(token: str) -> str:
    """Ambil nama executable tanpa directory & tanpa ekstensi .exe/.cmd/.bat."""
    text = str(token or "").strip().strip("'\"")
    text = text.replace("\\", "/")
    name = text.rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".com", ".ps1"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def _is_read_only_segment(segment: str) -> bool:
    """True bila satu segmen command (tanpa chaining) dipastikan read-only."""
    text = segment.strip()
    if not text:
        return True
    # Redirect/pipe -> berpotensi menulis / tidak pasti -> mutating.
    if _SHELL_MUTATION_RE.search(text):
        return False
    tokens = text.split()
    if not tokens:
        return True
    exe = _basename(tokens[0])
    args = tokens[1:]

    # Permintaan versi/help untuk executable apa pun -> read-only.
    if args and args[-1] in _VERSION_FLAGS:
        return True

    if exe in _READ_ONLY_EXECUTABLES:
        return True

    if exe == "git":
        return _is_read_only_git(args)

    return False


def _is_read_only_git(args: List[str]) -> bool:
    """True bila subcommand git dipastikan read-only (tanpa mutasi)."""
    # `git -C <dir> status` -> lewati flag dengan nilai.
    positional: List[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in ("-C", "--git-dir", "--work-tree", "-c"):
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        positional.append(arg.lower())
    if not positional:
        return False
    sub = positional[0]
    if sub in _GIT_READ_SUBCOMMANDS:
        return True
    rest = positional[1:]
    if sub == "branch":
        return not any(a.startswith("-d") or a.startswith("-D") or a.startswith("-m")
                       or a.startswith("-M") or a.startswith("-c") or a.startswith("-C")
                       or a in ("--delete", "--move", "--copy", "--edit-description")
                       for a in args)
    if sub == "remote":
        return not any(a in ("add", "remove", "rm", "set-url", "rename", "set-head",
                             "set-branches", "prune", "update") for a in rest)
    if sub == "tag":
        return not any(a.startswith("-d") or a.startswith("-a") or a.startswith("-s")
                       or a == "-m" or a.startswith("--delete") for a in args)
    if sub == "config":
        return any(a in ("--get", "--get-all", "--list", "-l", "--get-regexp")
                   for a in args)
    if sub == "stash":
        return rest[:1] == ["list"]
    if sub == "worktree":
        return rest[:1] == ["list"]
    return False


def classify_terminal_command(command: Any) -> str:
    """Klasifikasi command terminal -> "read" (read-only) atau "mutate".

    Deterministik dan KONSERVATIF: hanya command yang dipastikan read-only yang
    dikembalikan sebagai ``read``; selain itu ``mutate`` (default aman).
    """
    text = str(command or "").strip()
    if not text:
        return TERMINAL_MUTATE
    segments = re.split(r"&&|\|\||;|\n", text)
    for segment in segments:
        if not _is_read_only_segment(segment):
            return TERMINAL_MUTATE
    return TERMINAL_READ


# --------------------------------------------------------------------------- #
# Deskripsi target action (untuk UI approval ASK)
# --------------------------------------------------------------------------- #
#: Urutan key argumen yang paling informatif sebagai "target" sebuah action.
_TARGET_ARGUMENT_KEYS = (
    "command",
    "path",
    "file",
    "filepath",
    "file_path",
    "target",
    "destination",
    "dest",
    "dst",
    "new_path",
    "source",
    "src",
    "old_path",
    "url",
    "cwd",
    "dir",
    "directory",
    "folder",
)


def describe_target(arguments: Optional[Mapping[str, Any]]) -> str:
    """Deskripsi target/path sebuah action (untuk ditampilkan saat approval).

    Deterministik, tanpa mengubah makna argumen. Mengembalikan string kosong
    bila tidak ada target yang dapat ditampilkan (mis. action tanpa path).
    """
    args: Mapping[str, Any] = arguments or {}
    if not isinstance(args, Mapping):
        return ""
    for key in _TARGET_ARGUMENT_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


__all__ = [
    "DEFAULT_MATRIX_RULES",
    "MATRIX_ACTIONS",
    "MATRIX_SCOPES",
    "MODE_LABELS",
    "TERMINAL_MUTATE",
    "TERMINAL_READ",
    "PermissionMatrix",
    "classify_terminal_command",
    "describe_target",
    "is_path_within_root",
    "matrix_mode_value",
    "normalize_matrix_mode",
    "resolve_matrix_action",
    "resolve_scope",
]
