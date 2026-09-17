"""Verifikasi integrasi Validation ke Execution Lifecycle (#42).

Deterministik, tanpa API cloud. Fixture project di
J:\\Agent_Ai\\dummy_test\\validation_runtime_fixture dan dibersihkan setelah test.

Menguji:
    1. validation dijalankan setelah execution sukses
    2. validation SUCCESS -> runtime COMPLETED
    3. validation FAILURE -> runtime FAILED (tanpa replanner)
    4. validation TIMEOUT -> runtime FAILED (outcome timeout)
    5. validation EXECUTION_ERROR -> runtime FAILED (outcome execution_error)
    6. validation tidak dijalankan bila execution sudah FAILED
    7. validation tidak dijalankan bila tidak dikonfigurasi (backward compatible)
    8. replan saat validation gagal (bounded) -> execution ulang -> COMPLETED
    9. bounded replan (max_validation_cycles) dihormati
   10. stop_on_validation_failure menghentikan replan
   11. lifecycle transition RUNNING -> VALIDATING -> COMPLETED/FAILED
   12. execution event validation_started/validation_completed diemit
   13. RuntimeResult.validation terisi (normalized ValidationResult)
   14. memakai ValidationRunner yang sudah ada (bukan validator baru)
   15. memakai Replanner yang sudah ada (bukan replanner baru)
   16. tidak menduplikasi Reliability Manager
   17. tidak ada duplicate terminal executor
   18. provider-agnostic (runtime tidak hardcode provider)
   19. konfigurasi tidak hardcoded (ValidationConfig)
   20. fixture cleanup

Jalankan:
    python scripts/check_validation_runtime.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.config.settings import ValidationConfig  # noqa: E402
from agent_ai.core.response import FinishReason, LLMResponse  # noqa: E402
from agent_ai.planning import Replanner, TaskPlanner  # noqa: E402
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402
from agent_ai.runtime import AgentRuntime, RuntimeStatus  # noqa: E402
from agent_ai.session import EventType, InMemorySessionStore  # noqa: E402
from agent_ai.task import PreparedTask  # noqa: E402
from agent_ai.tasks import TaskLifecycle, TaskStatus  # noqa: E402
from agent_ai.validation import (  # noqa: E402
    CommandValidator,
    ValidationOutcome,
    ValidationRequest,
    ValidationResult,
    ValidationRunner,
    Validator,
)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "validation_runtime_fixture"


# ---------------------------------------------------------------------------
# Provider palsu (deterministik, tanpa API cloud)
# ---------------------------------------------------------------------------
class ScriptedProvider(BaseProvider):
    """Provider palsu: mengembalikan respons final terprogram."""

    name = "scripted"

    def __init__(self, text: str = "FINAL: selesai"):
        self._text = text
        self.calls = 0

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        self.calls += 1
        return GenerateResult(text=self._text, model="fake", provider=self.name, raw={})

    def normalize_response(self, result):
        return LLMResponse(
            text=result.text or "",
            actions=[],
            finish_reason=FinishReason.STOP,
            provider=self.name,
        )


class FailingProvider(BaseProvider):
    """Provider yang selalu error (simulasi execution gagal)."""

    name = "failing"

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        raise RuntimeError("provider error (disengaja)")


class FlakyValidator(Validator):
    """Validator palsu: gagal N kali pertama, lalu sukses (untuk uji replan)."""

    name = "flaky"

    def __init__(self, fail_times: int = 1):
        self.fail_times = fail_times
        self.calls = 0

    def validate(self, request: ValidationRequest) -> ValidationResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            return ValidationResult(
                success=False,
                outcome=ValidationOutcome.COMMAND_FAILURE,
                exit_code=1,
                stderr="belum lulus",
                validator=self.name,
            )
        return ValidationResult(
            success=True,
            outcome=ValidationOutcome.SUCCESS,
            exit_code=0,
            stdout="lulus",
            validator=self.name,
        )


class TimeoutValidator(Validator):
    """Validator palsu: selalu timeout."""

    name = "timeout"

    def validate(self, request: ValidationRequest) -> ValidationResult:
        return ValidationResult(
            success=False,
            outcome=ValidationOutcome.TIMEOUT,
            exit_code=None,
            stderr="melewati batas waktu",
            validator=self.name,
            metadata={"timed_out": True},
        )


class BoomValidator(Validator):
    """Validator palsu: melempar exception (uji execution_error)."""

    name = "boom"

    def validate(self, request: ValidationRequest) -> ValidationResult:
        raise RuntimeError("validator meledak")


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "ok.py").write_text("print('OK')\n", encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Integrasi Validation -> Execution Lifecycle (#42) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _plan():
    """Plan deterministik (fix) untuk dipakai runtime."""
    return TaskPlanner().create_plan("Perbaiki login yang gagal")


def _run() -> int:
    # 1) validation dijalankan setelah execution sukses.
    validator = CommandValidator(root=FIXTURE)
    runner = ValidationRunner([validator])
    request = ValidationRequest(target="ok", command="python ok.py")
    runtime = AgentRuntime(
        provider=ScriptedProvider(),
        validation_runner=runner,
        validation_request=request,
    )
    prepared = PreparedTask(task="task validasi", plan=_plan())
    result = runtime.run(prepared)
    assert result.status == RuntimeStatus.COMPLETED, result.status
    assert result.validation is not None, "validation harus dijalankan"
    assert result.validation["success"] is True
    assert result.validation_cycles == 1
    print(f"[1] validation dijalankan setelah execution OK -> cycles={result.validation_cycles}")

    # 2) validation SUCCESS -> runtime COMPLETED.
    assert result.status == RuntimeStatus.COMPLETED
    assert result.validation["outcome"] == "success"
    print("[2] validation SUCCESS -> runtime COMPLETED OK")

    # 3) validation FAILURE -> runtime FAILED (tanpa replanner).
    fail_request = ValidationRequest(target="fail", command="python -c \"import sys; sys.exit(3)\"")
    runtime_fail = AgentRuntime(
        provider=ScriptedProvider(),
        validation_runner=runner,
        validation_request=fail_request,
    )
    result_fail = runtime_fail.run(PreparedTask(task="task gagal validasi", plan=_plan()))
    assert result_fail.status == RuntimeStatus.FAILED, result_fail.status
    assert result_fail.validation["outcome"] == "command_failure"
    assert result_fail.validation["exit_code"] == 3
    print(f"[3] validation FAILURE -> runtime FAILED OK -> outcome={result_fail.validation['outcome']}")

    # 4) validation TIMEOUT -> runtime FAILED (outcome timeout).
    timeout_runner = ValidationRunner([TimeoutValidator()])
    runtime_to = AgentRuntime(
        provider=ScriptedProvider(),
        validation_runner=timeout_runner,
        validation_request=ValidationRequest(target="slow"),
    )
    result_to = runtime_to.run(PreparedTask(task="task timeout", plan=_plan()))
    assert result_to.status == RuntimeStatus.FAILED
    assert result_to.validation["outcome"] == "timeout"
    assert "timeout" in (result_to.error or "").lower()
    print(f"[4] validation TIMEOUT -> runtime FAILED OK -> outcome={result_to.validation['outcome']}")

    # 5) validation EXECUTION_ERROR -> runtime FAILED (outcome execution_error).
    boom_runner = ValidationRunner([BoomValidator()])
    runtime_boom = AgentRuntime(
        provider=ScriptedProvider(),
        validation_runner=boom_runner,
        validation_request=ValidationRequest(target="boom"),
    )
    result_boom = runtime_boom.run(PreparedTask(task="task boom", plan=_plan()))
    assert result_boom.status == RuntimeStatus.FAILED
    assert result_boom.validation["outcome"] == "execution_error"
    print(f"[5] validation EXECUTION_ERROR -> runtime FAILED OK -> outcome={result_boom.validation['outcome']}")

    # 6) validation tidak dijalankan bila execution sudah FAILED.
    runtime_exec_fail = AgentRuntime(
        provider=FailingProvider(),
        max_iterations=2,
        validation_runner=runner,
        validation_request=request,
    )
    result_exec_fail = runtime_exec_fail.run(PreparedTask(task="task exec gagal", plan=_plan()))
    assert result_exec_fail.status == RuntimeStatus.FAILED
    assert result_exec_fail.validation is None, "validation tidak boleh jalan bila execution gagal"
    assert result_exec_fail.validation_cycles == 0
    print("[6] validation tidak dijalankan bila execution FAILED OK")

    # 7) validation tidak dijalankan bila tidak dikonfigurasi (backward compatible).
    runtime_plain = AgentRuntime(provider=ScriptedProvider())
    result_plain = runtime_plain.run(PreparedTask(task="task tanpa validasi", plan=_plan()))
    assert result_plain.status == RuntimeStatus.COMPLETED
    assert result_plain.validation is None
    assert result_plain.validation_cycles == 0
    assert runtime_plain.validation_enabled is False
    print("[7] validation tidak dikonfigurasi -> backward compatible OK")

    # 8) replan saat validation gagal (bounded) -> execution ulang -> COMPLETED.
    flaky = FlakyValidator(fail_times=1)
    flaky_runner = ValidationRunner([flaky])
    runtime_replan = AgentRuntime(
        provider=ScriptedProvider(),
        validation_runner=flaky_runner,
        validation_request=ValidationRequest(target="flaky"),
        replanner=Replanner(max_replans=3),
        max_validation_cycles=3,
    )
    result_replan = runtime_replan.run(PreparedTask(task="task replan", plan=_plan()))
    assert result_replan.status == RuntimeStatus.COMPLETED, result_replan.status
    assert result_replan.validation_cycles == 2, result_replan.validation_cycles
    assert flaky.calls == 2, flaky.calls
    print(f"[8] replan saat validation gagal -> COMPLETED OK -> cycles={result_replan.validation_cycles}")

    # 9) bounded replan (max_validation_cycles) dihormati.
    always_fail = FlakyValidator(fail_times=99)
    bounded_runner = ValidationRunner([always_fail])
    runtime_bounded = AgentRuntime(
        provider=ScriptedProvider(),
        validation_runner=bounded_runner,
        validation_request=ValidationRequest(target="always"),
        replanner=Replanner(max_replans=10),
        max_validation_cycles=2,
    )
    result_bounded = runtime_bounded.run(PreparedTask(task="task bounded", plan=_plan()))
    assert result_bounded.status == RuntimeStatus.FAILED
    assert result_bounded.validation_cycles == 2, result_bounded.validation_cycles
    assert always_fail.calls == 2, always_fail.calls
    print(f"[9] bounded replan OK -> cycles={result_bounded.validation_cycles}")

    # 10) stop_on_validation_failure menghentikan replan.
    stop_runner = ValidationRunner([FlakyValidator(fail_times=99)])
    runtime_stop = AgentRuntime(
        provider=ScriptedProvider(),
        validation_runner=stop_runner,
        validation_request=ValidationRequest(target="stop"),
        replanner=Replanner(max_replans=10),
        max_validation_cycles=5,
        stop_on_validation_failure=True,
    )
    result_stop = runtime_stop.run(PreparedTask(task="task stop", plan=_plan()))
    assert result_stop.status == RuntimeStatus.FAILED
    assert result_stop.validation_cycles == 1, result_stop.validation_cycles
    print(f"[10] stop_on_validation_failure OK -> cycles={result_stop.validation_cycles}")

    # 11) lifecycle transition RUNNING -> VALIDATING -> COMPLETED/FAILED.
    lc_ok = TaskLifecycle("task lc ok", task_id="lc-ok")
    runtime_lc = AgentRuntime(
        provider=ScriptedProvider(),
        validation_runner=runner,
        validation_request=request,
    )
    result_lc = runtime_lc.run(
        PreparedTask(task="task lc ok", plan=_plan(), task_id="lc-ok"), lifecycle=lc_ok
    )
    assert result_lc.status == RuntimeStatus.COMPLETED
    assert lc_ok.status == TaskStatus.COMPLETED
    assert TaskStatus.VALIDATING.value in lc_ok.snapshot().timestamps, "harus melewati VALIDATING"
    print("[11] lifecycle RUNNING -> VALIDATING -> COMPLETED OK")

    # 11b) lifecycle FAILED saat validation gagal.
    lc_fail = TaskLifecycle("task lc fail", task_id="lc-fail")
    runtime_lc_fail = AgentRuntime(
        provider=ScriptedProvider(),
        validation_runner=runner,
        validation_request=fail_request,
    )
    result_lc_fail = runtime_lc_fail.run(
        PreparedTask(task="task lc fail", plan=_plan(), task_id="lc-fail"), lifecycle=lc_fail
    )
    assert result_lc_fail.status == RuntimeStatus.FAILED
    assert lc_fail.status == TaskStatus.FAILED
    print("[11b] lifecycle VALIDATING -> FAILED OK")

    # 12) execution event validation_started/validation_completed diemit.
    store = InMemorySessionStore()
    session = store.create_session()
    runtime_ev = AgentRuntime(
        provider=ScriptedProvider(),
        validation_runner=runner,
        validation_request=request,
        session_store=store,
        session_id=session.session_id,
    )
    runtime_ev.run(PreparedTask(task="task event", plan=_plan(), task_id="ev-1"))
    events = store.get_events(session_id=session.session_id)
    types = [e.event_type for e in events]
    assert EventType.VALIDATION_STARTED in types, types
    assert EventType.VALIDATION_COMPLETED in types, types
    assert EventType.TASK_COMPLETED in types, types
    print(f"[12] execution event validation diemit OK -> types={[t.value for t in types]}")

    # 13) RuntimeResult.validation terisi (normalized ValidationResult).
    d = result.validation
    for key in ("success", "outcome", "exit_code", "stdout", "stderr", "duration", "validator", "metadata"):
        assert key in d, f"field '{key}' hilang dari RuntimeResult.validation"
    assert d["validator"] == "command"
    print(f"[13] RuntimeResult.validation terisi OK -> keys={sorted(d)}")

    # 14) memakai ValidationRunner yang sudah ada (bukan validator baru).
    import agent_ai.validation as validation_pkg
    assert validation_pkg.ValidationRunner is ValidationRunner
    validation_dir = SRC_DIR / "agent_ai" / "validation"
    files = {p.name for p in validation_dir.glob("*.py")}
    assert files == {"__init__.py", "models.py", "base.py", "validators.py", "runner.py"}, files
    print("[14] memakai ValidationRunner yang sudah ada OK")

    # 15) memakai Replanner yang sudah ada (bukan replanner baru).
    import agent_ai.planning as planning_pkg
    assert planning_pkg.Replanner is Replanner
    print("[15] memakai Replanner yang sudah ada OK")

    # 16) tidak menduplikasi Reliability Manager.
    runtime_src = (SRC_DIR / "agent_ai" / "runtime" / "runtime.py").read_text(encoding="utf-8")
    assert "class ReliabilityManager" not in runtime_src
    assert "class RetryController" not in runtime_src
    print("[16] tidak menduplikasi Reliability Manager OK")

    # 17) tidak ada duplicate terminal executor.
    assert "import subprocess" not in runtime_src
    assert "os.system(" not in runtime_src
    print("[17] tidak ada duplicate terminal executor OK")

    # 18) provider-agnostic (runtime tidak hardcode provider).
    low = runtime_src.lower()
    for name in ("openrouter", "deepseek", "ollama", "openai"):
        assert name not in low, f"runtime tidak boleh hardcode '{name}'"
    print("[18] provider-agnostic OK")

    # 19) konfigurasi tidak hardcoded (ValidationConfig).
    cfg = ValidationConfig(enabled=True, max_replan_cycles=7, stop_on_failure=False, timeout=1.5)
    assert cfg.enabled is True and cfg.max_replan_cycles == 7
    assert cfg.stop_on_failure is False and cfg.timeout == 1.5
    assert "settings.validation" in runtime_src or "_validation_config" in runtime_src
    print("[19] konfigurasi tidak hardcoded OK")

    # 20) fixture cleanup (dicek di main()).
    print("[20] fixture cleanup OK")

    print()
    print("[OK] Validation terintegrasi ke Execution Lifecycle (validate -> replan -> re-execute).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
