"""Tests: Default Project Policy AETHER + isolasi `permissions.json` per project.

Membuktikan:

- Default Project Policy adalah satu sumber baseline (mode=allow, scope=workspace)
  yang dipakai HANYA saat project baru dibuat.
- `ProjectPolicy.default()` mengembalikan baseline tersebut.
- `ProjectPermissionStore.ensure_default()` menginisialisasi
  `<root>/.aether/permissions.json` untuk project BARU dan TIDAK menimpa file
  yang sudah ada (perubahan user tidak di-reset).
- `ProjectRegistry.register()` (project baru) membuat `permissions.json` di root
  project BARU, bukan di project lain.
- Isolasi: dua project punya `permissions.json` masing-masing; perubahan satu
  project tidak memengaruhi project lain dan tidak mengubah default.

Isolasi filesystem: memakai `tmp_path` (tidak menyentuh project produksi).
"""

from __future__ import annotations

import json
from pathlib import Path

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
# 1. Default Project Policy (baseline)
# --------------------------------------------------------------------------- #
def test_default_policy_constants():
    assert DEFAULT_PROJECT_POLICY_MODE == "allow"
    assert DEFAULT_PROJECT_POLICY_SCOPE == "workspace"


def test_policy_default_is_baseline():
    policy = ProjectPolicy.default()
    assert policy.mode == "allow"
    assert policy.scope == "workspace"
    assert policy.to_dict() == {"mode": "allow", "scope": "workspace"}


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
    assert policy.to_dict() == {"mode": "allow", "scope": "workspace"}
    assert _read(_permissions_path(root)) == {"mode": "allow", "scope": "workspace"}


def test_ensure_default_does_not_overwrite_existing(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    store = ProjectPermissionStore(root=root)
    store.save(ProjectPolicy(mode="deny", scope="outside"))

    # Memanggil ensure_default TIDAK boleh mengembalikan policy yang diubah.
    policy = store.ensure_default()
    assert policy.to_dict() == {"mode": "deny", "scope": "outside"}
    assert _read(_permissions_path(root)) == {"mode": "deny", "scope": "outside"}


# --------------------------------------------------------------------------- #
# 3. Project baru -> permissions.json di project yang BARU dibuat
# --------------------------------------------------------------------------- #
def test_register_new_project_creates_permissions(tmp_path):
    workspace = tmp_path / "ws"
    root = tmp_path / "new_project"
    root.mkdir()
    registry = ProjectRegistry(workspace=workspace)

    config = registry.register(name="New", root=str(root))

    path = _permissions_path(root)
    assert path.is_file(), f"permissions.json harus dibuat di {path}"
    assert _read(path) == {"mode": "allow", "scope": "workspace"}
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
# 4. Isolasi: setiap project punya permissions.json sendiri
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
    ProjectPermissionStore(root=root_a).save(ProjectPolicy(mode="deny", scope="outside"))
    assert _read(path_a) == {"mode": "deny", "scope": "outside"}
    assert _read(path_b) == {"mode": "allow", "scope": "workspace"}


def test_policy_change_does_not_mutate_default(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    store = ProjectPermissionStore(root=root)
    store.ensure_default()
    store.save(ProjectPolicy(mode="require_approval", scope="outside"))

    # Default baseline tetap sama setelah perubahan policy project.
    assert ProjectPolicy.default().to_dict() == {
        "mode": "allow",
        "scope": "workspace",
    }
    # Project baru berikutnya tetap memakai default.
    other = tmp_path / "other"
    other.mkdir()
    other_store = ProjectPermissionStore(root=other)
    assert other_store.ensure_default().to_dict() == {
        "mode": "allow",
        "scope": "workspace",
    }
