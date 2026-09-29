"""Task 06 — Extension Lifecycle + Git Installer validation (38 points)."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from agent_ai.extensions.api_version import CURRENT_API_VERSION
from agent_ai.extensions.base import Extension
from agent_ai.extensions.capabilities import CapabilityRegistry, CapabilityValidationError
from agent_ai.extensions.config import ConfigValueStore
from agent_ai.extensions.context import ExtensionContext
from agent_ai.extensions.lifecycle import ExtensionLifecycleStore
from agent_ai.extensions.loader import ExtensionLoader
from agent_ai.extensions.manager import ExtensionManager
from agent_ai.extensions.manifest import Manifest
from agent_ai.extensions.registry import ExtensionRegistry
from agent_ai.extensions.storage import ExtensionStorage
from agent_ai.tools.base import BaseTool
from agent_ai.tools.registry import ToolRegistry


def _manifest(ext_id: str, version: str = "1.0.0", api_version: str = "1", extra: dict = None) -> Manifest:
    raw = {}
    if extra:
        raw.update(extra)
    return Manifest(id=ext_id, name="Test", version=version, description="Desc", api_version=api_version, raw=raw or {}, source_path="")


def _make_extension_dir(base: Path, folder: str, manifest: dict, extension_py: str, pyproject: str = None):
    d = base / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps(manifest))
    (d / "extension.py").write_text(extension_py)
    (d / "__init__.py").write_text("")
    if pyproject is None:
        pyproject = '[project]\nname = "x"\nversion = "0.1.0"\ndependencies = []\n'
    (d / "pyproject.toml").write_text(pyproject)
    return d


def _fresh_manager(tmp_ext_dir: Path, tmp_db: Path, cap_reg=None, tool_reg=None):
    reg = ExtensionRegistry()
    cap = cap_reg if cap_reg is not None else CapabilityRegistry()
    tr = tool_reg if tool_reg is not None else ToolRegistry()
    lc = ExtensionLifecycleStore(str(tmp_db))
    lc.clear_all()
    manager = ExtensionManager(
        registry=reg,
        capability_registry=cap,
        lifecycle_store=lc,
        extensions_dir=tmp_ext_dir,
        tool_registry=tr,
        config_store=ConfigValueStore(str(tmp_db)),
    )
    return manager, reg, cap, lc, tr


def _init_git_repo(repo_path: Path, manifest: dict, extension_py: str, pyproject: str = None):
    repo_path.mkdir(parents=True, exist_ok=True)
    _make_extension_dir(repo_path, ".", manifest, extension_py, pyproject=pyproject)
    # Actually _make_extension_dir creates subfolder; we want repo_path itself to contain files
    # So we created repo_path/. with files; but we passed "." -> d = repo_path / "." = repo_path
    # So files are at repo_path/
    subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_path), capture_output=True, check=True)
    return repo_path


# ---------- Lifecycle ----------

def test_01_existing_extension_default_enabled(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    _make_extension_dir(ext_dir, "test-ext", {"id": "aether.test-existing", "name": "T", "version": "1.0", "description": "D", "api_version": "1"},
                        'from agent_ai.extensions import Extension\nextension = Extension()\n')
    db = tmp_path / "lc.db"
    lc = ExtensionLifecycleStore(str(db))
    lc.clear_all()
    # No record -> default enabled true
    assert lc.is_enabled("aether.test-existing") is True
    # After loader startup, registry loaded, lifecycle still enabled
    reg = ExtensionRegistry()
    cap = CapabilityRegistry()
    loader = ExtensionLoader(registry=reg, extensions_dir=ext_dir, enable_entry_points=False, capability_registry=cap, lifecycle_store=lc)
    res = loader.load_all()
    assert res.count_loaded == 1
    assert lc.is_enabled("aether.test-existing") is True

def test_02_disable_persistent(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    _make_extension_dir(ext_dir, "my-ext", {"id": "test.disable-persist", "name": "T", "version": "1.0", "description": "D", "api_version": "1"},
                        'from agent_ai.extensions import Extension\nclass E(Extension):\n    def register(self, ctx):\n        ctx.tools.register("test.disable-persist.tool1")\nextension = E()\n')
    db = tmp_path / "lc.db"
    manager, reg, cap, lc, tr = _fresh_manager(ext_dir, db)
    loader = ExtensionLoader(registry=reg, extensions_dir=ext_dir, enable_entry_points=False, capability_registry=cap, lifecycle_store=lc, tool_registry=tr)
    loader.load_all()
    # Initially enabled
    assert lc.is_enabled("test.disable-persist") is True
    # Disable via manager
    manager.disable("test.disable-persist")
    assert lc.is_enabled("test.disable-persist") is False
    # Simulate restart with new store instance same db
    lc2 = ExtensionLifecycleStore(str(db))
    assert lc2.is_enabled("test.disable-persist") is False

def test_03_enable_persistent(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    _make_extension_dir(ext_dir, "my-ext", {"id": "test.enable-persist", "name": "T", "version": "1.0", "description": "D", "api_version": "1"},
                        'from agent_ai.extensions import Extension\nextension = Extension()\n')
    db = tmp_path / "lc.db"
    manager, reg, cap, lc, _ = _fresh_manager(ext_dir, db)
    loader = ExtensionLoader(registry=reg, extensions_dir=ext_dir, enable_entry_points=False, capability_registry=cap, lifecycle_store=lc)
    loader.load_all()
    manager.disable("test.enable-persist")
    assert lc.is_enabled("test.enable-persist") is False
    manager.enable("test.enable-persist")
    assert lc.is_enabled("test.enable-persist") is True
    lc2 = ExtensionLifecycleStore(str(db))
    assert lc2.is_enabled("test.enable-persist") is True

def test_04_disable_not_delete_config(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    db = tmp_path / "lc.db"
    store = ConfigValueStore(str(db))
    cap = CapabilityRegistry()
    manifest = _manifest("test.cfg-keep")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap, config_store=store)
    ctx.config.register(key="api_key", type="string", default="def")
    ctx.config.set("api_key", "kept")
    assert ctx.config.get("api_key") == "kept"
    manager, reg, cap2, lc, _ = _fresh_manager(ext_dir, db, cap_reg=cap)
    # Simulate extension loaded
    _make_extension_dir(ext_dir, "cfg-ext", {"id": "test.cfg-keep", "name": "T", "version": "1.0", "description": "D", "api_version": "1"},
                        'from agent_ai.extensions import Extension\nextension = Extension()\n')
    reg2 = ExtensionRegistry()
    loader = ExtensionLoader(registry=reg, extensions_dir=ext_dir, enable_entry_points=False, capability_registry=cap, lifecycle_store=lc)
    loader.load_all()
    # Disable should not delete config
    # But we need registry has the extension
    m2 = ExtensionRegistry()
    loader2 = ExtensionLoader(registry=m2, extensions_dir=ext_dir, enable_entry_points=False, capability_registry=CapabilityRegistry(), lifecycle_store=ExtensionLifecycleStore(str(db)))
    # Use manager's registry which already has disable logic
    # Ensure disable doesn't delete config value
    manager.disable("test.cfg-keep")
    assert ctx.config.get("api_key") == "kept"

def test_05_disable_not_delete_state(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    db = tmp_path / "lc.db"
    manager, reg, cap, lc, _ = _fresh_manager(ext_dir, db)
    _make_extension_dir(ext_dir, "state-ext", {"id": "test.state-keep", "name": "T", "version": "1.0", "description": "D", "api_version": "1"},
                        'from agent_ai.extensions import Extension\nextension = Extension()\n')
    loader = ExtensionLoader(registry=reg, extensions_dir=ext_dir, enable_entry_points=False, capability_registry=cap, lifecycle_store=lc)
    loader.load_all()
    storage = ExtensionStorage("test.state-keep", aether_root=tmp_path)
    storage.set("prefs", {"theme": "dark"})
    manager.disable("test.state-keep")
    assert storage.get("prefs") == {"theme": "dark"}

def test_06_disabled_capability_not_active(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    db = tmp_path / "lc.db"
    cap = CapabilityRegistry()
    tr = ToolRegistry()
    manager, reg, cap, lc, tr = _fresh_manager(ext_dir, db, cap_reg=cap, tool_reg=tr)
    _make_extension_dir(ext_dir, "cap-ext", {"id": "test.cap-active", "name": "T", "version": "1.0", "description": "D", "api_version": "1"},
                        '''from agent_ai.extensions import Extension
from agent_ai.tools.base import BaseTool
class T(BaseTool):
    name = "test.cap-active.tool1"
    description = "d"
    input_schema = {"type":"object","properties":{}}
    def execute(self, **kw): return "ok"
class E(Extension):
    def register(self, ctx):
        ctx.tools.register(T())
        ctx.skills.register("test.cap-active.skill1", name="S")
extension = E()
''')
    loader = ExtensionLoader(registry=reg, extensions_dir=ext_dir, enable_entry_points=False, capability_registry=cap, lifecycle_store=lc, tool_registry=tr)
    loader.load_all()
    # Initially active
    assert cap.is_enabled("tool", "test.cap-active.tool1") is True
    assert cap.is_enabled("skill", "test.cap-active.skill1") is True
    assert tr.has("test.cap-active.tool1")
    # Disable
    manager.disable("test.cap-active")
    assert cap.is_enabled("tool", "test.cap-active.tool1") is False
    assert cap.is_enabled("skill", "test.cap-active.skill1") is False
    assert not tr.has("test.cap-active.tool1")

def test_07_enable_reactivates_capability(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    db = tmp_path / "lc.db"
    cap = CapabilityRegistry()
    tr = ToolRegistry()
    manager, reg, cap, lc, tr = _fresh_manager(ext_dir, db, cap_reg=cap, tool_reg=tr)
    _make_extension_dir(ext_dir, "cap-ext2", {"id": "test.cap-react", "name": "T", "version": "1.0", "description": "D", "api_version": "1"},
                        '''from agent_ai.extensions import Extension
class E(Extension):
    def register(self, ctx):
        ctx.tools.register("test.cap-react.tool1")
        ctx.skills.register("test.cap-react.skill1", name="S")
extension = E()
''')
    loader = ExtensionLoader(registry=reg, extensions_dir=ext_dir, enable_entry_points=False, capability_registry=cap, lifecycle_store=lc, tool_registry=tr)
    loader.load_all()
    manager.disable("test.cap-react")
    assert cap.is_enabled("tool", "test.cap-react.tool1") is False
    manager.enable("test.cap-react")
    assert cap.is_enabled("tool", "test.cap-react.tool1") is True
    assert cap.is_enabled("skill", "test.cap-react.skill1") is True

def test_08_on_enable_called(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    db = tmp_path / "lc.db"
    cap = CapabilityRegistry()
    tr = ToolRegistry()
    manager, reg, cap, lc, tr = _fresh_manager(ext_dir, db, cap_reg=cap, tool_reg=tr)
    _make_extension_dir(ext_dir, "enable-hook", {"id": "test.on-enable", "name": "T", "version": "1.0", "description": "D", "api_version": "1"},
                        '''from agent_ai.extensions import Extension
from pathlib import Path
class E(Extension):
    def register(self, ctx):
        ctx.tools.register("test.on-enable.tool1")
    def on_enable(self, ctx):
        Path(ctx.extension_root / "enable_called.txt").write_text("yes")
extension = E()
''')
    loader = ExtensionLoader(registry=reg, extensions_dir=ext_dir, enable_entry_points=False, capability_registry=cap, lifecycle_store=lc, tool_registry=tr)
    loader.load_all()
    manager.disable("test.on-enable")
    manager.enable("test.on-enable")
    assert (ext_dir / "enable-hook" / "enable_called.txt").exists()

def test_09_on_disable_called(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    db = tmp_path / "lc.db"
    manager, reg, cap, lc, tr = _fresh_manager(ext_dir, db)
    _make_extension_dir(ext_dir, "disable-hook", {"id": "test.on-disable", "name": "T", "version": "1.0", "description": "D", "api_version": "1"},
                        '''from agent_ai.extensions import Extension
from pathlib import Path
class E(Extension):
    def register(self, ctx):
        ctx.tools.register("test.on-disable.tool1")
    def on_disable(self, ctx):
        Path(ctx.extension_root / "disable_called.txt").write_text("yes")
extension = E()
''')
    loader = ExtensionLoader(registry=reg, extensions_dir=ext_dir, enable_entry_points=False, capability_registry=cap, lifecycle_store=lc, tool_registry=tr)
    loader.load_all()
    manager.disable("test.on-disable")
    assert (ext_dir / "disable-hook" / "disable_called.txt").exists()

def test_10_lifecycle_failure_not_crash(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    db = tmp_path / "lc.db"
    manager, reg, cap, lc, tr = _fresh_manager(ext_dir, db)
    _make_extension_dir(ext_dir, "fail-enable", {"id": "test.fail-enable", "name": "T", "version": "1.0", "description": "D", "api_version": "1"},
                        '''from agent_ai.extensions import Extension
class E(Extension):
    def register(self, ctx):
        ctx.tools.register("test.fail-enable.tool1")
    def on_enable(self, ctx):
        raise RuntimeError("enable boom")
extension = E()
''')
    _make_extension_dir(ext_dir, "good", {"id": "test.good", "name": "T", "version": "1.0", "description": "D", "api_version": "1"},
                        '''from agent_ai.extensions import Extension
class E(Extension):
    def register(self, ctx):
        ctx.tools.register("test.good.tool1")
extension = E()
''')
    loader = ExtensionLoader(registry=reg, extensions_dir=ext_dir, enable_entry_points=False, capability_registry=cap, lifecycle_store=lc, tool_registry=tr)
    loader.load_all()
    # Disable then enable failing
    manager.disable("test.fail-enable")
    with pytest.raises(Exception, match="enable hook failed"):
        manager.enable("test.fail-enable")
    # Should be marked failed and not active
    status = manager.get_status("test.fail-enable")
    assert status["status"] == "failed"
    assert status["enabled"] is False
    # New manager for second scenario - on_disable failure isolation
    ext_dir2 = tmp_path / "exts2"
    ext_dir2.mkdir()
    _make_extension_dir(ext_dir2, "fd", {"id": "test.fail-disable2", "name": "T", "version": "1.0", "description": "D", "api_version": "1"},
                        '''from agent_ai.extensions import Extension
class E(Extension):
    def register(self, ctx):
        ctx.tools.register("test.fail-disable2.tool1")
    def on_disable(self, ctx):
        raise RuntimeError("disable boom 2")
extension = E()
''')
    db2 = tmp_path / "lc2.db"
    manager2, reg2, cap2, lc2, _ = _fresh_manager(ext_dir2, db2)
    loader2 = ExtensionLoader(registry=reg2, extensions_dir=ext_dir2, enable_entry_points=False, capability_registry=cap2, lifecycle_store=lc2)
    loader2.load_all()
    result = manager2.disable("test.fail-disable2")
    assert result["status"] in ("disabled", "failed")
    # Check error logged
    acts = lc2.list_activity("test.fail-disable2")
    assert any("disable" in str(a).lower() or "boom" in str(a).lower() for a in acts)

def test_11_failed_extension_not_block_others(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    _make_extension_dir(ext_dir, "good-a", {"id": "test.good-a", "name": "T", "version": "1.0", "description": "D", "api_version": "1"},
                        'from agent_ai.extensions import Extension\nclass E(Extension):\n    def register(self, ctx):\n        ctx.tools.register("test.good-a.tool1")\nextension = E()\n')
    _make_extension_dir(ext_dir, "bad", {"id": "test.bad-ext", "name": "T", "version": "1.0", "description": "D", "api_version": "1"},
                        'from agent_ai.extensions import Extension\nclass E(Extension):\n    def register(self, ctx):\n        raise RuntimeError("boom")\nextension = E()\n')
    _make_extension_dir(ext_dir, "good-c", {"id": "test.good-c", "name": "T", "version": "1.0", "description": "D", "api_version": "1"},
                        'from agent_ai.extensions import Extension\nclass E(Extension):\n    def register(self, ctx):\n        ctx.tools.register("test.good-c.tool1")\nextension = E()\n')
    db = tmp_path / "lc.db"
    reg = ExtensionRegistry()
    cap = CapabilityRegistry()
    lc = ExtensionLifecycleStore(str(db))
    loader = ExtensionLoader(registry=reg, extensions_dir=ext_dir, enable_entry_points=False, capability_registry=cap, lifecycle_store=lc)
    res = loader.load_all()
    assert res.count_loaded == 2
    assert res.count_failed == 1
    assert reg.exists("test.good-a")
    assert reg.exists("test.good-c")
    assert not reg.exists("test.bad-ext")


# ---------- Install ----------

def test_12_install_valid_git_repository(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo = tmp_path / "repo-valid"
    _init_git_repo(repo, {"id": "test.install-valid", "name": "Valid", "version": "1.0.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nclass E(Extension):\n    def register(self, ctx):\n        ctx.tools.register("test.install-valid.tool1")\nextension = E()\n')
    db = tmp_path / "lc.db"
    manager, reg, cap, lc, tr = _fresh_manager(ext_dir, db)
    result = manager.install(str(repo))
    assert result["id"] == "test.install-valid"
    assert result["status"] in ("enabled", "loaded")
    assert (ext_dir / repo.name).exists()
    assert reg.exists("test.install-valid")

def test_13_manifest_validation(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo = tmp_path / "repo-bad-manifest"
    # Missing required field version
    _init_git_repo(repo, {"id": "bad.manifest", "name": "Bad"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    db = tmp_path / "lc.db"
    manager, _, _, _, _ = _fresh_manager(ext_dir, db)
    with pytest.raises(Exception, match="Invalid Extension manifest"):
        manager.install(str(repo))
    # No partial installation left
    assert not (ext_dir / repo.name).exists()

def test_14_api_version_compatibility(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo = tmp_path / "repo-bad-api"
    _init_git_repo(repo, {"id": "test.bad-api", "name": "BadAPI", "version": "1.0", "description": "D", "api_version": "999"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    db = tmp_path / "lc.db"
    manager, _, _, _, _ = _fresh_manager(ext_dir, db)
    with pytest.raises(Exception) as excinfo:
        manager.install(str(repo))
    msg = str(excinfo.value)
    assert "API version" in msg
    assert "999" in msg
    assert CURRENT_API_VERSION in msg
    assert not (ext_dir / repo.name).exists()

def test_15_duplicate_extension_id_rejected(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo1 = tmp_path / "repo-dup1"
    _init_git_repo(repo1, {"id": "test.dup-id", "name": "Dup1", "version": "1.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    repo2 = tmp_path / "repo-dup2"
    _init_git_repo(repo2, {"id": "test.dup-id", "name": "Dup2", "version": "1.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    db = tmp_path / "lc.db"
    manager, _, _, _, _ = _fresh_manager(ext_dir, db)
    manager.install(str(repo1))
    with pytest.raises(Exception, match="Duplicate Extension"):
        manager.install(str(repo2))
    # Folder collision also tested but duplicate is distinct
    assert len(list(ext_dir.iterdir())) == 1

def test_16_folder_collision_rejected(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    # Create existing folder exts/foo
    _make_extension_dir(ext_dir, "foo", {"id": "test.existing-foo", "name": "E", "version": "1.0", "description": "D", "api_version": "1"},
                        'from agent_ai.extensions import Extension\nextension = Extension()\n')
    repo = tmp_path / "foo"  # same folder name as existing
    _init_git_repo(repo, {"id": "test.new-foo", "name": "NewFoo", "version": "1.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    db = tmp_path / "lc.db"
    # Need registry that knows existing extension
    reg = ExtensionRegistry()
    cap = CapabilityRegistry()
    lc = ExtensionLifecycleStore(str(db))
    lc.clear_all()
    loader = ExtensionLoader(registry=reg, extensions_dir=ext_dir, enable_entry_points=False, capability_registry=cap, lifecycle_store=lc)
    loader.load_all()
    manager = ExtensionManager(registry=reg, capability_registry=cap, lifecycle_store=lc, extensions_dir=ext_dir, tool_registry=ToolRegistry())
    with pytest.raises(Exception, match="folder collision"):
        manager.install(str(repo))
    # Existing folder still intact, not deleted
    assert (ext_dir / "foo").exists()
    assert (ext_dir / "foo" / "manifest.json").exists()

def test_17_invalid_package_rejected(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo = tmp_path / "repo-invalid-pkg"
    repo.mkdir()
    (repo / "manifest.json").write_text(json.dumps({"id": "test.invalid-pkg", "name": "T", "version": "1.0", "description": "D", "api_version": "1"}))
    (repo / "extension.py").write_text('from agent_ai.extensions import Extension\nextension = Extension()\n')
    # Missing pyproject.toml and __init__.py -> should fail structure validation
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True)
    db = tmp_path / "lc.db"
    manager, _, _, _, _ = _fresh_manager(ext_dir, db)
    with pytest.raises(Exception, match="package structure"):
        manager.install(str(repo))
    assert not (ext_dir / repo.name).exists()

def test_18_dependency_failure_no_partial(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo = tmp_path / "repo-dep-fail"
    _init_git_repo(repo, {"id": "test.dep-fail", "name": "DepFail", "version": "1.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n',
                   pyproject='[project]\nname="x"\nversion="0.1.0"\ndependencies=["__fail_dependency_install__"]\n')
    db = tmp_path / "lc.db"
    manager, reg, _, _, _ = _fresh_manager(ext_dir, db)
    with pytest.raises(Exception, match="dependency"):
        manager.install(str(repo))
    assert not (ext_dir / repo.name).exists()
    assert not reg.exists("test.dep-fail")

def test_19_installed_extension_in_registry(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo = tmp_path / "repo-reg-check"
    _init_git_repo(repo, {"id": "test.in-registry", "name": "Reg", "version": "1.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nclass E(Extension):\n    def register(self, ctx):\n        ctx.tools.register("test.in-registry.tool1")\nextension = E()\n')
    db = tmp_path / "lc.db"
    manager, reg, cap, _, _ = _fresh_manager(ext_dir, db)
    manager.install(str(repo))
    assert reg.exists("test.in-registry")
    assert cap.exists("tool", "test.in-registry.tool1")

def test_20_installed_extension_can_load_if_not_active(tmp_path):
    # Install new extension that was not previously loaded; verify it registers immediately
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo = tmp_path / "repo-load-now"
    _init_git_repo(repo, {"id": "test.load-now", "name": "LoadNow", "version": "1.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nclass E(Extension):\n    def register(self, ctx):\n        ctx.tools.register("test.load-now.tool1")\nextension = E()\n')
    db = tmp_path / "lc.db"
    manager, reg, cap, _, _ = _fresh_manager(ext_dir, db)
    assert not reg.exists("test.load-now")
    manager.install(str(repo))
    # Should be immediately available via manager/cap without restart
    assert reg.exists("test.load-now")
    assert cap.exists("tool", "test.load-now.tool1")

def test_system_requirements_validation(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo = tmp_path / "repo-sysreq"
    _init_git_repo(repo, {"id": "test.sysreq", "name": "Sys", "version": "1.0", "description": "D", "api_version": "1", "system_requirements": ["nonexistent_binary_xyz123"]},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    db = tmp_path / "lc.db"
    manager, _, _, _, _ = _fresh_manager(ext_dir, db)
    with pytest.raises(Exception, match="system requirements not available"):
        manager.install(str(repo))
    assert not (ext_dir / repo.name).exists()

def test_folder_not_identity(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo = tmp_path / "my-browser-final"
    _init_git_repo(repo, {"id": "community.browser", "name": "Browser", "version": "1.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    db = tmp_path / "lc.db"
    manager, reg, _, _, _ = _fresh_manager(ext_dir, db)
    result = manager.install(str(repo))
    assert result["id"] == "community.browser"
    assert reg.exists("community.browser")
    assert not reg.exists("my-browser-final")


# ---------- Update ----------

def test_21_update_based_on_extension_id(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo_v1 = tmp_path / "repo-up-v1"
    _init_git_repo(repo_v1, {"id": "test.update-id", "name": "Upd", "version": "1.0.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nclass E(Extension):\n    def register(self, ctx):\n        ctx.tools.register("test.update-id.tool1")\nextension = E()\n')
    db = tmp_path / "lc.db"
    manager, reg, cap, lc, _ = _fresh_manager(ext_dir, db)
    manager.install(str(repo_v1))
    # Prepare v2 repo (different folder, same id, bump version)
    repo_v2 = tmp_path / "repo-up-v2"
    _init_git_repo(repo_v2, {"id": "test.update-id", "name": "Upd", "version": "2.0.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nclass E(Extension):\n    def register(self, ctx):\n        ctx.tools.register("test.update-id.tool1")\nextension = E()\n')
    result = manager.update("test.update-id", repository_url=str(repo_v2))
    assert result["id"] == "test.update-id"
    # Version updated in lifecycle store, but file manifest also updated (registry manifest replaced)
    assert reg.get("test.update-id").manifest.version == "2.0.0"

def test_22_manifest_id_must_be_same(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo_v1 = tmp_path / "repo-up-mismatch-v1"
    _init_git_repo(repo_v1, {"id": "test.mismatch", "name": "M", "version": "1.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    db = tmp_path / "lc.db"
    manager, _, _, _, _ = _fresh_manager(ext_dir, db)
    manager.install(str(repo_v1))
    repo_v2 = tmp_path / "repo-up-mismatch-v2"
    _init_git_repo(repo_v2, {"id": "test.different-id", "name": "M2", "version": "2.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    with pytest.raises(Exception, match="manifest ID mismatch"):
        manager.update("test.mismatch", repository_url=str(repo_v2))
    # Old package still intact
    assert (ext_dir / repo_v1.name / "manifest.json").exists()
    assert json.loads((ext_dir / repo_v1.name / "manifest.json").read_text())["version"] == "1.0"

def test_23_staging_used(tmp_path):
    # Count staging dirs before install, ensure not leaking after (exactly same as before or only delta cleaned)
    import pathlib as _pl
    tmpdir = Path(tempfile.gettempdir())
    before = {p.name for p in tmpdir.iterdir() if p.name.startswith("aether_ext_staging_")}
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo = tmp_path / "repo-staging-check"
    _init_git_repo(repo, {"id": "test.staging-used", "name": "S", "version": "1.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    db = tmp_path / "lc.db"
    manager, _, _, _, _ = _fresh_manager(ext_dir, db)
    manager.install(str(repo))
    after = {p.name for p in tmpdir.iterdir() if p.name.startswith("aether_ext_staging_")}
    # Check no new leftover staging dir was left behind (any created during install was cleaned)
    new_left = after - before
    assert len(new_left) == 0, f"Staging not cleaned: {new_left}"

def test_24_failed_update_restores_old_package(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo_v1 = tmp_path / "repo-restore-v1"
    _init_git_repo(repo_v1, {"id": "test.restore", "name": "R", "version": "1.0.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    db = tmp_path / "lc.db"
    manager, reg, _, _, _ = _fresh_manager(ext_dir, db)
    manager.install(str(repo_v1))
    target = ext_dir / repo_v1.name
    assert target.exists()
    # Prepare bad update repo with invalid structure (missing __init__.py)
    repo_bad = tmp_path / "repo-restore-bad"
    # Create repo with missing file scenario via staging validation: we'll make manifest with invalid API to trigger failure after backup
    repo_bad.mkdir()
    _make_extension_dir(repo_bad, ".", {"id": "test.restore", "name": "R", "version": "2.0.0", "description": "D", "api_version": "999"},
                        'from agent_ai.extensions import Extension\nextension = Extension()\n')
    subprocess.run(["git", "init"], cwd=str(repo_bad), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo_bad), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_bad), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo_bad), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_bad), capture_output=True, check=True)
    with pytest.raises(Exception):
        manager.update("test.restore", repository_url=str(repo_bad))
    # Old package still there and version unchanged
    assert target.exists()
    assert json.loads((target / "manifest.json").read_text())["version"] == "1.0.0"
    assert reg.get("test.restore").manifest.version == "1.0.0"

def test_25_dependency_update_failure_not_corrupt(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo_v1 = tmp_path / "repo-dep-up-v1"
    _init_git_repo(repo_v1, {"id": "test.dep-up", "name": "D", "version": "1.0.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    db = tmp_path / "lc.db"
    manager, reg, _, _, _ = _fresh_manager(ext_dir, db)
    manager.install(str(repo_v1))
    repo_v2 = tmp_path / "repo-dep-up-v2"
    _init_git_repo(repo_v2, {"id": "test.dep-up", "name": "D", "version": "2.0.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n',
                   pyproject='[project]\nname="x"\nversion="0.1.0"\ndependencies=["__fail_dependency_install__"]\n')
    with pytest.raises(Exception, match="dependency"):
        manager.update("test.dep-up", repository_url=str(repo_v2))
    assert (ext_dir / repo_v1.name).exists()
    assert json.loads((ext_dir / repo_v1.name / "manifest.json").read_text())["version"] == "1.0.0"

def test_26_no_unsafe_hot_reload(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo_v1 = tmp_path / "repo-hot-v1"
    _init_git_repo(repo_v1, {"id": "test.hot-reload", "name": "H", "version": "1.0.0", "description": "D", "api_version": "1"},
                   'value = "v1"\nfrom agent_ai.extensions import Extension\nclass E(Extension):\n    def register(self, ctx): pass\nextension = E()\n')
    db = tmp_path / "lc.db"
    manager, reg, _, _, _ = _fresh_manager(ext_dir, db)
    manager.install(str(repo_v1))
    old_instance = reg.get("test.hot-reload").extension
    # Update file package to v2 (extension.py value = "v2")
    repo_v2 = tmp_path / "repo-hot-v2"
    _init_git_repo(repo_v2, {"id": "test.hot-reload", "name": "H", "version": "2.0.0", "description": "D", "api_version": "1"},
                   'value = "v2"\nfrom agent_ai.extensions import Extension\nclass E(Extension):\n    def register(self, ctx): pass\nextension = E()\n')
    manager.update("test.hot-reload", repository_url=str(repo_v2))
    # Filesystem is v2, but Python instance remains old until reload/restart (no unsafe unload)
    assert reg.get("test.hot-reload").extension is old_instance
    # But manifest version updated for metadata
    assert reg.get("test.hot-reload").manifest.version == "2.0.0"


# ---------- Uninstall ----------

def test_27_uninstall_by_extension_id(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo = tmp_path / "repo-uninstall"
    _init_git_repo(repo, {"id": "test.uninstall-me", "name": "U", "version": "1.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    db = tmp_path / "lc.db"
    manager, reg, _, _, _ = _fresh_manager(ext_dir, db)
    manager.install(str(repo))
    assert reg.exists("test.uninstall-me")
    result = manager.uninstall("test.uninstall-me")
    assert result["status"] == "uninstalled"
    assert not reg.exists("test.uninstall-me")
    assert not (ext_dir / repo.name).exists()

def test_28_capability_removed_on_uninstall(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo = tmp_path / "repo-uninstall-cap"
    _init_git_repo(repo, {"id": "test.uninstall-cap", "name": "U", "version": "1.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nclass E(Extension):\n    def register(self, ctx):\n        ctx.tools.register("test.uninstall-cap.tool1")\nextension = E()\n')
    db = tmp_path / "lc.db"
    cap = CapabilityRegistry()
    tr = ToolRegistry()
    manager, reg, cap, _, _ = _fresh_manager(ext_dir, db, cap_reg=cap, tool_reg=tr)
    manager.install(str(repo))
    assert cap.exists("tool", "test.uninstall-cap.tool1")
    manager.uninstall("test.uninstall-cap")
    assert not cap.exists("tool", "test.uninstall-cap.tool1")
    assert not tr.has("test.uninstall-cap.tool1")

def test_29_package_source_removed(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo = tmp_path / "repo-uninstall-src"
    _init_git_repo(repo, {"id": "test.uninstall-src", "name": "U", "version": "1.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    db = tmp_path / "lc.db"
    manager, _, _, _, _ = _fresh_manager(ext_dir, db)
    manager.install(str(repo))
    target = ext_dir / repo.name
    assert target.exists()
    manager.uninstall("test.uninstall-src")
    assert not target.exists()

def test_30_config_state_not_deleted_on_uninstall(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo = tmp_path / "repo-uninstall-keep"
    _init_git_repo(repo, {"id": "test.keep-data", "name": "K", "version": "1.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nclass E(Extension):\n    def register(self, ctx):\n        ctx.config.register(key="api_key", type="string", default="def")\nextension = E()\n')
    db = tmp_path / "lc.db"
    cap = CapabilityRegistry()
    store = ConfigValueStore(str(db))
    manager, reg, cap, lc, _ = _fresh_manager(ext_dir, db, cap_reg=cap, tool_reg=ToolRegistry())
    # Pre-register config definition via manager's cap/store to test persistence
    # Install then set config
    manager.install(str(repo))
    # Need to register config via context after install (simulate runtime set)
    ctx = ExtensionContext(manifest=_manifest("test.keep-data"), capability_registry=cap, config_store=store)
    # But install already registered config via extension's register; we just set value
    # For test, register again if not already? Use manager's cap
    if not cap.exists("config", "test.keep-data.api_key"):
        ctx.config.register(key="api_key", type="string", default="def")
    ctx.config.set("api_key", "kept-secret")
    storage = ExtensionStorage("test.keep-data", aether_root=tmp_path)
    storage.set("prefs", {"theme": "dark"})
    manager.uninstall("test.keep-data")
    # Config and storage should remain
    assert store.get("test.keep-data", "api_key") == "kept-secret" or ctx.config.get("api_key") == "kept-secret"
    assert storage.get("prefs") == {"theme": "dark"}

def test_31_unrelated_extension_safe_on_uninstall(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo_a = tmp_path / "repo-keep-a"
    _init_git_repo(repo_a, {"id": "test.keep-a", "name": "A", "version": "1.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    repo_b = tmp_path / "repo-remove-b"
    _init_git_repo(repo_b, {"id": "test.remove-b", "name": "B", "version": "1.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    db = tmp_path / "lc.db"
    manager, reg, _, _, _ = _fresh_manager(ext_dir, db)
    manager.install(str(repo_a))
    manager.install(str(repo_b))
    manager.uninstall("test.remove-b")
    assert reg.exists("test.keep-a")
    assert (ext_dir / repo_a.name).exists()
    assert not reg.exists("test.remove-b")

def test_uninstall_protection_not_found(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    db = tmp_path / "lc.db"
    manager, _, _, _, _ = _fresh_manager(ext_dir, db)
    with pytest.raises(Exception, match="not found"):
        manager.uninstall("test.nonexistent")

def test_uninstall_protection_not_delete_arbitrary_path(tmp_path):
    # Ensure manager only deletes registered extension folder, not arbitrary path
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo = tmp_path / "repo-protect"
    _init_git_repo(repo, {"id": "test.protect", "name": "P", "version": "1.0", "description": "D", "api_version": "1"},
                   'from agent_ai.extensions import Extension\nextension = Extension()\n')
    db = tmp_path / "lc.db"
    manager, reg, _, _, _ = _fresh_manager(ext_dir, db)
    manager.install(str(repo))
    # Create an unrelated folder outside extensions dir and try to trick uninstall via path-like id (should fail not found)
    outside = tmp_path / "outside-folder"
    outside.mkdir()
    (outside / "file.txt").write_text("important")
    # Uninstall with wrong id should not delete outside
    with pytest.raises(Exception):
        manager.uninstall("test.not-exist")
    assert (outside / "file.txt").exists()


# ---------- Regression ----------

def test_32_foundation_still_pass(tmp_path):
    from agent_ai.extensions.manifest import load_manifest as _lm
    m_path = tmp_path / "manifest.json"
    m_path.write_text('{"id": "test.foundation", "name": "N", "version": "1.0", "description": "D", "api_version": "1"}')
    m = _lm(m_path)
    assert m.id == "test.foundation"

def test_33_registry_loader_catalog(tmp_path):
    from agent_ai.extensions.registry import ExtensionRegistry as _ER
    from agent_ai.extensions.loader import ExtensionLoader as _EL
    for nid in ["a", "b"]:
        d = tmp_path / f"ext-{nid}"
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps({"id": f"reg.{nid}", "name": nid, "version": "1", "description": "D", "api_version": "1"}))
        (d / "extension.py").write_text("from agent_ai.extensions import Extension\nextension = Extension()\n")
        (d / "__init__.py").write_text("")
        (d / "pyproject.toml").write_text('[project]\nname="x"\nversion="0.1.0"\n')
    reg = _ER()
    loader = _EL(registry=reg, extensions_dir=tmp_path, enable_entry_points=False)
    res = loader.load_all()
    assert res.count_loaded == 2

def test_34_capability_still(tmp_path):
    cap_reg = CapabilityRegistry()
    manifest = _manifest("cap.regress")
    ctx = ExtensionContext(manifest=manifest, capability_registry=cap_reg)
    ctx.tools.register("cap.regress.tool1")
    assert cap_reg.exists("tool", "cap.regress.tool1")

def test_35_config_storage_still(tmp_path):
    db = tmp_path / "t.db"
    store = ConfigValueStore(str(db))
    cap = CapabilityRegistry()
    ctx = ExtensionContext(manifest=_manifest("cs.regress"), capability_registry=cap, config_store=store)
    ctx.config.register(key="timeout", type="integer", default=60)
    ctx.config.set("timeout", 99)
    assert ctx.config.get("timeout") == 99

def test_36_ui_still(tmp_path):
    cap = CapabilityRegistry()
    ctx = ExtensionContext(manifest=_manifest("ui.regress"), capability_registry=cap)
    ctx.ui.register(id="ui.regress.panel1", type="panel", title="Panel")
    assert cap.exists("ui", "ui.regress.panel1")

def test_37_compression_false():
    from agent_ai.config.settings import compression_enabled
    assert compression_enabled() is False

def test_ref_checkout(tmp_path):
    ext_dir = tmp_path / "exts"
    ext_dir.mkdir()
    repo = tmp_path / "repo-ref"
    repo.mkdir()
    (repo / "manifest.json").write_text(json.dumps({"id": "test.ref-check", "name": "R", "version": "1.0", "description": "D", "api_version": "1"}))
    (repo / "extension.py").write_text('from agent_ai.extensions import Extension\nextension = Extension()\n')
    (repo / "__init__.py").write_text("")
    (repo / "pyproject.toml").write_text('[project]\nname="x"\nversion="0.1.0"\n')
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True)
    # create branch
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=str(repo), capture_output=True, check=True)
    (repo / "manifest.json").write_text(json.dumps({"id": "test.ref-check", "name": "R", "version": "2.0", "description": "D", "api_version": "1"}))
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "v2"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "checkout", "master"], cwd=str(repo), capture_output=True, check=True)
    db = tmp_path / "lc.db"
    manager, reg, _, _, _ = _fresh_manager(ext_dir, db)
    manager.install(str(repo), ref="feature")
    assert reg.get("test.ref-check").manifest.version == "2.0"
