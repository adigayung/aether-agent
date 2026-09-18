"""Project Intelligence / AI Project Bible.

Mengelola knowledge untuk sebuah project dengan DUA mode storage:

1. Bible project-local (mode utama, dipakai produksi):
   `<root project target>/.aether/bible/<kategori>.md`
   Ditulis oleh `BibleStore` (markdown berorientasi LLM). Aktif bila `root`
   project target diberikan.

2. Storage JSON legacy (kompatibilitas):
   `<project_dir>/intelligence/<kategori>.json` di bawah workspace Agent-Ai.
   Dipakai HANYA bila root project target tidak diketahui (mis. komponen lama/
   verifier tanpa root). Bukan sumber kebenaran kedua: satu instance memakai
   SATU backend.

`ProjectBrain` tetap facade utama; modul ini tetap satu-satunya akses knowledge
(lewat ProjectIntelligence). Tidak ada RAG/vector DB/embeddings.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agent_ai.projects.aether_store import BibleStore
from agent_ai.projects.models import (
    BIBLE_CATEGORIES,
    CATEGORY_ALIASES,
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
    """Akses baca/tulis knowledge untuk sebuah project.

    Args:
        project_dir: direktori project di bawah workspace Agent-Ai
            (mis. J:\\Agent_Ai\\projects\\<id>). Dipakai untuk storage JSON
            legacy bila `root` tidak diberikan.
        root: root project target. Bila diberikan, knowledge disimpan sebagai
            AI Project Bible project-local di `<root>/.aether/bible/`.
    """

    def __init__(self, project_dir: Path, root: Optional[Union[str, Path]] = None) -> None:
        self.project_dir = Path(project_dir)
        self.intelligence_dir = self.project_dir / "intelligence"
        self.root = Path(root) if root is not None else None
        self._bible = BibleStore(self.root) if self.root is not None else None

    @classmethod
    def for_project(cls, root: Union[str, Path], project_dir: Optional[Path] = None) -> "ProjectIntelligence":
        """Bangun instance Bible-backed untuk sebuah root project target."""
        return cls(Path(project_dir) if project_dir is not None else Path(root), root=root)

    @property
    def categories(self) -> tuple:
        """Kategori yang valid untuk backend aktif (deterministik)."""
        return BIBLE_CATEGORIES if self._bible is not None else INTELLIGENCE_CATEGORIES

    @property
    def uses_bible(self) -> bool:
        """True bila instance ini memakai storage Bible project-local."""
        return self._bible is not None

    # ------------------------------------------------------------------ #
    # Create / load / save
    # ------------------------------------------------------------------ #
    def create(self) -> None:
        """Buat struktur folder + file knowledge kosong bila belum ada."""
        if self._bible is not None:
            self._bible.ensure()
            return
        self.intelligence_dir.mkdir(parents=True, exist_ok=True)
        for category in INTELLIGENCE_CATEGORIES:
            path = self._category_path(category)
            if not path.exists():
                self._write_json(path, [])

    def load(self) -> Dict[str, List[IntelligenceEntry]]:
        """Muat semua kategori intelligence."""
        data: Dict[str, List[IntelligenceEntry]] = {}
        for category in self.categories:
            data[category] = self.read_category(category)
        return data

    def save(self) -> None:
        """Pastikan struktur folder/file ada (idempotent)."""
        self.create()

    # ------------------------------------------------------------------ #
    # Category helpers
    # ------------------------------------------------------------------ #
    def _resolve_category(self, category: str) -> str:
        """Normalisasi kategori (termasuk alias lama) ke kategori backend."""
        available = self.categories
        if category in available:
            return category
        alias = CATEGORY_ALIASES.get(category)
        if alias is not None and alias in available:
            return alias
        raise UnknownCategoryError(
            f"Kategori '{category}' tidak dikenal. Tersedia: {', '.join(available)}"
        )

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
        resolved = self._resolve_category(category)
        if self._bible is not None:
            return self._bible.read_category(resolved)
        raw = self._read_json(self._category_path(resolved))
        if not isinstance(raw, list):
            raw = []
        return [IntelligenceEntry.from_dict(item) for item in raw]

    def add_entry(self, category: str, entry: IntelligenceEntry) -> IntelligenceEntry:
        """Tambahkan entry baru ke sebuah kategori."""
        resolved = self._resolve_category(category)
        if self._bible is not None:
            return self._bible.add_entry(resolved, entry)
        entries = self.read_category(resolved)
        entries.append(entry)
        self._write_json(self._category_path(resolved), [e.to_dict() for e in entries])
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
        resolved = self._resolve_category(category)
        if self._bible is not None:
            return self._bible.update_entry(resolved, entry_id, **changes)
        entries = self.read_category(resolved)
        for entry in entries:
            if entry.id == entry_id:
                for key, value in changes.items():
                    if hasattr(entry, key):
                        setattr(entry, key, value)
                from agent_ai.projects.models import _now_iso

                entry.updated_at = _now_iso()
                self._write_json(self._category_path(resolved), [e.to_dict() for e in entries])
                return entry
        raise EntryNotFoundError(f"Entry '{entry_id}' tidak ditemukan di kategori '{resolved}'.")

    def get_entry(self, category: str, entry_id: str) -> Optional[IntelligenceEntry]:
        """Ambil entry berdasarkan id (None bila tidak ada)."""
        for entry in self.read_category(category):
            if entry.id == entry_id:
                return entry
        return None
