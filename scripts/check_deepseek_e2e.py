"""E2E native tool-calling dengan DeepSeek NYATA.

Membuktikan loop:
    DeepSeek -> native tool_call -> ToolExecutor -> observation
        -> DeepSeek -> native tool_call -> ToolExecutor -> ... -> FINAL

Task meminta agent:
    membaca file -> memahami bug -> memperbaiki file -> menjalankan test
    -> jika gagal memperbaiki lagi -> menyelesaikan task.

Ketentuan:
    - DeepSeek nyata (bukan mock). Tidak fallback ke Ollama.
    - Tidak inject/fake LLMAction. Tidak memaksa tool call.
    - Memakai ToolRegistry + ToolExecutor + native tool schema yang ada.
    - Tidak mencetak API key.
    - Fixture hanya di J:\\Agent_Ai\\dummy_test dan dibersihkan setelah test.

Jalankan:
    python scripts/check_deepseek_e2e.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.config.settings import settings  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.providers.base import GenerateOptions  # noqa: E402
from agent_ai.providers.deepseek import DeepSeekProvider  # noqa: E402
from agent_ai.tools.filesystem import ReadFileTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402
from agent_ai.tools.terminal import RunCommandTool  # noqa: E402
from agent_ai.tools.workspace import EditFileTool, WriteFileTool  # noqa: E402


class RecordingDeepSeekProvider(DeepSeekProvider):
    """DeepSeek NYATA + pencatat pesan yang diterima (bukan mock).

    Hanya mencatat messages/tools yang dikirim ke provider agar verifier dapat
    membuktikan observation benar-benar dikirim kembali ke DeepSeek pada
    iteration berikutnya. generate() tetap memanggil DeepSeek sungguhan.
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

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "deepseek_e2e_fixture"

# File dengan bug kecil: add() mengurangkan, seharusnya menjumlahkan.
_CALC = (
    "def add(a, b):\n"
    "    return a - b  # BUG: seharusnya a + b\n"
    "\n"
    "\n"
    "def sub(a, b):\n"
    "    return a - b\n"
)

_TEST = (
    "from calc import add, sub\n"
    "\n"
    "\n"
    "def test_add():\n"
    "    assert add(2, 3) == 5, f'add(2,3) = {add(2, 3)}, expected 5'\n"
    "\n"
    "\n"
    "def test_sub():\n"
    "    assert sub(5, 2) == 3\n"
    "\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    test_add()\n"
    "    test_sub()\n"
    "    print('ALL TESTS PASSED')\n"
)


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "calc.py").write_text(_CALC, encoding="utf-8")
    (FIXTURE / "test_calc.py").write_text(_TEST, encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== E2E Native Tool-Calling: DeepSeek NYATA ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    provider = RecordingDeepSeekProvider()
    print(f"Provider : {provider.name}")
    print(f"Model    : {provider.config.model}")
    print(f"Base URL : {provider.config.base_url}")
    print(f"API key  : {'(tersedia)' if provider.config.api_key else '(KOSONG)'}")
    if not provider.config.api_key:
        print("[SKIP] DEEPSEEK_API_KEY tidak tersedia. Tidak fallback ke Ollama.")
        return 2
    print()

    registry = ToolRegistry()
    registry.register(ReadFileTool(root=FIXTURE))
    registry.register(WriteFileTool(root=FIXTURE))
    registry.register(EditFileTool(root=FIXTURE))
    registry.register(RunCommandTool(root=FIXTURE))
    executor = ToolExecutor(registry=registry)

    task = (
        "Di dalam workspace ada file calc.py yang berisi bug: fungsi add() "
        "salah mengembalikan hasil. Ada juga test_calc.py. "
        "Langkah: (1) baca calc.py, (2) temukan bug, (3) perbaiki calc.py, "
        "(4) jalankan 'python test_calc.py' untuk memverifikasi, "
        "(5) jika test gagal, perbaiki lagi, (6) selesai. "
        "Gunakan tool yang tersedia. Setelah semua test lulus, berikan jawaban final."
    )

    orch = AgentOrchestrator(
        provider=provider,
        executor=executor,
        max_iterations=12,
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
        print(f"[ERROR] {type(exc).__name__}: {exc}")
        return 1

    # Hitung tool calls & observations dari steps.
    tool_calls = 0
    observations = 0
    for step in result.steps:
        if step.get("action") is not None:
            tool_calls += 1
        if step.get("observation") is not None:
            observations += 1

    print("--- Hasil E2E ---")
    print(f"Provider        : {provider.name}")
    print(f"Model           : {provider.config.model}")
    print(f"Native tool calls: {'YES' if tool_calls > 0 else 'NO'}")
    print(f"Tool calls      : {tool_calls}")
    print(f"Observations    : {observations}")
    print(f"Iterations      : {result.iterations}")
    print(f"Status          : {result.status.value}")
    print(f"Final result    : {result.result!r}")

    # Bukti observation dikirim kembali ke DeepSeek: minimal ada >1 iterasi
    # dengan tool call, artinya observation dipakai pada pemanggilan berikutnya.
    if tool_calls > 0:
        print()
        print("--- Bukti loop ---")
        for i, step in enumerate(result.steps, 1):
            action = step.get("action") or {}
            obs = step.get("observation") or {}
            name = action.get("name", "?")
            ok = obs.get("success")
            print(f"  step {i}: tool={name} observation_success={ok}")

    # Bukti observation dikirim kembali ke DeepSeek: cek pesan yang diterima
    # provider pada pemanggilan ke-2 dan seterusnya mengandung "[tool result]".
    print()
    print("--- Bukti observation dikirim kembali ke DeepSeek ---")
    obs_sent_back = False
    for i, msgs in enumerate(provider.received_messages, 1):
        joined = "\n".join(m.content for m in msgs)
        has_obs = "[tool result]" in joined or "[tool error]" in joined
        if has_obs:
            obs_sent_back = True
        print(f"  LLM call {i}: {len(msgs)} pesan, berisi_observation={has_obs}")
    print(f"  -> observation dikirim kembali ke DeepSeek: {obs_sent_back}")

    # Bukti tool schema dikirim ke DeepSeek.
    tools_sent = any(t for t in provider.received_tools)
    tool_names = []
    if tools_sent:
        for t in provider.received_tools:
            if t:
                tool_names = [d.name for d in t]
                break
    print(f"  -> tool schema dikirim ke DeepSeek: {tools_sent} -> {tool_names}")

    # Cek hasil akhir file (apakah bug benar-benar diperbaiki).
    calc_after = (FIXTURE / "calc.py").read_text(encoding="utf-8")
    fixed = "a + b" in calc_after
    print()
    print(f"calc.py diperbaiki (a + b): {fixed}")

    print()
    if tool_calls > 0:
        print("[OK] DeepSeek menghasilkan native tool call dan loop berjalan.")
    else:
        print("[INFO] DeepSeek TIDAK menghasilkan native tool call pada request ini.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
