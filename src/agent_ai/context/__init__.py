"""Package Context Manager.

Mengelola context (text, file, image reference) yang diberikan/diminta Agent
tanpa terikat provider tertentu.

    from agent_ai.context import ContextManager, ContextItem, ContextType

Tahap ini hanya fondasi: belum ada image processing/vision, OCR, planner,
LLM tool calling, autonomous loop, memory/database, Git, atau Django.
"""

from agent_ai.context.manager import ContextManager
from agent_ai.context.models import ContextItem, ContextType

__all__ = ["ContextManager", "ContextItem", "ContextType"]
