"""Verifikasi Reliability Subsystem.

Menguji secara deterministik (tanpa API cloud):
    1. event model
    2. retry policy
    3. retryable error
    4. non-retryable error
    5. exponential backoff calculation
    6. repeated identical action
    7. repeated identical arguments
    8. repeated failed action
    9. no-progress detection
   10. legitimate repeated action tidak langsung dianggap failure
   11. iteration limit
   12. provider error
   13. timeout
   14. decision: retry/recover/stop/fail
   15. OpenRouter-style repeated verification scenario
   16. tidak ada duplicate terminal/provider executor
   17. provider-agnostic

Jalankan:
    python scripts/check_reliability.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.reliability import (  # noqa: E402
    DecisionAction,
    Detector,
    EventType,
    ProgressSnapshot,
    ReliabilityDecision,
    ReliabilityEvent,
    ReliabilityManager,
    RetryPolicy,
)


def main() -> int:
    print("=== Verifikasi Reliability Subsystem ===")
    return _run()


def _run() -> int:
    # 1) event model.
    ev = ReliabilityEvent(type=EventType.PROVIDER_ERROR, message="x", severity=0.7)
    d = ev.to_dict()
    assert d["type"] == "provider_error" and d["severity"] == 0.7
    print("[1] event model OK")

    # 2) retry policy.
    policy = RetryPolicy(max_retries=3, base_delay=0.5, backoff_factor=2.0, max_delay=10.0)
    assert policy.max_retries == 3
    print("[2] retry policy OK")

    # 3) retryable error.
    assert policy.is_retryable("provider_error") is True
    assert policy.is_retryable("timeout") is True
    assert policy.is_retryable("malformed_tool_response") is True
    print("[3] retryable error OK")

    # 4) non-retryable error.
    assert policy.is_retryable("command_failure") is False
    assert policy.is_retryable("execution_error") is False
    assert policy.is_retryable("unknown_outcome") is False
    print("[4] non-retryable error OK")

    # 5) exponential backoff calculation.
    assert policy.delay_for(0) == 0.5
    assert policy.delay_for(1) == 1.0
    assert policy.delay_for(2) == 2.0
    assert policy.delay_for(3) == 4.0
    assert policy.delay_for(10) == 10.0  # dibatasi max_delay
    print("[5] exponential backoff OK -> 0.5,1.0,2.0,4.0,...(cap 10)")

    # 6) repeated identical action (tanpa perubahan observation).
    det = Detector(repeat_threshold=3, no_progress_threshold=3, iteration_limit=10)
    hist = [
        ProgressSnapshot(iteration=i, action_signature="read_file:calc.py",
                         outcome="success", observation_signature="same", made_progress=False)
        for i in range(1, 4)
    ]
    events = det.detect(hist)
    types = {e.type for e in events}
    assert EventType.REPEATED_ACTION in types
    print("[6] repeated identical action OK")

    # 7) repeated identical arguments.
    assert EventType.REPEATED_ARGUMENTS in types
    print("[7] repeated identical arguments OK")

    # 8) repeated failed action.
    hist_fail = [
        ProgressSnapshot(iteration=i, action_signature="run_command:test",
                         outcome="command_failure", observation_signature="err", made_progress=False)
        for i in range(1, 4)
    ]
    fail_events = det.detect(hist_fail)
    assert EventType.REPEATED_FAILED_ACTION in {e.type for e in fail_events}
    print("[8] repeated failed action OK")

    # 9) no-progress detection.
    hist_np = [
        ProgressSnapshot(iteration=i, action_signature=f"a{i}", outcome="success",
                         observation_signature=f"o{i}", made_progress=False)
        for i in range(1, 4)
    ]
    np_events = det.detect(hist_np)
    assert EventType.NO_PROGRESS in {e.type for e in np_events}
    print("[9] no-progress detection OK")

    # 10) legitimate repeated action TIDAK dianggap failure.
    #     Action sama berulang tetapi observation BERUBAH (ada info baru).
    hist_legit = [
        ProgressSnapshot(iteration=1, action_signature="read_file:calc.py",
                         outcome="success", observation_signature="v1", made_progress=True),
        ProgressSnapshot(iteration=2, action_signature="read_file:calc.py",
                         outcome="success", observation_signature="v2", made_progress=True),
        ProgressSnapshot(iteration=3, action_signature="read_file:calc.py",
                         outcome="success", observation_signature="v3", made_progress=True),
    ]
    legit_events = det.detect(hist_legit)
    legit_types = {e.type for e in legit_events}
    assert EventType.REPEATED_ACTION not in legit_types, "repeated action sah tidak boleh jadi event"
    assert EventType.NO_PROGRESS not in legit_types
    print("[10] legitimate repeated action tidak dianggap failure OK")

    # 11) iteration limit.
    hist_limit = [ProgressSnapshot(iteration=10, action_signature="x", outcome="success",
                                   observation_signature="o", made_progress=True)]
    limit_events = det.detect(hist_limit)
    assert EventType.ITERATION_LIMIT in {e.type for e in limit_events}
    print("[11] iteration limit OK")

    # 12) provider error.
    pe = det.detect_error(RuntimeError("provider down"))
    assert pe.type == EventType.PROVIDER_ERROR
    print("[12] provider error OK")

    # 13) timeout.
    te = det.detect_error(TimeoutError("request timed out"))
    assert te.type == EventType.TIMEOUT
    print("[13] timeout OK")

    # 14) decision: retry/recover/stop/fail.
    # retry
    mgr = ReliabilityManager(detector=Detector(iteration_limit=10), retry_policy=policy)
    dec_retry = mgr.decide(outcome="provider_error")
    assert dec_retry.action == DecisionAction.RETRY and dec_retry.retryable
    assert dec_retry.delay == 0.5
    print(f"[14a] decision RETRY OK -> delay={dec_retry.delay}")

    # recover (repetisi tanpa progress)
    mgr2 = ReliabilityManager(detector=Detector(repeat_threshold=3, no_progress_threshold=3, iteration_limit=10))
    for i in range(1, 4):
        mgr2.record_progress(ProgressSnapshot(
            iteration=i, action_signature="read_file:x", outcome="success",
            observation_signature="same", made_progress=False))
    dec_recover = mgr2.decide()
    assert dec_recover.action == DecisionAction.RECOVER
    print(f"[14b] decision RECOVER OK -> {dec_recover.reason}")

    # stop (iteration limit)
    mgr3 = ReliabilityManager(detector=Detector(iteration_limit=3))
    mgr3.record_progress(ProgressSnapshot(iteration=3, action_signature="x",
                                          outcome="success", observation_signature="o", made_progress=True))
    dec_stop = mgr3.decide()
    assert dec_stop.action == DecisionAction.STOP
    print(f"[14c] decision STOP OK -> {dec_stop.reason}")

    # fail (repeated failed action)
    mgr4 = ReliabilityManager(detector=Detector(repeat_threshold=3, iteration_limit=10))
    for i in range(1, 4):
        mgr4.record_progress(ProgressSnapshot(
            iteration=i, action_signature="run_command:test", outcome="command_failure",
            observation_signature="err", made_progress=False))
    dec_fail = mgr4.decide()
    assert dec_fail.action == DecisionAction.FAIL
    print(f"[14d] decision FAIL OK -> {dec_fail.reason}")

    # 15) OpenRouter-style repeated verification scenario.
    #     Model melakukan verifikasi berulang (read_file + run_command) dengan
    #     observation yang sama, mendekati batas iterasi.
    mgr5 = ReliabilityManager(
        detector=Detector(repeat_threshold=3, no_progress_threshold=3, iteration_limit=8, iteration_warn_margin=2),
        retry_policy=policy,
    )
    pattern = [
        ("read_file:calculator.py", "success", "content-v1", True),
        ("run_command:test", "success", "ALL TESTS PASSED", True),
        ("read_file:calculator.py", "success", "content-v1", False),
        ("run_command:test", "success", "ALL TESTS PASSED", False),
        ("read_file:calculator.py", "success", "content-v1", False),
        ("run_command:test", "success", "ALL TESTS PASSED", False),
        ("read_file:calculator.py", "success", "content-v1", False),
    ]
    for i, (sig, outcome, obs, prog) in enumerate(pattern, 1):
        mgr5.record_progress(ProgressSnapshot(
            iteration=i, action_signature=sig, outcome=outcome,
            observation_signature=obs, made_progress=prog))
    detected = {e.type for e in mgr5.latest_events()}
    assert EventType.NO_PROGRESS in detected, "harus mendeteksi no-progress"
    assert EventType.ITERATION_LIMIT in detected, "harus mendeteksi mendekati iteration limit"
    dec_or = mgr5.decide()
    assert dec_or.action in (DecisionAction.RECOVER, DecisionAction.STOP)
    print(f"[15] OpenRouter-style repeated verification OK -> events={sorted(t.value for t in detected)} decision={dec_or.action.value}")

    # 16) tidak ada duplicate terminal/provider executor.
    reliability_dir = SRC_DIR / "agent_ai" / "reliability"
    files = {p.name for p in reliability_dir.glob("*.py")}
    assert files == {"__init__.py", "models.py", "detector.py", "retry.py", "manager.py"}, files
    # Tidak ada modul yang mengimpor requests/HTTP client baru.
    for p in reliability_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "import requests" not in text, f"{p.name} tidak boleh membuat HTTP client"
        assert "subprocess" not in text, f"{p.name} tidak boleh membuat terminal executor"
    print("[16] tidak ada duplicate terminal/provider executor OK")

    # 17) provider-agnostic.
    for p in reliability_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8").lower()
        for name in ("openrouter", "deepseek", "ollama"):
            assert name not in text, f"{p.name} tidak boleh hardcode provider '{name}'"
    print("[17] provider-agnostic OK")

    print()
    print("[OK] Reliability Subsystem bekerja (detector, retry, manager, decisions).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
