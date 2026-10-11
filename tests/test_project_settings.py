"""Tests: Project Settings per-project (System Prompt Agent & Consultant).

Membuktikan:

- `ProjectSettingsStore` menyimpan/membaca System Prompt per-project di
  `<root>/.aether/settings/agent.json` dan `consultant.json`.
- Project tanpa override -> None (runtime tetap memakai default existing).
- Isolasi antar-project: prompt project A tidak bocor ke project B.
- Prompt kosong = hapus override (default berlaku kembali).
- Guard tipe/panjang nilai yang tidak valid.

Isolasi filesystem: memakai `tmp_path` (tidak menyentuh project produksi).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_ai.projects.project_settings import (
    AGENT_FILE_NAME,
    CONSULTANT_FILE_NAME,
    MAX_PROJECT_SYSTEM_PROMPT_CHARS,
    ProjectSettingsError,
    ProjectSettingsStore,
    project_agent_system_prompt,
    project_consultant_system_prompt,
)


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_paths_are_under_settings_dir(tmp_path):
    store = ProjectSettingsStore(root=tmp_path / "proj")
    assert store.settings_dir == tmp_path / "proj" / ".aether" / "settings"
    assert store.agent_path.name == AGENT_FILE_NAME == "agent.json"
    assert store.consultant_path.name == CONSULTANT_FILE_NAME == "consultant.json"


def test_no_override_returns_none(tmp_path):
    store = ProjectSettingsStore(root=tmp_path / "proj")
    assert store.agent_system_prompt() is None
    assert store.consultant_system_prompt() is None
    assert not store.agent_exists
    assert not store.consultant_exists


def test_save_and_load_agent_prompt(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    store = ProjectSettingsStore(root=root)
    store.save_agent_settings({"system_prompt": "PROMPT AGENT"})
    assert store.agent_system_prompt() == "PROMPT AGENT"
    assert _read(root / ".aether" / "settings" / "agent.json") == {
        "system_prompt": "PROMPT AGENT"
    }


def test_save_and_load_consultant_prompt(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    store = ProjectSettingsStore(root=root)
    store.save_consultant_settings({"system_prompt": "PROMPT CONSULTANT"})
    assert store.consultant_system_prompt() == "PROMPT CONSULTANT"


def test_empty_prompt_clears_override(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    store = ProjectSettingsStore(root=root)
    store.save_agent_settings({"system_prompt": "X"})
    store.save_agent_settings({"system_prompt": "   "})
    assert store.agent_system_prompt() is None


def test_prompts_isolated_across_projects(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    ProjectSettingsStore(root=root_a).save_agent_settings({"system_prompt": "A"})
    assert ProjectSettingsStore(root=root_a).agent_system_prompt() == "A"
    assert ProjectSettingsStore(root=root_b).agent_system_prompt() is None
    assert not (root_b / ".aether").exists()


def test_invalid_type_rejected(tmp_path):
    store = ProjectSettingsStore(root=tmp_path / "proj")
    with pytest.raises(ProjectSettingsError):
        store.save_agent_settings({"system_prompt": 123})
    with pytest.raises(ProjectSettingsError):
        store.save_consultant_settings({"system_prompt": ["x"]})


def test_too_long_prompt_rejected(tmp_path):
    store = ProjectSettingsStore(root=tmp_path / "proj")
    with pytest.raises(ProjectSettingsError):
        store.save_agent_settings(
            {"system_prompt": "x" * (MAX_PROJECT_SYSTEM_PROMPT_CHARS + 1)}
        )


def test_helpers_never_raise(tmp_path):
    # Root kosong / tidak ada -> None (bukan crash).
    assert project_agent_system_prompt(None) is None
    assert project_consultant_system_prompt("") is None
    assert project_agent_system_prompt(str(tmp_path / "missing")) is None
    # File korup -> None (fallback default).
    root = tmp_path / "corrupt"
    (root / ".aether" / "settings").mkdir(parents=True)
    (root / ".aether" / "settings" / "agent.json").write_text("{not json", encoding="utf-8")
    assert project_agent_system_prompt(str(root)) is None


def test_agent_and_consultant_independent(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    store = ProjectSettingsStore(root=root)
    store.save_agent_settings({"system_prompt": "AGENT ONLY"})
    assert store.agent_system_prompt() == "AGENT ONLY"
    assert store.consultant_system_prompt() is None


# --------------------------------------------------------------------------- #
# Runtime wiring: AgentRuntime memakai System Prompt per-project
# --------------------------------------------------------------------------- #
def test_runtime_uses_project_agent_prompt(tmp_path):
    """Runtime dgn project_root beroverride memakai prompt project (bukan global)."""
    import sys

    from agent_ai.runtime.runtime import AgentRuntime

    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    for _p in (str(PROJECT_ROOT / "src"),):
        if _p not in sys.path:
            sys.path.insert(0, _p)

    class _StubProvider:
        name = "stub"

    root = tmp_path / "proj"
    root.mkdir()
    ProjectSettingsStore(root=root).save_agent_settings(
        {"system_prompt": "PROMPT PROJECT"}
    )
    runtime = AgentRuntime(provider=_StubProvider(), project_root=str(root))
    orchestrator = runtime._make_orchestrator(runtime.provider)  # noqa: SLF001
    assert orchestrator.system_prompt == "PROMPT PROJECT"


def test_runtime_falls_back_to_global_without_override(tmp_path):
    import sys

    from agent_ai.config.settings import agent_system_prompt
    from agent_ai.runtime.runtime import AgentRuntime

    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    for _p in (str(PROJECT_ROOT / "src"),):
        if _p not in sys.path:
            sys.path.insert(0, _p)

    class _StubProvider:
        name = "stub"

    root = tmp_path / "proj_no_override"
    root.mkdir()
    runtime = AgentRuntime(provider=_StubProvider(), project_root=str(root))
    orchestrator = runtime._make_orchestrator(runtime.provider)  # noqa: SLF001
    assert orchestrator.system_prompt == agent_system_prompt()
