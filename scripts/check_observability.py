"""Verifikasi Observability / Telemetry (#55).

Deterministik, TANPA model/API cloud nyata. Memakai provider fake + tool dummy.
Tidak menyentuh filesystem.

Menguji:
    1. event sink dapat menerima event
    2. lifecycle event tercatat (task_started/completed/failed)
    3. tool event tercatat (tool_called/tool_completed/observation_received)
    4. validation/recovery event dapat tercatat
    5. event masuk SessionStore (event system existing)
    6. SSE tetap kompatibel
    7. tidak ada secret/API key dalam event payload (sanitasi)
    8. existing runtime tetap bekerja tanpa event sink (backward compatible)
    9. architecture boundary tetap valid (tanpa event bus kedua)

Jalankan:
    python scripts/check_observability.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _make_fake_provider(tool_name: str, tool_args: dict):
    """Provider fake: iterasi 1 -> tool_call, iterasi 2+ -> final."""
    from agent_ai.core.response import ActionType, FinishReason, LLMAction, LLMResponse
    from agent_ai.providers.base import BaseProvider, GenerateResult

    class FakeProvider(BaseProvider):
        name = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
            self.calls += 1
            return GenerateResult(text="", provider="fake", model="fake-1")

        def is_available(self) -> bool:
            return True

        def normalize_response(self, result):
            if self.calls == 1:
                return LLMResponse(
                    text="",
                    actions=[LLMAction(name=tool_name, arguments=tool_args, type=ActionType.TOOL_CALL)],
                    finish_reason=FinishReason.TOOL_CALLS,
                    provider="fake",
                    model="fake-1",
                )
            return LLMResponse(
                text="selesai",
                actions=[],
                finish_reason=FinishReason.STOP,
                provider="fake",
                model="fake-1",
            )

    return FakeProvider()


def main() -> int:
    print("=== Verifikasi Observability / Telemetry (#55) ===")
    return _run()


def _run() -> int:
    from agent_ai.core.executor import ToolExecutor
    from agent_ai.core.observability import emit, sanitize_payload
    from agent_ai.runtime.runtime import AgentRuntime
    from agent_ai.session.events import EventType, make_event
    from agent_ai.session.store import InMemorySessionStore
    from agent_ai.task.models import PreparedTask
    from agent_ai.tools import BaseTool, ToolRegistry

    # Tool dummy (tidak menyentuh filesystem).
    class DummyTool(BaseTool):
        name = "write_file"
        description = "Dummy write tool."
        input_schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        }

        def execute(self, **arguments):
            return {"written": True, "path": arguments.get("path")}

    # 1) event sink dapat menerima event.
    received = []
    sink = lambda et, payload: received.append((et, payload))  # noqa: E731
    emit(sink, "tool_called", {"tool": "write_file"})
    assert received and received[0][0] == "tool_called", received
    print("[1] event sink dapat menerima event OK")

    # 2/3/5) lifecycle + tool event tercatat & masuk SessionStore.
    store = InMemorySessionStore()
    session = store.create_session(metadata={"task_id": "t1"})
    registry = ToolRegistry()
    registry.register(DummyTool())
    runtime = AgentRuntime(
        provider=_make_fake_provider("write_file", {"path": "a.txt", "content": "x"}),
        executor=ToolExecutor(registry=registry),
        session_store=store,
        session_id=session.session_id,
    )
    prepared = PreparedTask(task="tulis file a.txt", task_id="t1")
    result = runtime.run(prepared)
    assert result.success, result.error

    events = store.get_events(task_id="t1")
    types = [e.event_type.value for e in events]
    # 2) lifecycle event.
    assert "task_started" in types, types
    assert "task_completed" in types, types
    print(f"[2] lifecycle event tercatat OK -> task_started/task_completed")
    # 3) tool event.
    assert "tool_called" in types, types
    assert "tool_completed" in types, types
    assert "observation_received" in types, types
    print("[3] tool event tercatat OK -> tool_called/tool_completed/observation_received")
    # 5) event masuk SessionStore (event system existing).
    assert all(e.session_id == session.session_id for e in events)
    seqs = [e.sequence for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), seqs
    print(f"[5] event masuk SessionStore OK -> {len(events)} event, sequence deterministik")

    # 4) validation/recovery event dapat tercatat (memakai EventType existing).
    store2 = InMemorySessionStore()
    s2 = store2.create_session()
    for et in (EventType.VALIDATION_STARTED, EventType.VALIDATION_COMPLETED,
               EventType.RECOVERY_STARTED, EventType.RECOVERY_COMPLETED):
        store2.append_event(make_event(session_id=s2.session_id, event_type=et, task_id="t2"))
    types2 = [e.event_type.value for e in store2.get_events(task_id="t2")]
    assert "validation_started" in types2 and "validation_completed" in types2, types2
    assert "recovery_started" in types2 and "recovery_completed" in types2, types2
    print("[4] validation/recovery event dapat tercatat OK")

    # 6) SSE tetap kompatibel.
    from api.streaming import EventSubscription, format_sse  # noqa: E402

    sub = EventSubscription(store, task_id="t1")
    sub.start()
    store.append_event(make_event(session_id=session.session_id, event_type=EventType.PHASE_CHANGED, task_id="t1"))
    got = sub.get(timeout=1.0)
    assert got is not None
    frame = format_sse(got)
    assert "event: phase_changed" in frame and "data: " in frame
    sub.close()
    print("[6] SSE tetap kompatibel OK")

    # 7) tidak ada secret/API key dalam event payload (sanitasi).
    dirty = {
        "provider": "openai",
        "api_key": "sk-super-secret-123",
        "Authorization": "Bearer abcdef",
        "nested": {"token": "tok-xyz", "model": "gpt-4"},
        "list": [{"password": "p@ss"}],
    }
    clean = sanitize_payload(dirty)
    assert clean["api_key"] == "[redacted]", clean
    assert clean["Authorization"] == "[redacted]", clean
    assert clean["nested"]["token"] == "[redacted]", clean
    assert clean["nested"]["model"] == "gpt-4", clean
    assert clean["list"][0]["password"] == "[redacted]", clean
    # Emit lewat sink juga tersanitasi.
    captured = []
    emit(lambda et, p: captured.append(p), "provider_request", dirty)
    assert captured[0]["api_key"] == "[redacted]"
    # Runtime event payload tidak memuat secret (provider info aman).
    for e in events:
        blob = str(e.payload).lower()
        assert "sk-" not in blob and "bearer " not in blob, e.payload
    print("[7] tidak ada secret/API key dalam event payload OK -> sanitasi aktif")

    # 8) existing runtime tetap bekerja tanpa event sink (backward compatible).
    runtime_plain = AgentRuntime(
        provider=_make_fake_provider("write_file", {"path": "b.txt", "content": "y"}),
        executor=ToolExecutor(registry=registry),
    )
    result_plain = runtime_plain.run(PreparedTask(task="tulis file b.txt", task_id="t3"))
    assert result_plain.success, result_plain.error
    assert runtime_plain.session_store is None
    print("[8] existing runtime tetap bekerja tanpa event sink OK")

    # 9) architecture boundary tetap valid (tanpa event bus kedua).
    core_dir = SRC_DIR / "agent_ai" / "core"
    for p in core_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "class EventBus" not in text, f"{p.name} tidak boleh membuat event bus"
        assert "class MessageBroker" not in text, f"{p.name} tidak boleh membuat broker"
    obs_src = (core_dir / "observability.py").read_text(encoding="utf-8")
    assert "class EventBus" not in obs_src and "class Event(" not in obs_src
    # observability tidak mengimpor provider konkret / requests.
    for bad in ("ollama", "deepseek", "openai", "requests"):
        assert bad not in obs_src.lower(), f"observability tidak boleh bergantung '{bad}'"
    print("[9] architecture boundary tetap valid OK -> tanpa event bus kedua")

    print()
    print("[OK] Observability / Telemetry bekerja (event sink opsional + sanitasi).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
