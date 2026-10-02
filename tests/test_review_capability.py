"""Integration tests for Review Capability (Task 4) — diff/review capability."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest


@pytest.fixture(autouse=True)
def _enable_review_tools(monkeypatch):
    monkeypatch.setenv("AETHER_ENABLE_REVIEW_TOOLS", "1")


def test_review_changes_tool_registered(tmp_path: Path) -> None:
    from agent_ai.tools.registry import build_registry

    registry = build_registry(root=tmp_path)
    tool_names = {t.name for t in registry._tools.values()}
    assert "review_changes" in tool_names, "review_changes tool must be registered"
    assert "diff_file" in tool_names, "diff_file tool must be registered"


def test_review_changes_reads_workspace(tmp_path: Path) -> None:
    from agent_ai.tools.registry import build_registry

    (tmp_path / "test.txt").write_text("hello world\n", encoding="utf-8")
    registry = build_registry(root=tmp_path)
    review_tool = registry._tools["review_changes"]
    result = review_tool.execute()
    assert result["reviewed"] is True
    assert "workspace" in result


def test_review_changes_with_change_tracker(tmp_path: Path) -> None:
    from agent_ai.tools.registry import build_registry
    from agent_ai.tools.review import build_review_tools

    (tmp_path / "test.txt").write_text("hello\n", encoding="utf-8")
    tools = build_review_tools(root=tmp_path)
    review_tool = tools[0]
    result = review_tool.execute()
    assert result["reviewed"] is True


def test_diff_file_tool_works(tmp_path: Path) -> None:
    from agent_ai.tools.registry import build_registry

    (tmp_path / "test.txt").write_text("hello\n", encoding="utf-8")
    registry = build_registry(root=tmp_path)
    diff_tool = registry._tools["diff_file"]
    result = diff_tool.execute(path="test.txt")
    assert "path" in result
    assert "diff" in result or "source" in result
