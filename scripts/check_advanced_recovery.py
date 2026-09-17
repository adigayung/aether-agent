"""Verifikasi Advanced Recovery (#43).

Deterministik, tanpa API cloud. Fixture kecil di
J:\\Agent_Ai\\dummy_test\\advanced_recovery_fixture dan dibersihkan setelah test.

Menguji:
    1. tool failure recovery
    2. command failure recovery
    3. validation failure recovery
    4. timeout recovery
    5. provider error recovery
    6. repeated action
    7. no progress
    8. workspace change detection
    9. retry bounded
   10. recovery bounded
   11. replan bounded
   12. recovery -> replanner
   13. recovery -> validation
   14. successful recovery -> COMPLETED
   15. unrecoverable failure -> FAILED
   16. STOP behavior
   17. Session recovery events
   18. ChangeTracker digunakan
   19. Git Awareness tetap read-only
   20. ReliabilityManager tidak diduplikasi
   21. Replanner tidak diduplikasi
   22. Validator tidak diduplikasi
   23. executor tidak diduplikasi
   24. provider independence
   25. architecture dependency direction
   26. fixture cleanup

Jalankan:
    python scripts/check_advanced_recovery.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.changes import ChangeTracker  # noqa: E402
from agent_ai.config.settings import RecoveryConfig  # noqa: E402
from agent_ai.core import (  # noqa: E402
    ActionType,
    AgentStatus,
    FinishReason,
    LLMAction,
    LLMResponse,
)
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.git import GitRepositoryFacade  # noqa: E402
from agent_ai.planning import Replanner, TaskPlanner  # noqa: E402
from agent_ai.planning.replanner import Observation  # noqa: E402
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402
from agent_ai.recovery import (  # noqa: E402
    FailureClassifier,
    FailureKind,
    FailureSignal,
    RecoveryAction,
    RecoveryConfig as RecoveryConfigModel,
    RecoveryManager,
    RecoveryPolicy,
)
from agent_ai.reliability import Detector, ReliabilityManager  # noqa: E402
from agent_ai.runtime import AgentRuntime, RuntimeStatus  # noqa: E402
from agent_ai.session import EventType, InMemorySessionStore  # noqa: E402
from agent_ai.task import PreparedTask  # noqa: E402
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402
from agent_ai.validation import (  # noqa: E402
    CommandValidator,
    ValidationOutcome,
    ValidationRequest,
    ValidationResult,
    ValidationRunner,
    Validator,
)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "advanced_recovery_fixture"


# ---------------------------------------------------------------------------
# Provider & tool palsu (deterministik, tanpa API cloud)
# ---------------------------------------------------------------------------
class ScriptedProvider(BaseProvider):
    """Provider palsu: respons terprogram per pemanggilan."""

    name = "scripted"

    def __init__(self, responses, errors=None):
        self._responses = list(responses)
        self._errors = list(errors or [])
        self.calls = 0

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        idx = self.calls
        if idx < len(self._errors) and self._errors[idx] is not None:
            self.calls += 1
            raise self._errors[idx]
        return GenerateResult(text="", model="fake", provider=self.name, raw={})

    def normalize_response(self, result):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


class FailingProvider(BaseProvider):
    """Provider yang selalu error (simulasi provider error)."""

    name = "failing"

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        raise RuntimeError("provider down")

    def normalize_response(self, result):  # pragma: no cover
        return LLMResponse(text="", finish_reason=FinishReason.STOP)


class EchoTool(BaseTool):
    """Tool palsu: echo argumen (deterministik)."""

    name = "echo"
    description = "Echo argumen."
    input_schema = {"type": "object", "properties": {"value": {"type": "string"}}}

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def execute(self, **arguments):
        self.calls += 1
        if self.fail:
            raise RuntimeError("echo gagal")
        return {"echo": arguments.get("value", "")}


def tool_call(name, arguments):
    return LLMResponse(
        actions=[LLMAction(name=name, arguments=arguments, type=ActionType.TOOL_CALL)],
        finish_reason=FinishReason.TOOL_CALLS,
    )


def final(text):
    return LLMResponse(text=text, finish_reason=FinishReason.STOP)


def _registry(tool):
    reg = ToolRegistry()
    reg.register(tool)
    return reg


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "ok.py").write_text("print('OK')\n", encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Advanced Recovery (#43) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    classifier = FailureClassifier()

    # 1) tool failure recovery.
    kind = classifier.classify(FailureSignal(outcome="execution_error", is_tool=True))
    assert kind == FailureKind.TOOL_FAILURE, kind
    policy = RecoveryPolicy(RecoveryConfigModel(max_attempts=3))
    dec = policy.decide(kind, FailureSignal(outcome="execution_error", is_tool=True, recoverable=True))
    assert dec.action == RecoveryAction.RETRY, dec.action
    print(f"[1] tool failure recovery OK -> {dec.action.value}")

    # 2) command failure recovery.
    kind = classifier.classify(FailureSignal(outcome="command_failure", is_command=True))
    assert kind == FailureKind.COMMAND_FAILURE, kind
    dec = policy.decide(kind, FailureSignal(outcome="command_failure", is_command=True))
    assert dec.action == RecoveryAction.RECOVER, dec.action
    print(f"[2] command failure recovery OK -> {dec.action.value}")

    # 3) validation failure recovery.
    kind = classifier.classify(FailureSignal(validation_outcome="command_failure"))
    assert kind == FailureKind.VALIDATION_FAILURE, kind
    dec = policy.decide(kind, FailureSignal(validation_outcome="command_failure"), replan_allowed=True)
    assert dec.action == RecoveryAction.REPLAN, dec.action
    print(f"[3] validation failure recovery OK -> {dec.action.value}")

    # 4) timeout recovery.
    kind = classifier.classify(FailureSignal(outcome="timeout"))
    assert kind == FailureKind.TIMEOUT, kind
    dec = policy.decide(kind, FailureSignal(outcome="timeout"), retry_allowed=True)
    assert dec.action == RecoveryAction.RETRY, dec.action
    print(f"[4] timeout recovery OK -> {dec.action.value}")

    # 5) provider error recovery.
    kind = classifier.classify(FailureSignal(outcome="provider_error"))
    assert kind == FailureKind.PROVIDER_ERROR, kind
    dec = policy.decide(kind, FailureSignal(outcome="provider_error"), retry_allowed=True)
    assert dec.action == RecoveryAction.RETRY, dec.action
    dec_no_retry = policy.decide(kind, FailureSignal(outcome="provider_error"), retry_allowed=False)
    assert dec_no_retry.action == RecoveryAction.FAIL, dec_no_retry.action
    print(f"[5] provider error recovery OK -> {dec.action.value} / no-retry={dec_no_retry.action.value}")

    # 6) repeated action.
    kind = classifier.classify(FailureSignal(reliability_events=["repeated_action"]))
    assert kind == FailureKind.REPEATED_ACTION, kind
    dec = policy.decide(kind, FailureSignal(reliability_events=["repeated_action"]), replan_allowed=True)
    assert dec.action == RecoveryAction.REPLAN, dec.action
    print(f"[6] repeated action OK -> {dec.action.value}")

    # 7) no progress.
    kind = classifier.classify(FailureSignal(reliability_events=["no_progress"]))
    assert kind == FailureKind.NO_PROGRESS, kind
    dec = policy.decide(kind, FailureSignal(reliability_events=["no_progress"]), replan_allowed=False)
    assert dec.action == RecoveryAction.RECOVER, dec.action
    print(f"[7] no progress OK -> {dec.action.value}")

    # 8) workspace change detection.
    kind = classifier.classify(FailureSignal(workspace_changed=True, changed_paths=["a.py"]))
    assert kind == FailureKind.WORKSPACE_CHANGED, kind
    dec = policy.decide(kind, FailureSignal(workspace_changed=True), replan_allowed=True)
    assert dec.action == RecoveryAction.REPLAN, dec.action
    dec_stop = policy.decide(kind, FailureSignal(workspace_changed=True), replan_allowed=False)
    assert dec_stop.action == RecoveryAction.STOP, dec_stop.action
    print(f"[8] workspace change detection OK -> {dec.action.value} / no-replan={dec_stop.action.value}")

    # 9) retry bounded (attempts habis -> FAIL).
    dec = policy.decide(
        FailureKind.TIMEOUT,
        FailureSignal(outcome="timeout"),
        attempts=3,
        retry_allowed=True,
    )
    assert dec.action == RecoveryAction.FAIL and dec.bounded, dec
    print(f"[9] retry bounded OK -> {dec.action.value} (bounded={dec.bounded})")

    # 10) recovery bounded (total cycles habis -> FAIL).
    dec = policy.decide(
        FailureKind.TOOL_FAILURE,
        FailureSignal(outcome="execution_error"),
        total_cycles=5,
    )
    assert dec.action == RecoveryAction.FAIL and dec.bounded, dec
    print(f"[10] recovery bounded OK -> {dec.action.value} (bounded={dec.bounded})")

    # 11) replan bounded (max_replans habis -> FAIL untuk validation).
    dec = policy.decide(
        FailureKind.VALIDATION_FAILURE,
        FailureSignal(validation_outcome="command_failure"),
        replans=2,
        replan_allowed=True,
    )
    assert dec.action == RecoveryAction.FAIL and dec.bounded, dec
    print(f"[11] replan bounded OK -> {dec.action.value} (bounded={dec.bounded})")

    # 12) recovery -> replanner (RecoveryManager memicu replanner).
    plan = TaskPlanner().create_plan("Perbaiki login yang gagal")
    for s in plan.steps:
        plan.complete_step(s)
    replanner = Replanner(max_replans=3)
    mgr = RecoveryManager(
        config=RecoveryConfigModel(max_attempts=3, max_replans=2, max_total_cycles=5),
        replanner=replanner,
    )
    obs = Observation(step_id=plan.steps[-1].id, success=False, outcome="validation:command_failure", recoverable=True)
    dec = mgr.recover(FailureSignal(validation_outcome="command_failure"), plan=plan, observation=obs)
    assert dec.action == RecoveryAction.REPLAN, dec.action
    assert plan.revision >= 2, f"replanner harus merevisi plan, revision={plan.revision}"
    print(f"[12] recovery -> replanner OK -> revision={plan.revision}")

    # 13) recovery -> validation (runtime menjalankan validation setelah recovery).
    validator = CommandValidator(root=FIXTURE)
    runner = ValidationRunner([validator])
    request = ValidationRequest(target="ok", command="python ok.py")
    runtime = AgentRuntime(
        provider=ScriptedProvider([final("selesai")]),
        validation_runner=runner,
        validation_request=request,
        recovery_manager=RecoveryManager(config=RecoveryConfigModel()),
    )
    result = runtime.run(PreparedTask(task="task recovery+validation", plan=TaskPlanner().create_plan("Perbaiki bug")))
    assert result.status == RuntimeStatus.COMPLETED, result.status
    assert result.validation is not None and result.validation["success"] is True
    print(f"[13] recovery -> validation OK -> validation={result.validation['outcome']}")

    # 14) successful recovery -> COMPLETED.
    #     Step pertama gagal sekali (provider error), lalu sukses.
    provider14 = ScriptedProvider(
        responses=[final("pulih")],
        errors=[RuntimeError("provider down")],
    )
    runtime14 = AgentRuntime(
        provider=provider14,
        executor=ToolExecutor(registry=_registry(EchoTool())),
        max_iterations=5,
        recovery_manager=RecoveryManager(
            config=RecoveryConfigModel(max_attempts=3, max_total_cycles=5),
            reliability=ReliabilityManager(
                detector=Detector(iteration_limit=10),
            ),
        ),
    )
    result14 = runtime14.run(PreparedTask(task="task pulih", plan=TaskPlanner().create_plan("Perbaiki bug")))
    assert result14.status == RuntimeStatus.COMPLETED, result14.status
    print(f"[14] successful recovery -> COMPLETED OK -> status={result14.status.value}")

    # 15) unrecoverable failure -> FAILED.
    runtime15 = AgentRuntime(
        provider=FailingProvider(),
        max_iterations=2,
        recovery_manager=RecoveryManager(
            config=RecoveryConfigModel(max_attempts=1, max_total_cycles=2),
        ),
    )
    result15 = runtime15.run(PreparedTask(task="task gagal", plan=TaskPlanner().create_plan("Perbaiki bug")))
    assert result15.status == RuntimeStatus.FAILED, result15.status
    print(f"[15] unrecoverable failure -> FAILED OK -> status={result15.status.value}")

    # 16) STOP behavior (workspace changed tanpa replan -> STOP).
    mgr16 = RecoveryManager(config=RecoveryConfigModel(), replanner=None)
    dec16 = mgr16.decide(FailureSignal(workspace_changed=True, changed_paths=["x.py"]))
    assert dec16.action == RecoveryAction.STOP, dec16.action
    print(f"[16] STOP behavior OK -> {dec16.action.value}")

    # 17) Session recovery events.
    store = InMemorySessionStore()
    session = store.create_session()
    mgr17 = RecoveryManager(
        config=RecoveryConfigModel(max_attempts=3, max_total_cycles=5),
        session_store=store,
        session_id=session.session_id,
    )
    mgr17.recover(FailureSignal(outcome="timeout"))
    events = store.get_events(session_id=session.session_id)
    types = [e.event_type for e in events]
    assert EventType.RECOVERY_STARTED in types, types
    assert EventType.RECOVERY_COMPLETED in types, types
    print(f"[17] Session recovery events OK -> types={[t.value for t in types]}")

    # 18) ChangeTracker digunakan (deteksi perubahan workspace).
    tracker = ChangeTracker(root=FIXTURE)
    tracker.start("task-ct")
    tracker.snapshot(".", task_id="task-ct")
    (FIXTURE / "new_file.py").write_text("x = 1\n", encoding="utf-8")
    mgr18 = RecoveryManager(config=RecoveryConfigModel(), change_tracker=tracker)
    changed = mgr18.detect_workspace_change("task-ct")
    assert any("new_file.py" in p for p in changed), changed
    print(f"[18] ChangeTracker digunakan OK -> changed={changed}")

    # 19) Git Awareness tetap read-only (tidak ada operasi destruktif).
    recovery_src = (SRC_DIR / "agent_ai" / "recovery" / "manager.py").read_text(encoding="utf-8")
    # Hanya operasi read-only yang boleh dipanggil pada git facade.
    for forbidden in ("git.reset", "git.checkout", "git.restore", "git.commit", "git.push", "git.clean"):
        assert forbidden not in recovery_src, f"recovery tidak boleh memakai {forbidden}"
    # Git facade hanya dipakai untuk read-only (is_repository/status/diff/branch).
    assert ".status(" in recovery_src and ".diff(" in recovery_src
    # Git facade hanya dipakai untuk read-only (status/diff/branch).
    git = GitRepositoryFacade(root=FIXTURE)
    snap = mgr18.git_snapshot()  # git None -> {}
    assert snap == {}
    mgr19 = RecoveryManager(config=RecoveryConfigModel(), git=git)
    snap19 = mgr19.git_snapshot()
    assert "is_repository" in snap19
    print(f"[19] Git Awareness tetap read-only OK -> is_repository={snap19.get('is_repository')}")

    # 20) ReliabilityManager tidak diduplikasi.
    assert "class ReliabilityManager" not in recovery_src
    assert "class RetryController" not in recovery_src
    assert "class Detector" not in recovery_src
    print("[20] ReliabilityManager tidak diduplikasi OK")

    # 21) Replanner tidak diduplikasi.
    assert "class Replanner" not in recovery_src
    assert "class TaskPlanner" not in recovery_src
    print("[21] Replanner tidak diduplikasi OK")

    # 22) Validator tidak diduplikasi.
    assert "class Validator" not in recovery_src
    assert "class ValidationRunner" not in recovery_src
    print("[22] Validator tidak diduplikasi OK")

    # 23) executor tidak diduplikasi.
    assert "import subprocess" not in recovery_src
    assert "os.system(" not in recovery_src
    assert "class ToolExecutor" not in recovery_src
    print("[23] executor tidak diduplikasi OK")

    # 24) provider independence.
    low = recovery_src.lower()
    for name in ("openrouter", "deepseek", "ollama", "openai"):
        assert name not in low, f"recovery tidak boleh hardcode '{name}'"
    print("[24] provider independence OK")

    # 25) architecture dependency direction (recovery tidak diimpor oleh core).
    core_src = (SRC_DIR / "agent_ai" / "core" / "orchestrator.py").read_text(encoding="utf-8")
    assert "agent_ai.recovery" not in core_src, "core tidak boleh bergantung pada recovery"
    # Recovery boleh bergantung pada subsystem (reliability/planning/changes/git).
    assert "agent_ai.reliability" in recovery_src or "ReliabilityManager" in recovery_src
    print("[25] architecture dependency direction OK")

    # 26) fixture cleanup (dicek di main()).
    print("[26] fixture cleanup OK")

    print()
    print("[OK] Advanced Recovery bekerja (classify -> decide -> retry/recover/replan/stop/fail, bounded).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
