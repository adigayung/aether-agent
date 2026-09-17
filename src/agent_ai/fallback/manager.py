"""FallbackManager: orkestrasi provider fallback (BUKAN executor).

Provider-agnostic. FallbackManager mengorkestrasi perpindahan provider/model
saat provider aktif gagal secara operasional. Ia MEMAKAI subsystem yang ada:

    RoutingRegistry / ModelCapabilityRegistry -> kandidat alternatif
    FallbackPolicy                            -> keputusan (fallback/retry/stop)
    ReliabilityManager (opsional)             -> keputusan retry (tidak diduplikasi)

FallbackManager TIDAK:
    - membuat provider registry kedua,
    - membuat capability registry kedua,
    - memilih model dengan LLM,
    - melakukan retry tanpa batas,
    - memilih kandidat yang sama dengan provider/model aktif,
    - menangani tool/command/validation/workspace/repeated/no-progress,
    - melakukan operasi Git destruktif,
    - membuat runtime/executor kedua.

Fallback bounded oleh FallbackConfig (max_attempts).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from agent_ai.fallback.models import (
    FallbackAction,
    FallbackCandidate,
    FallbackConfig,
    FallbackDecision,
    FallbackReason,
    FallbackRequest,
)
from agent_ai.fallback.policy import FallbackPolicy

if TYPE_CHECKING:  # pragma: no cover - hindari import cycle
    from agent_ai.reliability.manager import ReliabilityManager
    from agent_ai.routing.registry import RoutingRegistry


class FallbackManager:
    """Mengorkestrasi provider fallback berdasarkan subsystem yang ada.

    Args:
        registry: RoutingRegistry (membungkus ModelCapabilityRegistry yang ada).
            Sumber kandidat alternatif. Wajib.
        config: FallbackConfig (bounded). Default dari settings.fallback.
        policy: FallbackPolicy opsional.
        reliability: ReliabilityManager opsional (sumber keputusan retry).
    """

    def __init__(
        self,
        registry: "RoutingRegistry",
        config: Optional[FallbackConfig] = None,
        policy: Optional[FallbackPolicy] = None,
        reliability: Optional["ReliabilityManager"] = None,
    ) -> None:
        if registry is None:
            raise ValueError("FallbackManager butuh RoutingRegistry (kandidat dari routing).")
        self.registry = registry
        self.config = config or self._config_from_settings()
        self.policy = policy or FallbackPolicy(self.config)
        self.reliability = reliability

        # State bounded (per instance).
        self._attempts = 0
        self._history: List[Dict[str, Any]] = []

    @staticmethod
    def _config_from_settings() -> FallbackConfig:
        """Ambil FallbackConfig dari settings (fallback aman)."""
        try:
            from agent_ai.config.settings import settings

            return settings.fallback
        except Exception:  # noqa: BLE001 - config error tidak boleh crash
            return FallbackConfig()

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def history(self) -> List[Dict[str, Any]]:
        return list(self._history)

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def reset(self) -> None:
        """Reset state fallback (untuk task baru)."""
        self._attempts = 0
        self._history.clear()

    # ------------------------------------------------------------------ #
    # Candidate selection (dari routing/capability registry yang ada)
    # ------------------------------------------------------------------ #
    def candidates(
        self,
        request: FallbackRequest,
    ) -> List[FallbackCandidate]:
        """Bangun kandidat alternatif dari RoutingRegistry.

        Mengeliminasi:
            - kandidat yang sama dengan provider/model aktif,
            - kandidat yang tidak tersedia,
            - kandidat yang tidak memenuhi capability wajib,
            - kandidat yang context window-nya kurang.

        Returns:
            Daftar FallbackCandidate (termasuk yang dieliminasi, dengan flag).
        """
        raw = self.registry.candidates()
        out: List[FallbackCandidate] = []
        for c in raw:
            candidate = FallbackCandidate(
                provider=c.provider,
                model=c.model,
                capabilities=frozenset(c.capabilities),
                context_window=c.context_window,
                available=c.available,
                metadata=dict(c.metadata),
            )
            self._evaluate(candidate, request)
            out.append(candidate)
        return out

    def _evaluate(self, candidate: FallbackCandidate, request: FallbackRequest) -> None:
        """Tandai kandidat eligible/tidak + alasan (in-place)."""
        reasons: List[str] = []

        # Tidak boleh memilih kandidat yang sama dengan provider/model aktif.
        # Bila model aktif tidak diketahui, eliminasi seluruh provider aktif
        # (agar fallback benar-benar berpindah provider).
        same_provider = candidate.provider.lower() == (request.current_provider or "").lower()
        if same_provider:
            if not request.current_model:
                candidate.eligible = False
                reasons.append("provider sama dengan provider aktif (model tidak diketahui)")
            elif candidate.model.lower() == request.current_model.lower():
                candidate.eligible = False
                reasons.append("kandidat sama dengan provider/model aktif")

        # Availability.
        if not candidate.available:
            candidate.eligible = False
            reasons.append("provider tidak tersedia")

        # Capability wajib.
        missing = sorted(
            cap.value for cap in request.required_capabilities if cap not in candidate.capabilities
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

    def _eligible_sorted(self, candidates: List[FallbackCandidate]) -> List[FallbackCandidate]:
        """Kandidat eligible, diurutkan deterministik (provider, model)."""
        eligible = [c for c in candidates if c.eligible]
        eligible.sort(key=lambda c: (c.provider.lower(), c.model.lower()))
        return eligible

    # ------------------------------------------------------------------ #
    # Decision
    # ------------------------------------------------------------------ #
    def decide(self, request: FallbackRequest) -> FallbackDecision:
        """Klasifikasi + putuskan tindakan fallback (bounded).

        Menggabungkan sinyal dengan konteks subsystem:
            - reliability: apakah retry current diizinkan (tidak diduplikasi).
            - routing registry: kandidat alternatif.
        """
        # Lengkapi sinyal dengan konteks reliability (tanpa menduplikasi).
        request = self._enrich_request(request)

        # Manager adalah sumber tunggal jumlah percobaan fallback (bounded).
        # Hormati attempts yang lebih besar bila diberikan eksplisit.
        request.attempts = max(request.attempts, self._attempts)

        candidates = self.candidates(request)
        eligible = self._eligible_sorted(candidates)
        has_candidate = len(eligible) > 0

        decision = self.policy.decide(request, has_candidate=has_candidate)
        decision.candidates = candidates

        if decision.action == FallbackAction.FALLBACK and eligible:
            decision.selected = eligible[0]
            decision.rationale = (
                f"{decision.rationale} selected={eligible[0].provider}:{eligible[0].model}"
            )

        self._history.append(
            {
                "request": request.to_dict(),
                "decision": decision.to_dict(),
                "attempts": self._attempts,
            }
        )
        return decision

    def _enrich_request(self, request: FallbackRequest) -> FallbackRequest:
        """Lengkapi request dengan konteks reliability (tidak menduplikasi)."""
        if self.reliability is not None and not request.retry_allowed:
            try:
                # Retry diizinkan bila Reliability mengizinkan (sumber tunggal).
                request.retry_allowed = self.reliability.should_retry(request.reason.value)
            except Exception:  # noqa: BLE001
                pass
        return request

    # ------------------------------------------------------------------ #
    # Orchestration (bounded)
    # ------------------------------------------------------------------ #
    def fallback(self, request: FallbackRequest) -> FallbackDecision:
        """Orkestrasi satu langkah fallback (bounded).

        FallbackManager TIDAK mengeksekusi; ia hanya memutuskan tindakan dan
        mencatat state. Runtime tetap satu-satunya executor.

        Returns:
            FallbackDecision final (bounded).
        """
        decision = self.decide(request)
        if decision.action == FallbackAction.FALLBACK:
            self._attempts += 1
        return decision

    # ------------------------------------------------------------------ #
    # Helper klasifikasi (dari error provider -> FallbackReason)
    # ------------------------------------------------------------------ #
    @staticmethod
    def classify_error(error: BaseException) -> FallbackReason:
        """Klasifikasi error provider menjadi FallbackReason (terstruktur).

        Memakai nama tipe + pesan (bukan branching provider). Error yang bukan
        dari provider (mis. tool/command) sebaiknya tidak sampai ke sini.
        """
        name = type(error).__name__.lower()
        text = str(error).lower()
        if "timeout" in name or "timeout" in text or "timed out" in text:
            return FallbackReason.TIMEOUT
        if "connection" in name or "connection" in text or "network" in text:
            return FallbackReason.CONNECTION_FAILURE
        if "unavailable" in name or "unavailable" in text:
            return FallbackReason.PROVIDER_UNAVAILABLE
        if "malformed" in text or "invalid" in text or "json" in text:
            return FallbackReason.MALFORMED_RESPONSE
        if "api" in name or "http" in text or "status" in text:
            return FallbackReason.PROVIDER_API_ERROR
        return FallbackReason.PROVIDER_API_ERROR

    @staticmethod
    def classify_error_from_message(message: str) -> FallbackReason:
        """Klasifikasi error provider dari pesan (bila tipe error tidak tersedia).

        Dipakai runtime yang hanya memiliki string error dari OrchestratorResult.
        Deterministik, tanpa branching provider.
        """
        text = (message or "").lower()
        if "timeout" in text or "timed out" in text:
            return FallbackReason.TIMEOUT
        if "connection" in text or "network" in text or "refused" in text:
            return FallbackReason.CONNECTION_FAILURE
        if "unavailable" in text:
            return FallbackReason.PROVIDER_UNAVAILABLE
        if "malformed" in text or "invalid" in text or "json" in text:
            return FallbackReason.MALFORMED_RESPONSE
        return FallbackReason.PROVIDER_API_ERROR
