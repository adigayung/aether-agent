"""Cooperative Cancellation Token untuk eksekusi task AETHER.

Primitif TUNGGAL (bukan sistem cancellation kedua) yang menjadi signal
pembatalan kooperatif lintas layer:

    GatewayService.cancel_task()
        -> CancellationToken.request()
             -> AgentOrchestrator (safe boundary: sebelum iteration / tool call)
                  -> break
             -> AgentRuntime (RuntimeStatus.CANCELLED)
        -> TaskExecutor (status CANCELLED ke gateway) -> TaskLifecycle + event

Prinsip:
    - TIDAK ada ``thread.kill`` / force termination / pembunuhan proses.
    - Cancel adalah signal satu arah; eksekusi berhenti pada SAFE BOUNDARY
      (sebelum memanggil LLM lagi atau sebelum tool call berikutnya).
    - Thread-safe: ``request()`` boleh dipanggil lintas thread (HTTP handler).
    - Tidak menyimpan state AETHER apa pun; hanya flag in-memory per task.
"""

from __future__ import annotations

import threading
from typing import Optional


class CancellationToken:
    """Signal pembatalan kooperatif (thread-safe, satu arah).

    Attributes:
        reason: alasan pembatalan (opsional, hanya diisi sekali).
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self.reason: Optional[str] = None

    def request(self, reason: Optional[str] = None) -> None:
        """Minta pembatalan (idempotent, aman dipanggil dari thread lain)."""
        with self._lock:
            if reason and not self.reason:
                self.reason = reason
        self._event.set()

    def is_cancelled(self) -> bool:
        """True bila pembatalan sudah diminta."""
        return self._event.is_set()

    @property
    def cancelled(self) -> bool:
        """Alias properti untuk ``is_cancelled()``."""
        return self._event.is_set()

    def reset(self) -> None:
        """Kembalikan token ke keadaan awal (dipakai ulang antar task)."""
        with self._lock:
            self.reason = None
        self._event.clear()
