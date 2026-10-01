"""Tests: Default Project Permission Matrix + isolasi `permissions.json` per project.

Membuktikan:

- Default Project Policy adalah satu sumber baseline (Project Permission Matrix)
  yang dipakai HANYA saat project baru dibuat, dan untuk project lama yang belum
  punya file policy.
- `ProjectPolicy.default()` mengembalikan baseline matrix tersebut.
- `ProjectPermissionStore.ensure_default()` menginisialisasi
  `<root>/.aether/permissions.json` (matrix default) untuk project BARU dan
  TIDAK menimpa file yang sudah ada (perubahan user tidak di-reset).
- File LEGACY (`{"mode","scope"}`) tetap dibaca (backward compatible).
- `ProjectRegistry.register()` (project baru) membuat `permissions.json` di root
  project BARU, bukan di project lain.
- Isolasi: dua project punya `permissions.json` masing-masing; perubahan satu
  project tidak memengaruhi project lain dan tidak mengubah default.

Isolasi filesystem: memakai `tmp_path` (tidak menyentuh project produksi).
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_ai.permission.matrix import DEFAULT_MATRIX_RULES
from agent_ai.permission.models import ActionScope, MatrixAction, PolicyMode
from agent_ai.projects.permissions import (
    DEFAULT_PROJECT_POLICY_MODE,
    DEFAULT_PROJECT_POLICY_SCOPE,
    PERMISSIONS_FILE_NAME,
    ProjectPermissionStore,
    ProjectPolicy,
)
from agent_ai.projects.registry import ProjectRegistry


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _permissions_path(root: Path) -> Path:
    return root / ".aether" / PERMISSIONS_FILE_NAME


# --------------------------------------------------------------------------- #
# 1. Default Project Policy (baseline matrix)
# --------------------------------------------------------------------------- #
def test_default_policy_constants():
    assert DEFAULT_PROJECT_POLICY_MODE == "allow"
    assert DEFAULT_PROJECT_POLICY_SCOPE == "workspace"


def test_policy_default_is_baseline():
    policy = ProjectPolicy.default()
    assert policy.to_dict() == DEFAULT_MATRIX_RULES
    assert policy.mode_for(MatrixAction.READ_FILES, ActionScope.INSIDE) == PolicyMode.ALLOW
    assert policy.mode_for(MatrixAction.MODIFY_FILES, ActionScope.OUTSIDE) == PolicyMode.DENY
    assert (
        policy.mode_for(MatrixAction.TERMINAL_MUTATING, ActionScope.INSIDE)
        == PolicyMode.REQUIRE_APPROVAL
    )


# --------------------------------------------------------------------------- #
# 2. ensure_default: inisialisasi project baru (idempotent, tidak menimpa)
# --------------------------------------------------------------------------- #
def test_ensure_default_creates_file(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    store = ProjectPermissionStore(root=root)
    assert not store.exists()

    policy = store.ensure_default()
    assert store.exists()
    assert policy.to_dict() == DEFAULT_MATRIX_RULES
    assert _read(_permissions_path(root)) == DEFAULT_MATRIX_RULES


def test_ensure_default_does_not_overwrite_existing(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    store = ProjectPermissionStore(root=root)
    custom = {
        "read_files": {"inside": "deny", "outside": "deny"},
        "modify_files": {"inside": "allow", "outside": "allow"},
        "delete_files": {"inside": "allow", "outside": "allow"},
        "move_files": {"inside": "allow", "outside": "allow"},
        "terminal_read": {"inside": "allow", "outside": "allow"},
        "terminal_mutating": {"inside": "deny", "outside": "deny"},
    }
    store.save(ProjectPolicy.from_dict(custom))

    # Memanggil ensure_default TIDAK boleh mengembalikan policy yang diubah.
    policy = store.ensure_default()
    assert policy.to_dict() == custom
    assert _read(_permissions_path(root)) == custom


# --------------------------------------------------------------------------- #
# 3. Backward compatible: file LEGACY `{"mode","scope"}` tetap dibaca
# --------------------------------------------------------------------------- #
def test_legacy_mode_scope_file_is_migrated(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    path = _permissions_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mode": "deny", "scope": "outside"}), encoding="utf-8")

    policy = ProjectPermissionStore(root=root).load()
    # Mode deny/outside -> mutasi di LUAR workspace ditolak, baca tetap allow.
    assert policy.mode_for(MatrixAction.MODIFY_FILES, ActionScope.OUTSIDE) == PolicyMode.DENY
    assert policy.mode_for(MatrixAction.READ_FILES, ActionScope.INSIDE) == PolicyMode.ALLOW


# --------------------------------------------------------------------------- #
# 4. Project baru -> permissions.json di project yang BARU dibuat
# --------------------------------------------------------------------------- #
def test_register_new_project_creates_permissions(tmp_path):
    workspace = tmp_path / "ws"
    root = tmp_path / "new_project"
    root.mkdir()
    registry = ProjectRegistry(workspace=workspace)

    config = registry.register(name="New", root=str(root))

    path = _permissions_path(root)
    assert path.is_file(), f"permissions.json harus dibuat di {path}"
    assert _read(path) == DEFAULT_MATRIX_RULES
    # File berada di root project baru, bukan di workspace AETHER.
    assert not (workspace / config.id / PERMISSIONS_FILE_NAME).exists()


def test_register_does_not_touch_other_project(tmp_path):
    workspace = tmp_path / "ws"
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    registry = ProjectRegistry(workspace=workspace)

    registry.register(name="A", root=str(root_a))

    # Project B belum dibuat -> tidak ada file policy yang bocor ke sana.
    assert not _permissions_path(root_b).exists()


# --------------------------------------------------------------------------- #
# 5. Isolasi: setiap project punya permissions.json sendiri
# --------------------------------------------------------------------------- #
def test_each_project_has_isolated_permissions(tmp_path):
    workspace = tmp_path / "ws"
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    registry = ProjectRegistry(workspace=workspace)

    registry.register(name="A", root=str(root_a))
    registry.register(name="B", root=str(root_b))

    path_a = _permissions_path(root_a)
    path_b = _permissions_path(root_b)
    assert path_a.is_file() and path_b.is_file()
    assert path_a != path_b

    # Ubah policy project A -> B tidak terpengaruh.
    deny_all = {
        action: {"inside": "deny", "outside": "deny"} for action in DEFAULT_MATRIX_RULES
    }
    ProjectPermissionStore(root=root_a).save(ProjectPolicy.from_dict(deny_all))
    assert _read(path_a) == deny_all
    assert _read(path_b) == DEFAULT_MATRIX_RULES


def test_policy_change_does_not_mutate_default(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    store = ProjectPermissionStore(root=root)
    store.ensure_default()
    store.save(
        ProjectPolicy.from_dict(
            {
                action: {"inside": "require_approval", "outside": "outside"}
                if action == "read_files"
                else {"inside": "allow", "outside": "deny"}
                for action in DEFAULT_MATRIX_RULES
            }
        )
    )

    # Default baseline tetap sama setelah perubahan policy project.
    assert ProjectPolicy.default().to_dict() == DEFAULT_MATRIX_RULES
    # Project baru berikutnya tetap memakai default.
    other = tmp_path / "other"
    other.mkdir()
    other_store = ProjectPermissionStore(root=other)
    assert other_store.ensure_default().to_dict() == DEFAULT_MATRIX_RULES
