"""Infrastructure retry di layer provider (technical only).

Tujuan: menangani gangguan TEKNIS sementara dari provider — timeout, koneksi
terputus, network error, dan status HTTP 429/500/529 — dengan retry + backoff
eksponensial TERBATAS, tanpa menyentuh logika agent.

Batas tanggung jawab (penting):
    - Retry terjadi DI DALAM satu pemanggilan `provider.generate()`. Karena itu
      AgentLoop/AgentRuntime/ConversationHistory TIDAK pernah melihat retry:
      tidak ada pesan assistant/tool yang terduplikasi dan tidak ada tool yang
      dieksekusi ulang.
    - HANYA kegagalan INFRASTRUKTUR. Kegagalan logika agent (tool error, command
      exit != 0, validation gagal, prompt salah) TIDAK di-retry di sini dan
      tetap ditangani loop/tool seperti sebelumnya.
    - TIDAK ada orchestration/runtime kedua, TIDAK ada routing/fallback baru.
      Provider Fallback (#45) tetap berada di layer atas.

Policy dibaca dari config (`settings.provider_retry`), dapat di-override .env.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

import requests

from agent_ai.config.settings import settings
from agent_ai.providers.base import (
    RETRYABLE_HTTP_STATUSES,
    ProviderError,
    ProviderUnavailableError,
)


@dataclass(frozen=True)
class InfrastructureRetryPolicy:
    """Kebijakan retry infrastruktur (bounded, deterministik).

    Attributes:
        enabled: bila False, retry dimatikan (0 retry efektif).
        max_retries: jumlah retry maksimum setelah percobaan pertama.
        base_delay: delay awal (detik) sebelum retry pertama.
        max_delay: batas atas delay (detik) antar retry.
        backoff_factor: faktor backoff eksponensial antar retry.
    """

    enabled: bool = True
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 8.0
    backoff_factor: float = 2.0

    @classmethod
    def from_settings(cls) -> "InfrastructureRetryPolicy":
        """Bangun policy dari config terpusat (settings.provider_retry)."""
        cfg = settings.provider_retry
        return cls(
            enabled=cfg.enabled,
            max_retries=cfg.max_retries,
            base_delay=cfg.base_delay,
            max_delay=cfg.max_delay,
            backoff_factor=cfg.backoff_factor,
        )

    @property
    def effective_max_retries(self) -> int:
        """Jumlah retry efektif (0 bila policy dimatikan). Selalu >= 0."""
        if not self.enabled:
            return 0
        return max(0, int(self.max_retries))

    def delay_for(self, attempt: int) -> float:
        """Delay backoff eksponensial untuk retry ke-`attempt` (0-based).

        delay = min(base_delay * backoff_factor**attempt, max_delay)
        """
        if attempt < 0:
            attempt = 0
        delay = self.base_delay * (self.backoff_factor ** attempt)
        return min(delay, self.max_delay)

    def is_retryable_status(self, status_code: int) -> bool:
        """True bila status HTTP termasuk infrastruktur (429/500/529)."""
        return status_code in RETRYABLE_HTTP_STATUSES

    def to_dict(self) -> Dict[str, object]:
        return {
            "enabled": self.enabled,
            "max_retries": self.max_retries,
            "base_delay": self.base_delay,
            "max_delay": self.max_delay,
            "backoff_factor": self.backoff_factor,
        }


def is_infrastructure_error(error: BaseException) -> bool:
    """True bila error termasuk kegagalan INFRASTRUKTUR yang boleh di-retry.

    Hanya error dengan tanda `retryable=True` (ProviderUnavailableError,
    ProviderAPIError 429/5xx) atau RequestException mentah dari `requests`.
    """
    if isinstance(error, ProviderError):
        return bool(getattr(error, "retryable", False))
    return isinstance(error, requests.RequestException)


def _default_sleep(seconds: float) -> None:
    """Sleep default. Indirection agar test dapat mem-patch tanpa delay nyata."""
    if seconds > 0:
        time.sleep(seconds)


def post_with_infrastructure_retry(
    send: Callable[[], "requests.Response"],
    *,
    policy: InfrastructureRetryPolicy,
    provider_name: str,
    endpoint: str,
    sleep: Optional[Callable[[float], None]] = None,
) -> "requests.Response":
    """Jalankan `send()` dengan retry INFRASTRUKTUR terbatas (bounded).

    Retry dilakukan untuk:
        - `requests.RequestException` (connection/timeout/network), dan
        - response dengan status retryable (429/500/529) SELAMA kuota tersedia.

    Bila kuota retry habis pada kegagalan koneksi -> raise
    `ProviderUnavailableError` (pesan tanpa secret: tanpa header/API key).

    Args:
        send: callable yang melakukan SATU request HTTP dan mengembalikan
            `requests.Response`.
        policy: InfrastructureRetryPolicy (bounded).
        provider_name: nama provider (hanya untuk pesan error).
        endpoint: base URL/host (hanya untuk pesan error, tanpa secret).
        sleep: fungsi sleep injectable (default: time.sleep). Retry memakai
            fungsi ini sehingga test dapat menghindari delay nyata.

    Returns:
        `requests.Response` terakhir. Untuk status retryable yang kuota
        retry-nya sudah habis, response dikembalikan apa adanya agar caller
        memutuskan (mis. melempar ProviderAPIError berstatus 429/5xx).
    """
    sleeper = sleep or _default_sleep
    max_retries = policy.effective_max_retries
    attempt = 0
    while True:
        try:
            response = send()
        except requests.RequestException as exc:
            if attempt >= max_retries:
                # Jangan sertakan header/API key pada pesan error.
                raise ProviderUnavailableError(
                    f"Gagal menghubungi provider '{provider_name}' di {endpoint}: "
                    f"{type(exc).__name__}"
                ) from exc
            sleeper(policy.delay_for(attempt))
            attempt += 1
            continue

        if policy.is_retryable_status(response.status_code) and attempt < max_retries:
            sleeper(policy.delay_for(attempt))
            attempt += 1
            continue
        return response
