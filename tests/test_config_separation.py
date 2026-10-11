"""Tests: PEMISAHAN KONFIGURASI AETHER (Global Settings vs Project Policy).

Membuktikan bahwa dua surface konfigurasi TIDAK PERNAH tercampur dan tetap
memakai sumber konfigurasi existing masing-masing:

    - Global Settings AETHER  -> `data/settings.json`
      (loader `agent_ai.config.settings`).
    - Project Settings/Policy -> `<root>/.aether/settings/permissions.json`
      (`agent_ai.projects.permissions.ProjectPermissionStore`).

Yang diuji:
    1. Global Settings MENOLAK key policy/permission project (mode/scope/...),
       dan file global TIDAK berubah.
    2. Project Policy MENOLAK key Global Settings (port/compression/...),
       dan file policy project TIDAK berubah.
    3. Perubahan di satu surface TIDAK menyentuh file surface lain.
    4. Isolasi antar-project dipertahankan.

Isolasi: `SETTINGS_PATH` diarahkan ke file sementara (`tmp_path`) dan registry
project memakai `tmp_path` — tidak menyentuh `data/settings.json`,
`data/aether.db`, maupun project produksi.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT / "src"), str(PROJECT_ROOT / "web" / "django_app")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_ai.config import settings as settings_mod
from agent_ai.config.settings import (
    SettingsWriteError,
    global_settings,
    update_global_settings,
)
from agent_ai.permission.matrix import DEFAULT_MATRIX_RULES
from agent_ai.projects.permissions import (
    PERMISSIONS_FILE_NAME,
    SETTINGS_DIR_NAME,
    ProjectPermissionStore,
    ProjectPolicy,
)
from agent_ai.projects.registry import ProjectRegistry


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _policy_path(root: Path) -> Path:
    """Lokasi KANONIK policy project (`.aether/settings/permissions.json`)."""
    return root / ".aether" / SETTINGS_DIR_NAME / PERMISSIONS_FILE_NAME


#: Matrix custom (semua deny) untuk menguji pemisahan konfigurasi.
_DENY_ALL_MATRIX = {
    action: {"inside": "deny", "outside": "deny"} for action in DEFAULT_MATRIX_RULES
}


# --------------------------------------------------------------------------- #
# 1. Sumber konfigurasi tetap TERPISAH (dua file berbeda)
# --------------------------------------------------------------------------- #
def test_global_and_project_sources_are_different_files(tmp_path):
    store = ProjectPermissionStore(root=tmp_path / "proj")
    assert store.path.name == PERMISSIONS_FILE_NAME == "permissions.json"
    assert settings_mod.SETTINGS_PATH.name == "settings.json"
    assert store.path != settings_mod.SETTINGS_PATH


# --------------------------------------------------------------------------- #
# 2. Global Settings MENOLAK key Project Policy
# --------------------------------------------------------------------------- #
@pytest.fixture()
def settings_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"port": 8123, "api_retry": {"failed_count": 9}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", path)
    return path


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "deny"},
        {"scope": "outside"},
        {"policy": {}},
        {"permission": "allow"},
        {"permissions": {"mode": "deny"}},
        {"mode": "deny", "port": 9000},
    ],
)
def test_global_settings_rejects_project_policy_keys(settings_file, payload):
    before = _read(settings_file)
    with pytest.raises(SettingsWriteError) as exc:
        update_global_settings(payload)
    # Pesan mengarahkan ke Project Settings / Policy (clear separation).
    assert "Project Settings" in str(exc.value)
    # File global TIDAK berubah (tidak menyimpan policy project).
    assert _read(settings_file) == before
    for key in ("mode", "scope", "policy", "permission", "permissions"):
        assert key not in _read(settings_file)


def test_global_settings_still_accepts_global_keys(settings_file):
    update_global_settings({"port": 9100, "compression": {"enabled": True}})
    stored = _read(settings_file)
    assert stored["port"] == 9100
    assert stored["compression"]["enabled"] is True
    # Key global lain (tanpa kontrol UI) tetap utuh.
    assert stored["api_retry"] == {"failed_count": 9}
    assert global_settings()["port"] == 9100


# --------------------------------------------------------------------------- #
# 3. Project Policy tetap project-local & terpisah dari file global
# --------------------------------------------------------------------------- #
def test_project_policy_writes_only_project_file(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    store = ProjectPermissionStore(root=root)
    store.save(ProjectPolicy.from_dict(_DENY_ALL_MATRIX))
    stored = _read(root / ".aether" / SETTINGS_DIR_NAME / PERMISSIONS_FILE_NAME)
    # Hanya aksi matrix yang tersimpan: TIDAK ada key Global Settings.
    assert set(stored) == set(DEFAULT_MATRIX_RULES), stored
    assert stored == _DENY_ALL_MATRIX


def test_project_policy_does_not_touch_settings_json(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"port": 8000}), encoding="utf-8")
    before = _read(settings_path)

    store = ProjectPermissionStore(root=tmp_path / "proj")
    store.save(ProjectPolicy.default())

    assert _read(settings_path) == before


def test_global_settings_does_not_touch_project_policy(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"port": 8000}), encoding="utf-8")
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", settings_path)

    root = tmp_path / "proj"
    root.mkdir()
    store = ProjectPermissionStore(root=root)
    store.save(ProjectPolicy.from_dict(_DENY_ALL_MATRIX))
    policy_path = root / ".aether" / SETTINGS_DIR_NAME / PERMISSIONS_FILE_NAME
    before = _read(policy_path)

    update_global_settings({"port": 9999})

    assert _read(policy_path) == before
    assert _read(settings_path)["port"] == 9999


# --------------------------------------------------------------------------- #
# 4. Isolasi antar-project (registry) dipertahankan
# --------------------------------------------------------------------------- #
def test_policies_isolated_across_projects(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    registry = ProjectRegistry(workspace=tmp_path / "ws")
    registry.register(name="A", root=str(root_a))
    registry.register(name="B", root=str(root_b))

    ProjectPermissionStore(root=root_a).save(ProjectPolicy.from_dict(_DENY_ALL_MATRIX))

    path_a = root_a / ".aether" / SETTINGS_DIR_NAME / PERMISSIONS_FILE_NAME
    path_b = root_b / ".aether" / SETTINGS_DIR_NAME / PERMISSIONS_FILE_NAME
    assert path_a != path_b
    assert _read(path_a) == _DENY_ALL_MATRIX
    assert _read(path_b) == DEFAULT_MATRIX_RULES


# --------------------------------------------------------------------------- #
# 5. Layer gateway: endpoint Policy menolak key Global Settings
# --------------------------------------------------------------------------- #
def _ensure_django() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
    import django

    try:
        django.setup()
    except RuntimeError:
        pass


def test_gateway_policy_rejects_global_settings_keys(tmp_path, monkeypatch):
    _ensure_django()
    import api.services as services_mod
    from api.project_store import ProjectStore

    from agent_ai.llm_config import LLMConfigService

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"port": 8000}), encoding="utf-8")
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", settings_path)

    root = tmp_path / "proj"
    root.mkdir()
    service = services_mod.GatewayService(
        project_registry=ProjectRegistry(workspace=tmp_path / "ws"),
        project_store=ProjectStore(db_path=tmp_path / "store.db"),
        llm_config_service=LLMConfigService(
            db_path=tmp_path / "llm.db", env_path=tmp_path / ".env"
        ),
        auto_execute=False,
    )
    project = service.create_project(name="P", path=str(root))
    pid = project["id"]
    # Policy valid -> tersimpan.
    service.save_project_policy(pid, _DENY_ALL_MATRIX)
    policy_path = root / ".aether" / SETTINGS_DIR_NAME / PERMISSIONS_FILE_NAME
    before = _read(policy_path)

    # Key Global Settings ditolak (400 ValidationError) & file policy tidak berubah.
    for bad in ({"port": 9000}, {"compression": {"enabled": True}}, {"port": 1}):
        with pytest.raises(services_mod.ValidationError):
            service.save_project_policy(pid, bad)
    # Nilai matrix tidak valid juga ditolak.
    for bad in (
        {"read_files": {"inside": "ngawur"}},
        {"aksi_aneh": {"inside": "allow"}},
        {"read_files": {"kemana_mana": "allow"}},
    ):
        with pytest.raises(services_mod.ValidationError):
            service.save_project_policy(pid, bad)
    assert _read(policy_path) == before
    # settings.json tidak tersentuh.
    assert _read(settings_path) == {"port": 8000}
