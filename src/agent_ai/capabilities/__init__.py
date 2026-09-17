"""Model Capability Registry AETHER.

Provider-agnostic source of truth capability model/provider.

    from agent_ai.capabilities import (
        ModelCapability,
        ModelCapabilities,
        ModelCapabilityRegistry,
    )

Registry ini BUKAN decision engine: tidak ada fallback/routing/selection.
Capability didaftarkan eksplisit oleh konfigurasi/metadata.
"""

from agent_ai.capabilities.models import ModelCapabilities, ModelCapability
from agent_ai.capabilities.registry import ModelCapabilityRegistry

__all__ = [
    "ModelCapability",
    "ModelCapabilities",
    "ModelCapabilityRegistry",
]
