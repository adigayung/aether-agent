"""Tests: Agent Execution Policy (fast/balanced/deep) + dynamic escalation.

Membuktikan sistem policy ADDITIVE untuk Agent Execution Policy:

    1. Policy resolver: metadata policy per mode (exploration/planning/
       verification/context) + normalisasi & default.
    2. Default `balanced`: task tanpa mode -> requested=effective=balanced dan
       TIDAK mengubah perilaku task lama.
    3. requested_mode vs effective_mode dipisah; escalation fast -> balanced ->
       deep (naik saja) dengan alasan tersimpan.
    4. Escalation flow lewat orchestrator/runtime (mekanisme, BUKAN rule
       keyword): Agent meminta escalation -> policy diperbarui -> orchestrator
       melihat effective_mode baru & loop tetap murni keputusan LLM.
    5. Metadata propagation dari task metadata -> runtime (requested_mode).
    6. Activity/logging policy ([POLICY] blok + event `policy_applied`/
       `policy_escalated`).
    7. Backward compatibility: runtime tanpa mode tidak berubah; Consultant
       tidak terpengaruh; TaskStatus/TaskPhase core tidak berubah.

Deterministik, tanpa network/LLM nyata (provider scripted).

Jalankan:
    python -m pytest tests/test_agent_execution_policy.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.consultant.service import ConsultantService  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.runtime.models import RuntimeStatus  # noqa: E402
from agent_ai.runtime.policy import (  # noqa: E402
    DEFAULT_MODE,
    ESCALATION_ORDER,
    ExecutionPolicyResolver,
    ExecutionPolicyState,
    PolicyEscalationError,
    available_modes,
    format_escalation_block,
    format_policy_block,
    get_policy,
    is_valid_mode,
    mode_order,
    next_mode,
    normalize_mode,
    policy_activity_text,
)
from agent_ai.runtime.runtime import AgentRuntime  # noqa: E402
from agent_ai.session.store import InMemorySessionStore  # noqa: E402
from agent_ai.task.models import PreparedTask  # noqa: E402
from agent_ai.tasks.models import TaskPhase, TaskStatus  # noqa: E402
from agent_ai.tools.registry import build_registry  # noqa: E402


# --------------------------------------------------------------------------- #
# Provider palsu (scripted) + helper respons OpenAI-compatible
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
    """Provider palsu: respons skrip berurutan (tanpa network)."""

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
# 1. Resolver policy: metadata per mode + normalisasi
# --------------------------------------------------------------------------- #
def test_three_modes_have_policy_metadata():
    assert available_modes() == ["fast", "balanced", "deep"]
    for mode in ESCALATION_ORDER:
        policy = get_policy(mode)
        assert policy.mode == mode
        assert policy.exploration_level
        assert policy.planning_depth
        assert policy.verification_preference
        assert policy.context_preference
        assert policy.label
        assert policy.to_dict()["mode"] == mode


def test_modes_have_distinct_preferences():
    fast = get_policy("fast")
    balanced = get_policy("balanced")
    deep = get_policy("deep")
    assert fast.exploration_level != deep.exploration_level
    assert fast.context_preference != deep.context_preference
    assert balanced.mode == DEFAULT_MODE


def test_normalize_mode_rules():
    assert normalize_mode("fast") == "fast"
    assert normalize_mode("DEEP") == "deep"
    assert normalize_mode("  Balanced ") == "balanced"
    # alias TaskComposer (minimal -> fast) tanpa memaksa perubahan UI.
    assert normalize_mode("minimal") == "fast"
    # tidak dikenal / kosong -> default balanced.
    assert normalize_mode("unknown") == DEFAULT_MODE
    assert normalize_mode(None) == DEFAULT_MODE
    assert normalize_mode("") == DEFAULT_MODE
    assert is_valid_mode("fast") and not is_valid_mode("unknown")


def test_escalation_order_helpers():
    assert mode_order("fast") < mode_order("balanced") < mode_order("deep")
    assert next_mode("fast") == "balanced"
    assert next_mode("balanced") == "deep"
    assert next_mode("deep") is None


# --------------------------------------------------------------------------- #
# 2. Default balanced (task tanpa mode)
# --------------------------------------------------------------------------- #
def test_resolver_default_is_balanced_when_no_mode():
    state = ExecutionPolicyResolver().resolve(None)
    assert state.requested_mode == "balanced"
    assert state.effective_mode == "balanced"
    assert state.reason  # alasan default diisi
    assert state.escalated is False


def test_resolver_reads_mode_from_metadata():
    resolver = ExecutionPolicyResolver()
    state = resolver.resolve(None, metadata={"mode": "deep"})
    assert state.requested_mode == "deep"
    assert state.effective_mode == "deep"
    # requested eksplisit menang atas metadata.
    state2 = resolver.resolve("fast", metadata={"mode": "deep"})
    assert state2.requested_mode == "fast"
    # agent_mode > policy_mode > mode.
    state3 = resolver.resolve(None, metadata={"agent_mode": "fast", "mode": "deep"})
    assert state3.requested_mode == "fast"


def test_resolver_unknown_mode_falls_back_to_default():
    state = ExecutionPolicyResolver().resolve("tidak-ada-mode-ini")
    assert state.requested_mode == DEFAULT_MODE
    assert state.effective_mode == DEFAULT_MODE
    assert state.reason


# --------------------------------------------------------------------------- #
# 3. requested_mode vs effective_mode + escalation
# --------------------------------------------------------------------------- #
def test_requested_and_effective_separated_on_escalation():
    resolver = ExecutionPolicyResolver()
    state = resolver.resolve("fast")
    assert state.requested_mode == "fast"
    assert state.effective_mode == "fast"

    state.escalate("Architecture investigation required")
    assert state.requested_mode == "fast"  # permintaan user TIDAK diubah
    assert state.effective_mode == "balanced"
    assert state.reason == "Architecture investigation required"
    assert state.escalated is True
    assert state.escalations[0]["from"] == "fast"
    assert state.escalations[0]["to"] == "balanced"


def test_escalation_can_skip_level_fast_to_deep():
    resolver = ExecutionPolicyResolver()
    state = resolver.resolve("fast")
    resolver.escalate(state, "Butuh investigasi arsitektur", target_mode="deep")
    assert state.requested_mode == "fast"
    assert state.effective_mode == "deep"


@pytest.mark.parametrize("bad_reason", ["", "   ", None])
def test_escalation_requires_reason(bad_reason):
    state = ExecutionPolicyResolver().resolve("fast")
    with pytest.raises(PolicyEscalationError):
        state.escalate(bad_reason)


def test_escalation_cannot_go_backwards_or_duplicate():
    state = ExecutionPolicyResolver().resolve("balanced")
    with pytest.raises(PolicyEscalationError):
        state.escalate("mundur", target_mode="fast")
    with pytest.raises(PolicyEscalationError):
        state.escalate("sama", target_mode="balanced")
    with pytest.raises(PolicyEscalationError):
        state.escalate("tidak dikenal", target_mode="super-deep")


def test_escalation_stops_at_highest_mode():
    state = ExecutionPolicyResolver().resolve("deep")
    assert state.can_escalate() is False
    with pytest.raises(PolicyEscalationError):
        state.escalate("tidak ada mode lebih tinggi")


def test_no_keyword_heuristic_escalation():
    """Escalation TIDAK boleh dipicu keyword task (fast tetap fast)."""
    resolver = ExecutionPolicyResolver()
    for prompt in ("refactor seluruh arsitektur", "ubah 200 file sekaligus"):
        state = resolver.resolve("fast", metadata={"mode": "fast"})
        assert state.effective_mode == "fast", prompt
        # task prompt sendiri tidak dibaca oleh resolver.
        assert state.escalated is False


# --------------------------------------------------------------------------- #
# 4. Escalation flow di runtime/orchestrator (mekanisme, bukan rule)
# --------------------------------------------------------------------------- #
def test_orchestrator_escalation_mechanism_keeps_loop_pure(tmp_path):
    """Agent meminta escalation -> policy diperbarui -> loop tetap keputusan LLM."""
    provider = ScriptedProvider([_final_turn("selesai")])
    escalations: List[Dict[str, Any]] = []

    def escalator(reason: str, target_mode: Optional[str]) -> Any:
        escalations.append({"reason": reason, "target": target_mode})
        return SimpleNamespace(to_dict=lambda: {"effective_mode": "deep"})

    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=executor(tmp_path),
        execution_policy={"requested_mode": "fast", "effective_mode": "fast"},
        policy_escalator=escalator,
    )
    assert orchestrator.effective_mode == "fast"

    orchestrator.request_policy_escalation("Arsitektur perlu ditelusuri", "deep")

    assert escalations == [
        {"reason": "Arsitektur perlu ditelusuri", "target": "deep"}
    ]
    # Snapshot policy orchestrator mengikuti escalation terbaru (metadata saja).
    assert orchestrator.effective_mode == "deep"

    # Loop tetap murni keputusan LLM: escalation tidak menyelesaikan task.
    result = orchestrator.run("kerjakan task")
    assert result.status.value == "done"
    assert result.result == "selesai"
    assert provider.calls == 1


def test_orchestrator_escalation_is_noop_without_mechanism(tmp_path):
    orchestrator = AgentOrchestrator(
        provider=ScriptedProvider([_final_turn("ok")]),
        executor=executor(tmp_path),
    )
    assert orchestrator.effective_mode is None
    assert orchestrator.request_policy_escalation("apapun") is None


def test_runtime_escalate_policy_updates_state_and_emits_event(tmp_path):
    store = InMemorySessionStore()
    session = store.create_session(metadata={"task_id": "pol"})
    runtime = AgentRuntime(
        provider=ScriptedProvider([_final_turn("selesai")]),
        executor=executor(tmp_path),
        session_store=store,
        session_id=session.session_id,
        requested_mode="fast",
    )
    result = runtime.run(PreparedTask(task="task kecil", task_id="pol"))
    assert result.status == RuntimeStatus.COMPLETED
    assert result.policy["requested_mode"] == "fast"
    assert result.policy["effective_mode"] == "fast"

    # Mekanisme escalation: state diperbarui + event dipancarkan.
    state = runtime.escalate_policy("Architecture investigation required", "deep")
    assert state.effective_mode == "deep"
    assert state.requested_mode == "fast"
    assert runtime.policy.effective_mode == "deep"

    types = [e.event_type.value for e in store.get_events(session_id=session.session_id)]
    assert types.count("policy_applied") == 1
    assert types.count("policy_escalated") == 1
    escalated = [
        e for e in store.get_events(session_id=session.session_id)
        if e.event_type.value == "policy_escalated"
    ][0]
    assert escalated.payload["from_mode"] == "fast"
    assert escalated.payload["to_mode"] == "deep"
    assert escalated.payload["reason"] == "Architecture investigation required"
    assert "Fast → Deep" in escalated.payload["activity"]
    assert "Architecture investigation required" in escalated.payload["activity"]

    # Escalation TIDAK mengubah status task (lifecycle tidak disentuh).
    assert result.status == RuntimeStatus.COMPLETED


def test_runtime_escalate_policy_is_none_when_policy_inactive(tmp_path):
    runtime = AgentRuntime(
        provider=ScriptedProvider([_final_turn("selesai")]),
        executor=executor(tmp_path),
    )
    result = runtime.run(PreparedTask(task="task tanpa mode"))
    assert result.status == RuntimeStatus.COMPLETED
    assert result.policy is None
    assert runtime.escalate_policy("alasan") is None


# --------------------------------------------------------------------------- #
# 5. Metadata propagation sampai runtime
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({"mode": "fast"}, "fast"),
        ({"agent_mode": "deep"}, "deep"),
        ({"mode": "minimal"}, "fast"),  # alias TaskComposer
        ({"policy_mode": "balanced"}, "balanced"),
    ],
)
def test_mode_from_task_metadata_reaches_runtime(tmp_path, metadata, expected):
    provider = ScriptedProvider([_final_turn("selesai")])
    runtime = AgentRuntime(
        provider=provider,
        executor=executor(tmp_path),
        options=GenerateOptions(model="scripted-model"),
    )
    prepared = PreparedTask(task="kerjakan", metadata=dict(metadata), task_id="meta")
    result = runtime.run(prepared)
    assert result.status == RuntimeStatus.COMPLETED
    assert result.policy is not None
    assert result.policy["requested_mode"] == expected
    assert result.policy["effective_mode"] == expected


def test_runtime_requested_mode_argument_wins_over_metadata(tmp_path):
    runtime = AgentRuntime(
        provider=ScriptedProvider([_final_turn("selesai")]),
        executor=executor(tmp_path),
        requested_mode="deep",
    )
    prepared = PreparedTask(task="kerjakan", metadata={"mode": "fast"})
    result = runtime.run(prepared)
    assert result.policy["requested_mode"] == "deep"


def test_declarative_escalation_from_metadata(tmp_path):
    provider = ScriptedProvider([_final_turn("selesai")])
    runtime = AgentRuntime(
        provider=provider,
        executor=executor(tmp_path),
    )
    prepared = PreparedTask(
        task="investigasi arsitektur",
        metadata={
            "mode": "fast",
            "escalate_to": "deep",
            "escalate_reason": "Butuh pemahaman lintas modul",
        },
    )
    result = runtime.run(prepared)
    assert result.policy["requested_mode"] == "fast"
    assert result.policy["effective_mode"] == "deep"
    assert result.policy["reason"] == "Butuh pemahaman lintas modul"
    assert result.policy["escalated"] is True


def test_invalid_declarative_escalation_is_ignored(tmp_path):
    """Permintaan escalation tidak valid TIDAK menggagalkan task."""
    runtime = AgentRuntime(
        provider=ScriptedProvider([_final_turn("selesai")]),
        executor=executor(tmp_path),
    )
    prepared = PreparedTask(
        task="kerjakan",
        metadata={"mode": "deep", "escalate_to": "fast"},  # mundur -> invalid
    )
    result = runtime.run(prepared)
    assert result.status == RuntimeStatus.COMPLETED
    assert result.policy["effective_mode"] == "deep"
    assert result.policy["escalated"] is False


# --------------------------------------------------------------------------- #
# 6. Activity / logging policy
# --------------------------------------------------------------------------- #
def test_policy_block_format_without_escalation():
    state = ExecutionPolicyResolver().resolve("fast")
    text = format_policy_block(state)
    assert "[POLICY]" in text
    assert "Requested Mode: Fast" in text
    assert "Effective Mode: Fast" in text


def test_escalation_block_format():
    state = ExecutionPolicyResolver().resolve("fast")
    state.escalate("Architecture investigation required", target_mode="deep")
    text = format_escalation_block("fast", state)
    assert "[POLICY]" in text
    assert "Escalated:" in text
    assert "Fast → Deep" in text
    assert "Reason:" in text
    assert "Architecture investigation required" in text
    # policy_activity_text memilih blok escalation saat ada escalation.
    assert policy_activity_text(state) == text


def test_policy_activity_in_task_log(tmp_path):
    """Activity policy ikut tercatat di Task Log project-local (.aether/log)."""
    project = tmp_path / "proj"
    project.mkdir()
    runtime = AgentRuntime(
        provider=ScriptedProvider([_final_turn("selesai")]),
        executor=executor(project),
        project_root=str(project),
        requested_mode="fast",
    )
    result = runtime.run(PreparedTask(task="kerjakan", task_id="policy-log"))
    assert result.status == RuntimeStatus.COMPLETED

    log_path = project / ".aether" / "log" / "policy-log.log"
    assert log_path.is_file()
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    applied = [e for e in events if e["event"] == "policy_applied"]
    assert len(applied) == 1
    data = applied[0]["data"]
    assert data["requested_mode"] == "fast"
    assert data["effective_mode"] == "fast"
    assert "[POLICY]" in data["activity"]


# --------------------------------------------------------------------------- #
# 7. Backward compatibility
# --------------------------------------------------------------------------- #
def test_task_without_mode_keeps_previous_behavior(tmp_path):
    """Task lama tanpa mode: hasil & jumlah panggilan provider TIDAK berubah."""
    provider = ScriptedProvider(
        [
            _tool_turn("tulis", [_tool_call("c1", "write_file", {"path": "a.txt", "content": "hi"})]),
            _final_turn("selesai"),
        ]
    )
    runtime = AgentRuntime(
        provider=provider,
        executor=executor(tmp_path),
        options=GenerateOptions(model="scripted-model"),
    )
    result = runtime.run(PreparedTask(task="buat file"))
    assert result.status == RuntimeStatus.COMPLETED
    assert provider.calls == 2
    assert result.steps == []
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hi"
    # Tanpa mode -> policy tidak diaktifkan (tidak ada event/kejutan).
    assert result.policy is None


def test_core_task_status_and_phase_unchanged():
    """Policy SENGAJA bukan bagian enum TaskStatus/TaskPhase core."""
    assert {s.value for s in TaskStatus} == {
        "created", "preparing", "planning", "running",
        "validating", "completed", "failed", "cancelled",
    }
    assert {p.value for p in TaskPhase} == {
        "none", "preparation", "planning", "execution",
        "tool_execution", "validation", "finalization",
    }
    assert "fast" not in {s.value for s in TaskStatus}
    assert "deep" not in {p.value for p in TaskPhase}


def test_consultant_unaffected_by_policy(tmp_path, monkeypatch):
    """Consultant TIDAK memakai policy Agent (default AgentRuntime tidak bocor)."""
    captured: Dict[str, Any] = {}

    def fake_run_continuous_loop(self, task, **kwargs):
        captured["policy"] = self.execution_policy
        from agent_ai.core.orchestrator import OrchestratorResult
        from agent_ai.core.models import AgentStatus

        return OrchestratorResult(
            status=AgentStatus.DONE, result="jawaban", error=None, iterations=1, steps=[]
        )

    monkeypatch.setattr(
        AgentOrchestrator, "run_continuous_loop", fake_run_continuous_loop
    )

    service = ConsultantService()
    service.consult(
        "pertanyaan",
        provider=ScriptedProvider([_final_turn("jawaban")]),
        root=str(tmp_path),
        session_id="cons",
        mode="quick",
    )
    assert captured["policy"] is None


def test_runtime_result_policy_field_is_optional_in_to_dict(tmp_path):
    """RuntimeResult.to_dict selalu memuat kunci `policy` (None bila nonaktif)."""
    runtime = AgentRuntime(
        provider=ScriptedProvider([_final_turn("selesai")]),
        executor=executor(tmp_path),
    )
    result = runtime.run(PreparedTask(task="tanpa mode"))
    data = result.to_dict()
    assert "policy" in data
    assert data["policy"] is None
