"""Tests: Agent Execution Mode (Fast / Balanced / Deep), Settings Persistence & Metadata Flow.

Memvalidasi:
1. Default mode dibaca dari data/settings.json -> agent.default_mode
2. Normalisasi & backward compatibility (minimal -> fast, mode tak dikenal -> balanced)
3. Update settings.json via update_global_settings (persisten, tidak merusak key lain)
4. Task preparation & runtime menerima metadata mode
5. Telemetry requested vs effective mode pada event policy_applied / policy_escalated
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from agent_ai.config import settings as settings_mod
from agent_ai.config.settings import (
    agent_default_mode,
    global_settings,
    update_global_settings,
)
from agent_ai.providers.base import GenerateOptions, GenerateResult
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider
from agent_ai.runtime.models import RuntimeStatus
from agent_ai.runtime.policy import (
    DEFAULT_MODE,
    ExecutionPolicyResolver,
    format_escalation_block,
    format_policy_block,
    normalize_mode,
)
from agent_ai.runtime.runtime import AgentRuntime
from agent_ai.session.store import InMemorySessionStore
from agent_ai.task.models import PreparedTask
from agent_ai.tools.registry import build_registry


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """Arahkan SETTINGS_PATH ke file sementara (isolasi total)."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", path)
    return path


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class ScriptedProvider(OpenAICompatibleProvider):
    name = "scripted"

    def __init__(self, script: Optional[List[Dict[str, Any]]] = None) -> None:
        self.config = SimpleNamespace(model="scripted-model")
        self.script = list(script or [{"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}])
        self.calls = 0

    def generate(self, *args, **kwargs) -> GenerateResult:
        idx = self.calls
        self.calls += 1
        raw = self.script[idx] if idx < len(self.script) else {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        return GenerateResult(text="ok", model="scripted-model", provider=self.name, raw=raw)


# --------------------------------------------------------------------------- #
# 1. Default Mode Settings & Persistence
# --------------------------------------------------------------------------- #
def test_default_mode_when_file_absent(settings_file):
    assert not settings_file.exists()
    assert agent_default_mode() == "balanced"


def test_default_mode_reads_from_settings(settings_file):
    _write(settings_file, {"agent": {"default_mode": "fast"}})
    assert agent_default_mode() == "fast"


def test_default_mode_normalizes_minimal_to_fast(settings_file):
    _write(settings_file, {"agent": {"default_mode": "minimal"}})
    assert agent_default_mode() == "fast"


def test_default_mode_falls_back_on_invalid_value(settings_file):
    _write(settings_file, {"agent": {"default_mode": "super_smart_mode"}})
    assert agent_default_mode() == "balanced"


def test_update_global_settings_persists_default_mode(settings_file):
    _write(settings_file, {"port": 8478, "agent": {"system_prompt": "hello"}})
    update_global_settings({"agent": {"default_mode": "deep"}})
    data = _read(settings_file)
    assert data["port"] == 8478
    assert data["agent"]["system_prompt"] == "hello"
    assert data["agent"]["default_mode"] == "deep"
    assert agent_default_mode() == "deep"


def test_update_global_settings_rejects_non_string_default_mode(settings_file):
    with pytest.raises(Exception):
        update_global_settings({"agent": {"default_mode": 123}})


# --------------------------------------------------------------------------- #
# 2. Metadata Flow to Runtime & Policy
# --------------------------------------------------------------------------- #
def test_task_metadata_mode_propagates_to_runtime(tmp_path):
    store = InMemorySessionStore()
    session = store.create_session()
    provider = ScriptedProvider()
    from agent_ai.core.executor import ToolExecutor

    runtime = AgentRuntime(
        provider=provider,
        executor=ToolExecutor(build_registry(root=tmp_path)),
        session_store=store,
        session_id=session.session_id,
    )
    prepared = PreparedTask(task="lakukan perbaikan", metadata={"mode": "fast"})
    result = runtime.run(prepared)
    assert result.status == RuntimeStatus.COMPLETED
    assert result.policy is not None
    assert result.policy["requested_mode"] == "fast"
    assert result.policy["effective_mode"] == "fast"

    # Verifikasi event policy_applied
    events = store.get_events(session_id=session.session_id)
    applied = [e for e in events if e.event_type.value == "policy_applied"]
    assert len(applied) == 1
    assert applied[0].payload["requested_mode"] == "fast"
    assert applied[0].payload["effective_mode"] == "fast"
    assert "Requested Mode: Fast" in applied[0].payload["activity"]
    assert "Effective Mode: Fast" in applied[0].payload["activity"]


def test_task_metadata_minimal_normalizes_to_fast(tmp_path):
    store = InMemorySessionStore()
    session = store.create_session()
    provider = ScriptedProvider()
    from agent_ai.core.executor import ToolExecutor

    runtime = AgentRuntime(
        provider=provider,
        executor=ToolExecutor(build_registry(root=tmp_path)),
        session_store=store,
        session_id=session.session_id,
    )
    prepared = PreparedTask(task="lakukan perbaikan", metadata={"mode": "minimal"})
    result = runtime.run(prepared)
    assert result.status == RuntimeStatus.COMPLETED
    assert result.policy["requested_mode"] == "fast"
    assert result.policy["effective_mode"] == "fast"


def test_task_escalation_telemetry_shows_requested_and_effective(tmp_path):
    store = InMemorySessionStore()
    session = store.create_session()
    provider = ScriptedProvider()
    from agent_ai.core.executor import ToolExecutor

    runtime = AgentRuntime(
        provider=provider,
        executor=ToolExecutor(build_registry(root=tmp_path)),
        session_store=store,
        session_id=session.session_id,
    )
    prepared = PreparedTask(
        task="investigasi arsitektur",
        metadata={
            "mode": "fast",
            "escalate_to": "deep",
            "escalate_reason": "Architecture impact detected",
        },
    )
    result = runtime.run(prepared)
    assert result.status == RuntimeStatus.COMPLETED
    assert result.policy["requested_mode"] == "fast"
    assert result.policy["effective_mode"] == "deep"

    events = store.get_events(session_id=session.session_id)
    applied = [e for e in events if e.event_type.value == "policy_applied"]
    assert len(applied) == 1
    act = applied[0].payload["activity"]
    assert "Requested Mode: Fast" in act
    assert "Effective Mode: Deep" in act
    assert "Reason:" in act
    assert "Architecture impact detected" in act


# --------------------------------------------------------------------------- #
# 3. Backward Compatibility
# --------------------------------------------------------------------------- #
def test_task_without_mode_runs_successfully(tmp_path):
    store = InMemorySessionStore()
    session = store.create_session()
    provider = ScriptedProvider()
    from agent_ai.core.executor import ToolExecutor

    runtime = AgentRuntime(
        provider=provider,
        executor=ToolExecutor(build_registry(root=tmp_path)),
        session_store=store,
        session_id=session.session_id,
    )
    prepared = PreparedTask(task="task lama tanpa mode")
    result = runtime.run(prepared)
    assert result.status == RuntimeStatus.COMPLETED
    # Tanpa mode eksplisit di task, policy tidak aktif di runtime
    assert result.policy is None
