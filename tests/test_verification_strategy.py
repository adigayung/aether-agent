"""Tests: Mode-aware verification strategy integrated with execution policy.

Membuktikan integration antara Agent Execution Policy (fast/balanced/deep) dan
verification flow:

    1. VerificationStrategy: metadata per mode + check list + telemetry format.
    2. Runtime mengaitkan verification_strategy dengan effective_mode (termasuk
       setelah escalation), dan mempropagalkannya ke validation request metadata.
    3. No hard blocking: Fast tidak menolak test tambahan; Deep tidak memaksa
       seluruh repo test; LLM tetap menentukan completion.
    4. Backward compatibility: task tanpa mode -> verification_strategy None,
       behavior persis sebelumnya.

Jalankan:
    python -m pytest tests/test_verification_strategy.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.runtime.models import RuntimeStatus  # noqa: E402
from agent_ai.runtime.policy import (  # noqa: E402
    DEFAULT_MODE,
    ExecutionPolicyResolver,
)
from agent_ai.runtime.runtime import AgentRuntime  # noqa: E402
from agent_ai.session.store import InMemorySessionStore  # noqa: E402
from agent_ai.task.models import PreparedTask  # noqa: E402
from agent_ai.validation.models import (  # noqa: E402
    ValidationOutcome,
    ValidationRequest,
    ValidationResult,
)
from agent_ai.validation.runner import ValidationRunner  # noqa: E402
from agent_ai.validation.strategy import (  # noqa: E402
    VerificationStrategy,
    format_verification_activity,
    strategy_for_mode,
)
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.tools.registry import build_registry  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402


# --------------------------------------------------------------------------- #
# Scripted provider (sama pola test_agent_execution_policy.py)
# --------------------------------------------------------------------------- #
def _tool_call(call_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _tool_turn(text: str, calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"content": text, "tool_calls": calls},
                "finish_reason": "tool_calls",
            }
        ]
    }


def _final_turn(text: str) -> Dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


def _msg_to_dict(message: Any) -> Dict[str, Any]:
    if isinstance(message, dict):
        return dict(message)
    return {
        "role": getattr(message, "role", ""),
        "content": getattr(message, "content", ""),
    }


class ScriptedProvider(OpenAICompatibleProvider):
    name = "scripted"

    def __init__(self, script: List[Dict[str, Any]]) -> None:
        self.config = SimpleNamespace(model="scripted-model")
        self.script = list(script)
        self.calls = 0
        self.requests: List[List[Dict[str, Any]]] = []

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Any]] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[List[Any]] = None,
        tool_choice: Optional[Any] = None,
    ) -> GenerateResult:
        self.requests.append([_msg_to_dict(m) for m in (messages or [])])
        idx = self.calls
        self.calls += 1
        raw = self.script[idx] if idx < len(self.script) else _final_turn("(default)")
        return GenerateResult(text="", model="scripted-model", provider=self.name, raw=raw)


def executor(root: Path) -> ToolExecutor:
    return ToolExecutor(build_registry(root=root))


# --------------------------------------------------------------------------- #
# 1. VerificationStrategy metadata per mode
# --------------------------------------------------------------------------- #
def test_fast_strategy_has_light_checks():
    s = strategy_for_mode("fast")
    assert s.mode == "fast"
    assert "syntax validation" in s.checks
    assert any("changed" in c for c in s.checks)


def test_balanced_strategy_is_default_normal():
    s = strategy_for_mode("balanced")
    assert s.mode == "balanced"
    assert "related tests" in s.checks
    assert "existing checker" in s.checks


def test_deep_strategy_has_wide_checks():
    s = strategy_for_mode("deep")
    assert s.mode == "deep"
    assert "regression suite" in s.checks
    assert "architecture validation" in s.checks
    assert "integration validation" in s.checks


def test_strategy_for_unknown_mode_falls_back_balanced():
    s = strategy_for_mode("tidak-ada")
    assert s.mode == DEFAULT_MODE


# --------------------------------------------------------------------------- #
# 2. Telemetry format
# --------------------------------------------------------------------------- #
def test_verification_activity_format_for_fast():
    s = strategy_for_mode("fast")
    text = format_verification_activity(s)
    assert "[VERIFY]" in text
    assert "Mode: Fast" in text
    assert "Checks:" in text
    assert "- syntax validation" in text


def test_verification_activity_format_for_deep():
    s = strategy_for_mode("deep")
    text = format_verification_activity(s)
    assert "[VERIFY]" in text
    assert "Mode: Deep" in text
    assert "- regression suite" in text


def test_verification_activity_on_escalation_shows_change():
    s = strategy_for_mode("deep")
    text = format_verification_activity(s, previous_mode="fast")
    assert "Effective Mode changed:" in text
    assert "Fast → Deep" in text


# --------------------------------------------------------------------------- #
# 3. Runtime attaches verification_strategy to effective_mode
# --------------------------------------------------------------------------- #
def test_runtime_sets_fast_verification_strategy(tmp_path):
    provider = ScriptedProvider([_final_turn("selesai")])
    runtime = AgentRuntime(
        provider=provider,
        executor=executor(tmp_path),
        requested_mode="fast",
    )
    result = runtime.run(PreparedTask(task="task kecil", task_id="fast-ver"))
    assert result.status == RuntimeStatus.COMPLETED
    assert runtime.verification_strategy is not None
    assert runtime.verification_strategy.mode == "fast"


def test_runtime_balanced_strategy_when_explicitly_requested(tmp_path):
    runtime = AgentRuntime(
        provider=ScriptedProvider([_final_turn("selesai")]),
        executor=executor(tmp_path),
        requested_mode="balanced",
    )
    result = runtime.run(PreparedTask(task="normal"))
    assert result.status == RuntimeStatus.COMPLETED
    assert runtime.verification_strategy is not None
    assert runtime.verification_strategy.mode == "balanced"


def test_runtime_deep_strategy_is_deep(tmp_path):
    runtime = AgentRuntime(
        provider=ScriptedProvider([_final_turn("selesai")]),
        executor=executor(tmp_path),
        requested_mode="deep",
    )
    result = runtime.run(PreparedTask(task="refactor besar", task_id="deep-ver"))
    assert result.status == RuntimeStatus.COMPLETED
    assert runtime.verification_strategy is not None
    assert runtime.verification_strategy.mode == "deep"
    assert "regression suite" in runtime.verification_strategy.checks


# --------------------------------------------------------------------------- #
# 4. Escalation updates verification strategy dynamically
# --------------------------------------------------------------------------- #
def test_escalation_updates_verification_strategy(tmp_path):
    provider = ScriptedProvider([_final_turn("selesai")])
    runtime = AgentRuntime(
        provider=provider,
        executor=executor(tmp_path),
        requested_mode="fast",
    )
    runtime.run(PreparedTask(task="t1", task_id="esc"))
    assert runtime.verification_strategy.mode == "fast"

    runtime.escalate_policy("arsitektur", "deep")
    assert runtime.verification_strategy.mode == "deep"
    assert "regression suite" in runtime.verification_strategy.checks


def test_escalation_emits_verify_activity_event(tmp_path):
    store = InMemorySessionStore()
    session = store.create_session(metadata={"task_id": "esc-ev"})
    runtime = AgentRuntime(
        provider=ScriptedProvider([_final_turn("selesai")]),
        executor=executor(tmp_path),
        session_store=store,
        session_id=session.session_id,
        requested_mode="fast",
    )
    runtime.run(PreparedTask(task="t", task_id="esc-ev"))
    runtime.escalate_policy("arch", "deep")
    events = store.get_events(session_id=session.session_id)
    verfy_events = [e for e in events if e.event_type.value == "verification_strategy_applied"]
    assert len(verfy_events) >= 2  # initial + after escalation
    escalated = [
        e for e in verfy_events
        if "Effective Mode changed" in (e.payload.get("activity") or "")
    ]
    assert len(escalated) >= 1
    assert "Fast → Deep" in escalated[0].payload["activity"]


# --------------------------------------------------------------------------- #
# 5. No hard blocking
# --------------------------------------------------------------------------- #
class _CapturingValidator:
    """Validator sederhana untuk memverifikasi strategi dipropagasikan."""

    name = "capturing"

    def __init__(self) -> None:
        self.last_request: Optional[ValidationRequest] = None

    def validate(self, request: ValidationRequest) -> ValidationResult:
        self.last_request = request
        return ValidationResult(success=True, outcome=ValidationOutcome.SUCCESS, validator="capturing")


def test_fast_does_not_reject_extra_tests_when_requested(tmp_path):
    """Fast tidak boleh menolak test tambahan bila Agent/runner memilih menjalankannya."""
    capturing = _CapturingValidator()
    runner = ValidationRunner([capturing])
    req = ValidationRequest(target="extra-tests", command="python -m pytest tests/")
    runtime = AgentRuntime(
        provider=ScriptedProvider([_tool_turn("l", [_tool_call("c1", "run_command", {"command": "python -m pytest tests/"})]),
                                   _final_turn("selesai")]),
        executor=executor(tmp_path),
        requested_mode="fast",
        validation_runner=runner,
        validation_request=req,
        use_continuous_loop=True,
    )
    result = runtime.run(PreparedTask(task="task", task_id="fast-no-block"))
    # Fast tidak boleh mencegah test dijalankan; strategy tetap fast.
    assert runtime.verification_strategy.mode == "fast"
    assert capturing.last_request is not None
    # Strategi propagasikan ke metadata request (bukan blocking).
    meta = capturing.last_request.metadata
    assert meta.get("effective_mode") == "fast"
    assert "verification_strategy" in meta


def test_deep_does_not_force_full_repo_tests(tmp_path):
    """Deep memberi preferensi regression, tapi tidak memaksa seluruh repo test."""
    capturing = _CapturingValidator()
    runner = ValidationRunner([capturing])
    req = ValidationRequest(target="single-check", command="python -m pytest tests/test_one.py")
    runtime = AgentRuntime(
        provider=ScriptedProvider([_final_turn("selesai")]),
        executor=executor(tmp_path),
        requested_mode="deep",
        validation_runner=runner,
        validation_request=req,
    )
    result = runtime.run(PreparedTask(task="refactor", task_id="deep-no-force"))
    assert result.status == RuntimeStatus.COMPLETED
    assert runtime.verification_strategy.mode == "deep"
    assert "regression suite" in runtime.verification_strategy.checks
    # Runner tetap pakai request asli (Deep tidak mengganti/menambah command).
    assert capturing.last_request.command == "python -m pytest tests/test_one.py"


def test_llm_completion_unaffected_by_strategy(tmp_path):
    """Completion (DONE) hany dari response LLM, bukan dari verification."""
    provider = ScriptedProvider([_final_turn("selesai")])
    runtime = AgentRuntime(
        provider=provider,
        executor=executor(tmp_path),
        requested_mode="deep",
    )
    result = runtime.run(PreparedTask(task="kerjakan"))
    assert result.status == RuntimeStatus.COMPLETED
    assert provider.calls == 1


# --------------------------------------------------------------------------- #
# 7. Backward compatibility
# --------------------------------------------------------------------------- #
def test_no_mode_no_verification_strategy(tmp_path):
    runtime = AgentRuntime(
        provider=ScriptedProvider([_final_turn("selesai")]),
        executor=executor(tmp_path),
    )
    result = runtime.run(PreparedTask(task="tanpa mode"))
    assert result.status == RuntimeStatus.COMPLETED
    assert runtime.verification_strategy is None
    # _run_validation tidak menambahkan metadata strategi bila tidak ada mode.
    # (validation tidak aktif di sini sehingga tidak dipanggil.)


def test_verification_advisory_empty_when_no_mode(tmp_path):
    runtime = AgentRuntime(
        provider=ScriptedProvider([_final_turn("selesai")]),
        executor=executor(tmp_path),
    )
    runtime.run(PreparedTask(task="t"))
    # Tidak ada advisory jika policy tidak aktif.
    assert runtime._verification_advisory() == ""


def test_verification_advisory_present_when_fast(tmp_path):
    runtime = AgentRuntime(
        provider=ScriptedProvider([_final_turn("selesai")]),
        executor=executor(tmp_path),
        requested_mode="fast",
    )
    runtime.run(PreparedTask(task="t"))
    adv = runtime._verification_advisory()
    assert "Mode: Fast" in adv
    assert "syntax validation" in adv
    # Header "Strategi verifikasi" muncul di _build_continuous_task,
    # bukan di _verification_advisory.
    prepared = PreparedTask(task="t")
    full_task_text = runtime._build_continuous_task(prepared)
    assert "Strategi verifikasi" in full_task_text


def test_task_log_records_verification_strategy(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    runtime = AgentRuntime(
        provider=ScriptedProvider([_final_turn("selesai")]),
        executor=executor(project),
        project_root=str(project),
        requested_mode="fast",
    )
    result = runtime.run(PreparedTask(task="kerjakan", task_id="ver-log"))
    assert result.status == RuntimeStatus.COMPLETED

    log_path = project / ".aether" / "log" / "ver-log.log"
    assert log_path.is_file()
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    applied = [e for e in events if e["event"] == "verification_strategy_applied"]
    assert len(applied) == 1
    data = applied[0]["data"]
    assert data["mode"] == "fast"
    assert "activity" in data
    assert "[VERIFY]" in data["activity"]
