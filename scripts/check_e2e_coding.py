"""Uji coding end-to-end AETHER.

Alur: LLM -> read_file -> analisis -> edit_file -> run_command -> observasi
      -> perbaikan -> DONE.

Menggunakan provider scripted (tanpa API) yang mensimulasikan keputusan LLM
berdasarkan observation sebelumnya, sehingga loop benar-benar digerakkan oleh
AgentOrchestrator + ToolExecutor + ToolRegistry (bukan hardcode hasil).

Fixture project dummy dibuat di J:\Agent_Ai\dummy_test (workspace testing
terisolasi, BUKAN bagian source AETHER) dan dibersihkan setelah test.

Jalankan:
    python scripts/check_e2e_coding.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.response import FinishReason, LLMAction, LLMResponse  # noqa: E402
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402

# Workspace testing terisolasi (BUKAN bagian source AETHER).
DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
CALC_DIR = DUMMY_ROOT / "calculator"
CALC_REL = "dummy_test/calculator/calculator.py"
TEST_REL = "dummy_test/calculator/test_calculator.py"
PY = sys.executable

_CALCULATOR_SRC = '''"""Kalkulator sederhana (sengaja berisi bug untuk uji end-to-end)."""


def add(a, b):
    # BUG: seharusnya a + b
    return a - b


def subtract(a, b):
    return a - b


def multiply(a, b):
    return a * b


def divide(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b
'''

_TEST_SRC = '''"""Test untuk calculator (dijalankan via run_command)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from calculator import add, divide, multiply, subtract  # noqa: E402


def check(name, got, expected):
    if got != expected:
        raise AssertionError(f"{name}: got {got!r}, expected {expected!r}")


def main():
    check("add", add(2, 3), 5)
    check("subtract", subtract(5, 2), 3)
    check("multiply", multiply(4, 3), 12)
    check("divide", divide(10, 2), 5)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
'''


def setup_fixture() -> None:
    """Buat fixture project dummy di dummy_test/calculator."""
    CALC_DIR.mkdir(parents=True, exist_ok=True)
    (CALC_DIR / "calculator.py").write_text(_CALCULATOR_SRC, encoding="utf-8")
    (CALC_DIR / "test_calculator.py").write_text(_TEST_SRC, encoding="utf-8")


def teardown_fixture() -> None:
    """Bersihkan fixture setelah test."""
    shutil.rmtree(CALC_DIR, ignore_errors=True)


class CodingProvider(BaseProvider):
    """Provider scripted yang bereaksi terhadap observation (simulasi LLM)."""

    name = "coding-scripted"

    def __init__(self) -> None:
        self.step = 0
        self.trace = []  # (action, observation) untuk laporan

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        # Ambil observation terakhir dari history (pesan user terakhir).
        last = ""
        for m in reversed(messages or []):
            if m.role == "user":
                last = m.content
                break
        return GenerateResult(text=last, model="scripted", provider=self.name, raw={})

    def normalize_response(self, result):
        last = result.text or ""
        self.step += 1

        def tool(name, args):
            return LLMResponse(
                text="",
                actions=[LLMAction(name=name, arguments=args)],
                finish_reason=FinishReason.TOOL_CALLS,
                provider=self.name,
            )

        def final(text):
            return LLMResponse(
                text=text, actions=[], finish_reason=FinishReason.STOP, provider=self.name
            )

        # Step 1: baca file yang relevan.
        if self.step == 1:
            return tool("read_file", {"path": CALC_REL})

        # Step 2: baca test.
        if self.step == 2:
            return tool("read_file", {"path": TEST_REL})

        # Step 3: perbaikan PERTAMA (sengaja salah: ganti ke a * b).
        if self.step == 3:
            return tool(
                "edit_file",
                {
                    "path": CALC_REL,
                    "old_text": "    # BUG: seharusnya a + b\n    return a - b",
                    "new_text": "    # BUG: seharusnya a + b\n    return a * b",
                },
            )

        # Step 4: jalankan test (akan gagal).
        if self.step == 4:
            return tool(
                "run_command",
                {"command": f'"{PY}" {TEST_REL}'},
            )

        # Step 5: baca hasil test -> perbaikan KEDUA (benar: a + b).
        if self.step == 5:
            assert "AssertionError" in last or "add" in last, "test seharusnya gagal"
            return tool(
                "edit_file",
                {
                    "path": CALC_REL,
                    "old_text": "    # BUG: seharusnya a + b\n    return a * b",
                    "new_text": "    return a + b",
                },
            )

        # Step 6: jalankan test lagi (harus berhasil).
        if self.step == 6:
            return tool(
                "run_command",
                {"command": f'"{PY}" {TEST_REL}'},
            )

        # Step 7: verifikasi hasil test -> DONE.
        assert "ALL TESTS PASSED" in last, f"test seharusnya lulus, dapat: {last!r}"
        return final("Bug diperbaiki; semua test berhasil.")


def main() -> int:
    print("=== Uji Coding End-to-End AETHER ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    provider = CodingProvider()
    orchestrator = AgentOrchestrator(use_continuous_loop=False, provider=provider, max_iterations=12)

    result = orchestrator.run("Perbaiki bug pada calculator dan pastikan test berhasil.")

    print(f"status     : {result.status.value}")
    print(f"iterations : {result.iterations}")
    print(f"result     : {result.result!r}")
    print()

    print("--- urutan action -> observation ---")
    for i, step in enumerate(result.steps, 1):
        action = step.get("action") or {}
        obs = step.get("observation") or {}
        args = action.get("arguments", {})
        summary = args.get("path") or args.get("command") or ""
        print(f"{i}. {action.get('name')} ({summary})")
        if obs.get("success"):
            content = obs.get("content")
            if isinstance(content, dict):
                if "exit_code" in content:
                    print(f"   -> exit={content['exit_code']} success={content['success']}")
                elif "content" in content:
                    print(f"   -> read {content.get('total_lines')} lines")
                else:
                    print(f"   -> {content}")
            else:
                print(f"   -> {content}")
        else:
            print(f"   -> ERROR: {obs.get('error')}")
    print()

    # Verifikasi akhir.
    assert result.status == AgentStatus.DONE, "loop tidak selesai DONE"
    assert result.success
    calc = (CALC_DIR / "calculator.py").read_text(encoding="utf-8")
    assert "return a + b" in calc, "bug belum diperbaiki"
    # Pastikan add() benar (bukan a - b / a * b).
    add_body = calc.split("def add", 1)[1].split("def ", 1)[0]
    assert "return a + b" in add_body, "add() belum benar"

    # Pastikan benar-benar ada read_file, edit_file, run_command.
    names = [s["action"]["name"] for s in result.steps if s.get("action")]
    assert "read_file" in names and "edit_file" in names and "run_command" in names
    assert names.count("edit_file") >= 2, "harus ada perbaikan kedua setelah test gagal"

    print("[OK] Loop coding end-to-end berhasil (read -> edit -> test -> fix -> DONE).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
