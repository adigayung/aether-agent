"""Integration test: Working State konsisten antar multiple rounds."""
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


class MultiRoundProvider(OpenAICompatibleProvider):
    """Provider yang meminta 2 tool calls berurutan lalu selesai."""
    name = "multiround"

    def __init__(self) -> None:
        self.config = SimpleNamespace(model="scripted-model", context_window=0)
        self.calls = 0
        self.messages_seen: List[List[Dict[str, Any]]] = []

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        self.calls += 1
        if messages is not None:
            self.messages_seen.append(list(messages))
        if self.calls == 1:
            # First call: read a.txt
            return GenerateResult(
                text="",
                model="scripted-model",
                provider=self.name,
                raw={"choices": [{"message": {"content": "",
                                             "tool_calls": [{"id": "c1", "type": "function",
                                                             "function": {"name": "read_file",
                                                                         "arguments": json.dumps({"path": "a.txt"})}}]},
                                             "finish_reason": "tool_calls"}]}
            )
        elif self.calls == 2:
            # Second call: read b.txt
            return GenerateResult(
                text="",
                model="scripted-model",
                provider=self.name,
                raw={"choices": [{"message": {"content": "",
                                             "tool_calls": [{"id": "c2", "type": "function",
                                                             "function": {"name": "read_file",
                                                                         "arguments": json.dumps({"path": "b.txt"})}}]},
                                             "finish_reason": "tool_calls"}]}
            )
        else:
            # Third call: done
            return GenerateResult(
                text="selesai",
                model="scripted-model",
                provider=self.name,
                raw={"choices": [{"message": {"content": "selesai"}, "finish_reason": "stop"}]}
            )


def test_working_state_consistent_across_rounds(tmp_path: Path) -> None:
    """Verifikasi Working State tidak hilang/reset di antara round LLM."""
    (tmp_path / "a.txt").write_text("content a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("content b\n", encoding="utf-8")

    registry = build_registry(root=tmp_path)
    plan = TaskPlan(
        task="Test multi-round WS",
        steps=[
            PlanStep(title="Read a.txt", description="", id="s1"),
            PlanStep(title="Read b.txt", description="", id="s2"),
        ],
        status=PlanStatus.PENDING,
    )
    prepared = PreparedTask(task="Test multi-round WS", context=None, plan=plan)
    provider = MultiRoundProvider()
    runtime = AgentRuntime(provider=provider, executor=ToolExecutor(registry=registry))

    result = runtime.run(prepared)

    assert result.status == RuntimeStatus.COMPLETED
    ws = runtime.working_state

    # Goal harus tetap sama
    assert ws.goal == "Test multi-round WS"

    # Files inspected harus terakumulasi dari kedua round
    assert "a.txt" in ws.files_inspected
    assert "b.txt" in ws.files_inspected

    # Plan harus masih ada dan berisi 2 entry
    assert len(ws.plan) == 2

    # Working State harus di-inject ke provider messages (minimal 2 kali)
    ws_injected = [
        m for msgs in provider.messages_seen for m in msgs
        if m.get("role") == "system" and "Goal:" in str(m.get("content", ""))
    ]
    # Harus di-inject setelah round 1 dan 2 (karena state berubah)
    assert len(ws_injected) >= 2

    # State revision harus naik (setiap tool result memperbarui)
    assert ws.revision >= 2  # setidaknya 2 read calls

    print("✓ Multi-round Working State consistency verified!")


def test_working_state_reset_between_tasks(tmp_path: Path) -> None:
    """Verifikasi Working State di-reset untuk task baru (bukan shared)."""
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")

    registry = build_registry(root=tmp_path)
    provider = MultiRoundProvider()

    # Task 1
    plan1 = TaskPlan(task="Task 1", steps=[PlanStep(title="Read a", id="s1")])
    prep1 = PreparedTask(task="Task 1", context=None, plan=plan1)
    runtime1 = AgentRuntime(provider=provider, executor=ToolExecutor(registry=registry))
    runtime1.run(prep1)
    ws1 = runtime1.working_state
    assert ws1.goal == "Task 1"
    assert ws1.files_inspected == ["a.txt"]

    # Task 2 (instance runtime baru) — WS harus kosong/tidak mewarisi
    provider2 = MultiRoundProvider()
    plan2 = TaskPlan(task="Task 2", steps=[PlanStep(title="Read a", id="s1")])
    prep2 = PreparedTask(task="Task 2", context=None, plan=plan2)
    runtime2 = AgentRuntime(provider=provider2, executor=ToolExecutor(registry=registry))
    runtime2.run(prep2)
    ws2 = runtime2.working_state
    assert ws2.goal == "Task 2"
    # Instance runtime baru = WS baru
    assert ws2.revision > 0


if __name__ == "__main__":
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    test_working_state_consistent_across_rounds(tmp)
    test_working_state_reset_between_tasks(tmp)
    print("\nAll multi-round state consistency tests passed!")