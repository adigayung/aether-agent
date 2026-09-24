"""Diagnostic: aliran context per round pada continuous loop.

Membuktikan bahwa hasil tool (read_file) tidak "bocor"/hilang antara
ConversationHistory dan FINAL provider request, dan bahwa jendela kerja
terbaru dikirim UTUH (bukan ringkasan lossy) walau kepala (system + env +
Bible + task) sudah melampaui anggaran.

Skenario meniru laporan bug: Agent membaca data layer (cache/migration/
storage/transaction) lalu MENGULANG read_file yang sama di beberapa round.

Jalankan (arg ke-1 = context budget token):
    python scripts/diagnose_context_flow.py 4000
    python scripts/diagnose_context_flow.py 200000   # tanpa compaction
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
)
from agent_ai.tools.registry import build_registry  # noqa: E402

FILES = [
    "betrayer/data/cache.py",
    "betrayer/data/migration.py",
    "betrayer/data/storage.py",
    "betrayer/data/transaction.py",
]


def _write_project(root: Path) -> None:
    base = root / "betrayer" / "data"
    base.mkdir(parents=True, exist_ok=True)
    for idx, rel in enumerate(FILES):
        lines = [f"# {rel} module"] + [
            f"def func_{i}():  # marker_{idx}_{i}\n    return {i} + {idx}"
            for i in range(1, 41)
        ]
        (root / rel).write_text("\n".join(lines), encoding="utf-8")


def _tool_call(call_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _tool_turn(text: str, calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"content": text, "tool_calls": calls},
                "finish_reason": "tool_calls",
            }
        ]
    }


def _final_turn(text: str) -> Dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class RecordingProvider(OpenAICompatibleProvider):
    name = "scripted"

    def __init__(self, script: List[Dict[str, Any]]) -> None:
        self.config = SimpleNamespace(model="scripted-model")
        self.script = list(script)
        self.calls = 0
        self.requests: List[List[Dict[str, Any]]] = []

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Any]] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[List[Any]] = None,
        tool_choice: Optional[Any] = None,
    ) -> GenerateResult:
        self.requests.append(
            [dict(m) if isinstance(m, dict) else m for m in (messages or [])]
        )
        idx = self.calls
        self.calls += 1
        raw = self.script[idx] if idx < len(self.script) else _final_turn("(default)")
        return GenerateResult(
            text="", model="scripted-model", provider=self.name, raw=raw
        )


def _visible_paths(messages: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for message in messages:
        blob = json.dumps(message, ensure_ascii=False, default=str)
        for rel in FILES:
            if rel in blob:
                counts[rel] = counts.get(rel, 0) + 1
    return counts


def _roles(messages: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for message in messages:
        role = message.get("role", "?")
        counts[role] = counts.get(role, 0) + 1
    return counts


def _read_paths_in_messages(messages: List[Dict[str, Any]]) -> List[str]:
    paths: List[str] = []
    for message in messages:
        if message.get("role") != "tool" or message.get("name") != "read_file":
            continue
        content = message.get("content") or ""
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            if "[tool result dipadatkan" in content and "path:" in content:
                for line in content.splitlines():
                    if line.startswith("path:"):
                        paths.append("(compacted) " + line[5:].strip())
            continue
        if isinstance(data, dict) and data.get("path"):
            kind = "content" if "content" in data else "stub"
            paths.append(f"{kind}:{data['path']}")
    return paths


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_project(root)

        registry = build_registry(root=root)
        assert registry.read_cache is not None

        script: List[Dict[str, Any]] = []
        call_n = 0
        for rnd in range(3):
            calls = []
            for rel in FILES:
                call_n += 1
                calls.append(_tool_call(f"r{call_n}", "read_file", {"path": rel}))
            script.append(_tool_turn(f"baca data layer round {rnd}", calls))
        for rnd in range(5):
            rel = FILES[rnd % len(FILES)]
            call_n += 1
            script.append(
                _tool_turn(
                    f"cek lagi {rel}",
                    [_tool_call(f"r{call_n}", "read_file", {"path": rel})],
                )
            )
        script.append(_final_turn("selesai"))

        provider = RecordingProvider(script)
        budget = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
        big_env = "# ENVIRONMENT\n" + ("knowledge-line " + ("X" * 80) + "\n") * 120

        orchestrator = AgentOrchestrator(
            provider=provider,
            executor=ToolExecutor(registry=registry),
            options=GenerateOptions(model="scripted-model"),
            system_prompt="agent system prompt",
            use_continuous_loop=True,
            environment_context=big_env,
            context_budget_tokens=budget,
        )
        print(f"[budget={budget}]")
        print("[effective_budget]", orchestrator._context_budget_tokens())
        print("[tool_overhead]", orchestrator._tool_definitions_tokens(orchestrator._tool_definitions()))

        print("=" * 78)
        print("DIAGNOSTIC: aliran context per round")
        print("=" * 78)
        result = orchestrator.run("baca dan pakai data layer")

        for iteration, request in enumerate(provider.requests):
            roles = _roles(request)
            vis = _visible_paths(request)
            reads = _read_paths_in_messages(request)
            approx = sum(len(json.dumps(m, default=str)) for m in request) // 4
            print(
                f"\nRound {iteration + 1}: messages={len(request)} approx_tokens~{approx}"
            )
            print(f"  roles       : {roles}")
            print(f"  paths_seen  : {vis}")
            print(f"  read_results: {reads}")

        print("\n" + "=" * 78)
        print(f"status={result.status} iterations={result.iterations}")
        print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
