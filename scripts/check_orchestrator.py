"""Verifikasi Agent Orchestrator (iterative agent loop).

Menggunakan provider palsu (fake) yang mengembalikan LLMResponse terprogram,
sehingga tidak memanggil API cloud dan tidak bergantung pada Ollama.

Skenario:
    1) task -> LLM tool call -> tool dieksekusi -> observation -> LLM FINAL.
    2) max_iterations bekerja.
    3) tool error tidak membuat loop crash.

Jalankan:
    python scripts/check_orchestrator.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core import (  # noqa: E402
    ActionType,
    AgentOrchestrator,
    AgentStatus,
    FinishReason,
    LLMAction,
    LLMResponse,
)
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402


class ScriptedProvider(BaseProvider):
    """Provider palsu: mengembalikan LLMResponse terprogram per pemanggilan."""

    name = "scripted"

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        # Simpan messages terakhir untuk inspeksi history.
        self.last_messages = messages or []
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


def main() -> int:
    print("=== Verifikasi Agent Orchestrator ===")

    # 1) task -> tool call -> observation -> FINAL.
    provider = ScriptedProvider([
        tool_call("read_file", {"path": "src/agent_ai/tools/base.py", "end_line": 2}),
        final("File berhasil dibaca."),
    ])
    orch = AgentOrchestrator(provider=provider, max_iterations=5)
    result = orch.run("Baca file base.py lalu simpulkan.")
    print(f"status      = {result.status.value}")
    print(f"result      = {result.result!r}")
    print(f"iterations  = {result.iterations}")
    assert result.status == AgentStatus.DONE
    assert result.result == "File berhasil dibaca."
    assert result.iterations == 1
    # History harus memuat observation tool.
    assert any("[tool result]" in m.content for m in provider.last_messages), "observation tidak dikirim ke LLM"
    print("history observation terkirim ke LLM -> OK")
    print()

    # 2) max_iterations bekerja (provider selalu minta tool call).
    provider2 = ScriptedProvider([tool_call("list_files", {"path": "src"})])
    orch2 = AgentOrchestrator(provider=provider2, max_iterations=3)
    result2 = orch2.run("Loop terus.")
    print(f"max_iter status = {result2.status.value}, iterations = {result2.iterations}")
    assert result2.status == AgentStatus.FAILED
    assert result2.iterations == 3
    print()

    # 3) Tool error tidak membuat loop crash -> dikirim sebagai observation.
    provider3 = ScriptedProvider([
        tool_call("read_file", {"path": "tidak/ada.py"}),
        final("File tidak ditemukan, saya berhenti."),
    ])
    orch3 = AgentOrchestrator(provider=provider3, max_iterations=5)
    result3 = orch3.run("Baca file yang tidak ada.")
    print(f"tool error status = {result3.status.value}, result = {result3.result!r}")
    assert result3.status == AgentStatus.DONE
    assert any("[tool error]" in m.content for m in provider3.last_messages), "tool error tidak dikirim ke LLM"
    print("tool error dikirim sebagai observation -> OK")
    print()

    print("[OK] Agent Orchestrator (iterative loop, FINAL, max_iterations, tool error) bekerja.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
