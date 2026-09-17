"""Provider Fallback Subsystem AETHER (#45).

Menangani kegagalan OPERASIONAL provider/model (provider unavailable,
connection/network failure, timeout, provider API error, malformed response)
dengan berpindah ke kandidat provider/model alternatif dan melanjutkan task.

    from agent_ai.fallback import (
        FallbackManager,
        FallbackRequest,
        FallbackDecision,
        FallbackAction,
        FallbackReason,
    )

Fallback memakai subsystem yang sudah ada:
    - routing/       -> RoutingRegistry (kandidat dari ModelCapabilityRegistry)
    - capabilities/  -> ModelCapabilityRegistry (source of truth capability)
    - reliability/   -> keputusan retry (tidak diduplikasi)
    - config/        -> FallbackConfig

Fallback TIDAK menangani tool/command/validation/workspace/repeated/no-progress/
replanning/recovery, TIDAK membuat provider/capability registry kedua, TIDAK
memakai LLM untuk memilih model, dan TIDAK melakukan fallback tanpa batas.
"""

from agent_ai.fallback.manager import FallbackManager
from agent_ai.fallback.models import (
    FallbackAction,
    FallbackCandidate,
    FallbackConfig,
    FallbackDecision,
    FallbackReason,
    FallbackRequest,
)
from agent_ai.fallback.policy import FallbackPolicy

__all__ = [
    "FallbackManager",
    "FallbackPolicy",
    "FallbackRequest",
    "FallbackCandidate",
    "FallbackDecision",
    "FallbackConfig",
    "FallbackAction",
    "FallbackReason",
]
