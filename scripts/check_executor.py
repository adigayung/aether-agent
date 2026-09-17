"""Verifikasi Tool Executor (bridge LLMResponse -> ToolRegistry -> AgentObservation).

Menguji:
    - LLMResponse berisi tool call read_file.
    - Executor menjalankan tool melalui ToolRegistry.
    - Hasil menjadi AgentObservation sukses.
    - Tool error menghasilkan AgentObservation gagal (bukan crash).
    - Tidak ada provider-specific logic di executor.

Jalankan:
    python scripts/check_executor.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core import (  # noqa: E402
    FinishReason,
    LLMAction,
    LLMResponse,
    ToolExecutor,
)
from agent_ai.core.models import AgentObservation  # noqa: E402


def main() -> int:
    print("=== Verifikasi Tool Executor ===")
    executor = ToolExecutor()
    print(f"Tool tersedia : {', '.join(executor.registry.list())}")
    print()

    # 1) LLMResponse berisi tool call read_file.
    response = LLMResponse(
        actions=[LLMAction(name="read_file", arguments={"path": "src/agent_ai/tools/base.py", "end_line": 3})],
        finish_reason=FinishReason.TOOL_CALLS,
    )
    print(f"LLMResponse tool_calls -> {[a.name for a in response.tool_calls()]}")

    # 2) Executor menjalankan tool melalui ToolRegistry -> observation sukses.
    observations = executor.execute_response(response)
    obs = observations[0]
    print(f"observation success = {obs.success}")
    print(f"observation tool    = {obs.metadata.get('tool')}")
    print(f"content preview     = {str(obs.content)[:80]}...")
    assert isinstance(obs, AgentObservation)
    assert obs.success is True
    assert obs.metadata.get("tool") == "read_file"
    assert obs.content and "content" in obs.content
    print()

    # 3) Tool error -> observation gagal (bukan crash).
    bad = LLMResponse(
        actions=[LLMAction(name="read_file", arguments={"path": "tidak/ada/file.py"})],
        finish_reason=FinishReason.TOOL_CALLS,
    )
    bad_obs = executor.execute_response(bad)[0]
    print(f"error observation success = {bad_obs.success}")
    print(f"error observation error   = {bad_obs.error}")
    assert bad_obs.success is False
    assert bad_obs.error
    print()

    # 4) Tool tidak terdaftar -> observation gagal.
    unknown = LLMResponse(
        actions=[LLMAction(name="tool_tidak_ada", arguments={})],
        finish_reason=FinishReason.TOOL_CALLS,
    )
    unknown_obs = executor.execute_response(unknown)[0]
    print(f"unknown tool success = {unknown_obs.success}, error = {unknown_obs.error}")
    assert unknown_obs.success is False
    print()

    # 5) Tidak ada provider-specific logic di executor.
    # Cek: tidak ada import provider konkret dan tidak memakai requests.
    src = Path("src/agent_ai/core/executor.py").read_text(encoding="utf-8")
    import_lines = [l.strip().lower() for l in src.splitlines() if l.strip().startswith(("import ", "from "))]
    for forbidden in ("ollama", "deepseek", "openai", "requests"):
        assert not any(forbidden in l for l in import_lines), f"executor meng-import '{forbidden}'"
    print("provider-specific check -> executor tidak meng-import provider konkret/requests")
    print()

    print("[OK] Tool Executor (LLMResponse -> ToolRegistry -> AgentObservation) bekerja.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

