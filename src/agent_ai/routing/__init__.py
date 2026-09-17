"""Model Routing Subsystem AETHER (#44).

Memilih provider/model paling sesuai untuk sebuah task SEBELUM eksekusi
dimulai. Deterministik (berbasis aturan/scoring), TIDAK memakai LLM.

    from agent_ai.routing import (
        ModelRouter,
        RoutingRegistry,
        RoutingRequest,
        RoutingDecision,
        TaskComplexity,
    )

Routing memakai subsystem yang sudah ada:
    - capabilities/  -> ModelCapabilityRegistry (source of truth capability)
    - providers/     -> provider abstraction (ketersediaan provider)
    - config/        -> RoutingConfig

Routing TIDAK melakukan fallback/retry/recovery/replanning (itu tahap lain),
dan TIDAK membuat capability registry kedua.
"""

from agent_ai.routing.classifier import TaskClassifier
from agent_ai.routing.models import (
    RoutingCandidate,
    RoutingConfig,
    RoutingDecision,
    RoutingRequest,
    TaskComplexity,
)
from agent_ai.routing.registry import RoutingRegistry
from agent_ai.routing.router import ModelRouter

__all__ = [
    "ModelRouter",
    "RoutingRegistry",
    "TaskClassifier",
    "RoutingRequest",
    "RoutingCandidate",
    "RoutingDecision",
    "RoutingConfig",
    "TaskComplexity",
]
