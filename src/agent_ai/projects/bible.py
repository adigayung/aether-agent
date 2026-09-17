"""Project Intelligence / AI Project Bible Generator.

Menganalisis hasil Discovery Engine dengan LLM (via provider abstraction) dan
menghasilkan knowledge terstruktur untuk AI (bukan dokumentasi manusia),
lalu menyimpannya melalui ProjectIntelligence.

Alur:
    DiscoveryResult -> ProjectBibleGenerator -> LLM -> parse/validasi ketat
        -> ProjectIntelligence.add_entry() (kategori: architecture, facts,
           decisions, rules, learnings, problems)

Prinsip:
    - Provider-agnostic (memakai BaseProvider.generate + normalize_response).
    - TIDAK menulis file apa pun ke project source; hanya lewat storage
      ProjectIntelligence (di bawah workspace Agent-Ai).
    - Output LLM TIDAK langsung dipercaya: parsing + validasi ketat.
    - Tidak menghapus intelligence lama; hanya menambah entry baru.
    - Tidak ada RAG/embeddings/vector DB/autonomous learning/terminal/Git.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent_ai.projects.discovery import DiscoveryResult
from agent_ai.projects.intelligence import ProjectIntelligence
from agent_ai.projects.models import (
    INTELLIGENCE_CATEGORIES,
    IntelligenceEntry,
)
from agent_ai.providers.base import BaseProvider, GenerateOptions, Message


class BibleError(Exception):
    """Base error untuk Project Bible Generator."""


class BibleParseError(BibleError):
    """Output LLM tidak dapat diparse menjadi struktur yang valid."""


# ---------------------------------------------------------------------------
# Prompt (provider-agnostic; hanya teks biasa)
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "You are an AI that builds a machine-readable knowledge base about a "
    "software project. Produce knowledge FOR AN AI AGENT, not human "
    "documentation. Be concise, factual, and structured. "
    "Respond ONLY with a single JSON object, no prose, no markdown fences."
)

_INSTRUCTIONS = """\
Analyze the project discovery data below and produce structured knowledge.

Return a JSON object with EXACTLY these keys (arrays of entries):
  - "architecture": high-level structure, components, layering
  - "facts": concrete, verifiable facts about the project
  - "decisions": notable technical decisions implied by the project
  - "rules": conventions/constraints an AI should follow when working here
  - "learnings": insights useful for future work
  - "problems": risks, gaps, or issues observed

Each entry MUST be an object:
  {"content": <string or object>, "confidence": <number 0..1>}

Rules:
  - Only include categories you have evidence for; empty arrays are allowed.
  - Do NOT invent files or facts not supported by the discovery data.
  - Output MUST be valid JSON. No trailing commas. No comments.

DISCOVERY DATA (JSON):
"""


@dataclass
class BibleResult:
    """Ringkasan hasil pembuatan Bible."""

    categories: Dict[str, int] = field(default_factory=dict)
    total: int = 0
    provider: str = ""
    model: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "categories": self.categories,
            "total": self.total,
            "provider": self.provider,
            "model": self.model,
        }


class ProjectBibleGenerator:
    """Menghasilkan Project Intelligence dari DiscoveryResult via LLM.

    Args:
        provider: instance BaseProvider (abstraction).
        intelligence: ProjectIntelligence tempat menyimpan hasil.
        options: GenerateOptions untuk pemanggilan LLM.
    """

    def __init__(
        self,
        provider: BaseProvider,
        intelligence: ProjectIntelligence,
        options: Optional[GenerateOptions] = None,
    ) -> None:
        self.provider = provider
        self.intelligence = intelligence
        self.options = options

    # ------------------------------------------------------------------ #
    # Prompt building
    # ------------------------------------------------------------------ #
    def build_prompt(self, discovery: DiscoveryResult) -> str:
        """Bangun prompt dari DiscoveryResult (provider-agnostic)."""
        data = discovery.to_dict()
        # Batasi ukuran agar prompt tidak membengkak (discovery sudah bounded).
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        return _INSTRUCTIONS + payload

    # ------------------------------------------------------------------ #
    # Parsing / validation (strict)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_json(text: str) -> Dict[str, Any]:
        """Ekstrak objek JSON dari teks LLM secara ketat.

        Menerima JSON murni atau yang dibungkus code fence. Menolak bila
        tidak ada objek JSON valid.
        """
        if not isinstance(text, str) or not text.strip():
            raise BibleParseError("Output LLM kosong.")

        candidate = text.strip()

        # Buang code fence ```json ... ``` bila ada.
        fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
        if fence:
            candidate = fence.group(1).strip()

        # Ambil objek JSON pertama yang seimbang bila ada teks tambahan.
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise BibleParseError("Tidak menemukan objek JSON pada output LLM.")
        candidate = candidate[start : end + 1]

        try:
            parsed = json.loads(candidate)
        except ValueError as exc:
            raise BibleParseError(f"JSON tidak valid: {exc}") from exc

        if not isinstance(parsed, dict):
            raise BibleParseError("JSON harus berupa objek (dict).")
        return parsed

    @staticmethod
    def _validate_entry(raw: Any, category: str, index: int) -> IntelligenceEntry:
        """Validasi satu entry mentah menjadi IntelligenceEntry.

        Raises:
            BibleParseError: bila entry tidak sesuai skema.
        """
        if not isinstance(raw, dict):
            raise BibleParseError(
                f"Entry #{index} pada kategori '{category}' bukan objek."
            )
        if "content" not in raw:
            raise BibleParseError(
                f"Entry #{index} pada kategori '{category}' tidak punya 'content'."
            )
        content = raw["content"]
        if content is None or (isinstance(content, str) and not content.strip()):
            raise BibleParseError(
                f"Entry #{index} pada kategori '{category}' punya 'content' kosong."
            )

        confidence = raw.get("confidence", 1.0)
        if not isinstance(confidence, (int, float)):
            raise BibleParseError(
                f"Entry #{index} pada kategori '{category}' punya 'confidence' non-numerik."
            )
        confidence = float(confidence)
        if not (0.0 <= confidence <= 1.0):
            raise BibleParseError(
                f"Entry #{index} pada kategori '{category}' punya 'confidence' di luar 0..1."
            )

        return IntelligenceEntry(
            content=content,
            source="ai",
            confidence=confidence,
        )

    def parse(self, text: str) -> Dict[str, List[IntelligenceEntry]]:
        """Parse + validasi output LLM menjadi entry per kategori.

        Raises:
            BibleParseError: bila output invalid.
        """
        data = self._extract_json(text)

        result: Dict[str, List[IntelligenceEntry]] = {}
        for category in INTELLIGENCE_CATEGORIES:
            raw_entries = data.get(category, [])
            if raw_entries is None:
                raw_entries = []
            if not isinstance(raw_entries, list):
                raise BibleParseError(
                    f"Kategori '{category}' harus berupa array, bukan {type(raw_entries).__name__}."
                )
            entries: List[IntelligenceEntry] = []
            for index, raw in enumerate(raw_entries):
                entries.append(self._validate_entry(raw, category, index))
            result[category] = entries
        return result

    # ------------------------------------------------------------------ #
    # Generate + store
    # ------------------------------------------------------------------ #
    def generate(self, discovery: DiscoveryResult) -> BibleResult:
        """Jalankan LLM, parse, validasi, dan simpan ke ProjectIntelligence.

        Tidak menghapus intelligence lama; hanya menambah entry baru.

        Raises:
            BibleParseError: bila output LLM invalid.
            ProviderError: error dari provider diteruskan apa adanya.
        """
        prompt = self.build_prompt(discovery)
        messages = [
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=prompt),
        ]

        gen_result = self.provider.generate(messages=messages, options=self.options)
        response = self.provider.normalize_response(gen_result)

        parsed = self.parse(response.text)

        # Pastikan struktur storage ada, lalu simpan (append) per kategori.
        self.intelligence.create()
        counts: Dict[str, int] = {}
        total = 0
        for category, entries in parsed.items():
            for entry in entries:
                self.intelligence.add_entry(category, entry)
            counts[category] = len(entries)
            total += len(entries)

        return BibleResult(
            categories=counts,
            total=total,
            provider=response.provider or self.provider.name,
            model=response.model,
        )
