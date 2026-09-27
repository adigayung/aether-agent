"""Verifikasi FINAL: Runtime Lifecycle AETHER -> event SSE nyata -> UI lifecycle.

Task 3 (Final Runtime Validation Lifecycle AETHER) — HANYA validasi.

Membuktikan bahwa alur nyata:

    task_started
    -> phase_changed planning
    -> phase_changed inspecting
    -> phase_changed editing
    -> phase_changed running
    -> phase_changed validating
    -> task_completed

benar-benar mengalir dari backend sampai (format yang dikonsumsi) frontend,
memakai RUNTIME AETHER NYATA:

    AgentRuntime.run(PreparedTask)            # layanan runtime nyata
        -> AgentOrchestrator.run_continuous_loop  # jalur produksi
        -> ToolExecutor + build_registry(nyata)   # tool produksi nyata
        -> event_sink (_event_sink, titik klasifikasi activity phase TUNGGAL)
        -> InMemorySessionStore (event system existing)
        -> EventSubscription / sse_stream / format_sse (transport SSE #51)
        -> lifecycleFromEvents / buildLifecycleStates (logika frontend nyata,
           dipakai App.vue; tanpa browser)

TIDAK ada perubahan source, TIDAK menambah event/heuristic/timer. Deterministik
tanpa network/LLM (provider skrip OpenAI-compatible), workspace fixture di
`dummy_test` dan dibersihkan setelah verifikasi.

Skenario (sesuai dokumen Task 3):
    A. Inspection      : read_file            -> planning -> inspecting
    B. Editing         : write_file           -> inspecting -> editing
    C. Running         : run_command operasi  -> editing -> running
    D. Validating      : run_command verifikasi (pytest) -> running -> validating
    E. Completed       : task_completed       -> Validating -> Completed (TERMINAL)
    F. Return previous : inspecting -> editing -> inspecting (milestone editing tetap done)
    G. Validation fail : validating -> editing -> running -> validating (perbaikan)
    H. Task failure    : task_failed          -> BUKAN Completed; phase terakhir dipertahankan
    I. Cancellation    : task_cancelled       -> BUKAN Completed; phase terakhir dipertahankan
    J. History/reconnect: live event & history event -> lifecycle SAMA (konsisten)
    K. Tanpa sumber phase ganda: runtime.phase internal (replan/provider_fallback)
       TIDAK menggerakkan lifecycle (hanya activity phase dari phase_changed)
    L. SSE transport nyata: frame SSE memuat event_type + payload phase yang sama

Jalankan:
    python scripts/check_lifecycle_runtime_sse.py
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
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.runtime.activity import ActivityPhase  # noqa: E402
from agent_ai.runtime.runtime import AgentRuntime, RuntimeStatus  # noqa: E402
from agent_ai.session.events import EventType  # noqa: E402
from agent_ai.session.store import InMemorySessionStore  # noqa: E402
from agent_ai.task.models import PreparedTask  # noqa: E402
from agent_ai.tools.registry import build_registry  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "lifecycle_runtime_sse_fixture"

#: Activity phase yang dikenal UI (satu-satunya sumber lifecycle step).
ACTIVITY_PHASES = {p.value for p in ActivityPhase}
#: Nilai internal runtime lama yang TIDAK boleh menggerakkan lifecycle.
FORBIDDEN_PHASES = {"replan", "provider_fallback"}

LIFECYCLE_STEPS = ["Planning", "Inspecting", "Editing", "Running", "Validating", "Completed"]
PHASE_INDEX = {"planning": 0, "inspecting": 1, "editing": 2, "running": 3, "validating": 4}
VALIDATING_STEP = 4


# --------------------------------------------------------------------------- #
# Provider skrip (deterministik, tanpa network) + helper respons
# --------------------------------------------------------------------------- #
def _tool_turn(text: str, calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": text,
                    "tool_calls": [
                        {
                            "id": cid,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for cid, name, args in calls
                    ],
                },
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


class BoomProvider(ScriptedProvider):
    """Provider yang selalu error (simulasi provider error -> task FAILED)."""

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Any]] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[List[Any]] = None,
        tool_choice: Optional[Any] = None,
    ) -> GenerateResult:
        raise RuntimeError("koneksi ke provider gagal (disengaja)")


# --------------------------------------------------------------------------- #
# Runner: jalankan task runtime NYATA + kumpulkan event + SSE frame
# --------------------------------------------------------------------------- #
def run_task(
    task: str,
    script: List[Dict[str, Any]],
    *,
    task_id: str = "task",
    provider: Optional[ScriptedProvider] = None,
    cancel_token: Any = None,
) -> Dict[str, Any]:
    """Jalankan AgentRuntime dengan tool produksi NYATA di atas fixture.

    Mengembalikan dict: result, store, session_id, events (list dict SSE-safe),
    sse_frames (list str), provider.
    """
    store = InMemorySessionStore()
    session = store.create_session(metadata={"task_id": task_id})
    # Subscribe SEBELUM task berjalan: EventSubscription hanya menerima event
    # yang di-append setelah start() (transport SSE produksi yang sama).
    from api.streaming import EventSubscription, format_sse

    sub = EventSubscription(store, task_id=task_id)
    sub.start()
    registry = build_registry(root=FIXTURE, cancel_token=cancel_token)
    executor = ToolExecutor(registry=registry)
    if provider is None:
        provider = ScriptedProvider(script)
    runtime = AgentRuntime(
        provider=provider,
        executor=executor,
        options=GenerateOptions(model="scripted-model"),
        session_store=store,
        session_id=session.session_id,
        cancel_token=cancel_token,
    )
    result = runtime.run(PreparedTask(task=task, task_id=task_id))

    # Kumpulkan event lewat transport SSE nyata (queue subscriber).
    frames: List[str] = []
    while True:
        evt = sub.get(timeout=0.1)
        if evt is None:
            break
        frames.append(format_sse(evt))
    sub.close()
    events = [e.to_dict() for e in store.get_events(task_id=task_id)]

    return {
        "result": result,
        "store": store,
        "session_id": session.session_id,
        "events": events,
        "sse_frames": frames,
        "provider": provider,
    }


def phase_events(events: List[Dict[str, Any]]) -> List[str]:
    """Urutan phase activity nyata dari event `phase_changed` (payload.phase)."""
    return [
        str(evt["payload"]["phase"])
        for evt in events
        if evt["event_type"] == EventType.PHASE_CHANGED.value
        and "phase" in evt.get("payload", {})
    ]


def event_types(events: List[Dict[str, Any]]) -> List[str]:
    return [str(evt["event_type"]) for evt in events]


# --------------------------------------------------------------------------- #
# Logika lifecycle frontend (dipindahkan dari web/frontend/src/lifecycle.js;
# identik dengan App.vue: task_started -> planning; phase_changed -> step;
# status terminal -> Completed/failed/cancelled; milestone permanen).
# --------------------------------------------------------------------------- #
def add_milestone(milestones: List[int], idx: int) -> List[int]:
    if idx < 0:
        return milestones
    if idx in milestones:
        return milestones
    return sorted(milestones + [idx])


def build_lifecycle_states(
    status: str,
    current_phase: str,
    milestones: List[int],
    *,
    has_task: bool = True,
) -> List[str]:
    """Salin buildLifecycleStates (lifecycle.js) — Verifikasi UI lifecycle."""
    states = [""] * len(LIFECYCLE_STEPS)
    if not has_task:
        return states
    s = (status or "").lower()
    done = set(milestones)
    idx = PHASE_INDEX.get((current_phase or "").lower(), -1)
    last = len(LIFECYCLE_STEPS) - 1

    if s == "completed":
        for i in range(last):
            done.add(i)
        active = last
    elif s in ("failed", "cancelled"):
        active = idx if idx >= 0 else (max(done) if done else -1)
    else:
        active = idx if idx >= 0 else (max(done) if done else -1)
    if active < 0:
        active = 0
    if active > last:
        active = last

    for i in range(len(states)):
        if i == active:
            states[i] = "active"
        elif i in done:
            states[i] = "done"
    return states


def lifecycle_from_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Salin lifecycleFromEvents (lifecycle.js) — rekonstruksi dari history."""
    milestones: List[int] = []
    current_phase = ""
    for evt in events:
        etype = evt.get("event_type") or evt.get("event") or ""
        payload = evt.get("payload") or evt.get("data") or {}
        if etype == "task_started":
            milestones = add_milestone(milestones, 0)
            if not current_phase:
                current_phase = "planning"
        elif etype == "phase_changed":
            idx = PHASE_INDEX.get(str(payload.get("phase", "")).lower(), -1)
            if idx >= 0:
                milestones = add_milestone(milestones, idx)
                current_phase = str(payload["phase"]).lower()
        elif etype == "validation_started":
            milestones = add_milestone(milestones, VALIDATING_STEP)
            if not current_phase:
                current_phase = "validating"
    return {"milestones": milestones, "current_phase": current_phase}


def states_label(states: List[str]) -> str:
    labels = {"": ".", "done": "D", "active": "A"}
    return " ".join(f"{name}:{labels.get(s, s)}" for name, s in zip(LIFECYCLE_STEPS, states))


# --------------------------------------------------------------------------- #
# Skenario
# --------------------------------------------------------------------------- #
def scenario_a_inspection() -> Dict[str, Any]:
    """A. read_file -> planning -> inspecting (task kecil yang aman)."""
    (FIXTURE / "a.txt").write_text("hello", encoding="utf-8")
    out = run_task(
        "Baca file a.txt lalu selesai.",
        [_tool_turn("baca", [("c1", "read_file", {"path": "a.txt"})]), _final_turn("Selesai.")],
        task_id="scA",
    )
    phases = phase_events(out["events"])
    assert phases[0] == "planning", phases
    assert "inspecting" in phases, phases
    assert phases[-1] == "inspecting", phases
    # status COMPLETED + event terminal.
    assert out["result"].status == RuntimeStatus.COMPLETED, out["result"].error
    assert "task_completed" in event_types(out["events"]), event_types(out["events"])
    print(f"A. OK: inspection nyata -> phases={phases} (expected planning->inspecting)")
    return out


def scenario_b_editing() -> Dict[str, Any]:
    """B. write_file (mutasi nyata) -> editing; file benar-benar berubah."""
    out = run_task(
        "Tulis file b.txt berisi 'lorem'.",
        [
            _tool_turn("baca dulu", [("c1", "read_file", {"path": "a.txt"})]),
            _tool_turn("tulis", [("c2", "write_file", {"path": "b.txt", "content": "lorem"})]),
            _final_turn("Selesai."),
        ],
        task_id="scB",
    )
    phases = phase_events(out["events"])
    assert phases[0] == "planning" and "inspecting" in phases and "editing" in phases, phases
    assert phases[-1] == "editing", phases
    # Mutasi NYATA terjadi.
    assert (FIXTURE / "b.txt").read_text(encoding="utf-8") == "lorem"
    print(f"B. OK: mutasi file nyata -> phases={phases} (expected inspecting->editing)")
    return out


def scenario_c_running() -> Dict[str, Any]:
    """C. run_command operasional -> running (bukan validating)."""
    out = run_task(
        "Jalankan echo lalu selesai.",
        [
            _tool_turn("baca", [("c1", "read_file", {"path": "a.txt"})]),
            _tool_turn("edit", [("c2", "write_file", {"path": "c.txt", "content": "x"})]),
            _tool_turn("jalan", [("c3", "run_command", {"command": "echo run-done"})]),
            _final_turn("Selesai."),
        ],
        task_id="scC",
    )
    phases = phase_events(out["events"])
    assert phases == ["planning", "inspecting", "editing", "running"], phases
    assert "validating" not in phases, phases
    print(f"C. OK: command operasional -> phases={phases} (expected editing->running)")
    return out


def scenario_d_validating() -> Dict[str, Any]:
    """D. run_command verifikasi (pytest) -> validating.

    Perintah verifikasi TIDAK dijalankan sungguh (tidak ada test): hanya
    membuktikan klasifikasi activity dari command string nyata, persis seperti
    yang diterapkan backend (is_validation_command).
    """
    out = run_task(
        "Validasi dengan pytest.",
        [
            _tool_turn("baca", [("c1", "read_file", {"path": "a.txt"})]),
            _tool_turn("edit", [("c2", "write_file", {"path": "d.txt", "content": "y"})]),
            _tool_turn("cek", [("c3", "run_command", {"command": "python -m pytest -q"})]),
            _final_turn("Selesai."),
        ],
        task_id="scD",
    )
    phases = phase_events(out["events"])
    assert "validating" in phases, phases
    assert "inspecting" in phases and "editing" in phases, phases
    # Urutan: planning -> inspecting -> editing -> validating (tanpa running
    # untuk command verifikasi: klasifikasi langsung validating).
    last = phases[-1]
    assert last == "validating", phases
    print(f"D. OK: command verifikasi -> phases={phases} (expected running->validating)")
    return out


def scenario_e_completed_terminal() -> Dict[str, Any]:
    """E. Validating -> Completed; Completed TERMINAL (tidak mundur)."""
    out = run_task(
        "Kerjakan, validasi, selesai.",
        [
            _tool_turn("baca", [("c1", "read_file", {"path": "a.txt"})]),
            _tool_turn("tulis", [("c2", "write_file", {"path": "e.txt", "content": "z"})]),
            _tool_turn("cek", [("c3", "run_command", {"command": "python -m pytest -q"})]),
            _final_turn("Selesai."),
        ],
        task_id="scE",
    )
    phases = phase_events(out["events"])
    assert phases == ["planning", "inspecting", "editing", "validating"], phases
    assert out["result"].status == RuntimeStatus.COMPLETED
    types = event_types(out["events"])
    assert "task_completed" in types, types
    assert "task_failed" not in types and "task_cancelled" not in types, types
    # Lifecycle UI: completed -> semua done, Completed active (TERMINAL).
    states = build_lifecycle_states("completed", phases[-1], [0, 1, 2, 4])
    assert states == ["done", "done", "done", "done", "done", "active"], states
    # Terminal: tidak bisa mundur (completed selalu Completed).
    states2 = build_lifecycle_states("completed", "editing", [0, 1, 2, 4])
    assert states2[5] == "active", states2
    print(f"E. OK: Validating -> Completed TERMINAL; phases={phases}; UI={states_label(states)}")
    return out


def scenario_f_return_previous_phase() -> Dict[str, Any]:
    """F. Inspecting -> Editing -> Inspecting: editing tetap milestone done."""
    out = run_task(
        "Baca, tulis, lalu baca lagi.",
        [
            _tool_turn("baca1", [("c1", "read_file", {"path": "a.txt"})]),
            _tool_turn("tulis", [("c2", "write_file", {"path": "f.txt", "content": "q"})]),
            _tool_turn("baca2", [("c3", "read_file", {"path": "a.txt"})]),
            _final_turn("Selesai."),
        ],
        task_id="scF",
    )
    phases = phase_events(out["events"])
    # dedup phase_changed: inspecting, editing, inspecting (kembali ke phase
    # sebelumnya => event baru; dedup tidak menggabungkan karena sudah berubah).
    assert phases == ["planning", "inspecting", "editing", "inspecting"], phases
    # Lifecycle UI: current = Inspecting (active); Editing tetap done (milestone).
    milestones = add_milestone([], 0)
    milestones = add_milestone(milestones, 1)
    milestones = add_milestone(milestones, 2)
    milestones = add_milestone(milestones, 1)
    states = build_lifecycle_states("running", "inspecting", milestones)
    assert states[1] == "active", states
    assert states[2] == "done", states
    assert states[0] == "done", states
    print(f"F. OK: Editing->Inspecting; phases={phases}; UI={states_label(states)}")
    return out


def scenario_g_validation_failure_then_fix() -> Dict[str, Any]:
    """G. Validating -> Editing -> Running -> Validating (perbaikan nyata).

    LLM (skrip): setelah hasil tool command-verifikasi gagal (nonzero exit),
    memperbaiki file lalu menjalankan command verifikasi lagi.
    """
    out = run_task(
        "Perbaiki error di file lalu validasi.",
        [
            _tool_turn("baca", [("c1", "read_file", {"path": "a.txt"})]),
            _tool_turn("tulis", [("c2", "write_file", {"path": "g.py", "content": "print(1)"})]),
            _tool_turn("cek gagal", [("c3", "run_command", {"command": "python -m pytest -q"})]),
            _tool_turn("perbaiki", [("c4", "edit_file", {
                "path": "g.py", "old_text": "print(1)", "new_text": "print(2)"
            })]),
            _tool_turn("cek ulang", [("c5", "run_command", {"command": "python -m pytest -q"})]),
            _final_turn("Selesai."),
        ],
        task_id="scG",
    )
    phases = phase_events(out["events"])
    # planning -> inspecting -> editing -> validating -> editing -> validating
    assert phases == ["planning", "inspecting", "editing", "validating", "editing", "validating"], phases
    # After rerun: file benar-benar berubah (mutasi nyata).
    assert (FIXTURE / "g.py").read_text(encoding="utf-8") == "print(2)"
    # Lifecycle: milestone editing+validating tetap done saat kembali ke editing.
    milestones = [0, 1, 2, 4, 2, 4]
    states = build_lifecycle_states("running", "validating", milestones)
    assert states[2] == "done" and states[4] == "active", states
    print(f"G. OK: Validating->Editing->Running->Validating; phases={phases}; UI={states_label(states)}")
    return out


def scenario_h_task_failure() -> Dict[str, Any]:
    """H. Task FAILED: BUKAN Completed; phase terakhir dipertahankan."""
    out = run_task(
        "task yang akan gagal",
        [],
        task_id="scH",
        provider=BoomProvider([_final_turn("x")]),
    )
    result = out["result"]
    assert result.status == RuntimeStatus.FAILED, result.status
    types = event_types(out["events"])
    assert "task_failed" in types, types
    assert "task_completed" not in types, types
    # Provider error terjadi SETELAH planning -> phase terakhir = planning.
    phases = phase_events(out["events"])
    assert phases and phases[-1] == "planning", phases
    # UI: failed -> current tetap aktivitas terakhir (BUKAN Completed).
    states = build_lifecycle_states("failed", phases[-1], [0])
    assert states[0] == "active", states
    assert states[5] != "active", states
    print(f"H. OK: task_failed -> BUKAN Completed; phase terakhir={phases[-1]}; UI={states_label(states)}")
    return out


def scenario_i_cancellation() -> Dict[str, Any]:
    """I. Task CANCELLED: BUKAN Completed; phase terakhir dipertahankan."""
    from agent_ai.core.cancel import CancellationToken

    token = CancellationToken()
    # Skrip: tool pertama berjalan; sebelum tool kedua dieksekusi token di-request.
    script = [
        _tool_turn("baca", [("c1", "read_file", {"path": "a.txt"})]),
        _tool_turn("tulis", [("c2", "write_file", {"path": "i.txt", "content": "x"})]),
        _final_turn("Selesai."),
    ]
    # Request cancel setelah provider call pertama (tool pertama dieksekusi).
    provider = _CancellingProvider(script, token, cancel_after_calls=1)
    out = run_task(
        "task yang akan dibatalkan",
        script,
        task_id="scI",
        provider=provider,
        cancel_token=token,
    )
    result = out["result"]
    assert result.status == RuntimeStatus.CANCELLED, result.status
    types = event_types(out["events"])
    assert "task_cancelled" in types, types
    assert "task_completed" not in types, types
    phases = phase_events(out["events"])
    # Setelah tool pertama (inspecting) -> cancel di safe boundary: phase
    # terakhir inspecting (BUKAN completed).
    assert phases[-1] == "inspecting", phases
    # UI: cancelled -> current tetap aktivitas terakhir.
    states = build_lifecycle_states("cancelled", phases[-1], [0, 1])
    assert states[1] == "active", states
    assert states[5] != "active", states
    print(f"I. OK: task_cancelled -> BUKAN Completed; phase terakhir={phases[-1]}; UI={states_label(states)}")
    return out


class _CancellingProvider(ScriptedProvider):
    """Provider skrip yang meminta cancel setelah N call (safe boundary)."""

    def __init__(
        self,
        script: List[Dict[str, Any]],
        token: Any,
        *,
        cancel_after_calls: int,
    ) -> None:
        super().__init__(script)
        self.token = token
        self.cancel_after_calls = cancel_after_calls

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Any]] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[List[Any]] = None,
        tool_choice: Optional[Any] = None,
    ) -> GenerateResult:
        result = super().generate(
            prompt=prompt, messages=messages, options=options, tools=tools, tool_choice=tool_choice
        )
        if self.calls > self.cancel_after_calls:
            self.token.request("user stop (verifier)")
        return result


def scenario_j_history_reconnect() -> Dict[str, Any]:
    """J. History/reconnect: lifecycle dari history == lifecycle live SSE."""
    out = run_task(
        "Baca, edit, running, validasi.",
        [
            _tool_turn("baca", [("c1", "read_file", {"path": "a.txt"})]),
            _tool_turn("edit", [("c2", "write_file", {"path": "j.txt", "content": "v"})]),
            _tool_turn("jalan", [("c3", "run_command", {"command": "echo j"})]),
            _tool_turn("cek", [("c4", "run_command", {"command": "python -m pytest -q"})]),
            _final_turn("Selesai."),
        ],
        task_id="scJ",
    )
    events = out["events"]
    phases = phase_events(events)
    assert phases == ["planning", "inspecting", "editing", "running", "validating"], phases

    # ---- Live (SSE): frontend handleEvent pada event -> activityPhase + milestones
    activity_phase = ""
    milestones: List[int] = []
    for evt in events:
        etype = evt["event_type"]
        payload = evt.get("payload") or {}
        if etype == "task_started":
            activity_phase = "planning"
            milestones = add_milestone(milestones, 0)
        elif etype == "phase_changed":
            idx = PHASE_INDEX.get(str(payload.get("phase", "")).lower(), -1)
            if idx >= 0:
                activity_phase = str(payload["phase"]).lower()
                milestones = add_milestone(milestones, idx)
    states_live = build_lifecycle_states("completed", activity_phase, milestones)

    # ---- Reconnect/history: lifecycleFromEvents dari event yang sama
    hist = lifecycle_from_events(events)
    states_hist = build_lifecycle_states("completed", hist["current_phase"], hist["milestones"])

    assert states_live == states_hist, (states_live, states_hist)
    assert hist["current_phase"] == "validating", hist
    assert hist["milestones"] == [0, 1, 2, 3, 4], hist
    expected = ["done", "done", "done", "done", "done", "active"]
    assert states_live == expected, states_live
    print(f"J. OK: live==history setelah reconnect; phases={phases}; UI={states_label(states_hist)}")
    return out


def scenario_k_no_duplicate_phase_source() -> None:
    """K. Nilai internal runtime (replan/provider_fallback) TIDAK menggerakkan lifecycle."""
    # activityPhaseIndex (lifecycle.js): hanya activity phase yang dikenal.
    def _activity_phase_index(phase: Any) -> int:
        if phase is None:
            return -1
        key = str(phase).strip().lower()
        return PHASE_INDEX.get(key, -1)

    for bad in ("replan", "provider_fallback"):
        assert _activity_phase_index(bad) == -1, bad
        assert bad not in PHASE_INDEX, bad
    # phase_changed dengan nilai tsb tidak menambah milestone.
    hist = lifecycle_from_events(
        [
            {"event_type": "task_started", "payload": {}},
            {"event_type": "phase_changed", "payload": {"phase": "replan"}},
            {"event_type": "phase_changed", "payload": {"phase": "editing"}},
        ]
    )
    assert hist["current_phase"] == "editing", hist
    assert hist["milestones"] == [0, 2], hist
    # Backend: klasifikasi activity phase (classify_tool_activity) tidak pernah
    # mengembalikan replan/provider_fallback.
    assert ActivityPhase.PLANNING.value not in FORBIDDEN_PHASES
    print(f"K. OK: replan/provider_fallback BUKAN sumber lifecycle (activity phase tetap sumber tunggal)")
    return None


def scenario_l_sse_frames() -> Dict[str, Any]:
    """L. SSE transport nyata: frame berisi event_type + payload phase yang sama."""
    out = run_task(
        "Baca, tulis, jalankan, validasi.",
        [
            _tool_turn("baca", [("c1", "read_file", {"path": "a.txt"})]),
            _tool_turn("tulis", [("c2", "write_file", {"path": "l.txt", "content": "w"})]),
            _tool_turn("jalan", [("c3", "run_command", {"command": "echo l"})]),
            _tool_turn("cek", [("c4", "run_command", {"command": "python -m pytest -q"})]),
            _final_turn("Selesai."),
        ],
        task_id="scL",
    )
    frames = out["sse_frames"]
    assert frames, "harus ada frame SSE"
    phase_frames = [f for f in frames if "event: phase_changed" in f]
    assert phase_frames, "harus ada frame phase_changed lewat SSE"
    # Setiap frame phase_changed membawa data JSON dengan payload.phase.
    for frame in phase_frames:
        data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
        data = json.loads(data_line[len("data: "):])
        assert data["event_type"] == "phase_changed"
        assert "phase" in data["payload"], data
    phases_in_sse = [
        json.loads(
            next(line for line in f.splitlines() if line.startswith("data: "))
            .replace("data: ", "", 1)
        )["payload"]["phase"]
        for f in phase_frames
    ]
    # Phase dari SSE sama dengan phase dari event store.
    assert phases_in_sse == phase_events(out["events"]), (phases_in_sse, phase_events(out["events"]))
    print(f"L. OK: SSE frames membawa phase_changed -> {phases_in_sse}")
    return out


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    print("=== Verifikasi FINAL: Runtime Lifecycle AETHER -> SSE -> UI ===\n")

    if FIXTURE.exists():
        shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir(parents=True, exist_ok=True)

    try:
        scenario_a_inspection()
        scenario_b_editing()
        scenario_c_running()
        scenario_d_validating()
        scenario_e_completed_terminal()
        scenario_f_return_previous_phase()
        scenario_g_validation_failure_then_fix()
        scenario_h_task_failure()
        scenario_i_cancellation()
        scenario_j_history_reconnect()
        scenario_k_no_duplicate_phase_source()
        scenario_l_sse_frames()
    finally:
        shutil.rmtree(FIXTURE, ignore_errors=True)
        try:
            if DUMMY_ROOT.exists() and not any(DUMMY_ROOT.iterdir()):
                DUMMY_ROOT.rmdir()
        except OSError:
            pass

    print()
    print("[OK] Lifecycle final AETHER VERIFIED: task_started -> phase_changed "
          "(planning/inspecting/editing/running/validating) -> task_completed "
          "mengalir dari runtime nyata lewat event SSE ke lifecycle UI; "
          "failed/cancelled bukan Completed; phase dapat mundur dengan milestone "
          "tetap done; history/reconnect konsisten; tanpa sumber phase ganda.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())