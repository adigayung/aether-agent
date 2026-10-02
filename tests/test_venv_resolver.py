"""Unit tests for dynamic virtualenv detection via VenvResolver."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from agent_ai.projects.venv_resolver import (
    VenvResolver,
    _bin_dir_name,
    _is_venv_dir,
    _python_exe_name,
    prepend_path,
    venv_signature,
)


@pytest.fixture
def tmp_root():
    """Temporary project root."""
    ws = Path(tempfile.mkdtemp(prefix="venv_resolver_"))
    yield ws
    shutil.rmtree(ws, ignore_errors=True)


def _create_venv(root: Path, name: str = "test_venv") -> Path:
    """Create a real venv and return its path."""
    venv_dir = root / name
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
    )
    return venv_dir


def test_is_venv_dir_valid(tmp_root):
    venv = _create_venv(tmp_root, "test_venv")
    assert _is_venv_dir(venv)


def test_is_venv_dir_rejects_empty_folder(tmp_root):
    empty = tmp_root / "empty"
    empty.mkdir()
    assert not _is_venv_dir(empty)


def test_signature_changes_when_venv_created(tmp_root):
    resolver = VenvResolver(tmp_root)
    # No venv yet
    sig1 = venv_signature(resolver.resolve())
    assert sig1 == (None, 0.0)
    assert resolver.scans >= 1

    # Create venv
    _create_venv(tmp_root, "test_venv")

    # Re-resolve picks up new venv
    venv = resolver.resolve()
    assert venv is not None
    sig2 = venv_signature(venv)
    assert sig2 != sig1


def test_cache_used_after_first_detection(tmp_root):
    _create_venv(tmp_root, "test_venv")
    resolver = VenvResolver(tmp_root)

    first = resolver.resolve()
    assert first is not None
    scans_before = resolver.scans
    hits_before = resolver.cache_hits

    second = resolver.resolve()
    assert second == first
    # Should not scan again; cache hit
    assert resolver.scans == scans_before
    assert resolver.cache_hits == hits_before + 1


def test_venv_python_on_path_after_creation(tmp_root):
    """The command `python` resolves to the venv interpreter after venv is created mid-task."""
    resolver = VenvResolver(tmp_root)
    assert resolver.resolve() is None

    _create_venv(tmp_root, "test_venv")

    resolved = resolver.resolve_executable("python")
    assert resolved is not None
    venv_py = tmp_root / "test_venv" / _bin_dir_name() / _python_exe_name()
    assert Path(resolved).resolve() == venv_py.resolve()


def test_build_env_prepends_venv_bin_and_sets_virtual_env(tmp_root):
    _create_venv(tmp_root, "test_venv")
    resolver = VenvResolver(tmp_root)
    env = resolver.build_env()

    bin_dir = tmp_root / _bin_dir_name()
    first_path_entry = env["PATH"].split(os.pathsep)[0]
    assert Path(first_path_entry).resolve() == (tmp_root / "test_venv" / _bin_dir_name()).resolve()
    assert env["VIRTUAL_ENV"] == str(tmp_root / "test_venv")
    assert "PYTHONHOME" not in env


def test_prepend_path_is_idempotent():
    entry = r"C:\foo\bin" if os.name == "nt" else "/foo/bin"
    env = {"PATH": r"C:\bar;C:\baz" if os.name == "nt" else "/bar:/baz"}
    env1 = prepend_path(dict(env), entry)
    env2 = prepend_path(dict(env1), entry)
    assert env1["PATH"] == env2["PATH"]
    assert entry in env1["PATH"]
    assert env1["PATH"].count(entry) == 1
