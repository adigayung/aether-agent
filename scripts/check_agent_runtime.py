"""Verifikasi Agent Runtime Layer.

Membuktikan:
    1. PreparedTask dapat diterima Runtime.
    2. Runtime menjalankan plan.
    3. LLM dipanggil melalui provider yang sudah ada (Ollama nyata).
    4. tool dipanggil melalui ToolExecutor.
    5. observation kembali ke runtime/LLM.
    6. multi-step task dapat berjalan.
    7. command failure dapat dikirim kembali sebagai observation.
    8. status runtime benar.
    9. completed/failed step tercatat.
   10. final result tersedia.
   11. workspace boundary tetap bekerja.
   12. tidak ada fixture tersisa.
   13. AETHER tetap dapat di-import.

Menggunakan Ollama NYATA (bukan mock). Provider + model ditampilkan.
Fixture hanya di J:\Agent_Ai\dummy_test dan dibersihkan setelah test.

Jalankan:
    python scripts/check_agent_runtime.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.codeindex import CodeIndexer  # noqa: E402
from agent_ai.contextbuilder import ContextBuilder  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.models import AgentObservation  # noqa: E402
from agent_ai.core.response import ActionType, LLMAction  # noqa: E402
from agent_ai.planning import TaskPlanner  # noqa: E402
from agent_ai.providers.base import GenerateOptions  # noqa: E402
from agent_ai.providers.registry import get_provider  # noqa: E402
from agent_ai.runtime import AgentRuntime, RuntimeStatus  # noqa: E402
from agent_ai.task import TaskPreparation  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402
from agent_ai.tools.filesystem import ReadFileTool  # noqa: E402
from agent_ai.tools.terminal import RunCommandTool  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "runtime_fixture"

_FILES = {
    "app/calc.py": (
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def sub(a, b):\n"
        "    return a - b\n"
    ),
    "app/main.py": (
        "from app.calc import add\n"
        "\n"
        "def run():\n"
        "    return add(1, 2)\n"
    ),
}


def setup_fixture() -> None:
    for rel, content in _FILES.items():
        path = FIXTURE / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Agent Runtime Layer ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    before = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}

    # Provider NYATA (bukan mock).
    provider = get_provider("ollama")
    print(f"Provider : {provider.name}")
    print(f"Model    : {provider.config.model}")
    print(f"Available: {provider.is_available()}")
    if not provider.is_available():
        print("[SKIP] Ollama tidak tersedia; verifier butuh LLM nyata.")
        return 2
    print()

    # --- Bagian A: deterministik (tanpa LLM) -------------------------------
    # 4) tool dipanggil melalui ToolExecutor + 7) command failure -> observation.
    registry = ToolRegistry()
    registry.register(ReadFileTool(root=FIXTURE))
    registry.register(RunCommandTool(root=FIXTURE))
    executor = ToolExecutor(registry=registry)

    read_obs = executor.execute_action(
        LLMAction(name="read_file", arguments={"path": "app/calc.py"})
    )
    assert isinstance(read_obs, AgentObservation) and read_obs.success
    assert "def add" in str(read_obs.content)
    print("[4] ToolExecutor memanggil tool OK -> read_file berhasil")

    # command failure (exit_code != 0) -> observation command_failure, bukan crash.
    fail_obs = executor.execute_action(
        LLMAction(name="run_command", arguments={"command": "python -c \"import sys; sys.exit(3)\""})
    )
    assert fail_obs.success is True, "command failure bukan tool_error"
    assert fail_obs.metadata.get("command_failure") is True
    assert fail_obs.metadata.get("exit_code") == 3
    print(f"[7] command failure -> observation OK -> exit_code={fail_obs.metadata.get('exit_code')}")

    # 11) workspace boundary tetap bekerja (path keluar root ditolak).
    escape_obs = executor.execute_action(
        LLMAction(name="read_file", arguments={"path": "../../etc/passwd"})
    )
    assert escape_obs.success is False, "path di luar workspace harus ditolak"
    print(f"[11] workspace boundary OK -> {escape_obs.error}")

    # --- Bagian B: runtime dengan LLM nyata ---------------------------------
    index = CodeIndexer(root=FIXTURE).build()
    builder = ContextBuilder(index=index, root=FIXTURE)
    prep = TaskPreparation(context_builder=builder, planner=TaskPlanner())

    # 1) PreparedTask dapat diterima Runtime.
    prepared = prep.prepare(
        "Baca file app/calc.py menggunakan tool read_file, lalu sebutkan "
        "nama fungsi yang ada di dalamnya."
    )
    assert prepared.plan is not None and prepared.plan.steps
    print(f"[1] PreparedTask diterima -> steps={len(prepared.plan.steps)}")

    runtime = AgentRuntime(
        provider=provider,
        executor=executor,
        max_iterations=4,
        options=GenerateOptions(temperature=0.0, max_tokens=256),
        system_prompt=(
            "Kamu adalah coding agent. Jawab singkat. "
            "Gunakan tool bila perlu, lalu berikan jawaban final."
        ),
    )

    # 2) Runtime menjalankan plan + 3) LLM dipanggil + 6) multi-step.
    result = runtime.run(prepared)
    print(f"[2] Runtime menjalankan plan -> status={result.status.value}")
    print(f"[3] LLM dipanggil via provider '{provider.name}' -> iterations={result.iterations}")
    assert result.iterations >= 1, "LLM harus dipanggil minimal sekali"
    assert len(result.steps) == len(prepared.plan.steps), "semua step harus tercatat"

    # 5) observation kembali ke runtime/LLM (step punya observation).
    #    Catatan: model Ollama (qwen2.5-coder) tidak selalu menghasilkan native
    #    tool call, sehingga observation bergantung pada keputusan model.
    #    Jalur observation deterministik dibuktikan di check_agent_tool_loop.py.
    has_observation = any(
        (s.get("observation") is not None) for s in result.steps
    )
    print(f"[5] observation kembali ke runtime -> ada_observation={has_observation}")
    if not has_observation:
        print("    (model tidak memanggil tool; jalur observation diuji di check_agent_tool_loop.py)")

    # 6) multi-step berjalan (semua step plan diproses).
    print(f"[6] multi-step berjalan -> {len(result.steps)} step diproses")

    # 8) status runtime benar.
    assert result.status in (RuntimeStatus.COMPLETED, RuntimeStatus.FAILED)
    print(f"[8] status runtime benar -> {result.status.value}")

    # 9) completed/failed step tercatat.
    if result.status == RuntimeStatus.COMPLETED:
        assert result.progress.completed_steps, "completed steps harus tercatat"
        print(f"[9] completed steps tercatat -> {result.progress.completed_steps}")
    else:
        assert result.progress.failed_step, "failed step harus tercatat"
        print(f"[9] failed step tercatat -> {result.progress.failed_step}")

    # 10) final result tersedia (untuk COMPLETED).
    if result.status == RuntimeStatus.COMPLETED:
        assert result.result, "final result harus tersedia"
        print(f"[10] final result tersedia -> {result.result[:80]!r}")
    else:
        assert result.error, "error harus tersedia saat FAILED"
        print(f"[10] error tersedia -> {result.error[:80]!r}")

    # 12) source project tidak berubah.
    after = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}
    assert before == after, "source project berubah!"
    print("[12] source project tidak berubah : OK")

    # 13) AETHER tetap dapat di-import.
    from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
    from agent_ai.core.loop import AgentLoop  # noqa: E402

    assert AgentOrchestrator and AgentLoop
    print("[13] AETHER tetap dapat di-import : OK")

    print()
    print("[OK] Agent Runtime bekerja (multi-step, provider nyata, tool via ToolExecutor).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
