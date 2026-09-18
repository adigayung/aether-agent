"""Verifier: AgentRuntime menjalankan task sebagai SATU percakapan kontinu.

Melengkapi `check_continuous_loop.py` (yang menguji orchestrator langsung).
Di sini yang diuji adalah LAPIS RUNTIME (`AgentRuntime.run`) memakai provider
palsu (scripted) -> deterministik, tanpa network/API.

Kontrak yang dibuktikan:
    A. Jalur NORMAL default = continuous (`use_continuous_loop is True`).
    B. Satu task = SATU percakapan kontinu meski plan punya banyak step:
       provider dipanggil 2x (turn tool + final), BUKAN per step.
    C. Plan TIDAK dijalankan per-step: `result.steps == []`, plan tetap
       PENDING, dan objek plan tidak diganti/diubah.
    D. Plan hanya context ADVISORY: teksnya disuntikkan ke prompt, tetapi
       urutan tool TIDAK mengikuti urutan step plan (LLM yang menentukan).
    E. Tool dieksekusi via ToolExecutor; hasil kembali sebagai role "tool"
       (native tool result), bukan pesan "user".
    F. SEMUA tool call dalam satu turn dieksekusi.
    G. Provider error -> RuntimeStatus.FAILED dengan pesan jelas.
    H. Observability tidak duplikat: 1 task_started / 1 task_completed.
    I. Tanpa plan -> tetap continuous (satu percakapan).
    J. Opt-in legacy (`use_continuous_loop=False`) tetap per-step.

Fixture workspace berada di `J:\\Agent_Ai\\dummy_test` (workspace uji
terisolasi, BUKAN bagian dari AETHER) dan dibersihkan setelah verifikasi.

Jalankan:
    python scripts/check_continuous_runtime.py
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
from agent_ai.planning.models import PlanStatus, PlanStep, StepStatus, TaskPlan  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.runtime.runtime import AgentRuntime, RuntimeStatus  # noqa: E402
from agent_ai.session.store import InMemorySessionStore  # noqa: E402
from agent_ai.task.models import PreparedTask  # noqa: E402
from agent_ai.tools.registry import build_registry  # noqa: E402

DUMMY_ROOT = Path(r"J:\Agent_Ai\dummy_test")
FIXTURE = DUMMY_ROOT / "continuous_runtime_fixture"


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
    """Provider palsu: respons skrip berurutan (tanpa network).

    Mewarisi `normalize_response` OpenAI-compatible agar `raw` skrip diparse
    persis seperti provider asli (tool_calls -> LLMAction).
    """

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


class BoomProvider(ScriptedProvider):
    """Provider palsu yang selalu gagal (simulasi provider error)."""

    def generate(self, *args: Any, **kwargs: Any) -> GenerateResult:
        raise RuntimeError("koneksi ke provider gagal")


# --------------------------------------------------------------------------- #
# Helper
# --------------------------------------------------------------------------- #
def executor() -> ToolExecutor:
    return ToolExecutor(build_registry(root=FIXTURE))


def make_plan(task: str, titles: List[str]) -> TaskPlan:
    plan = TaskPlan(task=task)
    plan.steps = [PlanStep(title=t) for t in titles]
    return plan


def user_texts(provider: ScriptedProvider, request_index: int) -> str:
    return " ".join(
        str(m.get("content"))
        for m in provider.requests[request_index]
        if m.get("role") == "user"
    )


# --------------------------------------------------------------------------- #
# Skenario
# --------------------------------------------------------------------------- #
def scenario_a_default_is_continuous() -> None:
    """A. Default runtime = continuous loop (jalur normal)."""
    runtime = AgentRuntime(provider=ScriptedProvider([_final_turn("ok")]))
    assert runtime.use_continuous_loop is True, "default harus continuous"
    print("A. OK: default AgentRuntime.use_continuous_loop is True (jalur normal)")


def scenario_b_single_conversation_with_plan() -> Dict[str, Any]:
    """B. Plan 4 step, tapi HANYA satu percakapan kontinu (provider 2x)."""
    plan = make_plan(
        "Buat file lalu laporkan",
        ["Inspect repo", "Rancang solusi", "Implementasi", "Verifikasi"],
    )
    provider = ScriptedProvider(
        [
            _tool_turn(
                "Menulis file.",
                [_tool_call("c1", "write_file", {"path": "out.txt", "content": "hi"})],
            ),
            _final_turn("Selesai menulis out.txt."),
        ]
    )
    runtime = AgentRuntime(provider=provider, executor=executor(), options=GenerateOptions(model="scripted-model"))
    prepared = PreparedTask(task="Buat file lalu laporkan", plan=plan)

    result = runtime.run(prepared)
    print(f"B. status={result.status.value} provider_calls={provider.calls} plan_steps={len(plan.steps)}")
    assert result.status == RuntimeStatus.COMPLETED, result.error
    assert provider.calls == 2, f"harus 2 panggilan (tool+final), dapat {provider.calls}"
    assert len(plan.steps) == 4, "plan harus punya 4 step"
    assert (FIXTURE / "out.txt").read_text(encoding="utf-8") == "hi"
    print("B. OK: 4 step plan TIDAK menjadi 4 eksekusi; satu percakapan kontinu")
    return {"plan": plan, "result": result, "provider": provider}


def scenario_c_plan_not_executed_per_step(plan_holder: Dict[str, Any]) -> None:
    """C. Plan tidak dijalankan per-step: result.steps kosong, plan PENDING."""
    plan = plan_holder["plan"]
    print(f"C. plan.status={plan.status.value}")
    assert plan_holder["result"].steps == [], "continuous tidak membuat TaskStep"
    assert plan.status == PlanStatus.PENDING, "plan tidak boleh dieksekusi per-step"
    assert all(s.status == StepStatus.PENDING for s in plan.steps), [
        s.status.value for s in plan.steps
    ]
    assert all(s.actual_outcome is None for s in plan.steps)
    print("C. OK: plan tidak dieksekusi per-step (semua step tetap PENDING)")


def scenario_d_plan_is_advisory_only() -> None:
    """D. Plan hanya saran; urutan tool ditentukan LLM, bukan urutan step."""
    # Urutan step plan sengaja 'terbalik' dari tool yang dipanggil LLM.
    plan = make_plan(
        "Kerjakan apa saja",
        ["Jalankan test lengkap", "Tulis dokumentasi", "Refactor modul"],
    )
    provider = ScriptedProvider(
        [
            _tool_turn(
                "Saya mulai dengan menulis file.",
                [_tool_call("c1", "write_file", {"path": "chosen.txt", "content": "x"})],
            ),
            _final_turn("Beres."),
        ]
    )
    runtime = AgentRuntime(provider=provider, executor=executor(), options=GenerateOptions(model="scripted-model"))
    result = runtime.run(PreparedTask(task="Kerjakan apa saja", plan=plan))
    assert result.status == RuntimeStatus.COMPLETED, result.error

    prompt = user_texts(provider, 0)
    assert "advisory" in prompt.lower(), "plan harus disuntikkan sebagai saran advisory"
    assert "Jalankan test lengkap" in prompt, "judul step plan harus muncul sebagai saran"

    # Yang dieksekusi adalah pilihan LLM (write_file chosen.txt), bukan step-1
    # plan (yang mengarah ke menjalankan test).
    assert (FIXTURE / "chosen.txt").exists(), "tool pilihan LLM harus dieksekusi"
    print("D. OK: plan hanya saran (advisory); urutan tool diambil dari LLM")


def scenario_e_f_tool_results_native() -> None:
    """E+F. Semua tool satu turn dieksekusi; hasil kembali sebagai role 'tool'."""
    provider = ScriptedProvider(
        [
            _tool_turn(
                "Menulis dua file.",
                [
                    _tool_call("call-a", "write_file", {"path": "a.txt", "content": "hello"}),
                    _tool_call("call-b", "write_file", {"path": "b.txt", "content": "world"}),
                ],
            ),
            _final_turn("Dua file selesai."),
        ]
    )
    runtime = AgentRuntime(provider=provider, executor=executor(), options=GenerateOptions(model="scripted-model"))
    result = runtime.run(PreparedTask(task="Tulis a.txt dan b.txt"))
    assert result.status == RuntimeStatus.COMPLETED, result.error
    assert (FIXTURE / "a.txt").read_text(encoding="utf-8") == "hello"
    assert (FIXTURE / "b.txt").read_text(encoding="utf-8") == "world"

    second = provider.requests[1]
    roles = [m.get("role") for m in second]
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert sorted(m.get("tool_call_id") for m in tool_msgs) == ["call-a", "call-b"], tool_msgs
    joined_user = user_texts(provider, 1)
    assert "hello" not in joined_user and "world" not in joined_user, joined_user
    print(f"E+F. OK: roles={roles}; hasil tool native (role 'tool' + tool_call_id)")


def scenario_g_provider_error() -> None:
    """G. Provider error -> RuntimeStatus.FAILED dengan pesan jelas."""
    runtime = AgentRuntime(provider=BoomProvider([]), executor=executor())
    result = runtime.run(PreparedTask(task="apa saja"))
    print(f"G. status={result.status.value} error={result.error!r}")
    assert result.status == RuntimeStatus.FAILED
    assert "koneksi ke provider gagal" in (result.error or ""), result.error
    print("G. OK: provider error -> FAILED (tanpa crash/menggantung)")


def scenario_h_single_observability() -> None:
    """H. Tidak ada observability duplikat: 1 task_started / 1 task_completed."""
    store = InMemorySessionStore()
    session = store.create_session(metadata={"task_id": "obs"})
    provider = ScriptedProvider(
        [
            _tool_turn("baca", [_tool_call("c1", "read_file", {"path": "a.txt"})]),
            _final_turn("selesai"),
        ]
    )
    runtime = AgentRuntime(
        provider=provider,
        executor=executor(),
        options=GenerateOptions(model="scripted-model"),
        session_store=store,
        session_id=session.session_id,
    )
    # a.txt harus ada untuk dibaca.
    (FIXTURE / "a.txt").write_text("hello", encoding="utf-8")
    result = runtime.run(PreparedTask(task="baca a.txt", task_id="obs"))
    assert result.status == RuntimeStatus.COMPLETED, result.error

    types = [e.event_type.value for e in store.get_events(session_id=session.session_id)]
    assert types.count("task_started") == 1, types
    assert types.count("task_completed") == 1, types
    print(f"H. OK: event terminal tidak duplikat -> started={types.count('task_started')}, "
          f"completed={types.count('task_completed')}")


def scenario_i_no_plan_still_continuous() -> None:
    """I. Tanpa plan -> tetap continuous (satu percakapan)."""
    provider = ScriptedProvider(
        [
            _tool_turn("baca", [_tool_call("c1", "read_file", {"path": "a.txt"})]),
            _final_turn("selesai"),
        ]
    )
    runtime = AgentRuntime(provider=provider, executor=executor(), options=GenerateOptions(model="scripted-model"))
    result = runtime.run(PreparedTask(task="baca a.txt"))
    assert result.status == RuntimeStatus.COMPLETED, result.error
    assert provider.calls == 2 and result.steps == []
    print("I. OK: tanpa plan tetap satu percakapan kontinu")


def scenario_j_legacy_opt_in() -> None:
    """J. Opt-in legacy -> eksekusi per-step tetap bekerja."""
    plan = make_plan("Perbaiki bug", ["Analisis", "Perbaiki", "Verifikasi"])
    provider = ScriptedProvider([_final_turn("step selesai")])
    runtime = AgentRuntime(
        provider=provider,
        executor=executor(),
        options=GenerateOptions(model="scripted-model"),
        use_continuous_loop=False,
    )
    result = runtime.run(PreparedTask(task="Perbaiki bug", plan=plan))
    print(f"J. status={result.status.value} provider_calls={provider.calls} steps={len(result.steps)}")
    assert result.status == RuntimeStatus.COMPLETED, result.error
    assert len(result.steps) == len(plan.steps), "legacy harus mencatat tiap step"
    assert plan.status == PlanStatus.COMPLETED, plan.status
    print("J. OK: opt-in legacy (use_continuous_loop=False) tetap per-step")


def main() -> int:
    print("=== Verifikasi Continuous Runtime (satu percakapan per task) ===")

    if FIXTURE.exists():
        shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir(parents=True, exist_ok=True)

    try:
        scenario_a_default_is_continuous()
        holder_b = scenario_b_single_conversation_with_plan()
        scenario_c_plan_not_executed_per_step(holder_b)
        scenario_d_plan_is_advisory_only()
        scenario_e_f_tool_results_native()
        scenario_g_provider_error()
        scenario_h_single_observability()
        scenario_i_no_plan_still_continuous()
        scenario_j_legacy_opt_in()
    finally:
        shutil.rmtree(FIXTURE, ignore_errors=True)
        try:
            if DUMMY_ROOT.exists() and not any(DUMMY_ROOT.iterdir()):
                DUMMY_ROOT.rmdir()
        except OSError:
            pass

    print()
    print("[OK] AgentRuntime menjalankan task sebagai satu percakapan kontinu "
          "(plan advisory, tanpa per-step TaskStep).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
