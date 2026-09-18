"""Verifikasi completion detection Agent Loop (fix: task selesai != iteration limit).

Provider palsu (scripted) -> tidak memanggil API cloud/Ollama. Fixture workspace
dibuat di `dummy_test/` (workspace testing terisolasi) dan dibersihkan setelah.

CATATAN: verifier ini menguji jalur LEGACY (`use_continuous_loop=False`),
    yaitu loop lama beserta heuristic completion-nya ("task selesai terdeteksi
    dari perubahan file / hasil command"). Sejak continuous loop menjadi jalur
    NORMAL, heuristic ini TIDAK dipakai lagi; continuous tidak menebak selesai —
    keputusan final murni dari response LLM (lihat check_continuous_loop.py).

Skenario:
    A) write_file -> run_command sukses -> final response.
       Loop berhenti sebelum max_iterations, status Completed, task_failed
       TIDAK dipanggil.
    B) Agent tidak pernah menyelesaikan task (selalu minta tool, tanpa final).
       Iteration limit tercapai -> status Failed (alasan iteration limit),
       task_failed dipanggil.
    C) Kasus aktual: implementasi (write_file) + validasi (run_command sukses),
       lalu agent terus memanggil tool sampai iteration limit TANPA final.
       Completion detection -> status Completed (bukan Failed), task_failed
       TIDAK dipanggil.
    D) Hanya membaca (tanpa perubahan) sampai iteration limit -> tetap Failed
       (safety limit), memastikan tidak over-complete.

Jalankan:
    python scripts/check_loop_completion.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core import (  # noqa: E402
    ActionType,
    FinishReason,
    LLMAction,
    LLMResponse,
)
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402
from agent_ai.runtime.runtime import AgentRuntime  # noqa: E402
from agent_ai.session.store import InMemorySessionStore  # noqa: E402
from agent_ai.task.models import PreparedTask  # noqa: E402
from agent_ai.tools.registry import build_registry  # noqa: E402

# Fixture workspace (isolated) sesuai aturan: dummy_test/.
FIXTURE = PROJECT_ROOT / "dummy_test" / "_check_loop_completion"


class ScriptedProvider(BaseProvider):
    """Provider palsu: kembalikan LLMResponse terprogram per pemanggilan.

    Setelah daftar habis, respons terakhir diulang (untuk mensimulasikan agent
    yang terus memanggil tool sampai iteration limit).
    """

    name = "scripted"

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        return GenerateResult(text="", model="fake", provider=self.name, raw={})

    def normalize_response(self, result):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


def tool_call(name, arguments):
    return LLMResponse(
        actions=[LLMAction(name=name, arguments=arguments, type=ActionType.TOOL_CALL)],
        finish_reason=FinishReason.TOOL_CALLS,
    )


def final(text):
    return LLMResponse(text=text, finish_reason=FinishReason.STOP)


def run_scenario(responses, max_iterations):
    """Jalankan satu skenario lewat AgentRuntime (jalur produksi)."""
    store = InMemorySessionStore()
    session = store.create_session(metadata={"task_id": "lc"})
    executor = ToolExecutor(registry=build_registry(root=str(FIXTURE)))
    runtime = AgentRuntime(
        provider=ScriptedProvider(responses),
        executor=executor,
        max_iterations=max_iterations,
        session_store=store,
        session_id=session.session_id,
        # Jalur legacy: heuristic completion + iteration limit diuji di sini.
        use_continuous_loop=False,
    )
    prepared = PreparedTask(task="Kerjakan task kecil.", task_id="lc")
    result = runtime.run(prepared)
    event_types = [e.event_type.value for e in store.get_events(session_id=session.session_id)]
    return result, event_types


def main() -> int:
    print("=== Verifikasi Agent Loop: completion detection ===")
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir(parents=True, exist_ok=True)

    try:
        # --- Skenario A: write_file -> run_command sukses -> final. ---
        print("\n[A] write_file -> run_command sukses -> final response")
        result_a, events_a = run_scenario(
            [
                tool_call("write_file", {"path": "hello.txt", "content": "hi"}),
                tool_call("run_command", {"command": "python --version"}),
                final("Selesai: hello.txt dibuat dan diverifikasi."),
            ],
            max_iterations=6,
        )
        print(f"    status={result_a.status.value} iterations={result_a.iterations}")
        assert result_a.status.value == "completed", result_a.error
        assert result_a.iterations < 6, "loop harus berhenti sebelum max_iterations"
        assert "task_failed" not in events_a, "task_failed tidak boleh dipanggil"
        assert "task_completed" in events_a, "task_completed harus dipanggil"
        assert (FIXTURE / "hello.txt").exists(), "file hasil implementasi harus ada"
        print("    OK: Completed sebelum limit, task_failed tidak dipanggil")

        # --- Skenario B: tidak pernah selesai -> iteration limit -> Failed. ---
        print("\n[B] selalu memanggil tool, tidak pernah final -> iteration limit")
        result_b, events_b = run_scenario(
            [tool_call("list_files", {"path": "."})],
            max_iterations=3,
        )
        print(f"    status={result_b.status.value} error={result_b.error!r}")
        assert result_b.status.value == "failed", "harus Failed pada iteration limit"
        assert "maksimum iterasi" in (result_b.error or "").lower(), result_b.error
        assert "task_failed" in events_b, "task_failed harus dipanggil"
        assert "task_completed" not in events_b
        print("    OK: Failed (iteration limit), task_failed dipanggil")

        # --- Skenario C: kasus aktual -> implementasi+validasi, lalu loop. ---
        print("\n[C] write_file -> run_command sukses -> tool tambahan sampai limit")
        result_c, events_c = run_scenario(
            [
                tool_call("write_file", {"path": "hello.txt", "content": "hi"}),
                tool_call("run_command", {"command": "python --version"}),
                tool_call("list_files", {"path": "."}),
            ],
            max_iterations=4,
        )
        print(f"    status={result_c.status.value} error={result_c.error!r}")
        assert result_c.status.value == "completed", result_c.error
        assert "task_failed" not in events_c, "task_failed tidak boleh dipanggil"
        assert "task_completed" in events_c
        print("    OK: Completed (completion terdeteksi di iteration limit)")

        # --- Skenario D: hanya membaca sampai limit -> tetap Failed. ---
        print("\n[D] hanya membaca (tanpa perubahan) sampai iteration limit")
        result_d, events_d = run_scenario(
            [
                tool_call("read_file", {"path": "hello.txt"}),
                tool_call("list_files", {"path": "."}),
            ],
            max_iterations=3,
        )
        print(f"    status={result_d.status.value} error={result_d.error!r}")
        assert result_d.status.value == "failed", "read-only harus tetap Failed"
        assert "task_failed" in events_d
        print("    OK: Failed (tidak over-complete)")

        print(
            "\n[OK] Agent Loop: final response = source of truth; iteration limit = "
            "safety limit; task yang sudah selesai tidak salah ditandai Failed."
        )
        return 0
    finally:
        shutil.rmtree(FIXTURE, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
