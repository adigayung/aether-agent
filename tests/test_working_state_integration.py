"""Integration tests for Working State (Task 1-4).

Memvalidasi:
1. Working State dibuat dan di-reset per task di AgentRuntime.
2. Plan (TaskPlan) diimpor menjadi living plan WorkingState.
3. Observation (tool result) memperbarui Working State (files inspected/changed).
4. Working State teks disuntikkan sebagai system message ke provider.
5. Tool Result, Agent Observation, dan UI Activity (observation_received)
   dipisah sebagai layer event yang berbeda.
6. Diff/Review capability tersedia bila diaktifkan.
7. State tetap konsisten antar-round ( tidak crash / tidak kehilangan goal).
8. Task lama tanpa Working State tetap berjalan (backward compatible).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from agent_ai.core.executor import ToolExecutor
from agent_ai.providers.base import GenerateOptions, GenerateResult
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider
from agent_ai.runtime.models import RuntimeStatus
from agent_ai.runtime.runtime import AgentRuntime
from agent_ai.planning.models import PlanStatus, PlanStep, TaskPlan
from agent_ai.task.models import PreparedTask
from agent_ai.tools.registry import build_registry
from agent_ai.runtime.working_state import PlanEntryStatus


class _ScriptedProvider(OpenAICompatibleProvider):
    name = "scripted"

    def __init__(self, script: List[Dict[str, Any]]) -> None:
        self.config = SimpleNamespace(model="scripted-model", context_window=0)
        self.script = list(script)
        self.calls = 0
        self.messages_seen: List[List[Dict[str, Any]]] = []

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        idx = self.calls
        self.calls += 1
        if messages is not None:
            self.messages_seen.append(list(messages))
        raw = self.script[idx] if idx < len(self.script) else _final_turn("done")
        return GenerateResult(text="", model="scripted-model", provider=self.name, raw=raw)


def _tool_call(call_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _tool_turn(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"content": "", "tool_calls": calls},
                "finish_reason": "tool_calls",
            }
        ]
    }


def _final_turn(text: str = "selesai") -> Dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


@pytest.fixture
def tmp_project(tmp_path: Path):
    (tmp_path / "a.txt").write_text("content a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("content b\n", encoding="utf-8")
    return tmp_path


def test_working_state_created_and_plan_imported(tmp_project: Path) -> None:
    registry = build_registry(root=tmp_project)
    plan = TaskPlan(
        task="Test plan import",
        steps=[
            PlanStep(title="Read a.txt", description="Read first file", id="s1"),
            PlanStep(title="Read b.txt", description="Read second file", id="s2"),
        ],
        status=PlanStatus.PENDING,
    )
    prepared = PreparedTask(task="Test plan import", context=None, plan=plan)
    provider = _ScriptedProvider([_final_turn("done")])
    runtime = AgentRuntime(provider=provider, executor=ToolExecutor(registry=registry))

    result = runtime.run(prepared)

    assert result.status == RuntimeStatus.COMPLETED
    ws = runtime.working_state
    assert ws is not None
    assert ws.goal == "Test plan import"
    assert len(ws.plan) == 2


def test_observation_updates_working_state_files(tmp_project: Path) -> None:
    registry = build_registry(root=tmp_project)
    plan = TaskPlan(
        task="Test observation bridge",
        steps=[
            PlanStep(title="Read a.txt", description="", id="s1"),
            PlanStep(title="Read b.txt", description="", id="s2"),
        ],
        status=PlanStatus.PENDING,
    )
    prepared = PreparedTask(task="Test observation bridge", context=None, plan=plan)
    calls = [
        _tool_call("c1", "read_file", {"path": "a.txt"}),
        _tool_call("c2", "read_file", {"path": "b.txt"}),
    ]
    script = [_tool_turn([c]) for c in calls] + [_final_turn("done")]
    provider = _ScriptedProvider(script)
    runtime = AgentRuntime(provider=provider, executor=ToolExecutor(registry=registry))

    result = runtime.run(prepared)

    assert result.status == RuntimeStatus.COMPLETED
    ws = runtime.working_state
    assert "a.txt" in ws.files_inspected
    assert "b.txt" in ws.files_inspected
    assert not ws.files_changed


def test_working_state_text_injected_as_system_message(tmp_project: Path) -> None:
    registry = build_registry(root=tmp_project)
    plan = TaskPlan(
        task="Test WS injection",
        steps=[PlanStep(title="Read a.txt", description="", id="s1")],
        status=PlanStatus.PENDING,
    )
    prepared = PreparedTask(task="Test WS injection", context=None, plan=plan)
    calls = [_tool_call("c1", "read_file", {"path": "a.txt"})]
    script = [_tool_turn([c]) for c in calls] + [_final_turn("done")]
    provider = _ScriptedProvider(script)
    runtime = AgentRuntime(provider=provider, executor=ToolExecutor(registry=registry))

    runtime.run(prepared)

    ws_injected = [
        m for msgs in provider.messages_seen for m in msgs
        if m.get("role") == "system" and "Goal:" in str(m.get("content", ""))
    ]
    assert ws_injected, "Working State (goal) harus disuntikkan sebagai system message"


def test_task_without_working_state_provider_still_runs(tmp_project: Path) -> None:
    registry = build_registry(root=tmp_project)
    provider = _ScriptedProvider([_final_turn("done")])
    orch = provider  # orchestrator tanpa working_state_provider
    runtime = AgentRuntime(provider=provider, executor=ToolExecutor(registry=registry))
    prepared = PreparedTask(task="Backward compat", context=None, plan=None)

    result = runtime.run(prepared)

    assert result.status == RuntimeStatus.COMPLETED


def test_tool_result_agent_observation_ui_events_separated(tmp_project: Path) -> None:
    """Test that the three event layers fire correctly via orchestrator event sink."""
    registry = build_registry(root=tmp_project)
    calls = [_tool_call("c1", "read_file", {"path": "a.txt"})]
    script = [_tool_turn([c]) for c in calls] + [_final_turn("done")]
    provider = _ScriptedProvider(script)
    events: List[Dict[str, Any]] = []
    runtime = AgentRuntime(
        provider=provider,
        executor=ToolExecutor(registry=registry),
    )
    # Use orchestrator event sink directly for this test
    runtime._event_sink = lambda et, payload: events.append({"type": et, **payload})
    prepared = PreparedTask(task="Test event layers", context=None, plan=None)

    runtime.run(prepared)

    tool_result_events = [e for e in events if e["type"] == "tool_result"]
    agent_obs_events = [e for e in events if e["type"] == "agent_observation"]
    ui_events = [e for e in events if e["type"] == "observation_received"]

    assert len(tool_result_events) == 1
    assert len(agent_obs_events) == 1
    assert len(ui_events) == 1

    # tool_result = factual payload (membawa path dan output)
    tr = tool_result_events[0]
    assert tr["tool"] == "read_file"
    assert isinstance(tr["output"], dict)
    assert tr["output"].get("path") == "a.txt"

    # agent_observation = normalized observation untuk loop
    ao = agent_obs_events[0]
    assert ao["action_id"] is not None
    assert ao["success"] is True

    # observation_received = UI/activity layer
    ui = ui_events[0]
    assert ui["tool"] == "read_file"


def test_working_state_plan_progress_tracks_entries(tmp_project: Path) -> None:
    from agent_ai.runtime.working_state import WorkingStateManager

    mgr = WorkingStateManager()
    mgr.create_plan_entry(title="Step 1", description="first")
    mgr.create_plan_entry(title="Step 2", description="second")
    entry = mgr.snapshot().plan[0]
    mgr.complete_plan_entry(entry.id, outcome="done")

    progress = mgr.snapshot().plan_progress()
    assert progress["total"] == 2
    assert progress["completed"] == 1
    assert progress["pending"] == 1
    assert progress["percent"] == 50.0


def test_review_changes_tool_available_when_enabled(tmp_project: Path, monkeypatch) -> None:
    import os
    monkeypatch.setenv("AETHER_ENABLE_REVIEW_TOOLS", "1")
    registry = build_registry(root=tmp_project)
    tool_names = {t.name for t in registry._tools.values()}
    assert "review_changes" in tool_names
    assert "diff_file" in tool_names
