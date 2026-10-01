"""E2E: token usage provider -> event provider_response -> Task Card.

Deterministik, offline (provider fake in-process). Membuktikan bahwa `usage`
yang dilaporkan provider mengalir sampai ke event `provider_response` (sumber
yang dibaca Task Card), dan provider yang TIDAK melaporkan usage membuat field
`usage` absen (UI -> "—").

Jalankan:
    python scripts/check_token_usage_e2e.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "token_usage_e2e"


def _make_provider(raw_usage: dict | None):
    from agent_ai.core.response import FinishReason, LLMResponse
    from agent_ai.providers.base import BaseProvider, GenerateResult

    class UsageProvider(BaseProvider):
        name = "usage-fake"

        def __init__(self) -> None:
            self.calls = 0

        def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
            self.calls += 1
            raw: dict = {}
            if raw_usage is not None and self.calls == 1:
                raw = {"usage": dict(raw_usage)}
            return GenerateResult(text="", provider=self.name, model="m", raw=raw)

        def is_available(self) -> bool:
            return True

        def normalize_response(self, result):
            if self.calls == 1:
                finish = FinishReason.TOOL_CALLS
            else:
                finish = FinishReason.STOP
            return LLMResponse(
                text="done" if self.calls > 1 else "",
                actions=[],
                finish_reason=finish,
                raw=result.raw,
                provider=self.name,
                model="m",
            )

    return UsageProvider()


def _run(provider, session_store, workspace: Path) -> list:
    """Jalankan orchestrator 1 task; kembalikan event provider_response."""
    from agent_ai.core.executor import ToolExecutor
    from agent_ai.core.orchestrator import AgentOrchestrator
    from agent_ai.tools.registry import build_registry

    events: list = []

    def sink(event_type, payload):
        events.append({"event_type": event_type, "payload": payload})

    registry = build_registry(root=str(workspace))
    executor = ToolExecutor(registry=registry)
    orch = AgentOrchestrator(
        provider=provider,
        executor=executor,
        event_sink=sink,
    )
    orch.run("katakan selesai tanpa tool")
    return [e for e in events if e["event_type"] == "provider_response"]


def _run_case(raw_usage, expected_total) -> str:
    from agent_ai.session.store import InMemorySessionStore

    shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "app.py").write_text("print('x')\n", encoding="utf-8")
    store = InMemorySessionStore()
    responses = _run(_make_provider(raw_usage), store, FIXTURE)
    assert responses, "harus ada event provider_response"
    # provider_response TERAKHIR (final) memuat usage.
    usage = responses[0]["payload"].get("usage")
    if expected_total is None:
        assert usage is None, f"provider tanpa usage -> usage harus absen, dapat {usage}"
    else:
        assert usage is not None, "provider melaporkan usage -> payload harus memuat usage"
        assert usage.get("total") == expected_total, usage
    return str(usage)


def main() -> int:
    print("=== E2E: token usage -> provider_response event ===")
    try:
        # 1) OpenAI-compatible usage.
        out = _run_case({"prompt_tokens": 1000, "completion_tokens": 250}, 1250)
        print(f"[1] OpenAI usage -> event memuat {out}")
        # 2) Ollama native (top-level) usage tidak berlaku di sini (raw = usage
        #    nested); pakai bentuk kanonik total.
        out = _run_case({"total": 140_500_000}, 140_500_000)
        print(f"[2] total kanonik -> event memuat {out}")
        # 3) Provider tanpa usage -> usage absen (UI "—").
        out = _run_case(None, None)
        print(f"[3] provider tanpa usage -> usage absen (UI '—') OK -> {out}")

        print()
        print("[OK] Token usage provider mengalir ke event provider_response (tanpa counter baru).")
        return 0
    finally:
        shutil.rmtree(FIXTURE, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
