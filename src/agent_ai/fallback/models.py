"""Model untuk Provider Fallback Subsystem (#45).

Provider-agnostic. Fallback menangani kegagalan OPERASIONAL provider/model
(provider unavailable, connection/network failure, timeout, provider API error,
malformed/invalid provider response) dengan berpindah ke kandidat provider/model
alternatif dan melanjutkan task.

Fallback TIDAK menangani:
    - tool failure / command failure
    - validation failure
    - repeated action / no progress
    - workspace change
    - replanning / recovery
Semua itu tetap milik subsystem masing-masing.

    FallbackReason   -> alasan kegagalan provider (terstruktur)
    FallbackAction   -> fallback / retry_current / stop
    FallbackRequest  -> input terstruktur (provider aktif + requirement)
    FallbackCandidate-> kandidat alternatif (dari routing/capability registry)
    FallbackDecision -> keputusan terstruktur + rationale
    FallbackConfig   -> policy bounded (dari config, tidak di-hardcode)

Tidak menyimpan chain-of-thought.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional

from agent_ai.capabilities.models import ModelCapability


class FallbackReason(str, Enum):
    """Alasan kegagalan provider (terstruktur, bukan string exception)."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"    # provider tidak dapat dihubungi
    CONNECTION_FAILURE = "connection_failure"        # network/connection error
    TIMEOUT = "timeout"                              # operasi melewati batas waktu
    PROVIDER_API_ERROR = "provider_api_error"        # HTTP/API error dari provider
    MALFORMED_RESPONSE = "malformed_response"        # response provider tidak valid
    NOT_PROVIDER_FAILURE = "not_provider_failure"    # bukan kegagalan provider


class FallbackAction(str, Enum):
    """Tindakan yang diputuskan fallback."""

    FALLBACK = "fallback"            # pindah ke kandidat provider/model alternatif
    RETRY_CURRENT = "retry_current"  # ulangi provider aktif (transient)
    STOP = "stop"                    # hentikan (bukan kegagalan provider / kuota habis)


@dataclass
class FallbackRequest:
    """Input terstruktur untuk fallback.

    Attributes:
        current_provider: provider yang sedang aktif.
        current_model: model yang sedang aktif.
        reason: alasan kegagalan provider (terstruktur).
        required_capabilities: capability WAJIB yang harus tetap dipenuhi.
        context_tokens: kebutuhan context (token) yang harus tetap dipenuhi.
        transient: apakah kegagalan bersifat transient (boleh retry current).
        retry_allowed: apakah retry current diizinkan (dari Reliability).
        attempts: jumlah percobaan fallback yang sudah dilakukan.
        metadata: info tambahan bebas.
    """

    current_provider: str = ""
    current_model: str = ""
    reason: FallbackReason = FallbackReason.NOT_PROVIDER_FAILURE
    required_capabilities: FrozenSet[ModelCapability] = field(default_factory=frozenset)
    context_tokens: Optional[int] = None
    transient: bool = False
    retry_allowed: bool = False
    attempts: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current_provider": self.current_provider,
            "current_model": self.current_model,
            "reason": self.reason.value,
            "required_capabilities": sorted(c.value for c in self.required_capabilities),
            "context_tokens": self.context_tokens,
            "transient": self.transient,
            "retry_allowed": self.retry_allowed,
            "attempts": self.attempts,
            "metadata": self.metadata,
        }


@dataclass
class FallbackCandidate:
    """Kandidat provider/model alternatif.

    Attributes:
        provider: nama provider.
        model: nama model.
        capabilities: capability yang dimiliki (dari capability registry).
        context_window: context window (token) bila diketahui.
        available: apakah provider tersedia.
        eligible: apakah lolos eliminasi capability/context/identity.
        reasons: alasan singkat (bukan chain-of-thought).
        metadata: info tambahan bebas.
    """

    provider: str
    model: str
    capabilities: FrozenSet[ModelCapability] = field(default_factory=frozenset)
    context_window: Optional[int] = None
    available: bool = True
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
            "eligible": self.eligible,
            "reasons": self.reasons,
            "metadata": self.metadata,
        }


@dataclass
class FallbackDecision:
    """Keputusan fallback terstruktur.

    Attributes:
        action: fallback / retry_current / stop.
        reason: alasan kegagalan yang mendasari.
        selected: kandidat alternatif terpilih (None bila bukan FALLBACK).
        candidates: seluruh kandidat (termasuk yang dieliminasi).
        rationale: alasan singkat keputusan (bukan chain-of-thought).
        bounded: apakah keputusan dibatasi policy (mis. kuota habis).
        metadata: info tambahan bebas.
    """

    action: FallbackAction
    reason: FallbackReason = FallbackReason.NOT_PROVIDER_FAILURE
    selected: Optional[FallbackCandidate] = None
    candidates: List[FallbackCandidate] = field(default_factory=list)
    rationale: str = ""
    bounded: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def provider(self) -> Optional[str]:
        return self.selected.provider if self.selected else None

    @property
    def model(self) -> Optional[str]:
        return self.selected.model if self.selected else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason.value,
            "selected": self.selected.to_dict() if self.selected else None,
            "candidates": [c.to_dict() for c in self.candidates],
            "rationale": self.rationale,
            "bounded": self.bounded,
            "metadata": self.metadata,
        }


@dataclass
class FallbackConfig:
    """Policy fallback (bounded). Dibaca dari config, tidak di-hardcode.

    Attributes:
        enabled: apakah fallback aktif.
        max_attempts: batas total percobaan fallback.
        allow_retry_current: izinkan retry provider aktif untuk error transient.
    """

    enabled: bool = True
    max_attempts: int = 2
    allow_retry_current: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_attempts": self.max_attempts,
            "allow_retry_current": self.allow_retry_current,
        }
