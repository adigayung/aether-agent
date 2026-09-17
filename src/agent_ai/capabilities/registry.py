"""Model Capability Registry.

Source of truth capability provider/model. Provider-agnostic.

Registry ini TIDAK melakukan:
    - API call / network request
    - provider fallback / model routing
    - automatic model selection
    - retry / recovery

Capability didaftarkan secara eksplisit oleh konfigurasi/metadata.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from agent_ai.capabilities.models import ModelCapabilities, ModelCapability


class ModelCapabilityRegistry:
    """Registry capability model, di-key oleh (provider, model).

    Args:
        entries: daftar ModelCapabilities awal (opsional).
    """

    def __init__(self, entries: Optional[List[ModelCapabilities]] = None) -> None:
        self._entries: Dict[str, ModelCapabilities] = {}
        for entry in entries or []:
            self.register(entry)

    @staticmethod
    def _key(provider: str, model: str) -> str:
        return f"{(provider or '').lower()}:{(model or '').lower()}"

    def register(self, entry: ModelCapabilities, *, overwrite: bool = False) -> None:
        """Daftarkan capability sebuah model.

        Args:
            entry: ModelCapabilities.
            overwrite: bila False (default), registrasi duplikat akan menimpa
                hanya jika belum ada; bila sudah ada dan overwrite=False,
                registrasi diabaikan (idempotent, tidak error).

        Raises:
            ValueError: bila provider atau model kosong.
        """
        if not entry.provider or not entry.model:
            raise ValueError("ModelCapabilities butuh provider dan model.")
        key = entry.key
        if key in self._entries and not overwrite:
            return
        self._entries[key] = entry

    def get(self, provider: str, model: str) -> Optional[ModelCapabilities]:
        """Ambil capability model. Kembalikan None bila tidak terdaftar."""
        return self._entries.get(self._key(provider, model))

    def has(self, provider: str, model: str) -> bool:
        """True bila (provider, model) terdaftar."""
        return self._key(provider, model) in self._entries

    def supports(self, provider: str, model: str, capability: ModelCapability) -> bool:
        """True bila model terdaftar dan memiliki capability tersebut.

        Model yang tidak terdaftar -> False (tidak diasumsikan punya capability).
        """
        entry = self.get(provider, model)
        if entry is None:
            return False
        return entry.supports(capability)

    def list(self, provider: Optional[str] = None) -> List[ModelCapabilities]:
        """Daftar model terdaftar, opsional difilter per provider."""
        entries = list(self._entries.values())
        if provider is not None:
            p = provider.lower()
            entries = [e for e in entries if e.provider.lower() == p]
        return sorted(entries, key=lambda e: (e.provider.lower(), e.model.lower()))

    def providers(self) -> List[str]:
        """Daftar nama provider unik yang terdaftar."""
        return sorted({e.provider.lower() for e in self._entries.values()})

    def __len__(self) -> int:
        return len(self._entries)
