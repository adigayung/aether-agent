"""Verifikasi normalized LLM Action/Response protocol.

Menguji: text response, satu tool call, multiple tool calls, final response,
dan serialization/introspection.

Jalankan:
    python scripts/check_llm_response.py
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
    FinishReason,
    LLMAction,
    LLMResponse,
)


def main() -> int:
    print("=== Verifikasi LLM Response Protocol ===")

    # 1) Response text biasa (tanpa tool call).
    text_resp = LLMResponse(
        text="Halo, ada yang bisa dibantu?",
        finish_reason=FinishReason.STOP,
        provider="ollama",
        model="qwen2.5-coder:7b",
    )
    print(f"text response  -> has_tool_calls={text_resp.has_tool_calls}, is_final={text_resp.is_final}")
    assert text_resp.is_final and not text_resp.has_tool_calls
    print()

    # 2) Satu tool call.
    one = LLMResponse(
        text="",
        actions=[LLMAction(name="read_file", arguments={"path": "src/agent_ai/tools/base.py"})],
        finish_reason=FinishReason.TOOL_CALLS,
    )
    print(f"one tool call  -> {one.tool_calls()[0].name} args={one.tool_calls()[0].arguments}")
    assert one.has_tool_calls and not one.is_final
    print()

    # 3) Multiple tool calls.
    multi = LLMResponse(
        actions=[
            LLMAction(name="search_code", arguments={"query": "class BaseTool"}),
            LLMAction(name="list_files", arguments={"path": "src"}),
        ],
        finish_reason=FinishReason.TOOL_CALLS,
    )
    print(f"multi tool call-> {[a.name for a in multi.tool_calls()]}")
    assert len(multi.tool_calls()) == 2
    print()

    # 4) Final response (action bertipe FINAL).
    final = LLMResponse(
        text="Selesai.",
        actions=[LLMAction(name="final_answer", arguments={"answer": "Selesai."}, type=ActionType.FINAL)],
        finish_reason=FinishReason.STOP,
    )
    print(f"final response -> is_final={final.is_final}, actions={len(final.actions)}")
    assert final.is_final
    print()

    # 5) Serialization / introspection.
    print("to_dict() (tanpa raw):")
    print(f"  {multi.to_dict()}")
    print("to_dict(include_raw=True) menyertakan raw:")
    raw_resp = LLMResponse(text="x", raw={"secret": "SHOULD_NOT_LEAK"}, finish_reason=FinishReason.STOP)
    print(f"  keys default = {list(raw_resp.to_dict().keys())}")
    print(f"  keys +raw    = {list(raw_resp.to_dict(include_raw=True).keys())}")
    assert "raw" not in raw_resp.to_dict()
    assert "raw" in raw_resp.to_dict(include_raw=True)
    print()

    print("[OK] LLM Response Protocol (text/tool call/multi/final/serialization) bekerja.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
