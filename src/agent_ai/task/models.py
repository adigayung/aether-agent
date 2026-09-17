"""Model untuk Task Preparation Layer.

Mendefinisikan hasil persiapan task sebelum Agent Runtime berjalan:

    User Task -> TaskPreparation -> PreparedTask -> (Agent Runtime)

PreparedTask menggabungkan task asli + context relevan + execution plan.
Model di sini plain data (dataclass), provider-agnostic, dan tidak menyimpan
chain-of-thought.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from agent_ai.contextbuilder.models import ContextResult
from agent_ai.planning.models import TaskPlan


@dataclass
class PreparedTask:
    """Task yang sudah disiapkan untuk Agent Runtime.

    Attributes:
        task: task asli dari user (tidak diubah).
        context: ContextResult dari ContextBuilder (boleh None bila tidak ada).
        plan: TaskPlan dari TaskPlanner (boleh None bila tidak ada).
        metadata: info tambahan bebas (mis. sumber dependency, statistik).
        task_id: identifier task opsional (untuk Task Lifecycle & Change
            Tracking). None bila tidak dilacak.
    """

    task: str
    context: Optional[ContextResult] = None
    plan: Optional[TaskPlan] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    task_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "context": self.context.to_dict() if self.context is not None else None,
            "plan": self.plan.to_dict() if self.plan is not None else None,
            "metadata": self.metadata,
            "task_id": self.task_id,
        }

    def context_text(self) -> str:
        """Render context menjadi teks (bila ada), untuk dipakai runtime."""
        return self.context.to_text() if self.context is not None else ""

