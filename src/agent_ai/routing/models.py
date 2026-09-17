"""Model untuk Model Routing Subsystem (#44).

Provider-agnostic. Routing memilih provider/model paling sesuai untuk sebuah
task SEBELUM eksekusi dimulai. Deterministik (berbasis aturan/scoring), TIDAK
memakai LLM untuk memilih model.

    TaskComplexity   -> simple / medium / complex
    RoutingRequest   -> task + kebutuhan capability + context requirement
    RoutingCandidate -> kandidat provider/model + skor + alasan
    RoutingDecision  -> keputusan terstruktur (selected / failed) + rationale

Tidak menyimpan chain-of-thought; hanya data terstruktur + alasan singkat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional

from agent_ai.capabilities.models import ModelCapability


class TaskComplexity(str, Enum):
    """Tingkat kompleksitas task (deterministik)."""

    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"


@dataclass
class RoutingRequest:
    """Permintaan routing: task + kebutuhan.

    Attributes:
        task: teks task (dipakai untuk klasifikasi kompleksitas).
        required_capabilities: capability WAJIB (kandidat tanpa ini dieliminasi).
        preferred_capabilities: capability yang menambah skor (opsional).
        context_tokens: perkiraan kebutuhan context (token). Kandidat dengan
            context_window lebih kecil dieliminasi.
        complexity: kompleksitas task (bila None, diklasifikasi otomatis).
        preferred_provider: provider preferensi (mis. DEFAULT_PROVIDER).
        metadata: info tambahan bebas.
    """

    task: str = ""
    required_capabilities: FrozenSet[ModelCapability] = field(default_factory=frozenset)
    preferred_capabilities: FrozenSet[ModelCapability] = field(default_factory=frozenset)
    context_tokens: Optional[int] = None
    complexity: Optional[TaskComplexity] = None
    preferred_provider: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "required_capabilities": sorted(c.value for c in self.required_capabilities),
            "preferred_capabilities": sorted(c.value for c in self.preferred_capabilities),
            "context_tokens": self.context_tokens,
            "complexity": self.complexity.value if self.complexity else None,
            "preferred_provider": self.preferred_provider,
            "metadata": self.metadata,
        }


@dataclass
class RoutingCandidate:
    """Kandidat provider/model untuk routing.

    Attributes:
        provider: nama provider.
        model: nama model.
        capabilities: capability yang dimiliki (dari ModelCapabilityRegistry).
        context_window: context window (token) bila diketahui.
        available: apakah provider/model tersedia (dari provider abstraction).
        score: skor deterministik (diisi router).
        eligible: apakah lolos eliminasi capability/context.
        reasons: alasan singkat (bukan chain-of-thought).
        metadata: info tambahan bebas (mis. priority/cost bila tersedia).
    """

    provider: str
    model: str
    capabilities: FrozenSet[ModelCapability] = field(default_factory=frozenset)
    context_window: Optional[int] = None
    available: bool = True
    score: float = 0.0
    eligible: bool = True
    reasons: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.provider.lower()}:{self.model.lower()}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "capabilities": sorted(c.value for c in self.capabilities),
            "context_window": self.context_window,
            "available": self.available,
            "score": self.score,
            "eligible": self.eligible,
            "reasons": self.reasons,
            "metadata": self.metadata,
        }


@dataclass
class RoutingDecision:
    """Keputusan routing terstruktur.

    Attributes:
        selected: kandidat terpilih (None bila gagal).
        candidates: seluruh kandidat (termasuk yang dieliminasi).
        complexity: kompleksitas task yang dipakai.
        rationale: alasan singkat pemilihan (bukan chain-of-thought).
        failed: True bila tidak ada kandidat yang memenuhi requirement.
        metadata: info tambahan bebas.
    """

    selected: Optional[RoutingCandidate] = None
    candidates: List[RoutingCandidate] = field(default_factory=list)
    complexity: Optional[TaskComplexity] = None
    rationale: str = ""
    failed: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def provider(self) -> Optional[str]:
        return self.selected.provider if self.selected else None

    @property
    def model(self) -> Optional[str]:
        return self.selected.model if self.selected else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected": self.selected.to_dict() if self.selected else None,
            "candidates": [c.to_dict() for c in self.candidates],
            "complexity": self.complexity.value if self.complexity else None,
            "rationale": self.rationale,
            "failed": self.failed,
            "metadata": self.metadata,
        }


@dataclass
class RoutingConfig:
    """Konfigurasi routing (bounded, tidak di-hardcode).

    Attributes:
        enabled: apakah routing aktif.
        prefer_default_provider: beri bonus skor ke provider preferensi.
        complexity_threshold_medium: ambang panjang task untuk medium.
        complexity_threshold_complex: ambang panjang task untuk complex.
    """

    enabled: bool = True
    prefer_default_provider: bool = True
    complexity_threshold_medium: int = 120
    complexity_threshold_complex: int = 400

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "prefer_default_provider": self.prefer_default_provider,
            "complexity_threshold_medium": self.complexity_threshold_medium,
            "complexity_threshold_complex": self.complexity_threshold_complex,
        }
