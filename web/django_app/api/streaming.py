"""SSE streaming untuk AETHER Gateway (#51).

Django HANYA menjadi transport:
    AETHER Event -> Django Gateway -> SSE -> Client

TIDAK membuat event model kedua, event bus baru, broker, Redis, Celery,
Channels, database, atau persistent event store. Memakai SessionStore AETHER
yang sudah ada (subscription minimal ditambahkan di layer session).

Format SSE standar:
    event: <event_type>
    data: <JSON payload>

Setiap frame juga menyertakan `id:` (event_id) dan `retry:` opsional.
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any, Dict, Iterator, Optional

from agent_ai.session.events import ExecutionEvent
from agent_ai.session.store import SessionStore

#: Batas jumlah event yang di-buffer per subscriber (bounded behavior).
DEFAULT_QUEUE_MAXSIZE = 1000

#: Interval heartbeat (detik) agar koneksi tidak idle-timeout.
DEFAULT_HEARTBEAT_SECONDS = 15.0


def format_sse(event: ExecutionEvent) -> str:
    """Format satu ExecutionEvent menjadi frame SSE standar.

    Format:
        id: <event_id>
        event: <event_type>
        data: <JSON payload>

    JSON payload memuat: event_id, sequence, timestamp, session_id, task_id,
    event_type, payload.
    """
    data = event.to_dict()
    lines = [
        f"id: {event.event_id}",
        f"event: {event.event_type.value}",
        f"data: {json.dumps(data, ensure_ascii=False)}",
        "",
        "",
    ]
    return "\n".join(lines)


def format_comment(text: str) -> str:
    """Format komentar SSE (mis. heartbeat)."""
    return f": {text}\n\n"


class EventSubscription:
    """Subscription tipis ke SessionStore AETHER (bounded, tanpa thread permanen).

    Membungkus callback subscription store + queue bounded. Callback store
    dipanggil saat event di-append; event yang lolos filter dimasukkan ke queue
    (drop bila penuh, agar tidak memory leak).

    Args:
        store: SessionStore AETHER.
        session_id: filter session (opsional).
        task_id: filter task (opsional).
        maxsize: batas queue (bounded).
    """

    def __init__(
        self,
        store: SessionStore,
        *,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        maxsize: int = DEFAULT_QUEUE_MAXSIZE,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.task_id = task_id
        self._queue: "queue.Queue[ExecutionEvent]" = queue.Queue(maxsize=maxsize)
        self._closed = False
        self._lock = threading.Lock()
        self._callback = self._on_event

    def _matches(self, event: ExecutionEvent) -> bool:
        """Cek apakah event lolos filter session/task."""
        if self.session_id is not None and event.session_id != self.session_id:
            return False
        if self.task_id is not None and event.task_id != self.task_id:
            return False
        return True

    def _on_event(self, event: ExecutionEvent) -> None:
        """Callback store: masukkan event yang lolos filter ke queue."""
        if self._closed or not self._matches(event):
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # Bounded: drop event bila subscriber lambat (anti memory leak).
            pass

    def start(self) -> None:
        """Mulai subscription (daftarkan callback ke store)."""
        self.store.subscribe(self._callback)

    def close(self) -> None:
        """Akhiri subscription (unsubscribe dari store). Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.store.unsubscribe(self._callback)

    @property
    def closed(self) -> bool:
        return self._closed

    def get(self, timeout: float) -> Optional[ExecutionEvent]:
        """Ambil event berikutnya (None bila timeout)."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


def sse_stream(
    subscription: EventSubscription,
    *,
    heartbeat: float = DEFAULT_HEARTBEAT_SECONDS,
    is_disconnected: Optional[Any] = None,
) -> Iterator[str]:
    """Generator SSE: yield frame dari subscription sampai client disconnect.

    Args:
        subscription: EventSubscription aktif.
        heartbeat: interval heartbeat (detik).
        is_disconnected: callable opsional `() -> bool` untuk mendeteksi
            disconnect (mis. dari Django request). Bila None, generator berhenti
            saat subscription ditutup.

    Yields:
        Frame SSE (str).

    Catatan:
        Generator ini TIDAK membuat thread permanen. Ia memblokir pada queue
        dengan timeout (bounded) dan memeriksa disconnect secara berkala.
    """
    try:
        while True:
            if is_disconnected is not None and is_disconnected():
                break
            event = subscription.get(timeout=heartbeat)
            if event is None:
                # Tidak ada event dalam interval -> kirim heartbeat.
                yield format_comment("heartbeat")
                continue
            yield format_sse(event)
    finally:
        # Selalu unsubscribe saat generator berhenti (disconnect/close).
        subscription.close()
