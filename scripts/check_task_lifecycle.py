"""Verifikasi Task Lifecycle & Execution State.

Deterministik, tanpa API cloud. Fixture (bila perlu) di J:\\Agent_Ai\\dummy_test
dan dibersihkan setelah test.

Menguji:
    1. valid task creation
    2. unique task_id
    3. valid transitions
    4. invalid transition ditolak
    5. timestamps konsisten
    6. failure state
    7. cancellation state
    8. completion state
    9. snapshot (isolasi dari mutasi luar)
   10. integration compatibility dengan Runtime/Orchestrator
   11. task_id dapat diteruskan ke Change Tracking tanpa merusak API lama
   12. lifecycle tetap konsisten saat provider error / tool error / iteration limit

Jalankan:
    python scripts/check_task_lifecycle.py
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
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.response import FinishReason, LLMAction, LLMResponse  # noqa: E402
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402
from agent_ai.runtime import AgentRuntime, RuntimeStatus  # noqa: E402
from agent_ai.task import PreparedTask, TaskPreparation  # noqa: E402
from agent_ai.tasks import (  # noqa: E402
    InvalidTransitionError,
    TaskLifecycle,
    TaskPhase,
    TaskStatus,
)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "lifecycle_fixture"


class ScriptedProvider(BaseProvider):
    """Provider palsu: respons berurutan (tanpa API)."""

    name = "scripted"

    def __init__(self, responses):
        self._responses = list(responses)

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        item = self._responses.pop(0) if self._responses else "FINAL: selesai"
        return GenerateResult(text=item if isinstance(item, str) else "", model="m", provider=self.name, raw={"item": item})

    def normalize_response(self, result):
        item = result.raw.get("item")
        if isinstance(item, dict) and "tool" in item:
            return LLMResponse(
                text="",
                actions=[LLMAction(name=item["tool"], arguments=item.get("arguments", {}))],
                finish_reason=FinishReason.TOOL_CALLS,
                provider=self.name,
            )
        return LLMResponse(text=result.text or "", actions=[], finish_reason=FinishReason.STOP, provider=self.name)


class FailingProvider(BaseProvider):
    """Provider yang selalu melempar error (simulasi provider error)."""

    name = "failing"

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        raise RuntimeError("provider error (disengaja)")


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "a.txt").write_bytes(b"hello\n")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Task Lifecycle & Execution State ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    # 1) valid task creation.
    lc = TaskLifecycle("Perbaiki bug login")
    assert lc.status == TaskStatus.CREATED
    assert lc.phase == TaskPhase.NONE
    assert lc.task_id
    print("[1] valid task creation OK")

    # 2) unique task_id.
    ids = {TaskLifecycle("t").task_id for _ in range(50)}
    assert len(ids) == 50, "task_id harus unik"
    print("[2] unique task_id OK")

    # 3) valid transitions.
    lc.transition(TaskStatus.PREPARING)
    assert lc.status == TaskStatus.PREPARING and lc.phase == TaskPhase.PREPARATION
    lc.transition(TaskStatus.PLANNING)
    assert lc.status == TaskStatus.PLANNING and lc.phase == TaskPhase.PLANNING
    lc.transition(TaskStatus.RUNNING)
    assert lc.status == TaskStatus.RUNNING and lc.phase == TaskPhase.EXECUTION
    lc.set_phase(TaskPhase.TOOL_EXECUTION)
    assert lc.phase == TaskPhase.TOOL_EXECUTION
    lc.transition(TaskStatus.VALIDATING)
    assert lc.status == TaskStatus.VALIDATING and lc.phase == TaskPhase.VALIDATION
    print("[3] valid transitions OK")

    # 4) invalid transition ditolak.
    try:
        lc.transition(TaskStatus.CREATED)  # VALIDATING -> CREATED tidak valid
        raise AssertionError("transition tidak valid seharusnya ditolak")
    except InvalidTransitionError:
        pass
    # Terminal tidak bisa keluar.
    lc.complete(result="ok")
    assert lc.status == TaskStatus.COMPLETED and lc.is_terminal
    try:
        lc.transition(TaskStatus.RUNNING)
        raise AssertionError("transition dari terminal seharusnya ditolak")
    except InvalidTransitionError:
        pass
    print("[4] invalid transition ditolak OK")

    # 5) timestamps konsisten.
    lc2 = TaskLifecycle("task timestamps")
    t0 = lc2.snapshot().created_at
    lc2.transition(TaskStatus.PREPARING)
    lc2.transition(TaskStatus.RUNNING)
    snap = lc2.snapshot()
    assert snap.timestamps[TaskStatus.CREATED.value] == t0
    assert snap.timestamps[TaskStatus.PREPARING.value] >= t0
    assert snap.timestamps[TaskStatus.RUNNING.value] >= snap.timestamps[TaskStatus.PREPARING.value]
    assert snap.updated_at >= snap.created_at
    print("[5] timestamps konsisten OK")

    # 6) failure state.
    lc3 = TaskLifecycle("task gagal")
    lc3.transition(TaskStatus.PREPARING)
    lc3.transition(TaskStatus.RUNNING)
    lc3.fail("boom", failure={"kind": "provider_error"})
    assert lc3.status == TaskStatus.FAILED and lc3.is_terminal
    assert lc3.snapshot().error == "boom"
    assert lc3.snapshot().failure == {"kind": "provider_error"}
    print("[6] failure state OK")

    # 7) cancellation state.
    lc4 = TaskLifecycle("task dibatalkan")
    lc4.transition(TaskStatus.PREPARING)
    lc4.cancel(reason="user cancel")
    assert lc4.status == TaskStatus.CANCELLED and lc4.is_terminal
    assert lc4.snapshot().metadata.get("cancel_reason") == "user cancel"
    print("[7] cancellation state OK")

    # 8) completion state.
    lc5 = TaskLifecycle("task selesai")
    lc5.transition(TaskStatus.PREPARING)
    lc5.transition(TaskStatus.RUNNING)
    lc5.transition(TaskStatus.VALIDATING)
    lc5.complete(result="hasil akhir")
    assert lc5.status == TaskStatus.COMPLETED and lc5.is_terminal
    assert lc5.snapshot().result == "hasil akhir"
    print("[8] completion state OK")

    # 9) snapshot (isolasi dari mutasi luar).
    lc6 = TaskLifecycle("task snapshot", metadata={"k": "v"})
    snap_a = lc6.snapshot()
    snap_a.metadata["k"] = "diubah"
    snap_a.timestamps["x"] = 1.0
    snap_b = lc6.snapshot()
    assert snap_b.metadata["k"] == "v", "snapshot harus salinan (tidak memutasi state)"
    assert "x" not in snap_b.timestamps
    print("[9] snapshot OK")

    # 10) integration compatibility dengan Runtime/Orchestrator.
    #     PreparedTask -> Runtime dengan lifecycle yang sama.
    prepared = PreparedTask(task="task runtime", task_id="task-rt-1")
    lc_rt = TaskLifecycle("task runtime", task_id=prepared.task_id)
    provider = ScriptedProvider(["FINAL: selesai runtime"])
    runtime = AgentRuntime(provider=provider)
    result = runtime.run(prepared, lifecycle=lc_rt)
    assert result.status == RuntimeStatus.COMPLETED
    assert lc_rt.status == TaskStatus.COMPLETED, f"lifecycle harus COMPLETED, dapat {lc_rt.status}"
    assert lc_rt.task_id == prepared.task_id, "task identity harus sama"
    print("[10] integration Runtime/Orchestrator OK")

    # 10b) runtime gagal -> lifecycle FAILED.
    prepared_fail = PreparedTask(task="task gagal runtime", task_id="task-rt-2")
    lc_fail = TaskLifecycle("task gagal runtime", task_id=prepared_fail.task_id)
    runtime_fail = AgentRuntime(provider=FailingProvider(), max_iterations=2)
    result_fail = runtime_fail.run(prepared_fail, lifecycle=lc_fail)
    assert result_fail.status == RuntimeStatus.FAILED
    assert lc_fail.status == TaskStatus.FAILED, f"lifecycle harus FAILED, dapat {lc_fail.status}"
    print("[10b] integration runtime gagal -> lifecycle FAILED OK")

    # 10c) runtime tanpa lifecycle tetap bekerja (backward compatible).
    prepared_plain = PreparedTask(task="task tanpa lifecycle")
    result_plain = AgentRuntime(provider=ScriptedProvider(["FINAL: ok"])).run(prepared_plain)
    assert result_plain.status == RuntimeStatus.COMPLETED
    print("[10c] runtime tanpa lifecycle (backward compatible) OK")

    # 11) task_id dapat diteruskan ke Change Tracking tanpa merusak API lama.
    tracker = ChangeTracker(root=FIXTURE)
    tracker.start(prepared.task_id)
    tracker.snapshot(".", task_id=prepared.task_id)
    (FIXTURE / "b.txt").write_bytes(b"new\n")
    tracker.track(prepared.task_id)
    change_set = tracker.get_changes(prepared.task_id)
    assert change_set is not None and change_set.task_id == prepared.task_id
    assert any(c.path == "b.txt" for c in change_set.changes)
    # API lama (tanpa task_id) tetap bekerja.
    tracker.start("legacy-task")
    tracker.snapshot(".", task_id="legacy-task")
    tracker.track("legacy-task")
    assert tracker.get_changes("legacy-task") is not None
    print("[11] task_id diteruskan ke Change Tracking OK")

    # 11b) TaskPreparation meneruskan task_id ke PreparedTask.
    prep = TaskPreparation()
    prepared_with_id = prep.prepare("task dengan id", task_id="prep-1")
    assert prepared_with_id.task_id == "prep-1"
    prepared_no_id = prep.prepare("task tanpa id")
    assert prepared_no_id.task_id is None
    print("[11b] TaskPreparation meneruskan task_id OK")

    # 12) lifecycle tetap konsisten saat iteration limit tercapai.
    #     Provider yang selalu meminta tool call -> orchestrator kena limit.
    #     Iteration limit adalah proteksi jalur LEGACY -> pakai use_continuous_loop=False.
    loop_provider = ScriptedProvider([{"tool": "read_file", "arguments": {"path": "a.txt"}}] * 20)
    prepared_loop = PreparedTask(task="task loop", task_id="task-loop")
    lc_loop = TaskLifecycle("task loop", task_id=prepared_loop.task_id)
    result_loop = AgentRuntime(
        provider=loop_provider, max_iterations=3, use_continuous_loop=False
    ).run(prepared_loop, lifecycle=lc_loop)
    assert result_loop.status == RuntimeStatus.FAILED
    assert lc_loop.status == TaskStatus.FAILED, f"lifecycle harus FAILED, dapat {lc_loop.status}"
    print("[12] lifecycle konsisten saat iteration limit OK")

    print()
    print("[OK] Task Lifecycle & Execution State bekerja (transition, timestamps, integrasi).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
