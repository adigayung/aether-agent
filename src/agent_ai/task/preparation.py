"""TaskPreparation: menggabungkan task + context + plan sebelum runtime.

Flow:
    User Task
        -> TaskPreparation
             - ContextBuilder   (context relevan)
             - Project Brain    (via ContextBuilder)
             - Code Index / Repository Intelligence (via ContextBuilder)
             - TaskPlanner      (execution plan)
        -> PreparedTask
        -> (nantinya Agent Runtime)

Prinsip:
    - Read-only, tidak menjalankan tools, tidak mengubah source project.
    - Tidak memanggil ToolRegistry, tidak menjalankan AgentLoop.
    - Tidak melakukan LLM call sendiri; provider-agnostic.
    - Deterministic selama dependency-nya deterministic.
    - Tidak menduplikasi logic ContextBuilder / TaskPlanner.
"""

from __future__ import annotations

from typing import Any, Optional

from agent_ai.contextbuilder.builder import ContextBuilder
from agent_ai.contextbuilder.models import ContextRequest
from agent_ai.planning.planner import TaskPlanner
from agent_ai.task.models import PreparedTask


class TaskPreparation:
    """Menyiapkan task menjadi PreparedTask (context + plan).

    Args:
        context_builder: ContextBuilder opsional. Bila None, context tidak
            dibangun (PreparedTask.context = None).
        planner: TaskPlanner opsional. Bila None, dibuat TaskPlanner default.
        brain: ProjectBrain opsional. Hanya dipakai bila context_builder
            belum menerima brain (diteruskan ke ContextBuilder).
    """

    def __init__(
        self,
        context_builder: Optional[ContextBuilder] = None,
        planner: Optional[TaskPlanner] = None,
        brain: Optional[object] = None,
    ) -> None:
        self.context_builder = context_builder
        self.planner = planner or TaskPlanner()
        self.brain = brain

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def prepare(
        self,
        task: str,
        request: Optional[ContextRequest] = None,
        context: Optional[dict] = None,
        task_id: Optional[str] = None,
    ) -> PreparedTask:
        """Siapkan task menjadi PreparedTask.

        Args:
            task: task/request user.
            request: ContextRequest opsional (override default). Bila None,
                dibuat dari task.
            context: context tambahan opsional untuk planner (mis. hasil
                Repository Intelligence). Diteruskan apa adanya ke planner.
            task_id: identifier task opsional (untuk Task Lifecycle & Change
                Tracking). Diteruskan ke PreparedTask tanpa mengubah perilaku.

        Returns:
            PreparedTask berisi task asli + context + plan.

        Raises:
            ValueError: bila task kosong.
        """
        if not task or not task.strip():
            raise ValueError("Task tidak boleh kosong.")

        task = task.strip()

        # 1) Context dari ContextBuilder (bukan dibuat ulang di sini).
        context_result = None
        if self.context_builder is not None:
            req = request or ContextRequest(task=task)
            context_result = self.context_builder.build(req)

        # 2) Plan dari TaskPlanner (bukan dibuat ulang di sini).
        plan_context = dict(context or {})
        if context_result is not None:
            plan_context.setdefault("context_metadata", context_result.metadata)
        plan = self.planner.create_plan(task, context=plan_context)

        metadata = {
            "has_context": context_result is not None,
            "has_plan": plan is not None,
            "planner": plan.metadata.get("planner") if plan is not None else None,
            "plan_kind": plan.metadata.get("kind") if plan is not None else None,
            "step_count": len(plan.steps) if plan is not None else 0,
        }

        return PreparedTask(
            task=task,
            context=context_result,
            plan=plan,
            metadata=metadata,
            task_id=task_id,
        )

