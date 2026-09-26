"""Verifier Continuous Agent Loop (Native Tool Calling, satu percakapan kontinu).

Menguji `AgentOrchestrator.run_continuous_loop` TANPA network/API memakai provider
palsu (scripted). Yang diverifikasi:

    - Satu task = SATU percakapan kontinu (bukan nested LLM session per step).
    - `assistant(tool_calls)` selalu diikuti pesan role "tool" + `tool_call_id`.
    - Hasil tool TIDAK dikirim sebagai pesan "user" (bukan observation prefix).
    - SEMUA tool call dalam satu turn dieksekusi (bisa > 1).
    - Loop berhenti saat LLM memberi response TANPA tool call (final) -> DONE.
    - Tidak ada limit kecil (5/10): >10 turn tool tetap berjalan kontinu.
    - Provider error -> FAILED dengan pesan jelas (provider_error=True).
    - Default (`use_continuous_loop=True`) -> run() delegasikan ke continuous
          loop, jalur eksekusi NORMAL; loop lama hanya via eksplisit `False`.

Fixture workspace berada di `J:\\Agent_Ai\\dummy_test` (workspace uji terisolasi,
BUKAN bagian dari AETHER) dan dibersihkan setelah verifikasi selesai.

Jalankan:
    python scripts/check_continuous_loop.py
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

from agent_ai.core.cancel import CancellationToken  # noqa: E402
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
FIXTURE = DUMMY_ROOT / "continuous_loop_fixture"


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
    """Normalisasi pesan provider-format (dict) ATAU objek Message -> dict."""
    if isinstance(message, dict):
        return dict(message)
    return {
        "role": getattr(message, "role", ""),
        "content": getattr(message, "content", ""),
    }


class ScriptedProvider(OpenAICompatibleProvider):
    """Provider palsu: mengembalikan respons skrip berurutan (tanpa network).

    Mewarisi `normalize_response` OpenAI-compatible agar `raw` skrip diparse
    persis seperti provider asli (termasuk tool_calls -> LLMAction).
    """

    name = "scripted"

    def __init__(self, script: List[Dict[str, Any]]) -> None:
        self.config = SimpleNamespace(model="scripted-model")
        self.script = list(script)
        self.calls = 0
        self.requests: List[List[Dict[str, Any]]] = []
        self.tools_seen: List[Any] = []

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Any]] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[List[Any]] = None,
        tool_choice: Optional[Any] = None,
    ) -> GenerateResult:
        self.requests.append([_msg_to_dict(m) for m in (messages or [])])
        self.tools_seen.append(tools)
        idx = self.calls
        self.calls += 1
        raw = self.script[idx] if idx < len(self.script) else _final_turn("(default)")
        return GenerateResult(text="", model="scripted-model", provider=self.name, raw=raw)


# --------------------------------------------------------------------------- #
# Skenario
# --------------------------------------------------------------------------- #
def scenario_a_continuous_multi_tool(executor: ToolExecutor) -> None:
    """Satu turn multi tool -> hasil role 'tool' -> final. Berhenti tepat."""
    provider = ScriptedProvider(
        [
            _tool_turn(
                "Menulis dua file.",
                [
                    _tool_call("call-a", "write_file", {"path": "a.txt", "content": "hello"}),
                    _tool_call("call-b", "write_file", {"path": "b.txt", "content": "world"}),
                ],
            ),
            _final_turn("Selesai menulis a.txt dan b.txt."),
        ]
    )
    orch = AgentOrchestrator(
        provider=provider,
        executor=executor,
        options=GenerateOptions(model="scripted-model"),
        system_prompt="Kamu adalah coding agent.",
        use_continuous_loop=True,
    )
    result = orch.run("Tulis a.txt dan b.txt")

    print(f"A.status     -> {result.status.value}")
    print(f"A.iterations -> {result.iterations}")
    assert result.status == AgentStatus.DONE, result.error
    assert result.success
    assert provider.calls == 2, f"LLM dipanggil {provider.calls}x, harus 2 (turn tool + final)"
    assert (FIXTURE / "a.txt").read_text(encoding="utf-8") == "hello"
    assert (FIXTURE / "b.txt").read_text(encoding="utf-8") == "world"
    print("OK: SEMUA tool call dalam satu turn dieksekusi, loop berhenti di final")

    # Request KEDUA (setelah tool) memuat hasil tool sebagai role "tool".
    second = provider.requests[1]
    roles = [m.get("role") for m in second]
    print(f"A.roles turn2 -> {roles}")
    assert roles[0] == "system", roles
    assert "assistant" in roles and "tool" in roles, roles

    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert sorted(m.get("tool_call_id") for m in tool_msgs) == ["call-a", "call-b"], tool_msgs

    # Hasil tool TIDAK disisipkan sebagai pesan user.
    joined_user = " ".join(
        str(m.get("content")) for m in second if m.get("role") == "user"
    )
    assert "hello" not in joined_user and "world" not in joined_user, joined_user

    # assistant(tool_calls) tersimpan utuh tepat sebelum hasil tool.
    assistant_msgs = [m for m in second if m.get("role") == "assistant"]
    assert assistant_msgs and len(assistant_msgs[0].get("tool_calls") or []) == 2
    print("OK: hasil tool dikirim sebagai role 'tool' + tool_call_id (bukan 'user')")


def scenario_b_no_small_limit(executor_maker) -> None:
    """Banyak turn tool (>10) berjalan kontinu: tidak ada limit kecil 5/10."""
    turns = 12
    script = [
        _tool_turn(
            f"baca ke-{i}",
            [_tool_call(f"r{i}", "read_file", {"path": "a.txt"})],
        )
        for i in range(turns)
    ]
    script.append(_final_turn("Selesai."))
    provider = ScriptedProvider(script)
    orch = AgentOrchestrator(
        provider=provider,
        executor=executor_maker(),
        options=GenerateOptions(model="scripted-model"),
        use_continuous_loop=True,
    )
    result = orch.run("Baca a.txt berulang kali")

    print(f"B.calls      -> {provider.calls} (final di turn {turns + 1})")
    assert result.status == AgentStatus.DONE, result.error
    assert provider.calls == turns + 1, provider.calls
    assert result.iterations == turns, result.iterations
    print("OK: >10 turn tool berjalan kontinu (tidak ada limit kecil 5/10)")

    # Satu percakapan kontinu: hasil tool terakumulasi sebagai role "tool".
    last_req = provider.requests[-1]
    assert sum(1 for m in last_req if m.get("role") == "tool") == turns
    assert sum(1 for m in last_req if m.get("role") == "assistant") == turns
    print("OK: konteks tool terakumulasi dalam satu percakapan (role 'tool')")


def scenario_c_provider_error(executor_maker) -> None:
    """Provider error -> FAILED jelas (tidak menggantung, provider_error=True)."""

    class BoomProvider(ScriptedProvider):
        def generate(self, *args: Any, **kwargs: Any) -> GenerateResult:
            raise RuntimeError("koneksi gagal")

    orch = AgentOrchestrator(
        provider=BoomProvider([]),
        executor=executor_maker(),
        use_continuous_loop=True,
    )
    result = orch.run("apa saja")
    print(f"C.status     -> {result.status.value}")
    assert result.status == AgentStatus.FAILED
    assert result.provider_error is True
    assert "koneksi gagal" in (result.error or ""), result.error
    print("OK: provider error -> FAILED dengan pesan jelas (provider_error=True)")


def scenario_d_default_is_continuous(executor_maker) -> None:
    """Default = continuous loop (jalur NORMAL); legacy HANYA via eksplisit False."""
    provider = ScriptedProvider([_final_turn("halo")])
    orch = AgentOrchestrator(
        provider=provider,
        executor=executor_maker(),
        options=GenerateOptions(model="scripted-model"),
    )
    assert orch.use_continuous_loop is True, "production default harus continuous"
    result = orch.run("hai")
    assert result.status == AgentStatus.DONE and result.result == "halo", result
    print("OK: default use_continuous_loop=True -> run() delegasikan ke continuous loop")

    # Legacy tetap tersedia HANYA bila pemanggil memberi eksplisit False
    # (kompatibilitas/uji), sesuai P0-01.
    legacy = AgentOrchestrator(
        provider=ScriptedProvider([_final_turn("legacy halo")]),
        executor=executor_maker(),
        options=GenerateOptions(model="scripted-model"),
        use_continuous_loop=False,
    )
    assert legacy.use_continuous_loop is False
    lres = legacy.run("hai")
    
    assert lres.status == AgentStatus.DONE and lres.result == "legacy halo", lres
    print("OK: legacy loop tetap tersedia via eksplisit use_continuous_loop=False")


def scenario_e_safety_limit_abort(executor_maker) -> None:

    turns = 5
    script = [
        _tool_turn(
            f"baca ke-{i}", [_tool_call(f"s{i}", "read_file", {"path": "a.txt"})]
        )
        for i in range(turns)
    ]
    script.append(_final_turn("selesai"))
    provider = ScriptedProvider(script)
    orch = AgentOrchestrator(
        provider=provider,
        executor=executor_maker(),
        options=GenerateOptions(model="scripted-model"),
        use_continuous_loop=True,
    )
    result = orch.run_continuous_loop("baca a.txt berulang", max_steps=2)

    print(f"E.status     -> {result.status.value}")
    print(f"E.iterations -> {result.iterations}")
    print(f"E.calls      -> {provider.calls}")
    assert result.status == AgentStatus.FAILED, result.status
    assert not result.success, "safety limit harus FAILED, bukan success"
    assert "safety limit" in (result.error or "").lower(), result.error
    assert provider.calls == 2, f"LLM dipanggil {provider.calls}x, harus berhenti di 2"
    assert result.iterations == 2, result.iterations
    print("OK: mencapai max_steps -> FAILED (emergency safety), loop berhenti, bukan DONE")


def scenario_f_tool_failure_back_to_llm(executor_maker) -> None:

    provider = ScriptedProvider(
        [
            _tool_turn(
                "Baca file yang tak ada.",
                [_tool_call("f1", "read_file", {"path": "tidak_ada.txt"})],
            ),
            _final_turn("File tidak ditemukan; saya berhenti."),
        ]
    )
    orch = AgentOrchestrator(
        provider=provider,
        executor=executor_maker(),
        options=GenerateOptions(model="scripted-model"),
        use_continuous_loop=True,
    )
    result = orch.run("baca file tak ada")

    print(f"F.status     -> {result.status.value}")
    print(f"F.calls      -> {provider.calls}")
    assert result.status == AgentStatus.DONE, result
    assert provider.calls == 2, "LLM dipanggil (tool) + (final) = 2x"
    second = provider.requests[1]
    tool_msgs = [m.get("content") for m in second if m.get("role") == "tool"]
    assert tool_msgs, "harus ada hasil tool role 'tool' pada request berikutnya"
    assert any(str(c).strip() for c in tool_msgs), "konten tool gagal tidak boleh kosong"
    print("OK: tool failure -> error dikirim kembali ke LLM (role 'tool'), LLM lanjut ke final")


def scenario_g_user_cancel(executor_maker) -> None:

    provider = ScriptedProvider([_final_turn("halo")])
    token = CancellationToken()
    token.request("user menekan stop")
    orch = AgentOrchestrator(
        provider=provider,
        executor=executor_maker(),
        options=GenerateOptions(model="scripted-model"),
        use_continuous_loop=True,
        cancel_token=token,
    )
    result = orch.run("hai")
    print(f"G.status     -> {result.status.value}")
    assert result.status == AgentStatus.CANCELLED, result.status
    assert "stop" in (result.error or "").lower(), result.error
    assert provider.calls == 0, "LLM tidak boleh dipanggil setelah cancel"
    print("OK: user cancel -> CANCELLED, LLM tidak dipanggil")


def main() -> int:
    print("=== Verifikasi Continuous Agent Loop (Native Tool Calling) ===")

    if FIXTURE.exists():
        shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir(parents=True, exist_ok=True)

    def executor_maker() -> ToolExecutor:
        return ToolExecutor(build_registry(root=FIXTURE))

    try:
        scenario_a_continuous_multi_tool(executor_maker())
        scenario_b_no_small_limit(executor_maker)
        scenario_c_provider_error(executor_maker)
        scenario_d_default_is_continuous(executor_maker)
        scenario_e_safety_limit_abort(executor_maker)
        scenario_f_tool_failure_back_to_llm(executor_maker)
        scenario_g_user_cancel(executor_maker)
    finally:
        shutil.rmtree(FIXTURE, ignore_errors=True)
        # Bersihkan root dummy_test hanya bila sudah kosong.
        try:
            if DUMMY_ROOT.exists() and not any(DUMMY_ROOT.iterdir()):
                DUMMY_ROOT.rmdir()
        except OSError:
            pass

    print()
    print("[OK] Continuous Agent Loop (Native Tool Calling) bekerja sesuai kontrak.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
