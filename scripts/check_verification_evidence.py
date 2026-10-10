"""Verifier: Evidence-Based Self-Verification pada continuous loop.

Membuktikan integrasi minimal pada jalur produksi
(`AgentRuntime -> AgentOrchestrator -> run_continuous_loop`):

    A. `_tool_payload_to_observation` meneruskan metadata hasil tool yang MEMANG
       tersedia (exit_code/outcome/success) untuk sukses DAN gagal, serta
       menandai kegagalan command — tanpa mengarang nilai.
    B. Verification nudge dikirim MAKSIMAL SATU KALI per task dan memberi LLM
       kesempatan tambahan (provider dipanggil lagi setelah nudge).
    C. TIDAK ada nudge untuk: task read-only, task tanpa permintaan verifikasi,
       atau task yang sudah punya bukti validasi sukses SETELAH perubahan
       terakhir.
    D. Cancellation dan provider error (fallback) tetap mengikuti perilaku
       sebelumnya: TIDAK ada nudge pada kedua kasus.
    E. Hasil runtime mempertahankan teks final LLM verbatim dan menyediakan
       evidence terstruktur yang kompatibel (`RuntimeResult.evidence`,
       `OrchestratorResult.evidence`); status/teks final tidak berubah.

Provider palsu (scripted) -> tanpa network/API. Fixture workspace berada di
`J:\\Agent_Ai\\dummy_test` (workspace uji terisolasi, BUKAN bagian AETHER) dan
dibersihkan setelah verifikasi selesai.

Jalankan:
    python scripts/check_verification_evidence.py
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
from agent_ai.core.types import ToolResultPayload  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
)
from agent_ai.runtime.runtime import AgentRuntime, RuntimeStatus  # noqa: E402
from agent_ai.session.store import InMemorySessionStore  # noqa: E402
from agent_ai.task.models import PreparedTask  # noqa: E402
from agent_ai.tools.registry import build_registry  # noqa: E402

DUMMY_ROOT = Path(r"J:\Agent_Ai\dummy_test")
FIXTURE = DUMMY_ROOT / "verification_evidence_fixture"


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
        self.requests.append([_msg_to_dict(m) for m in (messages or [])])
        idx = self.calls
        self.calls += 1
        raw = self.script[idx] if idx < len(self.script) else _final_turn("(default)")
        return GenerateResult(text="", model="scripted-model", provider=self.name, raw=raw)


def _executor() -> ToolExecutor:
    return ToolExecutor(build_registry(root=FIXTURE))


def _make_orch(provider: ScriptedProvider, executor: ToolExecutor) -> AgentOrchestrator:
    return AgentOrchestrator(
        provider=provider,
        executor=executor,
        options=GenerateOptions(model="scripted-model"),
        system_prompt="Kamu adalah coding agent.",
        use_continuous_loop=True,
    )


def _nudge_messages(provider: ScriptedProvider) -> int:
    """Jumlah pesan user yang membawa penanda verification nudge."""
    count = 0
    for request in provider.requests:
        for message in request:
            if message.get("role") == "user" and str(
                message.get("content") or ""
            ).startswith("[verification]"):
                count += 1
    return count


# --------------------------------------------------------------------------- #
# A) Metadata hasil tool diteruskan dengan benar
# --------------------------------------------------------------------------- #
def scenario_a_metadata_forwarded() -> None:
    """Sukses & gagal command meneruskan exit_code/outcome tanpa mengarang."""
    success = ToolResultPayload.success(
        "c1",
        "run_command",
        {
            "command": "python --version",
            "stdout": "Python 3.11",
            "stderr": "",
            "exit_code": 0,
            "success": True,
            "timed_out": False,
            "outcome": "success",
        },
    )
    obs_ok = AgentOrchestrator._tool_payload_to_observation(success)
    assert obs_ok.success is True, obs_ok
    assert obs_ok.metadata.get("exit_code") == 0, obs_ok.metadata
    assert obs_ok.metadata.get("outcome") == "success", obs_ok.metadata
    assert obs_ok.metadata.get("tool") == "run_command", obs_ok.metadata
    assert not obs_ok.metadata.get("command_failure"), obs_ok.metadata
    assert not obs_ok.metadata.get("tool_error"), obs_ok.metadata

    failure = ToolResultPayload.success(
        "c2",
        "run_command",
        {
            "command": "exit 1",
            "stdout": "",
            "stderr": "boom",
            "exit_code": 1,
            "success": False,
            "timed_out": False,
            "outcome": "command_failure",
        },
    )
    obs_bad = AgentOrchestrator._tool_payload_to_observation(failure)
    # command jalan (payload success=True) tetapi exit_code != 0 -> harus
    # ditandai command_failure agar terdeteksi sebagai kegagalan.
    assert obs_bad.metadata.get("exit_code") == 1, obs_bad.metadata
    assert obs_bad.metadata.get("outcome") == "command_failure", obs_bad.metadata
    assert obs_bad.metadata.get("command_failure") is True, obs_bad.metadata

    timeout = ToolResultPayload.success(
        "c3",
        "run_command",
        {"exit_code": None, "success": False, "timed_out": True, "outcome": "timeout"},
    )
    obs_to = AgentOrchestrator._tool_payload_to_observation(timeout)
    assert obs_to.metadata.get("outcome") == "timeout", obs_to.metadata
    assert obs_to.metadata.get("command_failure") is True, obs_to.metadata

    # Payload tanpa metadata: TIDAK boleh mengarang exit_code/outcome.
    plain = ToolResultPayload.success("c4", "read_file", {"content": "hi"})
    obs_plain = AgentOrchestrator._tool_payload_to_observation(plain)
    assert "exit_code" not in obs_plain.metadata, obs_plain.metadata
    assert "outcome" not in obs_plain.metadata, obs_plain.metadata
    assert "command_failure" not in obs_plain.metadata, obs_plain.metadata

    # Tool error (payload error) -> tool_error=True, tidak ada exit_code palsu.
    err = ToolResultPayload.error("c5", "no_such_tool", {"error": "tidak ada"})
    obs_err = AgentOrchestrator._tool_payload_to_observation(err)
    assert obs_err.success is False, obs_err
    assert obs_err.metadata.get("tool_error") is True, obs_err.metadata
    assert "exit_code" not in obs_err.metadata, obs_err.metadata

    print("A. OK: metadata sukses/gagal diteruskan apa adanya (tanpa mengarang)")


# --------------------------------------------------------------------------- #
# B) Nudge maksimal 1x + memberi LLM kesempatan tambahan
# --------------------------------------------------------------------------- #
def scenario_b_nudge_once_adds_turn() -> None:
    """Task minta verifikasi: mutasi tanpa validasi -> nudge 1x lalu final."""
    (FIXTURE / "keep.txt").write_text("x", encoding="utf-8")
    provider = ScriptedProvider(
        [
            # Turn 1: implementasi (mutasi) tanpa validasi.
            _tool_turn(
                "Menulis hasil.",
                [_tool_call("c1", "write_file", {"path": "out.txt", "content": "hi"})],
            ),
            # Turn 2: LLM langsung final tanpa validasi -> harus kena nudge.
            _final_turn("FINAL-PERTAMA"),
            # Turn 3 (setelah nudge): jawaban final LLM.
            _final_turn("FINAL-SETELAH-NUDGE"),
        ]
    )
    orch = _make_orch(provider, _executor())
    result = orch.run_continuous_loop("Implementasikan fitur lalu pastikan test lulus.")
    print(
        f"B. status={result.status.value} calls={provider.calls} "
        f"nudges={_nudge_messages(provider)}"
    )
    assert result.status == AgentStatus.DONE, result.error
    assert provider.calls == 3, f"nudge harus memberi 1 turn tambahan (calls={provider.calls})"
    assert _nudge_messages(provider) == 1, "nudge harus tepat 1x"
    # Teks final = jawaban LLM setelah kesempatan tambahan (verbatim).
    assert result.result == "FINAL-SETELAH-NUDGE", result.result
    assert result.evidence is not None, "evidence harus tersedia"
    assert result.evidence["nudge_sent"] is True, result.evidence
    assert result.evidence["task_requires_validation"] is True, result.evidence
    assert result.evidence["change_applied"] is True, result.evidence
    print("B. OK: nudge dikirim 1x dan memberi LLM kesempatan tambahan")


def scenario_b2_nudge_only_once_even_if_insists() -> None:
    """LLM tetap final tanpa validasi: nudge TETAP hanya 1x (bukan berulang)."""
    (FIXTURE / "keep2.txt").write_text("x", encoding="utf-8")
    provider = ScriptedProvider(
        [
            _tool_turn(
                "Menulis.",
                [_tool_call("c1", "write_file", {"path": "out2.txt", "content": "hi"})],
            ),
            _final_turn("FINAL-1"),
            _final_turn("FINAL-2"),
            _final_turn("FINAL-3"),
        ]
    )
    orch = _make_orch(provider, _executor())
    result = orch.run_continuous_loop("Buat file dan verifikasi.")
    print(f"B2. calls={provider.calls} nudges={_nudge_messages(provider)}")
    assert result.status == AgentStatus.DONE, result.error
    assert _nudge_messages(provider) == 1, "nudge maksimal 1x per task"
    assert provider.calls == 3, f"setelah 1 nudge loop selesai (calls={provider.calls})"
    assert result.evidence["nudge_sent"] is True, result.evidence
    print("B2. OK: nudge tidak diulang walau LLM tetap final tanpa validasi")


# --------------------------------------------------------------------------- #
# C) Tidak ada nudge pada kasus yang tidak memenuhi syarat
# --------------------------------------------------------------------------- #
def scenario_c_no_nudge_read_only() -> None:
    """Task read-only (minta verifikasi) tetapi TANPA perubahan -> tanpa nudge."""
    (FIXTURE / "read_me.txt").write_text("hello", encoding="utf-8")
    provider = ScriptedProvider(
        [
            _tool_turn(
                "Membaca berkas.",
                [_tool_call("c1", "read_file", {"path": "read_me.txt"})],
            ),
            _final_turn("FINAL-READONLY"),
        ]
    )
    orch = _make_orch(provider, _executor())
    result = orch.run_continuous_loop("Periksa dan verifikasi isi read_me.txt.")
    print(f"C1. calls={provider.calls} nudges={_nudge_messages(provider)}")
    assert result.status == AgentStatus.DONE, result.error
    assert _nudge_messages(provider) == 0, "read-only TIDAK boleh kena nudge"
    assert provider.calls == 2, provider.calls
    assert result.evidence["change_applied"] is False, result.evidence
    assert result.evidence["nudge_sent"] is False, result.evidence
    print("C1. OK: task read-only tidak memicu nudge")


def scenario_c2_no_nudge_without_validation_request() -> None:
    """Mutasi ada, tetapi task TIDAK minta verifikasi -> tanpa nudge."""
    provider = ScriptedProvider(
        [
            _tool_turn(
                "Menulis berkas.",
                [_tool_call("c1", "write_file", {"path": "plain.txt", "content": "z"})],
            ),
            _final_turn("FINAL-TANPA-VERIFIKASI"),
        ]
    )
    orch = _make_orch(provider, _executor())
    result = orch.run_continuous_loop("Buat plain.txt berisi 'z'.")
    print(f"C2. calls={provider.calls} nudges={_nudge_messages(provider)}")
    assert result.status == AgentStatus.DONE, result.error
    assert _nudge_messages(provider) == 0, "task tanpa permintaan verifikasi: tanpa nudge"
    assert provider.calls == 2, provider.calls
    assert result.evidence["task_requires_validation"] is False, result.evidence
    print("C2. OK: task tanpa permintaan verifikasi tidak memicu nudge")


def scenario_c3_no_nudge_when_validation_succeeded() -> None:
    """Validasi sukses SETELAH perubahan terakhir -> tanpa nudge."""
    provider = ScriptedProvider(
        [
            _tool_turn(
                "Menulis.",
                [_tool_call("c1", "write_file", {"path": "verified.txt", "content": "v"})],
            ),
            _tool_turn(
                "Menjalankan verifikasi.",
                [_tool_call("c2", "run_command", {"command": "python --version"})],
            ),
            _final_turn("FINAL-SUDAH-VERIFIKASI"),
        ]
    )
    orch = _make_orch(provider, _executor())
    result = orch.run_continuous_loop("Buat verified.txt lalu jalankan test/verifikasi.")
    print(f"C3. calls={provider.calls} nudges={_nudge_messages(provider)}")
    assert result.status == AgentStatus.DONE, result.error
    assert _nudge_messages(provider) == 0, "validasi sukses setelah perubahan: tanpa nudge"
    assert provider.calls == 3, provider.calls
    assert result.evidence["validation_after_change"] is True, result.evidence
    assert result.evidence["validation_succeeded"] is True, result.evidence
    print("C3. OK: bukti validasi sudah ada setelah perubahan -> tanpa nudge")


# --------------------------------------------------------------------------- #
# D) Cancellation & provider error tetap perilaku sebelumnya
# --------------------------------------------------------------------------- #
class BoomProvider(ScriptedProvider):
    """Provider yang selalu gagal (simulasi provider error)."""

    def generate(self, *args: Any, **kwargs: Any) -> GenerateResult:
        raise RuntimeError("provider tidak tersedia")


def scenario_d1_provider_error_no_nudge() -> None:
    """Provider error -> FAILED, tanpa nudge."""
    provider = BoomProvider([])
    orch = _make_orch(provider, _executor())
    result = orch.run_continuous_loop("Buat file dan jalankan test.")
    print(f"D1. status={result.status.value} nudges={_nudge_messages(provider)}")
    assert result.status == AgentStatus.FAILED, result.status
    assert result.provider_error is True, result.provider_error
    assert _nudge_messages(provider) == 0, "provider error TIDAK boleh memicu nudge"
    print("D1. OK: provider error -> FAILED, tanpa nudge")


def scenario_d2_cancellation_no_nudge() -> None:
    """Cancellation sebelum LLM call -> CANCELLED, tanpa nudge."""
    from agent_ai.core.cancel import CancellationToken

    token = CancellationToken()
    token.request("user stop")
    provider = ScriptedProvider([_final_turn("tidak boleh tercapai")])
    orch = AgentOrchestrator(
        provider=provider,
        executor=_executor(),
        options=GenerateOptions(model="scripted-model"),
        use_continuous_loop=True,
        cancel_token=token,
    )
    result = orch.run_continuous_loop("Buat file dan jalankan test.")
    print(f"D2. status={result.status.value} calls={provider.calls}")
    assert result.status == AgentStatus.CANCELLED, result.status
    assert provider.calls == 0, "cancellation mencegah LLM call"
    assert _nudge_messages(provider) == 0, "cancellation TIDAK boleh memicu nudge"
    print("D2. OK: cancellation tetap CANCELLED tanpa memanggil provider")


def scenario_d3_provider_fallback_unchanged() -> None:
    """Perpindahan provider (fallback) tetap berjalan; tidak ada nudge."""
    primary = BoomProvider([])
    primary.name = "primary"
    fallback = ScriptedProvider([_final_turn("FINAL-FALLBACK")])
    fallback.name = "fallback"

    def resolver(_error: BaseException) -> Any:
        return fallback

    orch = AgentOrchestrator(
        provider=primary,
        executor=_executor(),
        options=GenerateOptions(model="scripted-model"),
        use_continuous_loop=True,
        provider_fallback_resolver=resolver,
    )
    result = orch.run_continuous_loop("Buat file lalu jalankan test.")
    print(
        f"D3. status={result.status.value} fallback_calls={fallback.calls} "
        f"nudges={_nudge_messages(fallback)}"
    )
    assert result.status == AgentStatus.DONE, result.error
    assert result.result == "FINAL-FALLBACK", result.result
    assert fallback.calls >= 1, "fallback provider harus dipakai"
    assert _nudge_messages(fallback) == 0, "perpindahan provider bukan pemicu nudge"
    print("D3. OK: provider fallback tetap berjalan tanpa nudge")


# --------------------------------------------------------------------------- #
# E) Hasil runtime mempertahankan teks final + evidence kompatibel
# --------------------------------------------------------------------------- #
def scenario_e_runtime_result_compat() -> None:
    """AgentRuntime: result verbatim + evidence additive; status COMPLETED."""
    store = InMemorySessionStore()
    session = store.create_session(metadata={"task_id": "ve"})
    provider = ScriptedProvider(
        [
            _tool_turn(
                "Menulis.",
                [_tool_call("c1", "write_file", {"path": "runtime_out.txt", "content": "hi"})],
            ),
            _tool_turn(
                "Menjalankan verifikasi.",
                [_tool_call("c2", "run_command", {"command": "python --version"})],
            ),
            _final_turn("LAPORAN FINAL VERBATIM."),
        ]
    )
    runtime = AgentRuntime(
        provider=provider,
        executor=_executor(),
        options=GenerateOptions(model="scripted-model"),
        session_store=store,
        session_id=session.session_id,
    )
    result = runtime.run(PreparedTask(task="Buat runtime_out.txt lalu verifikasi test.", task_id="ve"))
    print(f"E. status={result.status.value} evidence={result.evidence}")
    assert result.status == RuntimeStatus.COMPLETED, result.error
    # Teks final LLM utuh (verbatim).
    assert result.result == "LAPORAN FINAL VERBATIM.", result.result
    # Evidence additive + terstruktur.
    assert isinstance(result.evidence, dict), result.evidence
    for key in (
        "task_requires_validation",
        "change_applied",
        "validation_after_change",
        "last_observation_failed",
        "nudge_sent",
    ):
        assert key in result.evidence, f"{key} hilang dari evidence"
    assert result.evidence["task_requires_validation"] is True, result.evidence
    assert result.evidence["change_applied"] is True, result.evidence
    assert result.evidence["validation_after_change"] is True, result.evidence
    assert result.evidence["nudge_sent"] is False, result.evidence

    # to_dict tetap kompatibel dan memuat evidence.
    payload = result.to_dict()
    assert payload["status"] == "completed", payload
    assert payload["result"] == "LAPORAN FINAL VERBATIM.", payload
    assert "evidence" in payload, payload

    # Lifecycle event terminal tetap seperti sebelumnya (tidak berubah).
    events = [e.event_type.value for e in store.get_events(session_id=session.session_id)]
    assert events.count("task_completed") == 1, events
    assert events.count("task_started") == 1, events
    print("E. OK: teks final verbatim, evidence kompatibel, lifecycle event utuh")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    print("=== Verifikasi Evidence-Based Self-Verification (continuous loop) ===")

    if FIXTURE.exists():
        shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir(parents=True, exist_ok=True)

    failures: List[str] = []
    scenarios = [
        ("A", scenario_a_metadata_forwarded),
        ("B", scenario_b_nudge_once_adds_turn),
        ("B2", scenario_b2_nudge_only_once_even_if_insists),
        ("C1", scenario_c_no_nudge_read_only),
        ("C2", scenario_c2_no_nudge_without_validation_request),
        ("C3", scenario_c3_no_nudge_when_validation_succeeded),
        ("D1", scenario_d1_provider_error_no_nudge),
        ("D2", scenario_d2_cancellation_no_nudge),
        ("D3", scenario_d3_provider_fallback_unchanged),
        ("E", scenario_e_runtime_result_compat),
    ]
    try:
        for name, fn in scenarios:
            try:
                fn()
            except AssertionError as exc:
                failures.append(f"{name}: {exc}")
                print(f"    [FAIL] {name}: {exc}")
    finally:
        shutil.rmtree(FIXTURE, ignore_errors=True)
        try:
            if DUMMY_ROOT.exists() and not any(DUMMY_ROOT.iterdir()):
                DUMMY_ROOT.rmdir()
        except OSError:
            pass

    print()
    if failures:
        print(f"[FAIL] {len(failures)} skenario gagal:")
        for item in failures:
            print(f"  - {item}")
        return 1

    print(
        "[OK] Evidence-Based Self-Verification: metadata tool diteruskan, nudge "
        "maks 1x (read-only/tanpa-verifikasi/sudah-validasi tidak di-nudge), "
        "cancellation & provider error tidak berubah, hasil runtime kompatibel."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
