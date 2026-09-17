"""Verifikasi WebSocket / SSE Event Streaming (#51).

Deterministik, tanpa model/API nyata. Memakai Django test client + generator
SSE langsung. Fixture dibersihkan setelah test.

Menguji:
    1. endpoint SSE tersedia
    2. response content type benar (text/event-stream)
    3. event dapat dikirim (format SSE standar)
    4. event id/sequence/session/task/event_type/payload diteruskan
    5. filtering session
    6. filtering task
    7. multiple events menjaga sequence
    8. disconnect cleanup (unsubscribe)
    9. tidak membuat event model kedua
   10. tidak membuat runtime/loop/broker baru
   11. subscription minimal di layer session (bukan subsystem baru)

Jalankan:
    python scripts/check_streaming.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def main() -> int:
    print("=== Verifikasi WebSocket / SSE Event Streaming (#51) ===")
    return _run()


def _run() -> int:
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    # Django test client mengirim Host: testserver (hardening #57 membatasi host).
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
    import django
    django.setup()
    from django.test import Client

    from agent_ai.session.events import EventType, make_event
    from agent_ai.session.store import InMemorySessionStore

    # 1) endpoint SSE tersedia.
    client = Client()
    resp = client.get("/api/events")
    assert resp.status_code == 200, resp.status_code
    assert resp.streaming, "response harus streaming"
    print("[1] endpoint SSE tersedia OK -> GET /api/events")

    # 2) response content type benar.
    assert resp["Content-Type"].startswith("text/event-stream"), resp["Content-Type"]
    assert resp["Cache-Control"] == "no-cache"
    print(f"[2] content type OK -> {resp['Content-Type']}")

    # 3) event dapat dikirim (format SSE standar).
    from api.streaming import EventSubscription, format_sse, sse_stream

    store = InMemorySessionStore()
    sub = EventSubscription(store, session_id="s1")
    sub.start()
    event = make_event(
        session_id="s1",
        event_type=EventType.TASK_STARTED,
        task_id="t1",
        payload={"step": "plan"},
    )
    store.append_event(event)

    frame = format_sse(event)
    assert "event: task_started" in frame, frame
    assert "data: " in frame, frame
    assert frame.endswith("\n\n"), "frame harus diakhiri baris kosong"
    print("[3] event dapat dikirim OK -> format SSE standar")

    # 4) event id/sequence/session/task/event_type/payload diteruskan.
    got = sub.get(timeout=1.0)
    assert got is not None, "event harus diterima subscriber"
    data = got.to_dict()
    assert data["event_id"] == event.event_id
    assert data["sequence"] == 1, data["sequence"]
    assert data["session_id"] == "s1"
    assert data["task_id"] == "t1"
    assert data["event_type"] == "task_started"
    assert data["payload"] == {"step": "plan"}
    assert "timestamp" in data and data["timestamp"] > 0
    # Frame JSON memuat semua field.
    frame_json = json.loads(frame.split("data: ", 1)[1].strip())
    for key in ("event_id", "sequence", "timestamp", "session_id", "task_id", "event_type", "payload"):
        assert key in frame_json, f"field '{key}' hilang di frame"
    print("[4] field event diteruskan OK -> id/sequence/session/task/type/payload/timestamp")
    sub.close()

    # 5) filtering session.
    store2 = InMemorySessionStore()
    sub_s = EventSubscription(store2, session_id="sA")
    sub_s.start()
    store2.append_event(make_event(session_id="sA", event_type=EventType.TASK_STARTED, task_id="t1"))
    store2.append_event(make_event(session_id="sB", event_type=EventType.TASK_STARTED, task_id="t1"))
    store2.append_event(make_event(session_id="sA", event_type=EventType.TASK_COMPLETED, task_id="t1"))
    received = []
    while True:
        e = sub_s.get(timeout=0.2)
        if e is None:
            break
        received.append(e)
    assert len(received) == 2, [e.session_id for e in received]
    assert all(e.session_id == "sA" for e in received)
    sub_s.close()
    print(f"[5] filtering session OK -> {len(received)} event (hanya sA)")

    # 6) filtering task.
    store3 = InMemorySessionStore()
    sub_t = EventSubscription(store3, task_id="tX")
    sub_t.start()
    store3.append_event(make_event(session_id="s1", event_type=EventType.TOOL_CALLED, task_id="tX"))
    store3.append_event(make_event(session_id="s1", event_type=EventType.TOOL_CALLED, task_id="tY"))
    store3.append_event(make_event(session_id="s1", event_type=EventType.TOOL_COMPLETED, task_id="tX"))
    received_t = []
    while True:
        e = sub_t.get(timeout=0.2)
        if e is None:
            break
        received_t.append(e)
    assert len(received_t) == 2, [e.task_id for e in received_t]
    assert all(e.task_id == "tX" for e in received_t)
    sub_t.close()
    print(f"[6] filtering task OK -> {len(received_t)} event (hanya tX)")

    # 7) multiple events menjaga sequence.
    store4 = InMemorySessionStore()
    sub_seq = EventSubscription(store4, session_id="s1")
    sub_seq.start()
    for i in range(5):
        store4.append_event(make_event(session_id="s1", event_type=EventType.PHASE_CHANGED, payload={"i": i}))
    seqs = []
    while True:
        e = sub_seq.get(timeout=0.2)
        if e is None:
            break
        seqs.append(e.sequence)
    assert seqs == [1, 2, 3, 4, 5], seqs
    sub_seq.close()
    print(f"[7] multiple events menjaga sequence OK -> {seqs}")

    # 8) disconnect cleanup (unsubscribe).
    store5 = InMemorySessionStore()
    sub_d = EventSubscription(store5, session_id="s1")
    sub_d.start()
    assert store5.subscriber_count() == 1, store5.subscriber_count()
    # Simulasi disconnect: tutup subscription.
    sub_d.close()
    assert store5.subscriber_count() == 0, "subscriber harus di-unsubscribe"
    assert sub_d.closed
    # close idempotent.
    sub_d.close()
    assert store5.subscriber_count() == 0
    # Generator SSE juga unsubscribe di finally.
    store6 = InMemorySessionStore()
    sub_g = EventSubscription(store6, session_id="s1")
    sub_g.start()
    gen = sse_stream(sub_g, heartbeat=0.05)
    store6.append_event(make_event(session_id="s1", event_type=EventType.TASK_STARTED))
    first = next(gen)
    assert "event: task_started" in first
    gen.close()  # memicu finally -> unsubscribe
    assert store6.subscriber_count() == 0, "generator harus unsubscribe saat ditutup"
    print("[8] disconnect cleanup OK -> unsubscribe + idempotent + generator finally")

    # 9) tidak membuat event model kedua.
    api_dir = DJANGO_APP_DIR / "api"
    for p in api_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        # Tidak boleh mendefinisikan class event baru (model kedua).
        assert "class ExecutionEvent" not in text, f"{p.name} tidak boleh membuat event model kedua"
        assert "class EventType" not in text, f"{p.name} tidak boleh membuat EventType kedua"
        assert "class Event(" not in text, f"{p.name} tidak boleh membuat event class baru"
        assert "class StreamEvent" not in text, f"{p.name} tidak boleh membuat event class baru"
    # Streaming memakai event AETHER existing.
    streaming_src = (api_dir / "streaming.py").read_text(encoding="utf-8")
    assert "from agent_ai.session.events import" in streaming_src, "harus pakai event AETHER"
    assert "from agent_ai.session.store import" in streaming_src, "harus pakai SessionStore AETHER"
    print("[9] tidak ada event model kedua OK -> memakai event AETHER existing")

    # 10) tidak membuat runtime/loop/broker baru.
    forbidden = (
        "class AgentRuntime",
        "class AgentLoop",
        "class AgentOrchestrator",
        "class EventBus",
        "class MessageBroker",
        "import redis",
        "import celery",
        "import channels",
        "websockets",
    )
    for p in api_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        for bad in forbidden:
            assert bad not in text, f"{p.name} tidak boleh memakai '{bad}'"
    print("[10] tidak ada runtime/loop/broker baru OK")

    # 11) subscription minimal di layer session (bukan subsystem baru).
    store_src = (SRC_DIR / "agent_ai" / "session" / "store.py").read_text(encoding="utf-8")
    assert "def subscribe" in store_src, "SessionStore harus punya subscribe minimal"
    assert "def unsubscribe" in store_src, "SessionStore harus punya unsubscribe minimal"
    # Tidak ada file subsystem event baru di session.
    session_dir = SRC_DIR / "agent_ai" / "session"
    session_files = {f.name for f in session_dir.glob("*.py")}
    assert "bus.py" not in session_files, "tidak boleh membuat event bus baru"
    assert "broker.py" not in session_files, "tidak boleh membuat broker baru"
    print("[11] subscription minimal di layer session OK -> tanpa subsystem baru")

    print()
    print("[OK] SSE Event Streaming bekerja (transport tipis, event AETHER existing).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
