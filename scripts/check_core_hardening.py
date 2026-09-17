"""Verifikasi penguatan AETHER Core.

Menguji:
    1. command_failure vs tool_error dibedakan eksplisit.
    2. CodingTask abstraction (status, iteration, result, error, completion).
    3. edit ambiguity tetap ditolak (tidak permisif).

Jalankan:
    python scripts/check_core_hardening.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core.coding import CodingTask  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.core.response import ActionType, FinishReason, LLMAction, LLMResponse  # noqa: E402
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402
from agent_ai.tools import EditFileTool, ToolError, WriteFileTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402
from agent_ai.tools.terminal import RunCommandTool  # noqa: E402

PY = sys.executable


class ScriptedProvider(BaseProvider):
    name = "scripted"

    def __init__(self, responses):
        self._responses = list(responses)

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        item = self._responses.pop(0) if self._responses else "FINAL: selesai"
        return GenerateResult(text=item if isinstance(item, str) else "", model="m", provider=self.name, raw={"item": item})

    def normalize_response(self, result):
        item = result.raw.get("item")
        if isinstance(item, dict) and "tool" in item:
            return LLMResponse(
                text="",
                actions=[LLMAction(name=item["tool"], arguments=item.get("arguments", {}))],
                finish_reason=FinishReason.TOOL_CALLS,
                provider=self.name,
            )
        return LLMResponse(text=result.text or "", actions=[], finish_reason=FinishReason.STOP, provider=self.name)


def main() -> int:
    print("=== Verifikasi Penguatan AETHER Core ===")
    ws = Path(tempfile.mkdtemp(prefix="hard_ws_"))
    try:
        # Registry khusus dengan root = ws.
        reg = ToolRegistry()
        reg.register(RunCommandTool(root=ws))
        reg.register(WriteFileTool(root=ws))
        reg.register(EditFileTool(root=ws))
        executor = ToolExecutor(registry=reg)

        # 1) command_failure vs tool_error.
        # 1a) command_failure: command jalan, exit_code != 0.
        obs = executor.execute_action(
            LLMAction(name="run_command", arguments={"command": f'"{PY}" -c "import sys; sys.exit(2)"'})
        )
        print(f"command_failure: success={obs.success} metadata={obs.metadata}")
        assert obs.success is True, "tool berhasil dijalankan"
        assert obs.metadata.get("command_failure") is True
        assert obs.metadata.get("exit_code") == 2
        assert obs.content["outcome"] == "command_failure"

        # 1b) tool_error: invalid input (command kosong) -> ToolValidationError.
        obs2 = executor.execute_action(LLMAction(name="run_command", arguments={"command": ""}))
        print(f"tool_error     : success={obs2.success} metadata={obs2.metadata} error={obs2.error!r}")
        assert obs2.success is False
        assert obs2.metadata.get("tool_error") is True
        assert "command_failure" not in obs2.metadata

        # 1c) command sukses -> outcome success, tanpa flag failure.
        obs3 = executor.execute_action(
            LLMAction(name="run_command", arguments={"command": f'"{PY}" -c "print(1)"'})
        )
        assert obs3.success and obs3.content["outcome"] == "success"
        assert "command_failure" not in obs3.metadata
        print("command sukses : outcome=success, tanpa flag failure -> OK")
        print()

        # 2) CodingTask abstraction.
        task = CodingTask(request="Perbaiki bug")
        assert task.status == AgentStatus.RUNNING and not task.is_complete
        task.update_progress(3)
        task.complete(result="ok", iterations=6)
        print(f"CodingTask done : {task.to_dict()}")
        assert task.is_complete and task.success and task.iterations == 6 and task.result == "ok"

        task2 = CodingTask(request="gagal").start().fail(error="boom", iterations=2)
        assert task2.is_complete and not task2.success and task2.error == "boom"
        print("CodingTask fail : OK")
        print()

        # 2b) Integrasi minimal: run_coding_task via orchestrator.
        provider = ScriptedProvider(["FINAL: selesai coding"])
        orch = AgentOrchestrator(provider=provider, executor=executor)
        coding = orch.run_coding_task("task coding")
        print(f"run_coding_task : status={coding.status.value} result={coding.result!r}")
        assert coding.success and coding.result == "FINAL: selesai coding"
        print()

        # 3) edit ambiguity tetap ditolak (tidak permisif).
        (ws / "dup.txt").write_text("aa aa", encoding="utf-8")
        obs_edit = executor.execute_action(
            LLMAction(name="edit_file", arguments={"path": "dup.txt", "old_text": "aa", "new_text": "bb"})
        )
        print(f"edit ambigu : success={obs_edit.success} error={obs_edit.error!r}")
        assert obs_edit.success is False
        assert obs_edit.metadata.get("tool_error") is True
        assert "ambigu" in (obs_edit.error or "").lower()
        # File tidak berubah (tidak ada fuzzy replacement).
        assert (ws / "dup.txt").read_text(encoding="utf-8") == "aa aa"
        print("file tidak berubah (no fuzzy) -> OK")
        print()

        print("[OK] Penguatan core bekerja (command_failure vs tool_error, CodingTask, edit ketat).")
        return 0
    finally:
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
