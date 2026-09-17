"""Context Builder AETHER.

Membangun context relevan untuk LLM dari task + Code Index + Project
Intelligence/Bible + Brain + source files, tanpa mengirim seluruh project.

    from agent_ai.contextbuilder import ContextBuilder, ContextRequest

    builder = ContextBuilder(index=index, root=root, brain=brain)
    result = builder.build(ContextRequest(task="Perbaiki bug pada calculator"))
    text = result.to_text()
"""

from agent_ai.contextbuilder.builder import ContextBuilder
from agent_ai.contextbuilder.models import ContextRequest, ContextResult, RelevantFile

__all__ = [
    "ContextBuilder",
    "ContextRequest",
    "ContextResult",
    "RelevantFile",
]
