"""Verifikasi: Backend Activity Phase AETHER -> event `phase_changed`.

Membuktikan bahwa pemetaan aktivitas NYATA Agent menjadi activity phase
(planning/inspecting/editing/running/validating) benar-benar terjadi di
back-end dan dipancarkan lewat event `phase_changed` yang SUDAH ADA.

Kontrak yang dibuktikan:
    A. Klasifikasi tool -> activity phase (unit, deterministik).
    B. `run_command` default = running; command verifikasi -> validating.
    C. Task start -> phase `planning` (sebelum aktivitas tool).
    D. Tool inspection (read_file) -> `inspecting`.
    E. Tool editing (write_file) -> `editing`.
    F. Tool terminal (run_command biasa) -> `running`.
    G. Command verifikasi (pytest) -> `validating`.
    H. Dedup: phase hanya diemit saat BERUBAH (bukan tiap tool call).
    I. Mekanisme validation existing -> phase `validating`.
    J. `TaskPhase` internal TIDAK berubah (konsep terpisah).
    K. Event berasal dari aktivitas nyata (bukan timer/tebakan).

Deterministik, tanpa network/API. Fixture di
`<project>/dummy_test/activity_phase_fixture` dan dibersihkan setelah test.

Jalankan:
    python scripts/check_activity_phase.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.runtime.activity import (  # noqa: E402
    ActivityPhase,
    classify_tool_activity,
    is_validation_command,
)
from agent_ai.runtime.runtime import AgentRuntime, RuntimeStatus  # noqa: E402
from agent_ai.session import EventType, InMemorySessionStore  # noqa: E402
from agent_ai.task.models import PreparedTask  # noqa: E402
from agent_ai.tasks.models import TaskPhase  # noqa: E402
from agent_ai.tools.registry import build_registry  # noqa: E402
from agent_ai.validation import (  # noqa: E402
    ValidationRequest,
    ValidationResult,
    ValidationRunner,
    Validator,
)
from agent_ai.validation.models import ValidationOutcome  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "activity_phase_fixture"


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

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Any]] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[List[Any]] = None,
        tool_choice: Optional[Any] = None,
    ) -> GenerateResult:
        idx = self.calls
        self.calls += 1
        raw = self.script[idx] if idx < len(self.script) else _final_turn("(default)")
        return GenerateResult(text="", model="scripted-model", provider=self.name, raw=raw)


class OkValidator(Validator):
    """Validator palsu: selalu sukses (untuk menguji mekanisme validation existing)."""

    name = "ok"

    def validate(self, request: ValidationRequest) -> ValidationResult:
        return ValidationResult(
            success=True,
            outcome=ValidationOutcome.SUCCESS,
            exit_code=0,
            stdout="lulus",
            validator=self.name,
        )


def executor() -> ToolExecutor:
    return ToolExecutor(build_registry(root=FIXTURE))


def phase_events(store: InMemorySessionStore, session_id: str) -> List[Dict[str, Any]]:
    """Daftar payload event `phase_changed` untuk sebuah session (urut waktu).

    Hanya event yang benar-benar membawa field `phase` (activity phase). Ini
    memisahkan dari event lain (mis. `retrieval_repeat`) yang pada jalur lama
    di-append sebagai PHASE_CHANGED tanpa field `phase` (perilaku existing).
    """
    return [
        dict(e.payload)
        for e in store.get_events(session_id=session_id)
        if e.event_type == EventType.PHASE_CHANGED and "phase" in e.payload
    ]


def phases(store: InMemorySessionStore, session_id: str) -> List[str]:
    return [p.get("phase") for p in phase_events(store, session_id)]


def run_script(
    script: List[Dict[str, Any]],
    *,
    validation_runner: Optional[ValidationRunner] = None,
    validation_request: Optional[ValidationRequest] = None,
) -> List[str]:
    """Jalankan continuous loop dengan skrip tool (per-turn) & kembalikan phases."""
    store = InMemorySessionStore()
    session = store.create_session(metadata={"task_id": "phase"})
    provider = ScriptedProvider(script + [_final_turn("selesai")])
    runtime = AgentRuntime(
        provider=provider,
        executor=executor(),
        options=GenerateOptions(model="scripted-model"),
        session_store=store,
        session_id=session.session_id,
        validation_runner=validation_runner,
        validation_request=validation_request,
    )
    result = runtime.run(PreparedTask(task="task aktivitas nyata", task_id="phase"))
    assert result.status == RuntimeStatus.COMPLETED, result.error
    return phases(store, session.session_id)


# --------------------------------------------------------------------------- #
# Skenario
# --------------------------------------------------------------------------- #
def scenario_a_tool_mapping() -> None:
    """A. Klasifikasi tool aktual -> activity phase (unit)."""
    expected = {
        "read_file": ActivityPhase.INSPECTING,
        "search_code": ActivityPhase.INSPECTING,
        "list_files": ActivityPhase.INSPECTING,
        "atlas_query": ActivityPhase.INSPECTING,
        "rig_query": ActivityPhase.INSPECTING,
        "project_map_status": ActivityPhase.INSPECTING,
        "refresh_project_map": ActivityPhase.INSPECTING,
        "write_file": ActivityPhase.EDITING,
        "edit_file": ActivityPhase.EDITING,
        "delete_file": ActivityPhase.EDITING,
        "move_file": ActivityPhase.EDITING,
        "run_command": ActivityPhase.RUNNING,
    }
    for tool, phase in expected.items():
        got = classify_tool_activity(tool, {"command": "echo hi"})
        assert got == phase, f"{tool}: {got} != {phase}"
    # Tool tak dikenal -> tidak diklasifikasi (None).
    assert classify_tool_activity("unknown_tool", {}) is None
    print(f"A. OK: {len(expected)} tool -> activity phase (inspection/editing/running)")


def scenario_b_run_command_running_vs_validating() -> None:
    """B. run_command default running; command verifikasi -> validating."""
    running = [
        "echo hello",
        "python app.py",
        "npm install",
        "python -m http.server 8080",
        "git status",
    ]
    validating = [
        "python -m pytest -q",
        "pytest tests/",
        "python -m unittest",
        "ruff check .",
        "mypy src",
        "npx eslint .",
        "npm test",
        "npm run test",
        "npm run lint",
        "go test ./...",
        "python scripts/check_workbench.py",
        "python -m py_compile app.py",
    ]
    for cmd in running:
        phase = classify_tool_activity("run_command", {"command": cmd})
        assert phase == ActivityPhase.RUNNING, f"{cmd!r} -> {phase}"
        assert is_validation_command(cmd) is False, cmd
    for cmd in validating:
        phase = classify_tool_activity("run_command", {"command": cmd})
        assert phase == ActivityPhase.VALIDATING, f"{cmd!r} -> {phase}"
        assert is_validation_command(cmd) is True, cmd
    print(f"B. OK: {len(running)} command operasional -> running, "
          f"{len(validating)} command verifikasi -> validating")


def scenario_c_planning_at_task_start() -> None:
    """C. Task start -> phase planning (tanpa tool)."""
    got = run_script([])
    assert got and got[0] == "planning", got
    print(f"C. OK: task start -> planning (phases={got})")


def scenario_d_inspecting() -> None:
    """D. read_file (inspection) -> phase inspecting."""
    (FIXTURE / "a.txt").write_text("hello", encoding="utf-8")
    got = run_script([_tool_turn("baca", [_tool_call("c1", "read_file", {"path": "a.txt"})])])
    assert "planning" in got and "inspecting" in got, got
    print(f"D. OK: read_file -> inspecting (phases={got})")


def scenario_e_editing() -> None:
    """E. write_file (mutasi workspace) -> phase editing."""
    got = run_script(
        [_tool_turn("tulis", [_tool_call("c1", "write_file", {"path": "b.txt", "content": "hi"})])]
    )
    assert "editing" in got, got
    print(f"E. OK: write_file -> editing (phases={got})")


def scenario_f_running() -> None:
    """F. run_command operasional -> phase running."""
    got = run_script(
        [_tool_turn("jalan", [_tool_call("c1", "run_command", {"command": "echo hi"})])]
    )
    assert "running" in got and "validating" not in got, got
    print(f"F. OK: run_command operasional -> running (phases={got})")


def scenario_g_validating_command() -> None:
    """G. run_command verifikasi (pytest) -> phase validating."""
    got = run_script(
        [_tool_turn("cek", [_tool_call("c1", "run_command", {"command": "python -m pytest -q"})])]
    )
    assert "validating" in got and "running" not in got, got
    print(f"G. OK: run_command verifikasi -> validating (phases={got})")


def scenario_h_dedup_and_order() -> None:
    """H. Dedup: urutan phase unik planning->inspecting->editing->running."""
    got = run_script(
        [
            _tool_turn("baca1", [_tool_call("c1", "read_file", {"path": "a.txt"})]),
            _tool_turn("baca2", [_tool_call("c2", "read_file", {"path": "a.txt"})]),
            _tool_turn("cari", [_tool_call("c3", "search_code", {"query": "hello"})]),
            _tool_turn("tulis", [_tool_call("c4", "write_file", {"path": "c.txt", "content": "x"})]),
            _tool_turn("jalan", [_tool_call("c5", "run_command", {"command": "echo done"})]),
        ]
    )
    assert got == ["planning", "inspecting", "editing", "running"], got
    print(f"H. OK: dedup + urutan benar (phases={got})")


def scenario_i_existing_validation_mechanism() -> None:
    """I. Mekanisme validation existing (validation_started) -> validating."""
    runner = ValidationRunner([OkValidator()])
    request = ValidationRequest(target="ok")
    got = run_script([], validation_runner=runner, validation_request=request)
    assert "validating" in got, got
    print(f"I. OK: validation existing -> validating (phases={got})")


def scenario_j_task_phase_unchanged() -> None:
    """J. TaskPhase internal TIDAK berubah; activity phase konsep terpisah."""
    expected = {"none", "preparation", "planning", "execution",
                "tool_execution", "validation", "finalization"}
    actual = {p.value for p in TaskPhase}
    assert actual == expected, actual
    assert {p.value for p in ActivityPhase} == {
        "planning", "inspecting", "editing", "running", "validating"
    }
    # Terminal task status BUKAN activity phase.
    assert not ({"completed", "failed", "cancelled"}
                & {p.value for p in ActivityPhase})
    print(f"J. OK: TaskPhase internal tetap {sorted(actual)}; activity phase terpisah")


def main() -> int:
    print("=== Verifikasi Backend Activity Phase (event phase_changed) ===")

    if FIXTURE.exists():
        shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir(parents=True, exist_ok=True)

    try:
        scenario_a_tool_mapping()
        scenario_b_run_command_running_vs_validating()
        scenario_c_planning_at_task_start()
        scenario_d_inspecting()
        scenario_e_editing()
        scenario_f_running()
        scenario_g_validating_command()
        scenario_h_dedup_and_order()
        scenario_i_existing_validation_mechanism()
        scenario_j_task_phase_unchanged()
    finally:
        shutil.rmtree(FIXTURE, ignore_errors=True)

    print()
    print("[OK] Activity phase backend (planning/inspecting/editing/running/"
          "validating) dipancarkan lewat event phase_changed dari aktivitas "
          "nyata Agent; TaskPhase internal tidak berubah.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
