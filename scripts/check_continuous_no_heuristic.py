"""Verifier: continuous loop TIDAK memakai heuristic completion.

Membuktikan kontrak completion `AgentOrchestrator.run_continuous_loop`:

    - Satu-satunya yang menghentikan loop dengan SUKSES adalah response LLM
      TANPA tool call (LLM Final -> DONE).
    - Perubahan file (mutasi sukses) TIDAK menghentikan loop.
    - Hasil command / tool sukses TIDAK menghentikan loop.
    - Tool error TIDAK menghentikan loop (kontrol tetap ke LLM).
    - Keyword task (mis. "test"/"validasi") dan nama file eksplisit TIDAK
      memaksa step tambahan dan TIDAK memutus loop.
    - `_completion_detected()` (heuristic loop lama) TIDAK pernah dipanggil;
      walau dipaksa mengembalikan True, loop continuous tetap MENGABAIKAN-nya.
    - TIDAK ada cap jumlah step: melewati `max_steps` TIDAK mem-FAIL task;
      loop berhenti hanya karena LLM final (atau cancel/error fatal).

Provider palsu (scripted) -> tanpa network/API. Fixture workspace berada di
`J:\\Agent_Ai\\dummy_test` (workspace uji terisolasi, BUKAN bagian AETHER) dan
dibersihkan setelah verifikasi selesai.

Jalankan:
    python scripts/check_continuous_no_heuristic.py
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
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
)
from agent_ai.tools.registry import build_registry  # noqa: E402

# Workspace uji terisolasi (di luar source AETHER).
DUMMY_ROOT = Path(r"J:\Agent_Ai\dummy_test")
FIXTURE = DUMMY_ROOT / "continuous_no_heuristic_fixture"

# Teks result heuristic loop lama (bukti bahwa completion TIDAK dari heuristic).
HEURISTIC_RESULT_MARKER = "completion terdeteksi"


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


class ScriptedProvider(OpenAICompatibleProvider):
    """Provider palsu: kembalikan respons skrip berurutan (tanpa network)."""

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
        self.requests.append([dict(m) if isinstance(m, dict) else m for m in (messages or [])])
        idx = self.calls
        self.calls += 1
        raw = self.script[idx] if idx < len(self.script) else _final_turn("(default)")
        return GenerateResult(text="", model="scripted-model", provider=self.name, raw=raw)


class SpyCompletion:
    """Spy untuk `_completion_detected`: catat pemanggilan + paksa True.

    Bila continuous loop sampai memanggilnya, kita tahu heuristic masih bocor ke
    jalur ini. Karena spy mengembalikan True, loop (kalau memakainya) akan
    berhenti lebih awal -> terdeteksi dari jumlah pemanggilan LLM/result.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, loop: Any) -> bool:  # noqa: ANN001 - duck typing
        self.calls += 1
        return True


def _make_orch(provider: ScriptedProvider, executor: ToolExecutor) -> AgentOrchestrator:
    return AgentOrchestrator(
        provider=provider,
        executor=executor,
        options=GenerateOptions(model="scripted-model"),
        system_prompt="Kamu adalah coding agent.",
        use_continuous_loop=True,
    )


# --------------------------------------------------------------------------- #
# Skenario
# --------------------------------------------------------------------------- #
def scenario_mutation_does_not_stop(executor: ToolExecutor) -> None:
    """Mutasi file sukses diikuti tool call lagi -> loop TIDAK berhenti."""
    provider = ScriptedProvider(
        [
            _tool_turn("Menulis a.txt.", [_tool_call("c1", "write_file", {"path": "a.txt", "content": "hi"})]),
            _tool_turn("Membaca ulang.", [_tool_call("c2", "read_file", {"path": "a.txt"})]),
            _final_turn("Selesai."),
        ]
    )
    orch = _make_orch(provider, executor)
    spy = SpyCompletion()
    orch._completion_detected = spy  # type: ignore[assignment]

    result = orch.run("Tulis a.txt")
    print(f"[A] status={result.status.value} calls={provider.calls} spy={spy.calls}")
    assert result.status == AgentStatus.DONE, result.error
    assert provider.calls == 3, f"loop harus lanjut sampai final (dapat {provider.calls} panggilan)"
    assert spy.calls == 0, "continuous loop TIDAK boleh memanggil _completion_detected()"
    assert result.result == "Selesai.", result.result
    assert HEURISTIC_RESULT_MARKER not in (result.result or "")
    print("OK: mutasi file tidak menghentikan loop; heuristic tidak dipanggil")


def scenario_tool_success_then_tool_call(executor: ToolExecutor) -> None:
    """Tool sukses (command) diikuti tool call lagi -> loop TIDAK berhenti."""
    provider = ScriptedProvider(
        [
            _tool_turn("Versi python.", [_tool_call("c1", "run_command", {"command": "python --version"})]),
            _tool_turn("Tulis b.txt.", [_tool_call("c2", "write_file", {"path": "b.txt", "content": "x"})]),
            _final_turn("Selesai."),
        ]
    )
    orch = _make_orch(provider, executor)
    result = orch.run("Cek lalu tulis b.txt")
    print(f"[B] status={result.status.value} calls={provider.calls}")
    assert result.status == AgentStatus.DONE, result.error
    assert provider.calls == 3, provider.calls
    assert (FIXTURE / "b.txt").exists()
    print("OK: hasil command/tool sukses tidak menghentikan loop")


def scenario_tool_error_then_continue(executor: ToolExecutor) -> None:
    """Tool error diikuti tool call lagi -> loop TIDAK berhenti, kontrol ke LLM."""
    provider = ScriptedProvider(
        [
            _tool_turn("Coba tool tak dikenal.", [_tool_call("c1", "no_such_tool_xyz", {})]),
            _tool_turn("Tulis c.txt.", [_tool_call("c2", "write_file", {"path": "c.txt", "content": "y"})]),
            _final_turn("Selesai setelah error."),
        ]
    )
    orch = _make_orch(provider, executor)
    result = orch.run("Kerjakan")
    print(f"[C] status={result.status.value} calls={provider.calls}")
    assert result.status == AgentStatus.DONE, result.error
    assert provider.calls == 3, provider.calls
    # Error tool dikirim balik sebagai pesan role "tool" (bukan stop).
    third = provider.requests[2]
    tool_msgs = [m for m in third if m.get("role") == "tool"]
    assert len(tool_msgs) == 2, tool_msgs
    print("OK: tool error tidak menghentikan loop; hasil error tetap ke LLM")


def scenario_task_keywords_ignored(executor: ToolExecutor) -> None:
    """Keyword task ('test'/'validasi') + nama file tidak memaksa step/keputusan."""
    provider = ScriptedProvider(
        [
            _tool_turn("Tulis.", [_tool_call("c1", "write_file", {"path": "hello.txt", "content": "z"})]),
            _final_turn("done"),
        ]
    )
    orch = _make_orch(provider, executor)
    result = orch.run("Tulis hello.txt dan pastikan test/validasi lulus.")
    print(f"[D] status={result.status.value} calls={provider.calls} result={result.result!r}")
    assert result.status == AgentStatus.DONE, result.error
    # Heuristic loop lama akan MENOLAK completion (butuh run_command test) dan
    # tidak akan memakai result ini; continuous loop abaikan keyword tersebut.
    assert provider.calls == 2, provider.calls
    assert result.result == "done", result.result
    assert HEURISTIC_RESULT_MARKER not in (result.result or "")
    # Tidak ada nudge/sintesis pesan user tambahan (hanya task asli).
    second = provider.requests[1]
    user_msgs = [m for m in second if m.get("role") == "user"]
    assert len(user_msgs) == 1, user_msgs
    print("OK: keyword/nama file task tidak memaksa step tambahan & tidak memutus loop")


def scenario_forced_heuristic_ignored(executor: ToolExecutor) -> None:
    """Walau `_completion_detected` dipaksa True, misi tetap tuntas via LLM final."""
    provider = ScriptedProvider(
        [
            _tool_turn("Tulis.", [_tool_call("c1", "write_file", {"path": "d.txt", "content": "w"})]),
            _tool_turn("Tulis lagi.", [_tool_call("c2", "write_file", {"path": "e.txt", "content": "v"})]),
            _final_turn("FINAL-DARI-LLM"),
        ]
    )
    orch = _make_orch(provider, executor)
    spy = SpyCompletion()
    orch._completion_detected = spy  # type: ignore[assignment]

    result = orch.run("Tulis d.txt dan e.txt")
    print(f"[E] status={result.status.value} calls={provider.calls} spy={spy.calls}")
    assert result.status == AgentStatus.DONE, result.error
    # Bila heuristic dipakai, loop akan berhenti setelah turn 1 (calls == 1).
    assert provider.calls == 3, f"paksa-True tidak boleh memutus loop (calls={provider.calls})"
    assert spy.calls == 0
    assert result.result == "FINAL-DARI-LLM", result.result
    assert HEURISTIC_RESULT_MARKER not in (result.result or "")
    print("OK: _completion_detected dipaksa True pun DIABAIKAN oleh continuous loop")


def scenario_no_step_cap(executor: ToolExecutor) -> None:
    """Tidak ada cap step: melewati `max_steps` tetap lanjut sampai LLM final."""
    (FIXTURE / "a.txt").write_text("hi", encoding="utf-8")
    script = [
        _tool_turn(f"baca {i}", [_tool_call(f"r{i}", "read_file", {"path": "a.txt"})])
        for i in range(6)
    ]
    provider = ScriptedProvider(script)
    orch = _make_orch(provider, executor)
    result = orch.run_continuous_loop("Baca terus tanpa henti", max_steps=3)
    print(f"[F] status={result.status.value} calls={provider.calls} iterations={result.iterations}")
    assert result.status == AgentStatus.DONE, result.error
    assert provider.calls == 7, provider.calls
    assert result.iterations == 6, result.iterations
    print("OK: tidak ada cap step; loop berhenti karena LLM final (bukan FAILED)")


def main() -> int:
    print("=== Verifikasi Continuous Loop: TANPA heuristic completion ===")

    if FIXTURE.exists():
        shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir(parents=True, exist_ok=True)

    def executor() -> ToolExecutor:
        return ToolExecutor(build_registry(root=FIXTURE))

    try:
        scenario_mutation_does_not_stop(executor())
        scenario_tool_success_then_tool_call(executor())
        scenario_tool_error_then_continue(executor())
        scenario_task_keywords_ignored(executor())
        scenario_forced_heuristic_ignored(executor())
        scenario_no_step_cap(executor())
    finally:
        shutil.rmtree(FIXTURE, ignore_errors=True)
        # Bersihkan root dummy_test hanya bila sudah kosong.
        try:
            if DUMMY_ROOT.exists() and not any(DUMMY_ROOT.iterdir()):
                DUMMY_ROOT.rmdir()
        except OSError:
            pass

    print()
    print(
        "[OK] Continuous loop hanya selesai karena LLM Final; tidak ada heuristic "
        "file/command/keyword yang dapat memutusnya."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
