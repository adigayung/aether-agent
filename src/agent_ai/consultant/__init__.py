"""AETHER Consultant: reasoning layer untuk memahami & merencanakan project.

Consultant BUKAN Agent kedua. Perbedaannya tegas:

    CONSULTANT                              AGENT
    think / analyze / investigate           read / write / execute
    validate / understand / recommend       modify / validate / complete
    plan / generate Task Proposal           execute task
    maintain Project Knowledge (Bible)      -

Boundary:
    CODE PROJECT     -> Consultant READ ONLY
    PROJECT BIBLE    -> Consultant READ + UPDATE (lewat tool update_project_bible)

Package ini TIDAK menduplikasi subsystem yang sudah ada:
    - reasoning/tool loop  : memakai `agent_ai.core.orchestrator` (existing).
    - tool execution       : memakai `agent_ai.core.executor.ToolExecutor` +
                             `agent_ai.tools.registry.ToolRegistry` (existing).
    - read/search tools    : memakai tools filesystem/terminal yang sudah ada.
    - Project Knowledge    : memakai AI Project Bible (`agent_ai.projects`)
                             yang sudah ada, bukan store pengetahuan baru.
    - boundary             : memakai Permission Policy Layer (`agent_ai.permission`).

Web access: belum tersedia di AETHER (tidak ada tool web). Dicatat sebagai
capability yang belum tersedia; TIDAK dibuat tool web palsu.
"""

from agent_ai.consultant.models import (
    ConsultantResult,
    ConsultantTurn,
    DEFAULT_CONSULTANT_MODE,
    MODE_INVESTIGATE,
    MODE_QUICK,
    normalize_consultant_mode,
)
from agent_ai.consultant.guard import (
    ConsultantBoundProvider,
    ConsultantRetrievalGuard,
    normalize_map_query,
)
from agent_ai.consultant.policy import (
    ConsultantRetrievalBudget,
    build_consultant_permission_manager,
    retrieval_budget_for_mode,
)
from agent_ai.consultant.prompt import (
    CONSULTANT_SYSTEM_PROMPT,
    build_consultant_system_prompt,
)
from agent_ai.consultant.service import ConsultantService, extract_task_proposal
from agent_ai.consultant.tools import (
    ConsultantBibleTool,
    ConsultantRunCommandTool,
    build_consultant_registry,
)

__all__ = [
    "ConsultantService",
    "ConsultantResult",
    "ConsultantTurn",
    "extract_task_proposal",
    "build_consultant_registry",
    "build_consultant_permission_manager",
    "retrieval_budget_for_mode",
    "ConsultantRetrievalBudget",
    "ConsultantRetrievalGuard",
    "ConsultantBoundProvider",
    "normalize_map_query",
    "ConsultantBibleTool",
    "ConsultantRunCommandTool",
    "CONSULTANT_SYSTEM_PROMPT",
    "build_consultant_system_prompt",
    "DEFAULT_CONSULTANT_MODE",
    "MODE_QUICK",
    "MODE_INVESTIGATE",
    "normalize_consultant_mode",
]
