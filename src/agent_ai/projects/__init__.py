"""Package Project Intelligence / AI Project Bible.

Metadata project (`project.json`) disimpan di bawah workspace Agent-Ai
(mis. J:\\Agent_Ai\\projects\\<id>\\). Knowledge project (AI Project Bible)
disimpan project-local di `<root project target>/.aether/bible/`, dan log task
di `<root project target>/.aether/log/<task_id>.log`.

    from agent_ai.projects import ProjectRegistry

Tahap ini hanya fondasi storage + registry + loading (markdown Bible + JSON
legacy). Belum ada database, RAG/vector DB, embeddings, atau autonomous learning.
"""

from agent_ai.projects.aether_store import (
    AetherProjectStore,
    AetherTaskLog,
    BibleStore,
    TaskLog,
    new_task_id,
    safe_task_id,
)
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
    BIBLE_CATEGORIES,
    CATEGORY_ALIASES,
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
    "BIBLE_CATEGORIES",
    "CATEGORY_ALIASES",
    "AetherProjectStore",
    "AetherTaskLog",
    "BibleStore",
    "TaskLog",
    "new_task_id",
    "safe_task_id",
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
