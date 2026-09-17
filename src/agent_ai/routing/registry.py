"""RoutingRegistry: sumber kandidat provider/model untuk routing.

Provider-agnostic. RoutingRegistry TIDAK membuat capability registry kedua:
ia MEMBUNGKUS `ModelCapabilityRegistry` yang sudah ada (source of truth
capability) dan menggabungkannya dengan informasi ketersediaan provider dari
provider abstraction.

    ModelCapabilityRegistry (existing)  -> capability model
    ProviderRegistry (existing)         -> ketersediaan provider
        -> RoutingRegistry              -> daftar RoutingCandidate

RoutingRegistry TIDAK melakukan:
    - API call / network request
    - fallback / retry / recovery
    - pemilihan model (itu tugas ModelRouter)
"""

from __future__ import annotations

from typing import Callable, List, Optional

from agent_ai.capabilities.models import ModelCapabilities
from agent_ai.capabilities.registry import ModelCapabilityRegistry
from agent_ai.routing.models import RoutingCandidate


class RoutingRegistry:
    """Membangun kandidat routing dari capability registry + availability.

    Args:
        capability_registry: ModelCapabilityRegistry yang sudah ada. Wajib
            (tidak membuat registry kedua).
        availability: callable opsional `(provider) -> bool` untuk mengecek
            ketersediaan provider. Default: semua dianggap tersedia.
    """

    def __init__(
        self,
        capability_registry: ModelCapabilityRegistry,
        availability: Optional[Callable[[str], bool]] = None,
    ) -> None:
        if capability_registry is None:
            raise ValueError("RoutingRegistry butuh ModelCapabilityRegistry yang sudah ada.")
        self.capability_registry = capability_registry
        self._availability = availability

    def _is_available(self, provider: str) -> bool:
        """Cek ketersediaan provider (default: True)."""
        if self._availability is None:
            return True
        try:
            return bool(self._availability(provider))
        except Exception:  # noqa: BLE001 - availability error -> anggap tidak tersedia
            return False

    def candidates(self, provider: Optional[str] = None) -> List[RoutingCandidate]:
        """Bangun daftar kandidat dari capability registry.

        Args:
            provider: filter provider (opsional).

        Returns:
            Daftar RoutingCandidate (belum dieliminasi/di-score).
        """
        entries: List[ModelCapabilities] = self.capability_registry.list(provider=provider)
        candidates: List[RoutingCandidate] = []
        for entry in entries:
            candidates.append(
                RoutingCandidate(
                    provider=entry.provider,
                    model=entry.model,
                    capabilities=frozenset(entry.capabilities),
                    context_window=entry.context_window,
                    available=self._is_available(entry.provider),
                    metadata=dict(entry.metadata),
                )
            )
        return candidates
