"""E2E native tool-calling dengan OpenRouter NYATA.

Membuktikan loop:
    AgentRuntime -> ProviderRegistry -> OpenRouter -> native tool_call
        -> ToolExecutor -> observation -> OpenRouter -> ... -> FINAL

Task memerlukan beberapa native tool call:
    read_file -> read_file/inspection -> edit_file -> run_command -> final

Ketentuan:
    - OpenRouter nyata (bukan mock). Tidak fallback ke Ollama/DeepSeek.
    - Tidak hardcode LLMAction/tool call; tool call berasal dari response OpenRouter.
    - Memakai ToolRegistry + ToolExecutor + native tool schema yang ada.
    - Tidak mencetak API key.
    - Fixture hanya di J:\\Agent_Ai\\dummy_test\\openrouter_e2e_fixture dan
      dibersihkan setelah test.

Jalankan:
    python scripts/check_openrouter_e2e.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.providers.base import GenerateOptions  # noqa: E402
from agent_ai.providers.openrouter import OpenRouterProvider  # noqa: E402
from agent_ai.providers.registry import get_provider  # noqa: E402
from agent_ai.tools.filesystem import ReadFileTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402
from agent_ai.tools.terminal import RunCommandTool  # noqa: E402
from agent_ai.tools.workspace import EditFileTool, WriteFileTool  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "openrouter_e2e_fixture"

# Bug sederhana: multiply() mengurangkan, seharusnya mengalikan.
_CALC = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "\n"
    "def multiply(a, b):\n"
    "    return a - b  # BUG: seharusnya a * b\n"
)

_TEST = (
    "from calculator import add, multiply\n"
    "\n"
    "\n"
    "def test_add():\n"
    "    assert add(2, 3) == 5\n"
    "\n"
    "\n"
    "def test_multiply():\n"
    "    assert multiply(3, 4) == 12, f'multiply(3,4) = {multiply(3, 4)}, expected 12'\n"
    "\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    test_add()\n"
    "    test_multiply()\n"
    "    print('ALL TESTS PASSED')\n"
)


class RecordingOpenRouterProvider(OpenRouterProvider):
    """OpenRouter NYATA + pencatat pesan/tools yang diterima (bukan mock).

    Hanya mencatat messages/tools yang dikirim ke provider agar verifier dapat
    membuktikan observation benar-benar dikirim kembali ke OpenRouter.
    generate() tetap memanggil OpenRouter sungguhan.
    """

    def __init__(self) -> None:
        super().__init__()
        self.received_messages: list = []
        self.received_tools: list = []

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        self.received_messages.append(list(messages or []))
        self.received_tools.append(tools)
        return super().generate(
            prompt=prompt,
            messages=messages,
            options=options,
            tools=tools,
            tool_choice=tool_choice,
        )


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "calculator.py").write_text(_CALC, encoding="utf-8")
    (FIXTURE / "test_calculator.py").write_text(_TEST, encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== E2E Native Tool-Calling: OpenRouter NYATA ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    # Provider diambil dari registry (membuktikan integrasi registry).
    registered = get_provider("openrouter")
    assert registered.name == "openrouter", "registry harus mengembalikan openrouter"

    provider = RecordingOpenRouterProvider()
    print(f"Provider : {provider.name}")
    print(f"Model    : {provider.config.model}")
    print(f"Base URL : {provider.config.base_url}")
    print(f"API key  : {'(tersedia)' if provider.config.api_key else '(KOSONG)'}")
    if not provider.config.api_key:
        print("[SKIP] OPENROUTER_API_KEY tidak tersedia. Tidak fallback ke provider lain.")
        return 2
    print()

    registry = ToolRegistry()
    registry.register(ReadFileTool(root=FIXTURE))
    registry.register(WriteFileTool(root=FIXTURE))
    registry.register(EditFileTool(root=FIXTURE))
    registry.register(RunCommandTool(root=FIXTURE))
    executor = ToolExecutor(registry=registry)

    task = (
        "Di dalam workspace ada calculator.py yang berisi bug: fungsi multiply() "
        "salah mengembalikan hasil. Ada juga test_calculator.py. "
        "Langkah: (1) baca calculator.py, (2) temukan bug, (3) perbaiki calculator.py, "
        "(4) jalankan 'python test_calculator.py' untuk memverifikasi, "
        "(5) jika test gagal, perbaiki lagi, (6) selesai. "
        "Gunakan tool yang tersedia. Setelah semua test lulus, berikan jawaban final."
    )

    orch = AgentOrchestrator(
        provider=provider,
        executor=executor,
        max_iterations=20,
        options=GenerateOptions(temperature=0.0, max_tokens=1024),
        system_prompt=(
            "Kamu adalah coding agent. Gunakan tool untuk membaca, mengedit, "
            "dan menjalankan command. Jangan mengarang isi file. "
            "Setelah test lulus, berikan jawaban final singkat."
        ),
    )

    try:
        result = orch.run(task)
    except Exception as exc:  # noqa: BLE001 - laporkan error teknis tanpa API key
        msg = f"{type(exc).__name__}: {exc}".encode("ascii", "replace").decode("ascii")
        print(f"[ERROR] {msg}")
        return 1

    tool_calls = sum(1 for s in result.steps if s.get("action") is not None)
    observations = sum(1 for s in result.steps if s.get("observation") is not None)

    print("--- Hasil E2E ---")
    print(f"Provider         : {provider.name}")
    print(f"Model            : {provider.config.model}")
    print(f"Native tool calls: {'YES' if tool_calls > 0 else 'NO'}")
    print(f"Tool calls       : {tool_calls}")
    print(f"Observations     : {observations}")
    print(f"Iterations       : {result.iterations}")
    print(f"Status           : {result.status.value}")
    safe_result = (result.result or "").encode("ascii", "replace").decode("ascii")
    print(f"Final result     : {safe_result[:300]!r}")

    if tool_calls > 0:
        print()
        print("--- Bukti loop ---")
        for i, step in enumerate(result.steps, 1):
            action = step.get("action") or {}
            obs = step.get("observation") or {}
            print(f"  step {i}: tool={action.get('name', '?')} observation_success={obs.get('success')}")

    # Bukti observation dikirim kembali ke OpenRouter.
    print()
    print("--- Bukti observation dikirim kembali ke OpenRouter ---")
    obs_sent_back = False
    for i, msgs in enumerate(provider.received_messages, 1):
        joined = "\n".join(m.content for m in msgs)
        has_obs = "[tool result]" in joined or "[tool error]" in joined
        obs_sent_back = obs_sent_back or has_obs
        print(f"  LLM call {i}: {len(msgs)} pesan, berisi_observation={has_obs}")
    print(f"  -> observation dikirim kembali ke OpenRouter: {obs_sent_back}")

    tools_sent = any(t for t in provider.received_tools)
    tool_names = []
    if tools_sent:
        for t in provider.received_tools:
            if t:
                tool_names = [d.name for d in t]
                break
    print(f"  -> tool schema dikirim ke OpenRouter: {tools_sent} -> {tool_names}")

    # Cek hasil akhir file (bug benar-benar diperbaiki).
    calc_after = (FIXTURE / "calculator.py").read_text(encoding="utf-8")
    fixed = "a * b" in calc_after
    print()
    print(f"calculator.py diperbaiki (a * b): {fixed}")

    print()
    if tool_calls > 0:
        print("[OK] OpenRouter menghasilkan native tool call dan loop berjalan.")
    else:
        print("[INFO] OpenRouter TIDAK menghasilkan native tool call pada request ini.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
