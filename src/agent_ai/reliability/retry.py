"""Retry helper untuk Reliability Subsystem.

Provider-agnostic. TIDAK membuat HTTP client baru; provider tetap bertanggung
jawab terhadap request API. Modul ini hanya menghitung kebijakan retry
(apakah boleh retry, berapa delay) berdasarkan RetryPolicy.
"""

from __future__ import annotations

from typing import Optional

from agent_ai.reliability.models import RetryPolicy


class RetryController:
    """Mengelola state percobaan retry berdasarkan RetryPolicy.

    Args:
        policy: RetryPolicy yang dipakai.
    """

    def __init__(self, policy: Optional[RetryPolicy] = None) -> None:
        self.policy = policy or RetryPolicy()
        self._attempts: int = 0

    @property
    def attempts(self) -> int:
        """Jumlah percobaan retry yang sudah dilakukan."""
        return self._attempts

    def reset(self) -> None:
        """Reset state percobaan."""
        self._attempts = 0

    def can_retry(self, outcome: str) -> bool:
        """True bila outcome boleh di-retry dan kuota retry belum habis."""
        if self._attempts >= self.policy.max_retries:
            return False
        return self.policy.is_retryable(outcome)

    def next_delay(self, outcome: str) -> Optional[float]:
        """Delay backoff untuk retry berikutnya, atau None bila tidak boleh retry.

        Tidak menaikkan counter; gunakan `record_attempt()` setelah retry
        benar-benar dilakukan.
        """
        if not self.can_retry(outcome):
            return None
        return self.policy.delay_for(self._attempts)

    def record_attempt(self) -> int:
        """Catat satu percobaan retry. Kembalikan jumlah percobaan saat ini."""
        self._attempts += 1
        return self._attempts
