"""Model untuk Model Capability Registry.

Provider-agnostic. Merepresentasikan capability sebuah model/provider sebagai
data terstruktur. Registry ini adalah source of truth capability, BUKAN
decision engine (tidak ada routing/fallback/selection).

    ModelCapability  -> enum capability yang mudah diperluas
    ModelCapabilities-> deskripsi capability satu model (provider + model + caps)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional


class ModelCapability(str, Enum):
    """Capability yang dapat dimiliki sebuah model.

    Mudah diperluas: cukup tambahkan anggota baru. Tidak semua capability
    bernilai True untuk semua model.
    """

    TOOL_CALLING = "tool_calling"
    STRUCTURED_OUTPUT = "structured_output"
    VISION = "vision"
    STREAMING = "streaming"
    MULTIMODAL = "multimodal"
    REASONING = "reasoning"


@dataclass(frozen=True)
class ModelCapabilities:
    """Deskripsi capability satu model pada satu provider.

    Identity unik = (provider, model). Nama model saja TIDAK cukup karena
    provider berbeda dapat memakai nama model yang sama.

    Attributes:
        provider: nama provider (mis. "openrouter", "deepseek", "ollama").
        model: nama model (mis. "deepseek-chat", "qwen2.5-coder:7b").
        capabilities: himpunan capability yang dimiliki model.
        context_window: ukuran context window (token) bila diketahui.
        metadata: info tambahan bebas (mis. catatan, sumber).
    """

    provider: str
    model: str
    capabilities: FrozenSet[ModelCapability] = field(default_factory=frozenset)
    context_window: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Kunci identity unik: provider + model (lowercase)."""
        return f"{self.provider.lower()}:{self.model.lower()}"

    def supports(self, capability: ModelCapability) -> bool:
        """True bila model memiliki capability tersebut."""
        return capability in self.capabilities

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "capabilities": sorted(c.value for c in self.capabilities),
            "context_window": self.context_window,
            "metadata": self.metadata,
        }
