"""Regression tests: alur INVESTIGATION Consultant berhenti dengan benar.

Latar: pada kasus "Consultant berhenti di max_steps=40", pola yang terlihat
adalah read_file berulang (file sama, rentang makin sempit) dan run_command
untuk membaca source — meskipun informasi yang sama SUDAH tersedia. Penyebab
dari sisi AETHER:

    1. Prompt INVESTIGATE mendeskripsikan alur retrieval bertahap
       ("search lanjutan -> bandingkan -> lanjutkan sampai cukup") tanpa batas
       fase yang eksplisit, dan TIDAK menjelaskan arti state tool result
       (`already_available` / `already_searched`) → LLM mengulang read/search.
    2. Tool result sudah membawa state (`already_available` / `already_searched`)
       tetapi prompt tidak mengajarkan cara meresponsnya.

Perbaikan (MINIMAL, hanya sisi Consultant):
    - Prompt: fase eksplisit INVESTIGATION -> ANALYSIS -> FINAL + section
      "State Sumber Informasi" (arti `already_available` / `already_searched` /
      `consultant_retrieval_bound` + opsi `read_file(force=true)`), dan penegasan
      run_command BUKAN alat baca source.
    - Deskripsi tool `run_command` Consultant: eksplisit bukan alat baca source.

Yang diuji di sini (tanpa provider LLM eksternal, semua fake/lokal):
    A. Prompt memuat fase + penjelasan state sumber informasi.
    B. Deskripsi tool run_command Consultant menolak membaca source.
    C. read_file: read penuh lalu read rentang nested -> ALREADY_AVAILABLE
       (tanpa content); `force=true` mengirim ulang content.
    D. search_code: query sama diulang -> already_searched (tanpa eksekusi ulang).
    E. End-to-end: LLM (fake, sadar-state) berhenti saat melihat
       ALREADY_AVAILABLE dan menghasilkan jawaban final (DONE, jauh < 40 step),
       TANPA jatuh ke run_command.
    F. End-to-end: kasus rentang nested (file sama) tetap selesai normal.

Jalankan:
    python -m pytest tests/test_consultant_investigation_flow.py
atau:
    python tests/test_consultant_investigation_flow.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.consultant.prompt import build_consultant_system_prompt  # noqa: E402
from agent_ai.consultant.service import ConsultantService  # noqa: E402
from agent_ai.consultant.tools import (  # noqa: E402
    ConsultantRunCommandTool,
    build_consultant_registry,
)
from agent_ai.core.response import FinishReason, LLMAction, LLMResponse  # noqa: E402
from agent_ai.providers.base import (  # noqa: E402
    BaseProvider,
    GenerateOptions,
    GenerateResult,
)

PY_SAMPLE = (
    "import os\n"
    "\n"
    "def alpha(x):\n"
    "    y = x + 1\n"
    "    return y\n"
    "\n"
    "def beta(x):\n"
    "    z = x * 2\n"
    "    return z\n"
)


def _write(ws: Path, rel: str, text: str) -> Path:
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Fake provider yang BERPERILAKU seperti LLM kooperatif yang menghormati state
# --------------------------------------------------------------------------- #
class StateAwareProvider(BaseProvider):
    """Provider palsu: mengikuti skrip, tetapi BERHENTI begitu tool result
    mengembalikan state "sudah tersedia" (already_available/already_searched).

    Ini meniru LLM yang PATUH pada panduan prompt: ketika diberi tahu bahwa
    informasi sudah ada, ia tidak mengulang retrieval dan langsung menjawab.
    """

    name = "state-aware"

    STOP_MARKERS: Tuple[str, ...] = (
        "already_available",
        "already_searched",
        "consultant_retrieval_bound",
    )

    def __init__(self, script: List[Tuple[str, Dict[str, Any]]]) -> None:
        self.script = list(script)
        self.calls = 0
        self.saw_stop_state: Optional[str] = None
        self.last_tool_content: Optional[str] = None
        self.offered_history: List[set] = []

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[Any] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[Any] = None,
        tool_choice: Optional[Any] = None,
    ) -> GenerateResult:
        self.offered_history.append({getattr(t, "name", "") for t in (tools or [])})
        self.last_tool_content = None
        for message in messages or []:
            if isinstance(message, dict) and message.get("role") == "tool":
                self.last_tool_content = message.get("content") or ""
        if self.last_tool_content:
            low = self.last_tool_content.lower()
            for marker in self.STOP_MARKERS:
                if marker in low:
                    self.saw_stop_state = marker
        return GenerateResult(text="", model="fake", provider=self.name)

    def normalize_response(self, result: GenerateResult) -> LLMResponse:
        self.calls += 1
        # LLM kooperatif: state "sudah tersedia" => informasi cukup => jawab final.
        if self.saw_stop_state is not None:
            return LLMResponse(
                text="Jawaban final dari evidence yang sudah ada.",
                finish_reason=FinishReason.STOP,
            )
        if self.calls > len(self.script):
            return LLMResponse(
                text="Jawaban final dari evidence yang sudah ada.",
                finish_reason=FinishReason.STOP,
            )
        name, arguments = self.script[self.calls - 1]
        return LLMResponse(
            actions=[LLMAction(name=name, arguments=dict(arguments))],
            finish_reason=FinishReason.TOOL_CALLS,
        )


# --------------------------------------------------------------------------- #
# A. Prompt memuat fase + penjelasan state sumber informasi
# --------------------------------------------------------------------------- #
def test_a_prompt_defines_investigation_phases() -> None:
    prompt = build_consultant_system_prompt("investigate")
    # Transisi fase eksplisit.
    assert "INVESTIGATION -> ANALYSIS -> FINAL" in prompt
    assert "FASE INVESTIGATION" in prompt
    assert "FASE ANALYSIS" in prompt
    assert "FASE FINAL" in prompt
    # Instruksi berhenti retrieval ketika informasi cukup.
    assert "STOP retrieval" in prompt
    assert "BERHENTI memanggil tool" in prompt
    # Kalimat "search lanjutan ... lanjutkan sampai informasi cukup" yang lama
    # (yang mendorong investigasi tanpa batas) sudah tidak ada.
    assert "search lanjutan" not in prompt
    assert "sampai informasi cukup" not in prompt


def test_a2_prompt_documents_source_state_semantics() -> None:
    prompt = build_consultant_system_prompt("investigate")
    assert "State Sumber Informasi" in prompt
    # Menjelaskan arti state tool result + cara meresponsnya.
    assert "already_available" in prompt
    assert "already_searched" in prompt
    assert "consultant_retrieval_bound" in prompt
    # Opsi untuk benar-benar meminta isi mentah dikirim ulang.
    assert "force=true" in prompt
    # Mencegah fallback ke run_command hanya untuk membaca source.
    assert "JANGAN beralih ke run_command" in prompt


def test_a3_prompt_read_file_is_primary_and_run_command_is_not_for_source() -> None:
    prompt = build_consultant_system_prompt("investigate")
    # read_file/search_code = cara utama membaca source.
    assert "cara UTAMA untuk membaca source" in prompt
    # run_command tidak untuk menampilkan isi file.
    assert "MENAMPILKAN ISI FILE" in prompt
    assert "JANGAN pakai run_command untuk sekadar" in prompt


# --------------------------------------------------------------------------- #
# B. Deskripsi tool run_command Consultant: bukan alat baca source
# --------------------------------------------------------------------------- #
def test_b_run_command_description_discourages_source_read() -> None:
    assert "BUKAN alat baca source" in ConsultantRunCommandTool.description
    assert "menampilkan isi file" in ConsultantRunCommandTool.description
    assert "read_file/search_code" in ConsultantRunCommandTool.description


# --------------------------------------------------------------------------- #
# C. read_file: read penuh lalu rentang nested -> ALREADY_AVAILABLE
# --------------------------------------------------------------------------- #
def test_c_nested_range_read_is_already_available(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    registry = build_consultant_registry(tmp_path, mode="investigate")

    first = registry.execute("read_file", {"path": "svc.py", "start_line": 1, "end_line": 9})
    assert first.get("content") and not first.get("already_read")

    # Rentang lebih sempit (nested) dari yang sudah dibaca -> sudah tersedia.
    nested = registry.execute("read_file", {"path": "svc.py", "start_line": 3, "end_line": 7})
    assert nested.get("already_available") is True, nested
    assert nested.get("already_read") is True, nested
    assert "content" not in nested, nested
    assert "ALREADY_AVAILABLE" in nested.get("message", "")

    # Rentang overlap sebagian juga sudah tercakup.
    overlap = registry.execute("read_file", {"path": "svc.py", "start_line": 5, "end_line": 10})
    assert overlap.get("already_available") is True, overlap
    assert "content" not in overlap, overlap

    # File lain yang belum dibaca tetap mengirim content.
    _write(tmp_path, "other.py", PY_SAMPLE)
    fresh = registry.execute("read_file", {"path": "other.py", "start_line": 1, "end_line": 5})
    assert fresh.get("content") is not None, fresh


def test_c2_force_resends_content_after_already_available(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    registry = build_consultant_registry(tmp_path, mode="investigate")

    registry.execute("read_file", {"path": "svc.py", "symbol": "alpha"})
    stub = registry.execute("read_file", {"path": "svc.py", "symbol": "alpha"})
    assert stub.get("already_available") is True

    # force=true -> isi mentah dikirim ulang (pemulihan bila konteks diringkas).
    forced = registry.execute("read_file", {"path": "svc.py", "symbol": "alpha", "force": True})
    assert forced.get("content")
    assert "def alpha" in forced["content"]


# --------------------------------------------------------------------------- #
# D. search_code: query sama diulang -> already_searched (tanpa eksekusi ulang)
# --------------------------------------------------------------------------- #
def test_d_repeated_search_is_already_searched(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    registry = build_consultant_registry(tmp_path, mode="investigate")

    first = registry.execute("search_code", {"query": "alpha"})
    assert first.get("count") or first.get("matches") is not None

    second = registry.execute("search_code", {"query": "alpha"})
    assert second.get("already_searched") is True, second
    assert "matches" not in second, second


# --------------------------------------------------------------------------- #
# E. End-to-end: LLM sadar-state berhenti saat ALREADY_AVAILABLE -> final answer
# --------------------------------------------------------------------------- #
def test_e_state_aware_llm_stops_on_already_available(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)

    # search (locator) -> read (content) -> read ULANG file/symbol yang SAMA.
    provider = StateAwareProvider(
        [
            ("search_code", {"query": "alpha"}),
            ("read_file", {"path": "svc.py", "symbol": "alpha"}),
            ("read_file", {"path": "svc.py", "symbol": "alpha"}),
        ]
    )

    service = ConsultantService()
    result = service.consult(
        "Apa yang dilakukan fungsi alpha?",
        provider=provider,
        root=str(tmp_path),
        mode="investigate",
    )

    # 1) Konsultasi selesai NORMAL dengan jawaban final (bukan FAILED).
    assert result.status == "done", (result.status, result.error)
    assert result.reply.strip(), result.reply
    assert result.error is None, result.error
    assert "max_steps" not in (result.error or "")

    # 2) AETHER memberi tahu LLM bahwa source SUDAH tersedia (state ter-surface).
    assert provider.saw_stop_state == "already_available", provider.saw_stop_state

    # 3) Tidak runaway: berhenti jauh sebelum safeguard max_steps=40.
    assert result.iterations < 10, result.iterations
    tools_used = [event["tool"] for event in result.tool_events]
    assert "search_code" in tools_used and "read_file" in tools_used
    # 4) TIDAK jatuh ke run_command hanya untuk membaca source.
    assert "run_command" not in tools_used, tools_used


def test_e2_read_file_used_repeatedly_without_run_command(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    provider = StateAwareProvider(
        [
            ("read_file", {"path": "svc.py", "start_line": 1, "end_line": 9}),
            ("read_file", {"path": "svc.py", "start_line": 3, "end_line": 7}),
        ]
    )
    service = ConsultantService()
    result = service.consult(
        "Jelaskan svc.py.",
        provider=provider,
        root=str(tmp_path),
        mode="investigate",
    )
    assert result.status == "done", (result.status, result.error)
    assert provider.saw_stop_state == "already_available", provider.saw_stop_state
    assert result.iterations < 10, result.iterations
    assert all(event["tool"] != "run_command" for event in result.tool_events)


# --------------------------------------------------------------------------- #
# F. Regresi: alur wajar (search -> read -> jawab) tetap selesai normal
# --------------------------------------------------------------------------- #
def test_f_normal_flow_still_completes(tmp_path: Path) -> None:
    _write(tmp_path, "svc.py", PY_SAMPLE)
    provider = StateAwareProvider(
        [
            ("search_code", {"query": "beta"}),
            ("read_file", {"path": "svc.py", "symbol": "beta"}),
        ]
    )
    service = ConsultantService()
    result = service.consult(
        "Apa yang dilakukan fungsi beta?",
        provider=provider,
        root=str(tmp_path),
        mode="investigate",
    )
    assert result.status == "done", (result.status, result.error)
    # Tidak ada state "sudah tersedia" pada alur ini (info baru semua).
    assert provider.saw_stop_state is None
    assert result.iterations <= 4, result.iterations


# --------------------------------------------------------------------------- #
# Runner manual (opsional, tanpa pytest)
# --------------------------------------------------------------------------- #
def _standalone() -> int:
    print("=== Verifikasi alur INVESTIGATION Consultant ===")
    failures = 0
    with tempfile.TemporaryDirectory(prefix="aether_consultant_flow_") as tmp:
        ws = Path(tmp)
        checks = [
            ("A. prompt memuat fase INVESTIGATION/ANALYSIS/FINAL", test_a_prompt_defines_investigation_phases),
            ("A2. prompt menjelaskan state sumber informasi", test_a2_prompt_documents_source_state_semantics),
            ("A3. prompt: read_file utama, run_command bukan baca source", test_a3_prompt_read_file_is_primary_and_run_command_is_not_for_source),
            ("B. deskripsi run_command bukan alat baca source", test_b_run_command_description_discourages_source_read),
            ("C. read nested range -> ALREADY_AVAILABLE", lambda: test_c_nested_range_read_is_already_available(ws)),
            ("C2. force=true mengirim ulang content", lambda: test_c2_force_resends_content_after_already_available(ws)),
            ("D. search diulang -> already_searched", lambda: test_d_repeated_search_is_already_searched(ws)),
            ("E. LLM sadar-state berhenti & menjawab final", lambda: test_e_state_aware_llm_stops_on_already_available(ws)),
            ("E2. read_file diulang tanpa run_command", lambda: test_e2_read_file_used_repeatedly_without_run_command(ws)),
            ("F. alur wajar tetap selesai", lambda: test_f_normal_flow_still_completes(ws)),
        ]
        for label, fn in checks:
            try:
                fn()
                print(f"[OK] {label}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"[FAIL] {label}: {type(exc).__name__}: {exc}")
    print()
    if failures:
        print(f"[FAILED] {failures} test gagal")
        return 1
    print("[OK] Alur INVESTIGATION Consultant berhenti dengan benar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_standalone())
