"""Verifikasi integrasi native tool-calling: ToolRegistry -> Orchestrator -> Provider.

Membuktikan secara deterministik pada level orchestration:
    1. ToolRegistry.specs() diberikan ke provider.
    2. Provider menerima tool definitions.
    3. native LLMAction masuk ke ToolExecutor.
    4. ToolExecutor menjalankan tool.
    5. Observation dihasilkan.
    6. Observation dikirim kembali pada pemanggilan LLM berikutnya.
    7. FINAL mengakhiri loop.
    8. Tidak ada tool execution setelah FINAL.
    9. command failure tetap menjadi observation yang dapat diproses agent.
   10. workspace boundary tetap berlaku.

Bagian A memakai provider test/double (ScriptedProvider) untuk mengontrol
sequence response secara deterministik. Bagian B (opsional) adalah smoke test
nyata ke DeepSeek bila DEEPSEEK_API_KEY tersedia (tanpa fallback ke Ollama).

Fixture hanya di J:\Agent_Ai\dummy_test dan dibersihkan setelah selesai.

Jalankan:
    python scripts/check_agent_tool_loop.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.config.settings import settings  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.core.response import FinishReason  # noqa: E402
from agent_ai.providers.base import (  # noqa: E402
    BaseProvider,
    GenerateOptions,
    GenerateResult,
    Message,
    ToolChoice,
    ToolDefinition,
)
from agent_ai.providers.deepseek import DeepSeekProvider  # noqa: E402
from agent_ai.tools.filesystem import ReadFileTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402
from agent_ai.tools.terminal import RunCommandTool  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "tool_loop_fixture"

_FILES = {
    "app/calc.py": (
        "def add(a, b):\n"
        "    return a + b\n"
    ),
}


def setup_fixture() -> None:
    for rel, content in _FILES.items():
        path = FIXTURE / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


# ---------------------------------------------------------------------------
# Provider test/double (BUKAN mock LLM untuk menyembunyikan masalah).
# Mengembalikan sequence GenerateResult yang sudah berisi native tool_calls
# (format OpenAI-compatible) agar jalur nyata orchestrator->provider->
# normalize_response->ToolExecutor teruji. Mencatat tools yang diterima.
# ---------------------------------------------------------------------------
class ScriptedProvider(BaseProvider):
    """Provider double: mengembalikan sequence raw response yang sudah ditentukan."""

    name = "scripted"

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self._responses = list(responses)
        self._index = 0
        self.received_tools: List[Optional[List[ToolDefinition]]] = []
        self.received_tool_choice: List[Optional[ToolChoice]] = []
        self.received_messages: List[List[Message]] = []

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Message]] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[List[ToolDefinition]] = None,
        tool_choice: Optional[ToolChoice] = None,
    ) -> GenerateResult:
        self.received_tools.append(tools)
        self.received_tool_choice.append(tool_choice)
        self.received_messages.append(list(messages or []))
        if self._index >= len(self._responses):
            # Habis: kembalikan FINAL kosong agar loop berhenti.
            return GenerateResult(text="(habis)", provider=self.name, raw={"choices": []})
        raw = self._responses[self._index]
        self._index += 1
        return GenerateResult(text="", provider=self.name, model="scripted", raw=raw)

    def normalize_response(self, result: GenerateResult):
        # Pakai normalisasi OpenAI-compatible (jalur nyata yang sama).
        from agent_ai.providers.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider.normalize_response(self, result)


def _tool_call_response(name: str, arguments: str, call_id: str = "c1") -> Dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def _final_response(text: str) -> Dict[str, Any]:
    return {
        "choices": [
            {"message": {"content": text}, "finish_reason": "stop"}
        ]
    }


def main() -> int:
    print("=== Verifikasi Integrasi Native Tool-Calling (Orchestrator) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    before = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}

    registry = ToolRegistry()
    registry.register(ReadFileTool(root=FIXTURE))
    registry.register(RunCommandTool(root=FIXTURE))
    executor = ToolExecutor(registry=registry)

    # --- Skenario 1: tool call -> observation -> FINAL ---------------------
    provider = ScriptedProvider(
        [
            _tool_call_response("read_file", '{"path": "app/calc.py"}'),
            _final_response("File berisi fungsi add."),
        ]
    )
    orch = AgentOrchestrator(use_continuous_loop=False, 
        provider=provider,
        executor=executor,
        max_iterations=5,
        system_prompt="Kamu coding agent.",
    )
    result = orch.run("Baca app/calc.py lalu jelaskan.")

    # 1) ToolRegistry.specs() diberikan ke provider.
    assert provider.received_tools[0] is not None, "tools harus dikirim ke provider"
    names = [t.name for t in provider.received_tools[0]]
    assert "read_file" in names and "run_command" in names
    print(f"[1] ToolRegistry.specs() -> provider OK -> {names}")

    # 2) Provider menerima tool definitions (tipe ToolDefinition).
    assert all(isinstance(t, ToolDefinition) for t in provider.received_tools[0])
    print(f"[2] provider menerima ToolDefinition OK -> {len(provider.received_tools[0])} tool")

    # 3) native LLMAction masuk ke ToolExecutor + 4) tool dijalankan + 5) observation.
    steps = result.steps
    assert len(steps) >= 1, "harus ada step tool"
    first_obs = steps[0]["observation"]
    assert first_obs is not None, "observation harus dihasilkan"
    assert first_obs["success"] is True
    assert "def add" in str(first_obs["content"])
    print(f"[3] native LLMAction -> ToolExecutor OK -> {steps[0]['action']['name']}")
    print(f"[4] ToolExecutor menjalankan tool OK")
    print(f"[5] observation dihasilkan OK -> success={first_obs['success']}")

    # 6) Observation dikirim kembali pada pemanggilan LLM berikutnya.
    assert len(provider.received_messages) >= 2, "harus ada pemanggilan LLM kedua"
    second_msgs = provider.received_messages[1]
    joined = "\n".join(m.content for m in second_msgs)
    assert "[tool result]" in joined, "observation harus dikirim kembali ke LLM"
    assert "def add" in joined, "isi observation harus ada di pesan berikutnya"
    print("[6] observation dikirim kembali ke LLM OK")

    # 7) FINAL mengakhiri loop.
    assert result.success, "loop harus selesai DONE"
    assert result.result == "File berisi fungsi add."
    print(f"[7] FINAL mengakhiri loop OK -> result={result.result!r}")

    # 8) Tidak ada tool execution setelah FINAL.
    #    Jumlah pemanggilan LLM = 2 (tool call + final); tidak ada lagi setelahnya.
    assert len(provider.received_messages) == 2, "tidak boleh ada LLM call setelah FINAL"
    print(f"[8] tidak ada tool execution setelah FINAL OK -> llm_calls={len(provider.received_messages)}")

    # --- Skenario 2: command failure -> observation -> FINAL ---------------
    provider2 = ScriptedProvider(
        [
            _tool_call_response(
                "run_command",
                '{"command": "python -c \\"import sys; sys.exit(3)\\""}',
            ),
            _final_response("Command gagal dengan exit code 3."),
        ]
    )
    orch2 = AgentOrchestrator(use_continuous_loop=False, provider=provider2, executor=executor, max_iterations=5)
    result2 = orch2.run("Jalankan command yang gagal.")

    obs2 = result2.steps[0]["observation"]
    assert obs2["success"] is True, "command failure bukan tool_error"
    assert obs2["metadata"].get("command_failure") is True
    assert obs2["metadata"].get("exit_code") == 3
    # observation command failure dikirim kembali ke LLM.
    joined2 = "\n".join(m.content for m in provider2.received_messages[1])
    assert "[tool result]" in joined2
    print(f"[9] command failure -> observation OK -> exit_code={obs2['metadata'].get('exit_code')}")

    # --- Skenario 3: workspace boundary tetap berlaku ----------------------
    provider3 = ScriptedProvider(
        [
            _tool_call_response("read_file", '{"path": "../../etc/passwd"}'),
            _final_response("Path ditolak."),
        ]
    )
    orch3 = AgentOrchestrator(use_continuous_loop=False, provider=provider3, executor=executor, max_iterations=5)
    result3 = orch3.run("Baca file di luar workspace.")
    obs3 = result3.steps[0]["observation"]
    assert obs3["success"] is False, "path di luar workspace harus ditolak"
    assert obs3["metadata"].get("tool_error") is True
    print(f"[10] workspace boundary OK -> {obs3['error']}")

    # --- Skenario 4: tool_choice default tidak dipaksa ---------------------
    assert provider.received_tool_choice[0] is None, "tool_choice default harus None"
    print("[11] tool_choice default tidak dipaksa OK")

    # --- Skenario 5: FINAL langsung (tanpa tool) ---------------------------
    provider5 = ScriptedProvider([_final_response("Tidak perlu tool.")])
    orch5 = AgentOrchestrator(use_continuous_loop=False, provider=provider5, executor=executor, max_iterations=5)
    result5 = orch5.run("Jawab tanpa tool.")
    assert result5.success and result5.result == "Tidak perlu tool."
    assert len(result5.steps) == 0, "tidak boleh ada tool execution"
    print("[12] FINAL tanpa tool OK -> tidak ada tool execution")

    # source project tidak berubah.
    after = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}
    assert before == after, "source project berubah!"
    print("[13] source project tidak berubah : OK")

    # --- Bagian B: smoke test nyata DeepSeek (opsional) --------------------
    print()
    print("--- Smoke test DeepSeek (native tool calling) ---")
    ds = DeepSeekProvider()
    if not ds.config.api_key:
        print("[SKIP] DEEPSEEK_API_KEY tidak tersedia; smoke test dilewati.")
        print("       (tidak fallback ke Ollama)")
    else:
        _smoke_test_deepseek(ds, executor)

    print()
    print("[OK] Integrasi native tool-calling orchestrator bekerja.")
    return 0


def _smoke_test_deepseek(ds: DeepSeekProvider, executor: ToolExecutor) -> None:
    """Smoke test nyata: orchestrator + DeepSeek + native tool calling."""
    print(f"provider : {ds.name}")
    print(f"model    : {ds.config.model}")
    print(f"base_url : {ds.config.base_url}")

    orch = AgentOrchestrator(use_continuous_loop=False, 
        provider=ds,
        executor=executor,
        max_iterations=4,
        system_prompt="Kamu coding agent. Gunakan tool bila perlu, lalu beri jawaban final.",
    )
    try:
        result = orch.run("Baca file app/calc.py menggunakan tool read_file, lalu sebutkan isinya.")
    except Exception as exc:  # noqa: BLE001 - laporkan apa adanya
        print(f"[smoke] gagal: {type(exc).__name__}: {exc}")
        return

    tool_calls = sum(
        1 for s in result.steps if s.get("action") is not None
    )
    observations = sum(
        1 for s in result.steps if s.get("observation") is not None
    )
    print(f"[smoke] status          : {result.status.value}")
    print(f"[smoke] iterations      : {result.iterations}")
    print(f"[smoke] tool calls      : {tool_calls}")
    print(f"[smoke] observations    : {observations}")
    print(f"[smoke] final result    : {result.result!r}")
    if tool_calls == 0:
        print("[smoke] DeepSeek TIDAK menghasilkan native tool call pada request ini.")


if __name__ == "__main__":
    raise SystemExit(main())
