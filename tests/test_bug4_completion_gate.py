"""Regression tests: BUG-4 — cegah DONE tanpa validasi yang memadai.

Audit BUG-4 membuktikan bahwa jalur completion continuous loop
(`AgentOrchestrator.run_continuous_loop`) menetapkan DONE MURNI dari response
LLM tanpa tool call: task coding yang mengubah source/konfigurasi dapat selesai
tanpa bukti validasi, bahkan ketika validasi terakhir GAGAL.

Perbaikan (scope BUG-4 saja):

1. COMPLETION GATE: sebelum DONE, bila task membutuhkan validasi —
   (a) task (aslinya) eksplisit meminta test/validasi, ATAU
   (b) perubahan nyata menyentuh artifact source/konfigurasi —
   maka DONE HANYA diizinkan bila ada bukti eksekusi validasi SUKSES setelah
   perubahan terakhir. Read-only/konsultasi tanpa perubahan tetap boleh DONE.
2. BUKTI AKTUAL: bukti = observation dari `run_command` yang sukses (ada
   `exit_code`) dan bukan kegagalan; validasi lama tidak berlaku untuk
   perubahan yang lebih baru. Klaim teks LLM bukan bukti.
3. BOUNDED RECOVERY: bila bukan bukti, LLM diberi nudge terbatas
   (`_MAX_VERIFICATION_RECOVERIES`). Nudge tidak mengeksekusi tool. Setelah
   kesempatan habis, task dihentikan NON-SUKSES (FAILED) dengan alasan jelas.
4. TERMINAL: BUG-2 (`max_steps`) dan BUG-3 (truncation recovery) TIDAK berubah
   dan tetap non-sukses.

Test memakai mock provider + tool lokal (tanpa network/provider berbayar).
Status diuji dari jalur runtime AKTUAL (`run_continuous_loop`), bukan helper.

Jalankan:
    python -m pytest tests/test_bug4_completion_gate.py
"""

from __future__ import annotations

import json
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
from agent_ai.core.orchestrator import (  # noqa: E402
    AgentOrchestrator,
    _MAX_VERIFICATION_RECOVERIES,
)
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
)
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402

SYSTEM_PROMPT = "Kamu adalah coding agent."


# --------------------------------------------------------------------------- #
# Mock provider + tool lokal (deterministik, tanpa network)
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
    """Provider palsu: mengembalikan turn skrip (item terakhir diulang)."""

    name = "bug4-scripted"

    def __init__(self, script: List[Any]) -> None:
        self.config = SimpleNamespace(model="bug4-model")
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
        if not self.script:
            item = _final_turn("(default)")
        elif idx < len(self.script):
            item = self.script[idx]
        else:
            item = self.script[-1]
        return GenerateResult(text="", model="bug4-model", provider=self.name, raw=item)


class FakeMutationTool(BaseTool):
    """Tool mutasi palsu: mengembalikan payload sukses ber-marker mutasi.

    Payload = bentuk yang dikenali `_is_change_observation` (memuat marker
    "written"/"edited"), dengan `path` yang menentukan apakah perubahan
    dianggap source/konfigurasi. Tidak menyentuh filesystem.
    """

    def __init__(self, name: str = "edit_file", marker: str = "edited") -> None:
        self.name = name
        self.description = "fake mutation"
        self.input_schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
        self.marker = marker
        self.calls = 0

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        self.calls += 1
        path = str(arguments.get("path", ""))
        return {"path": path, self.marker: True, "success": True}


class FakeCommandTool(BaseTool):
    """Tool command palsu: exit_code dapat diatur untuk sukses/gagal.

    Bentuk payload menyerupai `run_command` (memuat `exit_code`), sehingga
    dapat menjadi BUKTI validasi (`_is_validation_observation`) atau kegagalan
    (`_is_failure_observation`).
    """

    def __init__(self, name: str = "run_command") -> None:
        self.name = name
        self.description = "fake command"
        self.input_schema = {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": [],
        }
        self.calls = 0
        self.exit_code = 0

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        self.calls += 1
        exit_code = int(self.exit_code)
        return {
            "command": str(arguments.get("command", "")),
            "exit_code": exit_code,
            "stdout": "ok" if exit_code == 0 else "fail",
            "success": exit_code == 0,
            "outcome": "success" if exit_code == 0 else "command_failure",
        }


class FakeReadTool(BaseTool):
    """Tool read-only palsu (tidak pernah menjadi bukti mutasi/validasi)."""

    def __init__(self, name: str = "read_file") -> None:
        self.name = name
        self.description = "fake read"
        self.input_schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": [],
        }
        self.calls = 0

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        self.calls += 1
        return {"path": str(arguments.get("path", "")), "content": "data"}


class FakeFailingMutationTool(BaseTool):
    """Tool mutasi palsu yang SELALU GAGAL (simulasi artifact gagal dibuat).

    Kegagalan direpresentasikan sebagai exception -> executor memetakannya ke
    observation gagal (tool_error) dengan nama tool yang benar.
    """

    def __init__(self, name: str = "write_file") -> None:
        self.name = name
        self.description = "fake failing mutation"
        self.input_schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path"],
        }
        self.calls = 0

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        self.calls += 1
        raise RuntimeError("disk penuh: gagal menulis artifact")


class FakeFailingReadTool(BaseTool):
    """Tool read-only palsu yang selalu gagal (kegagalan SAMPINGAN)."""

    def __init__(self, name: str = "read_file") -> None:
        self.name = name
        self.description = "fake failing read"
        self.input_schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": [],
        }
        self.calls = 0

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        self.calls += 1
        raise RuntimeError("gagal membaca berkas")


def _make_orchestrator(
    provider: ScriptedProvider,
    *,
    mutation: Optional[FakeMutationTool] = None,
    command: Optional[FakeCommandTool] = None,
    read: Optional[FakeReadTool] = None,
    events: Optional[List[Dict[str, Any]]] = None,
    extra_tools: Optional[List[Any]] = None,
) -> AgentOrchestrator:
    registry = ToolRegistry()
    for tool in (mutation, command, read):
        if tool is not None:
            registry.register(tool)
    for tool in extra_tools or []:
        registry.register(tool)
    sink = None
    if events is not None:
        sink = lambda et, payload: events.append({"type": et, **payload})  # noqa: E731
    return AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="bug4-model"),
        system_prompt=SYSTEM_PROMPT,
        use_continuous_loop=True,
        event_sink=sink,
    )


def _edit(path: str = "src/app.py", call_id: str = "m1") -> Dict[str, Any]:
    return _tool_call(call_id, "edit_file", {"path": path})


def _run_ok(call_id: str = "v1", command: str = "python -m pytest -q") -> Dict[str, Any]:
    return _tool_call(call_id, "run_command", {"command": command})


# --------------------------------------------------------------------------- #
# 1. Ubah file lalu langsung final tanpa validasi -> TIDAK boleh DONE
# --------------------------------------------------------------------------- #
def test_change_then_final_without_validation_not_completed() -> None:
    mutation = FakeMutationTool()
    # Skrip: edit source, lalu LLM terus memberi final (item terakhir diulang).
    provider = ScriptedProvider(
        [_tool_turn("edit", [_edit()]), _final_turn("selesai")]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation)

    result = orchestrator.run_continuous_loop("perbaiki bug di source code")

    assert result.status == AgentStatus.FAILED, (result.status, result.error)
    assert result.status != AgentStatus.DONE
    assert result.error is not None
    assert "Completion ditolak oleh verification gate" in result.error
    # Bukti tambahan: fakta gate ada di evidence hasil (additive).
    assert result.evidence.get("verification_gate_blocked") is True
    assert result.evidence.get("validation_state") == "not_run"
    # Loop tidak dibiarkan tanpa batas: jumlah recovery terbatas.
    assert result.evidence.get("verification_recoveries") == _MAX_VERIFICATION_RECOVERIES
    assert provider.calls <= _MAX_VERIFICATION_RECOVERIES + 2


# --------------------------------------------------------------------------- #
# 2. Perubahan + nudge diterima, tetap final tanpa validasi -> gate tetap berlaku
# --------------------------------------------------------------------------- #
def test_gate_holds_after_verification_nudge() -> None:
    mutation = FakeMutationTool()
    events: List[Dict[str, Any]] = []
    provider = ScriptedProvider(
        [_tool_turn("edit", [_edit()]), _final_turn("selesai")]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation, events=events)

    result = orchestrator.run_continuous_loop("ubah konfigurasi")

    # Setidaknya satu nudge dikirim (recovery aktif), tetapi completion tetap
    # ditahan karena tidak ada bukti sukses.
    nudges = [e for e in events if e["type"] == "verification_nudge"]
    assert len(nudges) >= 1, events
    assert nudges[0]["validation_state"] == "not_run"
    assert result.status == AgentStatus.FAILED
    assert result.evidence.get("verification_gate_blocked") is True


# --------------------------------------------------------------------------- #
# 3. Validasi sukses setelah perubahan terakhir -> DONE normal
# --------------------------------------------------------------------------- #
def test_successful_validation_after_change_allows_done() -> None:
    mutation = FakeMutationTool()
    command = FakeCommandTool()
    provider = ScriptedProvider(
        [
            _tool_turn("edit", [_edit()]),
            _tool_turn("validasi", [_run_ok()]),
            _final_turn("selesai"),
        ]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation, command=command)

    result = orchestrator.run_continuous_loop("ubah source dan jalankan test")

    assert result.status == AgentStatus.DONE, result.error
    assert result.result == "selesai"
    assert command.calls == 1
    assert result.evidence.get("validation_succeeded") is True
    assert result.evidence.get("validation_state") == "succeeded"
    assert "verification_gate_blocked" not in result.evidence


# --------------------------------------------------------------------------- #
# 4. Validasi GAGAL, lalu LLM klaim selesai -> TIDAK boleh DONE
# --------------------------------------------------------------------------- #
def test_failed_validation_then_final_not_completed() -> None:
    mutation = FakeMutationTool()
    command = FakeCommandTool()
    command.exit_code = 1  # validasi GAGAL
    provider = ScriptedProvider(
        [
            _tool_turn("edit", [_edit()]),
            _tool_turn("test", [_run_ok()]),
            _final_turn("sudah beres"),
        ]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation, command=command)

    result = orchestrator.run_continuous_loop("perbaiki lalu pastikan test lulus")

    assert result.status == AgentStatus.FAILED, (result.status, result.error)
    assert result.status != AgentStatus.DONE
    assert "verification gate" in (result.error or "")
    # Bukti sukses TIDAK dikarang dari klaim teks LLM.
    assert result.evidence.get("validation_succeeded") is False
    assert result.evidence.get("validation_state") == "failed"


def test_failed_validation_nudge_mentions_failure() -> None:
    """Kegagalan validasi disampaikan ke LLM (bukan sekadar 'belum ada bukti')."""
    mutation = FakeMutationTool()
    command = FakeCommandTool()
    command.exit_code = 1
    events: List[Dict[str, Any]] = []
    provider = ScriptedProvider(
        [_tool_turn("edit", [_edit()]), _tool_turn("t", [_run_ok()]), _final_turn("ok")]
    )
    orchestrator = _make_orchestrator(
        provider, mutation=mutation, command=command, events=events
    )

    orchestrator.run_continuous_loop("kerjakan dan validasi")

    nudges = [e for e in events if e["type"] == "verification_nudge"]
    assert any(n["validation_state"] == "failed" for n in nudges), nudges
    assert any(n["reason"] == "validation_failed" for n in nudges), nudges


# --------------------------------------------------------------------------- #
# 5. Task coding TANPA kata 'test'/'validasi' -> tetap butuh validasi (metadata)
# --------------------------------------------------------------------------- #
def test_source_change_without_keyword_still_requires_validation() -> None:
    mutation = FakeMutationTool()
    provider = ScriptedProvider(
        [_tool_turn("edit", [_edit("app/service.py")]), _final_turn("selesai")]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation)

    # Task TIDAK menyebut test/validasi/uji.
    result = orchestrator.run_continuous_loop("rapikan service pembayaran")

    assert result.evidence.get("task_requires_validation") is False
    assert result.evidence.get("change_targets_source") is True
    assert result.evidence.get("validation_required") is True
    assert result.status == AgentStatus.FAILED
    assert result.evidence.get("verification_gate_blocked") is True


# --------------------------------------------------------------------------- #
# 6. Task read-only/analisis -> tidak terhalang gate
# --------------------------------------------------------------------------- #
def test_read_only_task_without_change_allowed() -> None:
    read = FakeReadTool()
    provider = ScriptedProvider(
        [_tool_turn("baca", [_tool_call("r1", "read_file", {"path": "src/a.py"})]),
         _final_turn("analisis selesai")]
    )
    orchestrator = _make_orchestrator(provider, read=read)

    result = orchestrator.run_continuous_loop("analisis arsitektur modul ini")

    assert result.status == AgentStatus.DONE, result.error
    assert result.result == "analisis selesai"
    assert result.evidence.get("change_applied") is False


def test_final_only_task_analysis_allowed() -> None:
    provider = ScriptedProvider([_final_turn("jawaban konsultasi")])
    orchestrator = _make_orchestrator(provider)

    result = orchestrator.run_continuous_loop("jelaskan apa itu dependency injection")

    assert result.status == AgentStatus.DONE
    assert result.result == "jawaban konsultasi"


def test_no_validation_clause_disables_requirement() -> None:
    """User eksplisit 'tanpa test' -> requirement validasi ditiadakan."""
    mutation = FakeMutationTool()
    provider = ScriptedProvider(
        [_tool_turn("edit", [_edit()]), _final_turn("selesai")]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation)

    result = orchestrator.run_continuous_loop("ubah file ini tanpa test")

    assert result.status == AgentStatus.DONE, result.error


# --------------------------------------------------------------------------- #
# 7. Validasi sukses LALU perubahan berikutnya -> bukti lama tidak mencukupi
# --------------------------------------------------------------------------- #
def test_validation_then_newer_change_requires_revalidation() -> None:
    mutation = FakeMutationTool()
    command = FakeCommandTool()
    provider = ScriptedProvider(
        [
            _tool_turn("edit1", [_edit("src/a.py", "m1")]),
            _tool_turn("validasi", [_run_ok("v1")]),
            _tool_turn("edit2", [_edit("src/a.py", "m2")]),  # perubahan lebih baru
            _final_turn("selesai"),
        ]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation, command=command)

    result = orchestrator.run_continuous_loop("ubah source dua kali")

    # Bukti validasi lama tidak berlaku untuk perubahan terbaru.
    assert result.status == AgentStatus.FAILED, (result.status, result.error)
    assert result.evidence.get("validation_succeeded") is False
    assert result.evidence.get("validation_state") == "not_run"
    assert result.evidence.get("verification_gate_blocked") is True


def test_validation_after_last_change_allows_done() -> None:
    """Kontrol: validasi SETELAH perubahan terakhir -> DONE diizinkan."""
    mutation = FakeMutationTool()
    command = FakeCommandTool()
    provider = ScriptedProvider(
        [
            _tool_turn("edit", [_edit("src/a.py", "m1")]),
            _tool_turn("validasi", [_run_ok("v1")]),
            _tool_turn("edit", [_edit("src/b.py", "m2")]),
            _tool_turn("validasi", [_run_ok("v2")]),
            _final_turn("selesai"),
        ]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation, command=command)

    result = orchestrator.run_continuous_loop("ubah dua file lalu validasi")

    assert result.status == AgentStatus.DONE, result.error
    assert command.calls == 2


# --------------------------------------------------------------------------- #
# 8. Validasi tidak tersedia -> tidak mengarang bukti sukses, hasil jujur
# --------------------------------------------------------------------------- #
def test_validation_unavailable_makes_no_success_claim() -> None:
    """Tidak ada tool validasi; LLM tetap final -> DONE tidak diklaim sukses."""
    mutation = FakeMutationTool()
    provider = ScriptedProvider(
        [_tool_turn("edit", [_edit()]), _final_turn("semua beres")]
    )
    # Sengaja TIDAK mendaftarkan command tool: validasi tidak tersedia.
    orchestrator = _make_orchestrator(provider, mutation=mutation)

    result = orchestrator.run_continuous_loop("ubah kode produksi")

    assert result.status != AgentStatus.DONE
    assert result.evidence.get("validation_succeeded") is False
    # Tidak ada klaim sukses yang dikarang.
    assert result.evidence.get("validation_state") in ("not_run", "failed")


# --------------------------------------------------------------------------- #
# 9. Batas max_steps (BUG-2) & truncation (BUG-3) tetap non-sukses
# --------------------------------------------------------------------------- #
def test_max_steps_abort_stays_non_success() -> None:
    mutation = FakeMutationTool()
    provider = ScriptedProvider(
        [
            _tool_turn("edit", [_edit("src/a.py", "m1")]),
            _tool_turn("edit", [_edit("src/b.py", "m2")]),
            _final_turn("selesai"),
        ]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation)

    result = orchestrator.run_continuous_loop("kerjakan banyak", max_steps=1)

    assert result.status == AgentStatus.FAILED
    assert result.status != AgentStatus.DONE
    assert result.error is not None
    assert "Emergency safety limit" in result.error
    # Tidak ada DONE palsu meski ada perubahan (gate tidak dilonggarkan).
    assert result.evidence.get("verification_gate_blocked") is not True


def test_truncation_recovery_bound_stays_non_success() -> None:
    from agent_ai.core.orchestrator import _MAX_TRUNCATION_RECOVERIES

    truncated_turn = {
        "choices": [
            {
                "message": {"content": "potongan..."},
                "finish_reason": "length",
            }
        ]
    }
    provider = ScriptedProvider([truncated_turn])
    orchestrator = _make_orchestrator(provider)

    result = orchestrator.run_continuous_loop("task dengan output terpotong")

    assert result.status == AgentStatus.FAILED
    assert result.evidence.get("truncation_abort") is True
    assert provider.calls == _MAX_TRUNCATION_RECOVERIES


# --------------------------------------------------------------------------- #
# 10. Tidak ada infinite loop akibat verification recovery
# --------------------------------------------------------------------------- #
def test_verification_recovery_is_bounded_no_infinite_loop() -> None:
    mutation = FakeMutationTool()
    provider = ScriptedProvider(
        [_tool_turn("edit", [_edit()]), _final_turn("selesai")]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation)

    result = orchestrator.run_continuous_loop("ubah source tanpa validasi")

    # Nudge dibatasi: jumlah panggilan provider terbatas dan loop berakhir.
    assert result.status == AgentStatus.FAILED
    # 1 turn edit + N nudge + 1 final terakhir harus <= N + 2.
    assert provider.calls <= _MAX_VERIFICATION_RECOVERIES + 2, provider.calls
    assert result.evidence.get("verification_recoveries") == _MAX_VERIFICATION_RECOVERIES


def test_gate_does_not_execute_tools_by_itself() -> None:
    """Recovery gate TIDAK menambah eksekusi tool; hanya LLM yang bertindak."""
    mutation = FakeMutationTool()
    command = FakeCommandTool()
    provider = ScriptedProvider(
        [_tool_turn("edit", [_edit()]), _final_turn("selesai")]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation, command=command)

    result = orchestrator.run_continuous_loop("ubah source")

    assert result.status == AgentStatus.FAILED
    # Gate tidak pernah memanggil tool command sendiri.
    assert command.calls == 0
    # Hanya satu tool call nyata (edit) yang dieksekusi.
    assert mutation.calls == 1


# --------------------------------------------------------------------------- #
# 11. AUDIT LANJUTAN BUG-4 — klasifikasi task & completion gate saat gagal
# --------------------------------------------------------------------------- #
def test_readme_created_without_test_request_completes() -> None:
    """(1) Membuat README tanpa permintaan test -> DONE bila artifact dibuat."""
    mutation = FakeMutationTool(name="write_file", marker="written")
    provider = ScriptedProvider(
        [
            _tool_turn("tulis", [_tool_call("m1", "write_file", {"path": "README.md"})]),
            _final_turn("README selesai"),
        ]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation)

    result = orchestrator.run_continuous_loop("buat README.md")

    assert result.status == AgentStatus.DONE, result.error
    assert result.evidence.get("task_requires_validation") is False
    assert result.evidence.get("change_targets_source") is False
    assert result.evidence.get("validation_required") is False


def test_batch_script_creation_does_not_force_execution() -> None:
    """(2) Membuat .bat tanpa instruksi menjalankannya -> DONE, script TIDAK dieksekusi."""
    mutation = FakeMutationTool(name="write_file", marker="written")
    command = FakeCommandTool()
    provider = ScriptedProvider(
        [
            _tool_turn(
                "tulis",
                [_tool_call("m1", "write_file", {"path": "run_tools.bat"})],
            ),
            _final_turn("script selesai dibuat"),
        ]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation, command=command)

    result = orchestrator.run_continuous_loop("buat script run_tools.bat")

    assert result.status == AgentStatus.DONE, result.error
    # Pembuatan script BUKAN permintaan menjalankannya.
    assert command.calls == 0
    assert result.evidence.get("change_targets_source") is False
    assert result.evidence.get("validation_required") is False


def test_documentation_mentioning_test_does_not_require_running_tests() -> None:
    """(3) Dokumentasi yang hanya membahas test tidak memicu kewajiban test."""
    mutation = FakeMutationTool(name="write_file", marker="written")
    provider = ScriptedProvider(
        [
            _tool_turn(
                "tulis",
                [_tool_call("m1", "write_file", {"path": "docs/panduan.md"})],
            ),
            _final_turn("dokumentasi selesai"),
        ]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation)

    result = orchestrator.run_continuous_loop(
        "tulis dokumentasi yang membahas test dan strategi verifikasi"
    )

    assert result.status == AgentStatus.DONE, result.error
    assert result.evidence.get("task_requires_validation") is False
    assert result.evidence.get("validation_required") is False


def test_read_only_task_mentioning_test_file_completes() -> None:
    """(4) Read-only (menyebut file test) -> DONE tanpa validasi coding."""
    read = FakeReadTool()
    provider = ScriptedProvider(
        [
            _tool_turn(
                "baca",
                [_tool_call("r1", "read_file", {"path": "tests/test_bug4_completion_gate.py"})],
            ),
            _final_turn("analisis selesai"),
        ]
    )
    orchestrator = _make_orchestrator(provider, read=read)

    result = orchestrator.run_continuous_loop(
        "baca tests/test_bug4_completion_gate.py dan jelaskan isinya"
    )

    assert result.status == AgentStatus.DONE, result.error
    assert result.evidence.get("change_applied") is False
    assert result.evidence.get("task_requires_validation") is False


def test_explicit_no_run_test_variants_are_respected() -> None:
    """(5) Semua variasi instruksi eksplisit 'jangan jalankan test' dihormati."""
    variants = (
        "ubah src/app.py tanpa menjalankan test",
        "ubah src/app.py jangan jalankan test",
        "ubah src/app.py tidak perlu menjalankan test",
        "ubah src/app.py tanpa eksekusi test",
        "ubah src/app.py no run tests",
    )
    for task in variants:
        mutation = FakeMutationTool()
        provider = ScriptedProvider(
            [_tool_turn("edit", [_edit("src/app.py")]), _final_turn("selesai")]
        )
        orchestrator = _make_orchestrator(provider, mutation=mutation)

        result = orchestrator.run_continuous_loop(task)

        assert result.evidence.get("task_waives_validation") is True, task
        assert result.evidence.get("validation_required") is False, task
        assert result.status == AgentStatus.DONE, (task, result.error)
        # Tidak ada klaim validasi test yang tidak pernah dijalankan.
        assert result.evidence.get("validation_succeeded") is False, task


def test_source_change_without_waiver_still_requires_validation() -> None:
    """(6) Perubahan source tanpa waiver -> kebijakan validasi tetap berlaku."""
    mutation = FakeMutationTool()
    provider = ScriptedProvider(
        [_tool_turn("edit", [_edit("src/service.py")]), _final_turn("selesai")]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation)

    result = orchestrator.run_continuous_loop("rapikan service pembayaran")

    assert result.evidence.get("task_requires_validation") is False
    assert result.evidence.get("change_targets_source") is True
    assert result.evidence.get("validation_required") is True
    assert result.status == AgentStatus.FAILED
    assert result.evidence.get("verification_gate_blocked") is True


def test_user_requested_validation_is_mandatory() -> None:
    """(7) Validasi yang diminta user tetap diwajibkan (tidak boleh dilewati)."""
    mutation = FakeMutationTool()
    provider = ScriptedProvider(
        [_tool_turn("edit", [_edit()]), _final_turn("selesai")]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation)

    result = orchestrator.run_continuous_loop("jalankan test setelah mengubah kode")

    assert result.evidence.get("task_requires_validation") is True
    assert result.evidence.get("validation_required") is True
    assert result.status == AgentStatus.FAILED
    assert result.evidence.get("verification_gate_blocked") is True


def test_failed_requested_command_without_change_is_not_done() -> None:
    """(7b) Command validasi eksplisit yang GAGAL tidak boleh jadi DONE."""
    command = FakeCommandTool()
    command.exit_code = 2
    provider = ScriptedProvider(
        [
            _tool_turn("jalankan test", [_run_ok("v1", "jalankan test")]),
            _final_turn("semua beres"),
        ]
    )
    orchestrator = _make_orchestrator(provider, command=command)

    result = orchestrator.run_continuous_loop("jalankan test di tests/test_x.py")

    assert result.status == AgentStatus.FAILED, (result.status, result.error)
    assert result.evidence.get("verification_gate_blocked") is True
    assert result.evidence.get("verification_gate_reason") == "requested_operation_failed"
    assert result.evidence.get("validation_succeeded") is False


def test_failed_artifact_creation_is_not_false_done() -> None:
    """(8) Gagal membuat artifact yang diminta -> TIDAK boleh DONE palsu."""
    failing = FakeFailingMutationTool(name="write_file")
    provider = ScriptedProvider(
        [
            _tool_turn(
                "tulis",
                [_tool_call("m1", "write_file", {"path": "README.md", "content": "x"})],
            ),
            _final_turn("README sudah dibuat"),
        ]
    )
    orchestrator = _make_orchestrator(provider, extra_tools=[failing])

    result = orchestrator.run_continuous_loop("buat README.md")

    assert result.status == AgentStatus.FAILED, (result.status, result.error)
    assert result.status != AgentStatus.DONE
    assert result.evidence.get("change_applied") is False, result.evidence
    assert result.evidence.get("verification_gate_blocked") is True
    assert result.evidence.get("verification_gate_reason") == "requested_operation_failed"
    assert result.evidence.get("last_failure_tool") == "write_file"


def test_failed_creation_then_successful_write_allows_done() -> None:
    """(8b) Kegagalan pembuatan artifact yang DIPULIHKAN boleh DONE."""
    failing = FakeFailingMutationTool(name="write_file")
    recovered = FakeMutationTool(name="edit_file", marker="edited")
    provider = ScriptedProvider(
        [
            _tool_turn(
                "coba tulis",
                [_tool_call("m1", "write_file", {"path": "laporan.md", "content": "x"})],
            ),
            _tool_turn(
                "tulis ulang",
                [_tool_call("m2", "edit_file", {"path": "laporan.md"})],
            ),
            _final_turn("laporan selesai"),
        ]
    )
    orchestrator = _make_orchestrator(
        provider, extra_tools=[failing, recovered]
    )

    result = orchestrator.run_continuous_loop("buat laporan.md")

    assert result.status == AgentStatus.DONE, result.error
    assert result.evidence.get("change_applied") is True


def test_sidecar_read_failure_does_not_block_completed_task() -> None:
    """(8c) Kegagalan SAMPINGAN (read gagal) tidak menggagalkan hasil yang tercapai."""
    mutation = FakeMutationTool(name="write_file", marker="written")
    failing_read = FakeFailingReadTool(name="read_file")
    provider = ScriptedProvider(
        [
            _tool_turn(
                "tulis",
                [_tool_call("m1", "write_file", {"path": "catatan.md", "content": "x"})],
            ),
            _tool_turn("baca balik", [_tool_call("r1", "read_file", {"path": "catatan.md"})]),
            _final_turn("catatan dibuat"),
        ]
    )
    orchestrator = _make_orchestrator(
        provider, mutation=mutation, extra_tools=[failing_read]
    )

    result = orchestrator.run_continuous_loop("buat catatan.md")

    # Artifact yang diminta TERBUAT -> kegagalan sampingan tidak membatalkan DONE.
    assert result.status == AgentStatus.DONE, (result.status, result.error)
    assert result.evidence.get("validation_required") is False


def test_prose_phrase_does_not_create_false_file_target() -> None:
    """(9) Frasa biasa tidak menghasilkan target palsu; target jelas tetap dikenali."""
    detailed = AgentOrchestrator._task_target_files_detailed(
        "buat berita acara serah terima.md"
    )
    assert "terima.md" not in detailed["confident"], detailed
    assert "terima.md" in detailed["ambiguous"], detailed
    assert AgentOrchestrator._task_target_files("buat berita acara serah terima.md") == set()

    # Target yang JELAS diminta tetap dikenali.
    assert AgentOrchestrator._task_target_files("ubah src/app.py") == {"app.py"}
    assert AgentOrchestrator._task_target_files("tulis file readme.md") == {"readme.md"}
    assert AgentOrchestrator._task_target_files("buat `notes.txt`") == {"notes.txt"}
    assert AgentOrchestrator._task_target_files("update README.md") == {"readme.md"}


def test_ambiguous_target_alone_does_not_reject_completion() -> None:
    """(9b) Target ambigu tidak menjadi satu-satunya alasan menolak completion."""
    mutation = FakeMutationTool(name="write_file", marker="written")
    provider = ScriptedProvider(
        [
            _tool_turn(
                "tulis",
                [_tool_call("m1", "write_file", {"path": "berita_acara.md"})],
            ),
            _final_turn("berita acara selesai"),
        ]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation)

    result = orchestrator.run_continuous_loop(
        "susun berita acara serah terima.md untuk proyek ini"
    )

    assert result.status == AgentStatus.DONE, result.error


def test_legacy_completion_detected_ignores_ambiguous_target_only() -> None:
    """(9c) Legacy `_completion_detected`: target ambigu saja tidak menolak."""
    from agent_ai.core.loop import AgentLoop
    from agent_ai.core.models import AgentAction, AgentObservation

    mutation = FakeMutationTool(name="write_file", marker="written")
    provider = ScriptedProvider(
        [
            _tool_turn(
                "tulis",
                [_tool_call("m1", "write_file", {"path": "berita_acara.md"})],
            ),
            _final_turn("selesai"),
        ]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation)
    orchestrator.run_continuous_loop("susun berita acara serah terima.md")

    # Bangun loop langsung untuk menguji helper legacy.
    loop = AgentLoop(task="susun berita acara serah terima.md")
    loop.start()
    loop.record_action(
        AgentAction(name="write_file", arguments={"path": "berita_acara.md"})
    )
    loop.record_observation(
        AgentObservation(
            content={"path": "berita_acara.md", "written": True, "success": True},
            success=True,
            metadata={"tool": "write_file"},
        )
    )
    assert orchestrator._completion_detected(loop) is True

    # Kontrol: task dengan target JELAS yang belum dihasilkan tetap CONTINUE.
    loop2 = AgentLoop(task="buat a.html dan b.js")
    loop2.start()
    loop2.record_action(
        AgentAction(name="write_file", arguments={"path": "a.html"})
    )
    loop2.record_observation(
        AgentObservation(
            content={"path": "a.html", "written": True, "success": True},
            success=True,
            metadata={"tool": "write_file"},
        )
    )
    assert orchestrator._completion_detected(loop2) is False


def test_bug2_batch_budget_and_bug3_truncation_still_hold() -> None:
    """(10) Perbaikan klasifikasi TIDAK mengganggu BUG-2 dan BUG-3."""
    from agent_ai.core.orchestrator import _MAX_TRUNCATION_RECOVERIES

    # BUG-2: budget batch tetap men-FAIL walau task minta validasi.
    mutation = FakeMutationTool()
    provider = ScriptedProvider(
        [
            _tool_turn("e1", [_edit("src/a.py", "m1")]),
            _tool_turn("e2", [_edit("src/b.py", "m2")]),
            _final_turn("selesai"),
        ]
    )
    orchestrator = _make_orchestrator(provider, mutation=mutation)
    result = orchestrator.run_continuous_loop(
        "jahit source dan jalankan test", max_steps=1
    )
    assert result.status == AgentStatus.FAILED
    assert result.status != AgentStatus.DONE
    assert "Emergency safety limit" in (result.error or "")
    # Tidak ada DONE palsu meski ada perubahan (gate tidak dilonggarkan).
    assert result.evidence.get("verification_gate_blocked") is not True

    # BUG-3: truncation recovery tetap BOUNDED dan non-sukses.
    truncated_turn = {
        "choices": [{"message": {"content": "..."}, "finish_reason": "length"}]
    }
    provider2 = ScriptedProvider([truncated_turn])
    orchestrator2 = _make_orchestrator(provider2)
    result2 = orchestrator2.run_continuous_loop("task dengan output terpotong")
    assert result2.status == AgentStatus.FAILED
    assert result2.evidence.get("truncation_abort") is True
    assert provider2.calls == _MAX_TRUNCATION_RECOVERIES


if __name__ == "__main__":  # pragma: no cover - entry manual
    import traceback

    tests = [
        test_change_then_final_without_validation_not_completed,
        test_gate_holds_after_verification_nudge,
        test_successful_validation_after_change_allows_done,
        test_failed_validation_then_final_not_completed,
        test_failed_validation_nudge_mentions_failure,
        test_source_change_without_keyword_still_requires_validation,
        test_read_only_task_without_change_allowed,
        test_final_only_task_analysis_allowed,
        test_no_validation_clause_disables_requirement,
        test_validation_then_newer_change_requires_revalidation,
        test_validation_after_last_change_allows_done,
        test_validation_unavailable_makes_no_success_claim,
        test_max_steps_abort_stays_non_success,
        test_truncation_recovery_bound_stays_non_success,
        test_verification_recovery_is_bounded_no_infinite_loop,
        test_gate_does_not_execute_tools_by_itself,
        test_readme_created_without_test_request_completes,
        test_batch_script_creation_does_not_force_execution,
        test_documentation_mentioning_test_does_not_require_running_tests,
        test_read_only_task_mentioning_test_file_completes,
        test_explicit_no_run_test_variants_are_respected,
        test_source_change_without_waiver_still_requires_validation,
        test_user_requested_validation_is_mandatory,
        test_failed_requested_command_without_change_is_not_done,
        test_failed_artifact_creation_is_not_false_done,
        test_failed_creation_then_successful_write_allows_done,
        test_sidecar_read_failure_does_not_block_completed_task,
        test_prose_phrase_does_not_create_false_file_target,
        test_ambiguous_target_alone_does_not_reject_completion,
        test_legacy_completion_detected_ignores_ambiguous_target_only,
        test_bug2_batch_budget_and_bug3_truncation_still_hold,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  OK  {test.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {test.__name__}")
            traceback.print_exc()
    print(f"\n{'FAILED' if failed else 'OK'}: {len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
