"""ModelRouter: pemilihan provider/model deterministik (BUKAN LLM).

Provider-agnostic. Memilih kandidat paling sesuai untuk sebuah task SEBELUM
eksekusi dimulai. Deterministik: input sama -> keputusan sama.

Alur:
    RoutingRequest
        -> klasifikasi kompleksitas (TaskClassifier)
        -> ambil kandidat (RoutingRegistry, dari ModelCapabilityRegistry)
        -> eliminasi: capability wajib, context window, availability
        -> scoring deterministik
        -> RoutingDecision (selected / failed + rationale)

Prinsip:
    - TIDAK memakai LLM untuk memilih model.
    - TIDAK melakukan fallback ke provider lain (itu #45 Provider Fallback).
    - TIDAK ada branching khusus provider (provider-agnostic).
    - Bila tidak ada kandidat memenuhi -> keputusan gagal terstruktur.
"""

from __future__ import annotations

from typing import List, Optional

from agent_ai.routing.classifier import TaskClassifier
from agent_ai.routing.models import (
    RoutingCandidate,
    RoutingConfig,
    RoutingDecision,
    RoutingRequest,
    TaskComplexity,
)
from agent_ai.routing.registry import RoutingRegistry

# Bonus skor deterministik (bukan bobot ajaib; hanya urutan preferensi).
_SCORE_REQUIRED_CAPABILITY = 10.0
_SCORE_PREFERRED_CAPABILITY = 3.0
_SCORE_PREFERRED_PROVIDER = 5.0
_SCORE_COMPLEXITY_MATCH = 4.0
_SCORE_CONTEXT_HEADROOM = 2.0


class ModelRouter:
    """Memilih provider/model secara deterministik.

    Args:
        registry: RoutingRegistry (membungkus ModelCapabilityRegistry).
        config: RoutingConfig. Default: RoutingConfig().
        classifier: TaskClassifier opsional.
    """

    def __init__(
        self,
        registry: RoutingRegistry,
        config: Optional[RoutingConfig] = None,
        classifier: Optional[TaskClassifier] = None,
    ) -> None:
        if registry is None:
            raise ValueError("ModelRouter butuh RoutingRegistry.")
        self.registry = registry
        self.config = config or RoutingConfig()
        self.classifier = classifier or TaskClassifier(self.config)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def route(self, request: RoutingRequest) -> RoutingDecision:
        """Pilih kandidat terbaik untuk request (deterministik).

        Returns:
            RoutingDecision. Bila tidak ada kandidat memenuhi -> failed=True
            dengan rationale terstruktur (TIDAK fallback ke provider lain).
        """
        complexity = request.complexity or self.classifier.classify(request.task)

        candidates = self.registry.candidates()
        if not candidates:
            return RoutingDecision(
                selected=None,
                candidates=[],
                complexity=complexity,
                rationale="Tidak ada kandidat model terdaftar.",
                failed=True,
            )

        # 1) Eliminasi kandidat yang tidak memenuhi requirement.
        eligible: List[RoutingCandidate] = []
        for candidate in candidates:
            self._evaluate_eligibility(candidate, request)
            if candidate.eligible:
                eligible.append(candidate)

        if not eligible:
            return RoutingDecision(
                selected=None,
                candidates=candidates,
                complexity=complexity,
                rationale="Tidak ada kandidat yang memenuhi capability/context/availability.",
                failed=True,
            )

        # 2) Scoring deterministik.
        for candidate in eligible:
            candidate.score = self._score(candidate, request, complexity)

        # 3) Pilih skor tertinggi; tie-break deterministik (provider, model).
        eligible.sort(key=lambda c: (-c.score, c.provider.lower(), c.model.lower()))
        selected = eligible[0]

        rationale = self._rationale(selected, request, complexity)
        return RoutingDecision(
            selected=selected,
            candidates=candidates,
            complexity=complexity,
            rationale=rationale,
            failed=False,
            metadata={"eligible_count": len(eligible)},
        )

    # ------------------------------------------------------------------ #
    # Eligibility (eliminasi)
    # ------------------------------------------------------------------ #
    def _evaluate_eligibility(self, candidate: RoutingCandidate, request: RoutingRequest) -> None:
        """Tandai kandidat eligible/tidak + alasan (in-place)."""
        reasons: List[str] = []

        # Availability.
        if not candidate.available:
            candidate.eligible = False
            reasons.append("provider tidak tersedia")

        # Capability wajib.
        missing = sorted(
            c.value for c in request.required_capabilities if c not in candidate.capabilities
        )
        if missing:
            candidate.eligible = False
            reasons.append(f"capability wajib tidak terpenuhi: {missing}")

        # Context window.
        if request.context_tokens is not None:
            if candidate.context_window is None:
                candidate.eligible = False
                reasons.append("context window tidak diketahui")
            elif candidate.context_window < request.context_tokens:
                candidate.eligible = False
                reasons.append(
                    f"context window {candidate.context_window} < dibutuhkan {request.context_tokens}"
                )

        candidate.reasons = reasons

    # ------------------------------------------------------------------ #
    # Scoring (deterministik)
    # ------------------------------------------------------------------ #
    def _score(
        self,
        candidate: RoutingCandidate,
        request: RoutingRequest,
        complexity: TaskComplexity,
    ) -> float:
        """Hitung skor deterministik kandidat."""
        score = 0.0

        # Capability wajib (semua sudah terpenuhi; beri bobot tetap).
        score += _SCORE_REQUIRED_CAPABILITY * len(request.required_capabilities)

        # Capability preferensi.
        for cap in request.preferred_capabilities:
            if cap in candidate.capabilities:
                score += _SCORE_PREFERRED_CAPABILITY

        # Preferensi provider (mis. DEFAULT_PROVIDER).
        if (
            self.config.prefer_default_provider
            and request.preferred_provider
            and candidate.provider.lower() == request.preferred_provider.lower()
        ):
            score += _SCORE_PREFERRED_PROVIDER

        # Kesesuaian kompleksitas: task kompleks lebih cocok model reasoning.
        if complexity == TaskComplexity.COMPLEX:
            from agent_ai.capabilities.models import ModelCapability

            if ModelCapability.REASONING in candidate.capabilities:
                score += _SCORE_COMPLEXITY_MATCH
        elif complexity == TaskComplexity.SIMPLE:
            # Task sederhana: model dengan context lebih kecil (ringan) sedikit
            # lebih disukai agar tidak "overkill" (deterministik).
            if candidate.context_window is not None and candidate.context_window <= 32768:
                score += _SCORE_COMPLEXITY_MATCH

        # Headroom context (semakin besar, semakin baik) - normalisasi ringan.
        if candidate.context_window:
            score += _SCORE_CONTEXT_HEADROOM * min(candidate.context_window / 200000.0, 1.0)

        return round(score, 6)

    # ------------------------------------------------------------------ #
    # Rationale (struktural, bukan chain-of-thought)
    # ------------------------------------------------------------------ #
    def _rationale(
        self,
        selected: RoutingCandidate,
        request: RoutingRequest,
        complexity: TaskComplexity,
    ) -> str:
        parts = [
            f"complexity={complexity.value}",
            f"selected={selected.provider}:{selected.model}",
            f"score={selected.score}",
        ]
        if request.required_capabilities:
            parts.append(
                "required=" + ",".join(sorted(c.value for c in request.required_capabilities))
            )
        if request.context_tokens is not None:
            parts.append(f"context_tokens={request.context_tokens}")
        if request.preferred_provider:
            parts.append(f"preferred_provider={request.preferred_provider}")
        return "; ".join(parts)
