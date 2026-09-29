"""Windows read-only cleanup regression tests for the Task 07 install fix.

Bug: installing an Extension from a local Git repository on Windows failed with
``[WinError 5] Access is denied: ...\\aether_ext_staging_...\\repo\\.git\\objects\\...``.

Root cause: ``install()`` moved the staged repository into the extensions
directory with ``shutil.move``. Staging lives under ``tempfile.gettempdir()``
while the extensions directory lives next to the project (a different drive),
so ``shutil.move`` fell back to ``copytree`` + a **plain** ``shutil.rmtree``.
Git marks loose objects under ``.git/objects`` read-only (and local clones
hardlink them), so that plain rmtree raised ``WinError 5``. The OSError then
escaped ``install()`` and was mapped to HTTP 400 ``validation_error``.

These tests lock in the fix without depending on network/GitHub.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_ai.extensions import manager as mgr_mod
from agent_ai.extensions.capabilities import CapabilityRegistry
from agent_ai.extensions.errors import ExtensionValidationError
from agent_ai.extensions.lifecycle import ExtensionLifecycleStore
from agent_ai.extensions.manager import ExtensionManager, _robust_move, _robust_rmtree
from agent_ai.extensions.registry import ExtensionRegistry


def _make_git_repo(base: Path, extension_id: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    (base / "manifest.json").write_text(
        json.dumps(
            {
                "id": extension_id,
                "name": "WinCleanup",
                "version": "1.0.0",
                "description": "D",
                "api_version": "1",
            }
        )
    )
    (base / "extension.py").write_text(
        "from agent_ai.extensions import Extension\n"
        "class E(Extension):\n"
        "    pass\n"
        "extension = E()\n"
    )
    (base / "__init__.py").write_text("")
    (base / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.1.0"\n')
    subprocess.run(["git", "init"], cwd=str(base), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(base), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(base), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(base), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(base), capture_output=True, check=True)
    return base


def _freeze_git_objects(repo: Path) -> int:
    """Mark every loose object read-only (mimics what git does on clone)."""
    count = 0
    for obj in (repo / ".git" / "objects").rglob("*"):
        if obj.is_file():
            os.chmod(obj, stat.S_IREAD)
            count += 1
    return count


def _force_cross_device(monkeypatch) -> None:
    """Force ``_robust_move`` to take the cross-device copy fallback path."""

    def _raise(a, b):  # noqa: ANN001
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(mgr_mod.os, "rename", _raise)


def _make_manager(root: Path, extensions_dir: Path) -> ExtensionManager:
    return ExtensionManager(
        registry=ExtensionRegistry(),
        capability_registry=CapabilityRegistry(),
        lifecycle_store=ExtensionLifecycleStore(db_path=root / "life.db"),
        aether_root=root,
        extensions_dir=extensions_dir,
    )


def test_robust_rmtree_removes_readonly_files(tmp_path):
    tree = tmp_path / "tree"
    (tree / "safe" / "objects").mkdir(parents=True)
    ro = tree / "safe" / "objects" / "abc123"
    ro.write_text("payload")
    os.chmod(ro, stat.S_IREAD)

    # Sanity: a plain rmtree cannot delete the read-only file on Windows.
    if os.name == "nt":
        try:
            import shutil

            shutil.rmtree(str(tree))
        except PermissionError:
            pass
        # recreate for the real assertion
        if not tree.exists():
            (tree / "safe" / "objects").mkdir(parents=True)
            ro.write_text("payload")
            os.chmod(ro, stat.S_IREAD)

    _robust_rmtree(str(tree))
    assert not tree.exists()


def test_robust_move_cross_device_with_readonly_file(tmp_path, monkeypatch):
    src = tmp_path / "staging" / "repo"
    (src / ".git" / "objects").mkdir(parents=True)
    ro = src / ".git" / "objects" / "obj1"
    ro.write_text("gitobject")
    os.chmod(ro, stat.S_IREAD)

    _force_cross_device(monkeypatch)

    dst = tmp_path / "Extension" / "repo"
    _robust_move(src, dst)

    assert (dst / ".git" / "objects" / "obj1").is_file()
    assert not src.exists()
    # The parent (staging root) is intentionally left for the caller to remove.
    assert src.parent.is_dir()


def test_install_succeeds_with_readonly_git_objects_cross_device(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path / "src-repo", "win.cleanup.e2e")
    assert _freeze_git_objects(repo) > 0

    # Force the previously buggy cross-device fallback deterministically.
    _force_cross_device(monkeypatch)

    extensions_dir = tmp_path / "Extension"
    mgr = _make_manager(tmp_path, extensions_dir)

    staging_parent = Path(mgr_mod.tempfile.gettempdir())
    before = {p.name for p in staging_parent.glob("aether_ext_staging_*")}

    result = mgr.install(str(repo))

    assert result["id"] == "win.cleanup.e2e"
    assert result["status"] in ("enabled", "loaded")

    installed = extensions_dir / repo.name
    assert installed.is_dir()
    assert not (installed / ".git").exists()

    after = {p.name for p in staging_parent.glob("aether_ext_staging_*")}
    assert after <= before, f"staging directory leaked: {after - before}"


def test_primary_error_not_masked_by_cleanup_failure(tmp_path, monkeypatch):
    bad = tmp_path / "bad-repo"
    bad.mkdir()
    (bad / "manifest.json").write_text(json.dumps({"id": "win.bad", "version": "1.0"}))
    # Intentionally missing __init__.py / extension.py / pyproject.toml.
    subprocess.run(["git", "init"], cwd=str(bad), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(bad), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(bad), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(bad), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(bad), capture_output=True, check=True)

    real_rmtree = mgr_mod._robust_rmtree

    def _boom(path):  # noqa: ANN001
        if Path(path).name.startswith("aether_ext_staging_"):
            raise PermissionError(5, "Access is denied (simulated cleanup failure)")
        return real_rmtree(path)

    monkeypatch.setattr(mgr_mod, "_robust_rmtree", _boom)

    mgr = _make_manager(tmp_path, tmp_path / "Extension")

    with pytest.raises(ExtensionValidationError) as excinfo:
        mgr.install(str(bad))

    # Primary error preserved, cleanup failure swallowed.
    assert "Invalid Extension package structure" in str(excinfo.value)


def test_cleanup_failure_does_not_break_successful_install(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path / "ok-repo", "win.cleanup.cleanupfail")

    real_rmtree = mgr_mod._robust_rmtree

    def _boom(path):  # noqa: ANN001
        if Path(path).name.startswith("aether_ext_staging_"):
            raise PermissionError(5, "Access is denied (simulated cleanup failure)")
        return real_rmtree(path)

    monkeypatch.setattr(mgr_mod, "_robust_rmtree", _boom)

    mgr = _make_manager(tmp_path, tmp_path / "Extension")

    result = mgr.install(str(repo))

    assert result["id"] == "win.cleanup.cleanupfail"
    assert result["status"] in ("enabled", "loaded")
