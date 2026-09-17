"""Advanced Context / Token Budgeting AETHER (#40).

Mengurangi token yang dikirim ke LLM dengan retrieval profile (minimal/
balanced/deep), context budget, intelligent retrieval, partial reading,
duplicate-read prevention, dan context compaction.

Reuse Code Index + Repository Intelligence v2 + Context Builder.
Tanpa LLM/embeddings/vector DB/database. Policy configurable (tidak hardcode).

    from agent_ai.contextbudget import ProfileRegistry, ContextBudget

    registry = ProfileRegistry.default()
    budget = registry.budget("balanced")
"""

from agent_ai.contextbudget.budget import (
    BudgetUsage,
    ContextBudget,
    TokenEstimator,
)
from agent_ai.contextbudget.compaction import CompactContext, ContextCompactor
from agent_ai.contextbudget.dedup import ReadRecord, ReadTracker
from agent_ai.contextbudget.partial import PartialRead, PartialReader
from agent_ai.contextbudget.profiles import (
    PROFILE_NAMES,
    Budget,
    ProfileRegistry,
    RetrievalProfile,
)
from agent_ai.contextbudget.retrieval import IntelligentRetriever, RetrievedFile

__all__ = [
    "Budget",
    "RetrievalProfile",
    "ProfileRegistry",
    "PROFILE_NAMES",
    "TokenEstimator",
    "ContextBudget",
    "BudgetUsage",
    "PartialReader",
    "PartialRead",
    "ReadTracker",
    "ReadRecord",
    "IntelligentRetriever",
    "RetrievedFile",
    "ContextCompactor",
    "CompactContext",
]
