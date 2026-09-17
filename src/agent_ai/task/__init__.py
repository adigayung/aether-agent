"""Task Preparation Layer AETHER.

Menggabungkan task + ContextBuilder + Code Index/Repository Intelligence +
Project Brain + TaskPlanner menjadi PreparedTask sebelum Agent Runtime
berjalan:

    User Task -> TaskPreparation -> PreparedTask -> (Agent Runtime)

Layer ini read-only, tidak menjalankan tools, tidak memanggil ToolRegistry,
tidak menjalankan AgentLoop, dan tidak melakukan LLM call sendiri.

    from agent_ai.task import TaskPreparation

    prep = TaskPreparation(context_builder=builder, planner=planner)
    prepared = prep.prepare("Perbaiki login yang gagal")
"""

from agent_ai.task.models import PreparedTask
from agent_ai.task.preparation import TaskPreparation

__all__ = [
    "PreparedTask",
    "TaskPreparation",
]
