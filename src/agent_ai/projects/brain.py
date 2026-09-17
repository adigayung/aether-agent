"""Project Brain Integration Layer.

Facade/orchestration layer yang menghubungkan Project Intelligence dengan
Agent. ProjectBrain TIDAK menduplikasi logic Context atau Learning; ia hanya
mendelegasikan ke komponen yang sudah ada:

    - get_context(...)  -> ProjectIntelligenceContext
    - learn(...)        -> IntelligenceLearner
    - add_verified(...) -> IntelligenceLearner

Prinsip:
    - Provider-agnostic.
    - Tidak menulis ke project source; hanya lewat ProjectIntelligence.
    - Tidak ada RAG/embeddings/vector DB/web search/terminal/Git.

    from agent_ai.projects import ProjectBrain

    brain = ProjectBrain(intelligence, provider=provider)
    text = brain.get_context(categories=["facts", "rules"])
    result = brain.learn(observations=["..."])
    brain.add_verified("facts", "Python 3.11", confidence=0.9)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent_ai.projects.context import ProjectIntelligenceContext
from agent_ai.projects.intelligence import ProjectIntelligence
from agent_ai.projects.learning import IntelligenceLearner, LearningResult
from agent_ai.projects.models import IntelligenceEntry
from agent_ai.providers.base import BaseProvider, GenerateOptions


@dataclass
class BrainContext:
    """Hasil pembacaan context oleh Brain."""

    text: str = ""
    categories: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "categories": self.categories}


class ProjectBrain:
    """Facade untuk operasi Project Intelligence.

    Args:
        intelligence: instance ProjectIntelligence.
        provider: instance BaseProvider (opsional; diperlukan untuk learn()).
        options: GenerateOptions untuk pemanggilan LLM.
    """

    def __init__(
        self,
        intelligence: ProjectIntelligence,
        provider: Optional[BaseProvider] = None,
        options: Optional[GenerateOptions] = None,
    ) -> None:
        self.intelligence = intelligence
        self._context = ProjectIntelligenceContext(intelligence)
        self._learner = IntelligenceLearner(intelligence, provider=provider, options=options)

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #
    def get_context(
        self,
        categories: Optional[List[str]] = None,
        include_empty: bool = False,
    ) -> BrainContext:
        """Baca context terstruktur dari Project Intelligence.

        Args:
            categories: kategori opsional. Bila None, semua kategori.
            include_empty: sertakan kategori tanpa entry.

        Returns:
            BrainContext (text + jumlah entry per kategori).
        """
        text = self._context.to_context_text(categories=categories, include_empty=include_empty)
        summary = self._context.summary(categories=categories)
        return BrainContext(text=text, categories=summary)

    def read(
        self,
        categories: Optional[List[str]] = None,
    ) -> Dict[str, List[Any]]:
        """Baca content per kategori (tanpa metadata internal)."""
        return self._context.read(categories=categories)

    # ------------------------------------------------------------------ #
    # Write (delegasi ke IntelligenceLearner)
    # ------------------------------------------------------------------ #
    def learn(self, observations: List[str]) -> LearningResult:
        """Pelajari knowledge dari observations via LLM (delegasi ke learner)."""
        return self._learner.learn(observations)

    def add_verified(
        self,
        category: str,
        content: Any,
        confidence: float = 1.0,
        source: str = "system",
    ) -> Optional[IntelligenceEntry]:
        """Tambah knowledge terverifikasi (delegasi ke learner)."""
        return self._learner.add_verified(
            category, content, confidence=confidence, source=source
        )

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def summary(self, categories: Optional[List[str]] = None) -> Dict[str, int]:
        """Jumlah entry per kategori."""
        return self._context.summary(categories=categories)

    def __repr__(self) -> str:  # pragma: no cover - bantuan debug
        return f"<ProjectBrain categories={len(self.summary())}>"
