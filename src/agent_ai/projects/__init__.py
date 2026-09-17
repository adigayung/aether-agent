"""Package Project Intelligence / AI Project Bible.

Menyimpan intelligence project DI LUAR project target, di bawah workspace
Agent-Ai (mis. J:\\Agent_Ai\\projects\\<id>\\).

    from agent_ai.projects import ProjectRegistry

Tahap ini hanya fondasi storage + registry + loading (JSON sederhana).
Belum ada database, RAG/vector DB, embeddings, atau autonomous learning.
"""

from agent_ai.projects.bible import (
    BibleError,
    BibleParseError,
    BibleResult,
    ProjectBibleGenerator,
)
from agent_ai.projects.brain import BrainContext, ProjectBrain
from agent_ai.projects.context import ProjectIntelligenceContext
from agent_ai.projects.discovery import DiscoveryEngine, DiscoveryResult
from agent_ai.projects.intelligence import (
    EntryNotFoundError,
    IntelligenceError,
    ProjectIntelligence,
    UnknownCategoryError,
)
from agent_ai.projects.learning import (
    IntelligenceLearner,
    LearningError,
    LearningParseError,
    LearningResult,
)
from agent_ai.projects.models import (
    INTELLIGENCE_CATEGORIES,
    IntelligenceEntry,
    ProjectConfig,
)
from agent_ai.projects.registry import (
    ProjectError,
    ProjectNotFoundError,
    ProjectRegistry,
    ProjectRootNotFoundError,
)

__all__ = [
    "ProjectRegistry",
    "ProjectConfig",
    "ProjectIntelligence",
    "IntelligenceEntry",
    "INTELLIGENCE_CATEGORIES",
    "DiscoveryEngine",
    "DiscoveryResult",
    "ProjectBibleGenerator",
    "BibleResult",
    "BibleError",
    "BibleParseError",
    "ProjectIntelligenceContext",
    "ProjectBrain",
    "BrainContext",
    "IntelligenceLearner",
    "LearningResult",
    "LearningError",
    "LearningParseError",
    "ProjectError",
    "ProjectNotFoundError",
    "ProjectRootNotFoundError",
    "IntelligenceError",
    "UnknownCategoryError",
    "EntryNotFoundError",
]

