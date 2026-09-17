"""Intelligence Learner / Writer.

Mengambil hasil pekerjaan/observations dari agent dan menentukan knowledge
yang layak disimpan ke ProjectIntelligence.

Prinsip:
    - LLM TIDAK menulis langsung. LLM menghasilkan structured JSON yang
      divalidasi ketat sebelum disimpan.
    - Kategori dibatasi: architecture, facts, decisions, rules, learnings, problems.
    - Hindari duplikasi sederhana (bandingkan content dengan entry yang ada).
    - TIDAK menghapus knowledge lama; hanya add baru / skip duplicate.
    - Tidak menulis ke project source; hanya lewat ProjectIntelligence.
    - Tidak ada RAG/embeddings/vector DB/web search/terminal/Git.

    from agent_ai.projects import IntelligenceLearner

    learner = IntelligenceLearner(provider, intelligence)
    result = learner.learn(observations=["...", "..."])
    learner.add_verified("facts", "Python 3.11", confidence=0.9)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent_ai.projects.intelligence import ProjectIntelligence
from agent_ai.projects.models import (
    INTELLIGENCE_CATEGORIES,
    IntelligenceEntry,
)
from agent_ai.providers.base import BaseProvider, GenerateOptions, Message


class LearningError(Exception):
    """Base error untuk Intelligence Learner."""


class LearningParseError(LearningError):
    """Output LLM tidak dapat diparse menjadi struktur yang valid."""


# ---------------------------------------------------------------------------
# Prompt (provider-agnostic; teks biasa)
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "You extract durable, machine-readable knowledge about a software project "
    "from an agent's work observations. Produce knowledge FOR AN AI AGENT, not "
    "human documentation. Be concise and factual. "
    "Respond ONLY with a single JSON object, no prose, no markdown fences."
)

_INSTRUCTIONS = """\
From the observations below, extract knowledge worth storing.

Return a JSON object with EXACTLY these keys (arrays of entries):
  - "architecture": high-level structure, components, layering
  - "facts": concrete, verifiable facts
  - "decisions": notable technical decisions
  - "rules": conventions/constraints an AI should follow
  - "learnings": insights useful for future work
  - "problems": risks, gaps, or issues observed

Each entry MUST be an object:
  {"content": <string or object>, "confidence": <number 0..1>}

Rules:
  - Only include categories you have evidence for; empty arrays are allowed.
  - Do NOT invent facts not supported by the observations.
  - Output MUST be valid JSON. No trailing commas. No comments.

OBSERVATIONS:
"""


@dataclass
class LearningResult:
    """Ringkasan hasil learning."""

    added: Dict[str, int] = field(default_factory=dict)
    skipped_duplicates: int = 0
    total_added: int = 0
    provider: str = ""
    model: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "added": self.added,
            "skipped_duplicates": self.skipped_duplicates,
            "total_added": self.total_added,
            "provider": self.provider,
            "model": self.model,
        }


class IntelligenceLearner:
    """Menentukan & menyimpan knowledge baru ke ProjectIntelligence.

    Args:
        intelligence: ProjectIntelligence tempat menyimpan.
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
        self.provider = provider
        self.options = options

    # ------------------------------------------------------------------ #
    # Prompt building
    # ------------------------------------------------------------------ #
    @staticmethod
    def build_prompt(observations: List[str]) -> str:
        """Bangun prompt dari daftar observations (provider-agnostic)."""
        joined = "\n".join(f"- {obs}" for obs in observations)
        return _INSTRUCTIONS + joined

    # ------------------------------------------------------------------ #
    # Parsing / validation (strict)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_json(text: str) -> Dict[str, Any]:
        """Ekstrak objek JSON dari teks LLM secara ketat."""
        if not isinstance(text, str) or not text.strip():
            raise LearningParseError("Output LLM kosong.")

        candidate = text.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
        if fence:
            candidate = fence.group(1).strip()

        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise LearningParseError("Tidak menemukan objek JSON pada output LLM.")
        candidate = candidate[start : end + 1]

        try:
            parsed = json.loads(candidate)
        except ValueError as exc:
            raise LearningParseError(f"JSON tidak valid: {exc}") from exc

        if not isinstance(parsed, dict):
            raise LearningParseError("JSON harus berupa objek (dict).")
        return parsed

    @staticmethod
    def _validate_entry(raw: Any, category: str, index: int) -> IntelligenceEntry:
        """Validasi satu entry mentah menjadi IntelligenceEntry."""
        if not isinstance(raw, dict):
            raise LearningParseError(
                f"Entry #{index} pada kategori '{category}' bukan objek."
            )
        if "content" not in raw:
            raise LearningParseError(
                f"Entry #{index} pada kategori '{category}' tidak punya 'content'."
            )
        content = raw["content"]
        if content is None or (isinstance(content, str) and not content.strip()):
            raise LearningParseError(
                f"Entry #{index} pada kategori '{category}' punya 'content' kosong."
            )

        confidence = raw.get("confidence", 1.0)
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise LearningParseError(
                f"Entry #{index} pada kategori '{category}' punya 'confidence' non-numerik."
            )
        confidence = float(confidence)
        if not (0.0 <= confidence <= 1.0):
            raise LearningParseError(
                f"Entry #{index} pada kategori '{category}' punya 'confidence' di luar 0..1."
            )

        return IntelligenceEntry(content=content, source="ai", confidence=confidence)

    def parse(self, text: str) -> Dict[str, List[IntelligenceEntry]]:
        """Parse + validasi output LLM menjadi entry per kategori."""
        data = self._extract_json(text)

        result: Dict[str, List[IntelligenceEntry]] = {}
        for category in INTELLIGENCE_CATEGORIES:
            raw_entries = data.get(category, [])
            if raw_entries is None:
                raw_entries = []
            if not isinstance(raw_entries, list):
                raise LearningParseError(
                    f"Kategori '{category}' harus berupa array, bukan {type(raw_entries).__name__}."
                )
            entries: List[IntelligenceEntry] = []
            for index, raw in enumerate(raw_entries):
                entries.append(self._validate_entry(raw, category, index))
            result[category] = entries
        return result

    # ------------------------------------------------------------------ #
    # Duplicate detection (sederhana)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _content_key(content: Any) -> str:
        """Kunci perbandingan content (deterministik, sederhana)."""
        if isinstance(content, str):
            return content.strip().lower()
        return json.dumps(content, ensure_ascii=False, sort_keys=True).strip().lower()

    def _is_duplicate(self, category: str, content: Any) -> bool:
        """True bila content identik sudah ada di kategori."""
        key = self._content_key(content)
        for entry in self.intelligence.read_category(category):
            if self._content_key(entry.content) == key:
                return True
        return False

    # ------------------------------------------------------------------ #
    # Store (add / skip duplicate)
    # ------------------------------------------------------------------ #
    def _store(
        self,
        parsed: Dict[str, List[IntelligenceEntry]],
    ) -> LearningResult:
        """Simpan entry baru, lewati duplikat. Tidak menghapus yang lama."""
        self.intelligence.create()
        added: Dict[str, int] = {}
        skipped = 0
        total = 0
        for category, entries in parsed.items():
            count = 0
            for entry in entries:
                if self._is_duplicate(category, entry.content):
                    skipped += 1
                    continue
                self.intelligence.add_entry(category, entry)
                count += 1
                total += 1
            added[category] = count
        return LearningResult(added=added, skipped_duplicates=skipped, total_added=total)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def learn(self, observations: List[str]) -> LearningResult:
        """Pelajari knowledge dari observations via LLM, lalu simpan.

        Raises:
            LearningError: bila provider tidak tersedia.
            LearningParseError: bila output LLM invalid.
            ProviderError: error dari provider diteruskan apa adanya.
        """
        if self.provider is None:
            raise LearningError("Provider diperlukan untuk learn().")

        prompt = self.build_prompt(observations)
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=prompt),
        ]
        gen_result = self.provider.generate(messages=messages, options=self.options)
        response = self.provider.normalize_response(gen_result)

        parsed = self.parse(response.text)
        result = self._store(parsed)
        result.provider = response.provider or self.provider.name
        result.model = response.model
        return result

    def add_verified(
        self,
        category: str,
        content: Any,
        confidence: float = 1.0,
        source: str = "system",
    ) -> Optional[IntelligenceEntry]:
        """Tambah knowledge yang sudah diverifikasi eksplisit oleh sistem/agent.

        Args:
            category: kategori (harus valid).
            content: isi knowledge (tidak boleh kosong).
            confidence: 0..1.
            source: asal knowledge (default "system").

        Returns:
            IntelligenceEntry yang ditambahkan, atau None bila duplikat.

        Raises:
            LearningError: bila kategori/content/confidence invalid.
        """
        if category not in INTELLIGENCE_CATEGORIES:
            raise LearningError(
                f"Kategori '{category}' tidak valid. "
                f"Tersedia: {', '.join(INTELLIGENCE_CATEGORIES)}"
            )
        if content is None or (isinstance(content, str) and not content.strip()):
            raise LearningError("Content tidak boleh kosong.")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise LearningError("Confidence harus numerik 0..1.")
        confidence = float(confidence)
        if not (0.0 <= confidence <= 1.0):
            raise LearningError("Confidence harus berada di antara 0 dan 1.")

        if self._is_duplicate(category, content):
            return None

        self.intelligence.create()
        entry = IntelligenceEntry(content=content, source=source, confidence=confidence)
        return self.intelligence.add_entry(category, entry)
