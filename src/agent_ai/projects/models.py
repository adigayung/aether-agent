"""Model untuk Project Intelligence / AI Project Bible.

Project Intelligence disimpan DI LUAR project target, di bawah workspace
Agent-Ai (mis. J:\\Agent_Ai\\projects\\<id>\\). Tidak ada file Agent-Ai yang
ditulis ke root project target.

Model di sini provider-agnostic dan hanya representasi data (JSON-friendly).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _now_iso() -> str:
    """Timestamp UTC dalam format ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    """Identifier unik singkat."""
    return uuid.uuid4().hex


# Kategori intelligence yang didukung (sesuai struktur Project Bible).
INTELLIGENCE_CATEGORIES = (
    "architecture",
    "facts",
    "decisions",
    "rules",
    "learnings",
    "problems",
)


@dataclass
class ProjectConfig:
    """Konfigurasi sebuah project yang terdaftar di Agent-Ai.

    Attributes:
        id: identifier unik project (dipakai sebagai nama folder).
        name: nama tampilan project.
        root: absolute path root project target.
        created_at: timestamp pembuatan (ISO-8601).
        updated_at: timestamp update terakhir (ISO-8601).
        permission_mode: mode izin (default "workspace").
    """

    name: str
    root: str
    id: str = field(default_factory=_new_id)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    permission_mode: str = "workspace"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "root": self.root,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "permission_mode": self.permission_mode,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectConfig":
        return cls(
            id=data.get("id", _new_id()),
            name=data.get("name", ""),
            root=data.get("root", ""),
            created_at=data.get("created_at", _now_iso()),
            updated_at=data.get("updated_at", _now_iso()),
            permission_mode=data.get("permission_mode", "workspace"),
        )


@dataclass
class IntelligenceEntry:
    """Satu entri intelligence dalam sebuah kategori.

    Attributes:
        content: isi entri (teks/objek).
        source: asal entri (mis. "ai", "user", nama file).
        confidence: tingkat keyakinan (0.0 - 1.0).
        id: identifier unik entri.
        created_at: timestamp pembuatan (ISO-8601).
        updated_at: timestamp update terakhir (ISO-8601).
    """

    content: Any = ""
    source: str = "ai"
    confidence: float = 1.0
    id: str = field(default_factory=_new_id)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "source": self.source,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IntelligenceEntry":
        return cls(
            id=data.get("id", _new_id()),
            content=data.get("content", ""),
            source=data.get("source", "ai"),
            confidence=data.get("confidence", 1.0),
            created_at=data.get("created_at", _now_iso()),
            updated_at=data.get("updated_at", _now_iso()),
        )
