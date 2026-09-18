"""Project Intelligence Reader / Context Provider.

Membaca knowledge dari ProjectIntelligence dan menyediakan context terstruktur
untuk Agent/LLM.

Prinsip:
    - READ-ONLY: hanya membaca ProjectIntelligence; tidak mengubah intelligence.
    - Tidak membaca project source; tidak menjalankan tool.
    - Deterministic (urutan kategori & entry stabil).
    - Provider-agnostic (menghasilkan teks biasa, bukan format provider).
    - Tidak menyertakan metadata/path internal yang tidak diperlukan.
    - Tidak ada embeddings/RAG/vector search.

    from agent_ai.projects import ProjectIntelligenceContext

    ctx = ProjectIntelligenceContext(intelligence)
    text = ctx.to_context_text()                 # semua kategori
    text = ctx.to_context_text(categories=["facts", "rules"])
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent_ai.projects.intelligence import ProjectIntelligence
from agent_ai.projects.models import INTELLIGENCE_CATEGORIES


class ProjectIntelligenceContext:
    """Reader context dari ProjectIntelligence.

    Args:
        intelligence: instance ProjectIntelligence (read-only).
    """

    def __init__(self, intelligence: ProjectIntelligence) -> None:
        self.intelligence = intelligence

    # ------------------------------------------------------------------ #
    # Category resolution
    # ------------------------------------------------------------------ #
    def _resolve_categories(self, categories: Optional[List[str]]) -> List[str]:
        """Tentukan kategori yang dipakai (deterministik).

        Kategori mengikuti backend ProjectIntelligence aktif (Bible
        project-local atau storage JSON legacy).

        Args:
            categories: daftar kategori opsional. Bila None, gunakan semua.

        Returns:
            Daftar kategori valid, urut sesuai kategori kanonik backend.

        Raises:
            ValueError: bila ada kategori tidak dikenal.
        """
        available = list(getattr(self.intelligence, "categories", INTELLIGENCE_CATEGORIES))
        if categories is None:
            return available
        unknown = [c for c in categories if c not in available]
        if unknown:
            raise ValueError(
                f"Kategori tidak dikenal: {', '.join(unknown)}. "
                f"Tersedia: {', '.join(available)}"
            )
        # Pertahankan urutan kanonik & hilangkan duplikat.
        selected = set(categories)
        return [c for c in available if c in selected]

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #
    def read(
        self,
        categories: Optional[List[str]] = None,
    ) -> Dict[str, List[Any]]:
        """Baca entry per kategori (hanya `content`, tanpa metadata internal).

        Args:
            categories: kategori opsional. Bila None, semua kategori.

        Returns:
            Dict {kategori: [content, ...]} (kategori kosong tetap disertakan).
        """
        selected = self._resolve_categories(categories)
        data: Dict[str, List[Any]] = {}
        for category in selected:
            entries = self.intelligence.read_category(category)
            data[category] = [entry.content for entry in entries]
        return data

    # ------------------------------------------------------------------ #
    # Context text
    # ------------------------------------------------------------------ #
    def to_context_text(
        self,
        categories: Optional[List[str]] = None,
        include_empty: bool = False,
    ) -> str:
        """Hasilkan context teks terstruktur untuk LLM.

        Args:
            categories: kategori opsional. Bila None, semua kategori.
            include_empty: sertakan kategori tanpa entry (default False).

        Returns:
            Teks terstruktur (deterministik, provider-agnostic).
        """
        data = self.read(categories)

        lines: List[str] = ["# Project Intelligence"]
        for category, contents in data.items():
            if not contents and not include_empty:
                continue
            lines.append(f"\n## {category}")
            if not contents:
                lines.append("(kosong)")
                continue
            for content in contents:
                lines.append(f"- {self._format_content(content)}")
        return "\n".join(lines)

    @staticmethod
    def _format_content(content: Any) -> str:
        """Format satu content menjadi teks ringkas (tanpa metadata)."""
        if isinstance(content, str):
            return content
        if isinstance(content, (int, float, bool)):
            return str(content)
        # Struktur (dict/list): tampilkan sebagai JSON ringkas deterministik.
        import json

        return json.dumps(content, ensure_ascii=False, sort_keys=True)

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def summary(self, categories: Optional[List[str]] = None) -> Dict[str, int]:
        """Jumlah entry per kategori (untuk inspeksi cepat)."""
        return {category: len(contents) for category, contents in self.read(categories).items()}

