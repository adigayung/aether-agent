"""Tests for compact file-change learning metadata."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import sys
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core.executor import ToolExecutor
from agent_ai.core.response import ActionType, LLMAction
from agent_ai.runtime.models import RuntimeResult, RuntimeStatus
from agent_ai.runtime.runtime import AgentRuntime
from agent_ai.task.models import PreparedTask
from agent_ai.tools.registry import build_registry


def _runtime(root: Path) -> AgentRuntime:
    # Provider is unused by _build_learning_observations; executor is real.
    return AgentRuntime(provider=object(), executor=ToolExecutor(build_registry(root=root)), project_root=str(root))


def _observation(runtime: AgentRuntime, name: str, arguments: Dict[str, Any], success: bool = True):
    action = {"name": name, "arguments": arguments}
    step = {"action": action, "observation": {"success": success, "content": {"ok": True}}}
    result = RuntimeResult(status=RuntimeStatus.COMPLETED, result="done", steps=[step])
    runtime._current_prepared = PreparedTask(task="test")
    return runtime._build_learning_observations(result)


def _execute(root: Path, name: str, arguments: Dict[str, Any]):
    executor = ToolExecutor(build_registry(root=root))
    action = LLMAction(name=name, arguments=arguments, type=ActionType.TOOL_CALL)
    return executor.execute_action(action)


def test_successful_file_operations_generate_metadata(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "edit.txt").write_text("old", encoding="utf-8")
    (root / "delete.txt").write_text("remove", encoding="utf-8")
    (root / "move.txt").write_text("move", encoding="utf-8")
    runtime = _runtime(root)

    cases = [
        ("write_file", {"path": "write.txt", "content": "new content"}, "write", "write.txt"),
        ("edit_file", {"path": "edit.txt", "old_text": "old", "new_text": "new"}, "edit", "edit.txt"),
        ("delete_file", {"path": "delete.txt"}, "delete", "delete.txt"),
        ("move_file", {"source": "move.txt", "destination": "moved.txt"}, "move", "move.txt -> moved.txt"),
    ]
    for tool, args, operation, path in cases:
        result = _execute(root, tool, args)
        assert result.success, result.error
        observations = _observation(runtime, tool, args)
        metadata = [item for item in observations if item.startswith("File change:")]
        assert metadata == [f"File change: operation={operation} tool={tool} path={path} status=success"]


def test_failed_file_operation_does_not_generate_metadata(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    runtime = _runtime(root)
    args = {"path": "missing.txt", "old_text": "old", "new_text": "new"}
    result = _execute(root, "edit_file", args)
    assert not result.success
    assert not [item for item in _observation(runtime, "edit_file", args, success=False) if item.startswith("File change:")]


def test_metadata_excludes_payloads_and_full_content(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    runtime = _runtime(root)
    args = {"path": "large.txt", "old_text": "OLD", "new_text": "NEW", "content": "A" * 1000}
    metadata = [item for item in _observation(runtime, "write_file", args) if item.startswith("File change:")][0]
    assert "OLD" not in metadata and "NEW" not in metadata and "A" * 1000 not in metadata
    assert "old_text" not in metadata and "new_text" not in metadata and "content" not in metadata


def test_task_without_file_changes_keeps_existing_observations(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    runtime = _runtime(root)
    observations = _observation(runtime, "read_file", {"path": "README.md"})
    assert observations[0] == "Task: test"
    assert not [item for item in observations if item.startswith("File change:")]
