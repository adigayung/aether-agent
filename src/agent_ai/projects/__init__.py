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
from agent_ai.projects.project_map import (
    FINGERPRINT_ALGORITHM,
    MAP_TYPE_ATLAS,
    MAP_TYPE_RIG,
    MAP_TYPES,
    META_VERSION,
    SOURCE_EXTENSIONS,
    STATUS_AVAILABLE,
    STATUS_FRESH,
    STATUS_INVALID,
    STATUS_MISSING,
    STATUS_STALE,
    InvalidMapTypeError,
    MapEngineError,
    MapInvalidError,
    MapNotFoundError,
    ProjectMapError,
    ProjectMapService,
)
from agent_ai.projects.project_map_query import (
    ATLAS_KINDS,
    DEFAULT_MAX_RELATED,
    DEFAULT_MAX_RESULTS,
    HARD_MAX_RESULTS,
    RIG_KINDS,
    SUPPORTED_RELATIONS,
    AtlasMapQuery,
    EmptyQueryError,
    MapQueryError,
    RigMapQuery,
    UnsupportedRelationError,
    atlas_query,
    rig_query,
)
from agent_ai.projects.registry import (
    ProjectError,
    ProjectNotFoundError,
    ProjectRegistry,
    ProjectRootNotFoundError,
)
from agent_ai.projects.retrieval import (
    CATEGORY_IMPORTANCE,
    CORE_CATEGORIES,
    DEFAULT_BUDGET_TOKENS,
    BibleRetriever,
    RetrievalResult,
    RetrievedEntry,
    estimate_tokens,
    query_keywords,
    retrieve_bible_context,
)

__all__ = [
    "BibleRetriever",
    "RetrievalResult",
    "RetrievedEntry",
    "retrieve_bible_context",
    "query_keywords",
    "estimate_tokens",
    "DEFAULT_BUDGET_TOKENS",
    "CORE_CATEGORIES",
    "CATEGORY_IMPORTANCE",
    "ProjectMapService",
    "ProjectMapError",
    "InvalidMapTypeError",
    "MapNotFoundError",
    "MapInvalidError",
    "MapEngineError",
    "MAP_TYPE_ATLAS",
    "MAP_TYPE_RIG",
    "MAP_TYPES",
    "STATUS_MISSING",
    "STATUS_AVAILABLE",
    "STATUS_INVALID",
    "STATUS_FRESH",
    "STATUS_STALE",
    "SOURCE_EXTENSIONS",
    "FINGERPRINT_ALGORITHM",
    "META_VERSION",
    "AtlasMapQuery",
    "RigMapQuery",
    "MapQueryError",
    "EmptyQueryError",
    "UnsupportedRelationError",
    "atlas_query",
    "rig_query",
    "ATLAS_KINDS",
    "RIG_KINDS",
    "SUPPORTED_RELATIONS",
    "DEFAULT_MAX_RESULTS",
    "DEFAULT_MAX_RELATED",
    "HARD_MAX_RESULTS",
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
