"""Project Intelligence / AI Project Bible (storage JSON sederhana).

Mengelola file intelligence untuk sebuah project. SEMUA file ditulis di bawah
folder project Agent-Ai (mis. J:\\Agent_Ai\\projects\\<id>\\intelligence\\),
TIDAK PERNAH ke root project target.

Struktur:
    <project_dir>/
        project.json
        intelligence/
            architecture.json
            facts.json
            decisions.json
            rules.json
            learnings.json
            problems.json

Storage JSON sederhana agar mudah diganti (mis. ke database) nanti.
Belum ada RAG/vector DB/embeddings/autonomous learning.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_ai.projects.models import (
    INTELLIGENCE_CATEGORIES,
    IntelligenceEntry,
)


class IntelligenceError(Exception):
    """Base error untuk Project Intelligence."""


class UnknownCategoryError(IntelligenceError):
    """Kategori intelligence tidak dikenal."""


class EntryNotFoundError(IntelligenceError):
    """Entry intelligence tidak ditemukan."""


class ProjectIntelligence:
    """Akses baca/tulis file intelligence untuk sebuah project.

    Args:
        project_dir: direktori project di bawah workspace Agent-Ai
            (mis. J:\\Agent_Ai\\projects\\<id>). Semua file ditulis di sini.
    """

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = Path(project_dir)
        self.intelligence_dir = self.project_dir / "intelligence"

    # ------------------------------------------------------------------ #
    # Create / load / save
    # ------------------------------------------------------------------ #
    def create(self) -> None:
        """Buat struktur folder + file intelligence kosong bila belum ada."""
        self.intelligence_dir.mkdir(parents=True, exist_ok=True)
        for category in INTELLIGENCE_CATEGORIES:
            path = self._category_path(category)
            if not path.exists():
                self._write_json(path, [])

    def load(self) -> Dict[str, List[IntelligenceEntry]]:
        """Muat semua kategori intelligence."""
        data: Dict[str, List[IntelligenceEntry]] = {}
        for category in INTELLIGENCE_CATEGORIES:
            data[category] = self.read_category(category)
        return data

    def save(self) -> None:
        """Pastikan struktur folder/file ada (idempotent)."""
        self.create()

    # ------------------------------------------------------------------ #
    # Category helpers
    # ------------------------------------------------------------------ #
    def _category_path(self, category: str) -> Path:
        if category not in INTELLIGENCE_CATEGORIES:
            raise UnknownCategoryError(
                f"Kategori '{category}' tidak dikenal. "
                f"Tersedia: {', '.join(INTELLIGENCE_CATEGORIES)}"
            )
        return self.intelligence_dir / f"{category}.json"

    @staticmethod
    def _read_json(path: Path) -> Any:
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise IntelligenceError(f"Gagal membaca '{path}': {exc}") from exc

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------ #
    # Read / add / update
    # ------------------------------------------------------------------ #
    def read_category(self, category: str) -> List[IntelligenceEntry]:
        """Baca semua entry pada sebuah kategori."""
        raw = self._read_json(self._category_path(category))
        if not isinstance(raw, list):
            raw = []
        return [IntelligenceEntry.from_dict(item) for item in raw]

    def add_entry(self, category: str, entry: IntelligenceEntry) -> IntelligenceEntry:
        """Tambahkan entry baru ke sebuah kategori."""
        entries = self.read_category(category)
        entries.append(entry)
        self._write_json(self._category_path(category), [e.to_dict() for e in entries])
        return entry

    def update_entry(
        self,
        category: str,
        entry_id: str,
        **changes: Any,
    ) -> IntelligenceEntry:
        """Update entry berdasarkan id.

        Args:
            category: kategori entry.
            entry_id: id entry yang diupdate.
            **changes: field yang diubah (content/source/confidence).

        Raises:
            EntryNotFoundError: bila entry tidak ditemukan.
        """
        entries = self.read_category(category)
        for entry in entries:
            if entry.id == entry_id:
                for key, value in changes.items():
                    if hasattr(entry, key):
                        setattr(entry, key, value)
                from agent_ai.projects.models import _now_iso

                entry.updated_at = _now_iso()
                self._write_json(self._category_path(category), [e.to_dict() for e in entries])
                return entry
        raise EntryNotFoundError(f"Entry '{entry_id}' tidak ditemukan di kategori '{category}'.")

    def get_entry(self, category: str, entry_id: str) -> Optional[IntelligenceEntry]:
        """Ambil entry berdasarkan id (None bila tidak ada)."""
        for entry in self.read_category(category):
            if entry.id == entry_id:
                return entry
        return None
