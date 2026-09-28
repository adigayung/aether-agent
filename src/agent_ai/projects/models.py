"""Model untuk Project Intelligence / AI Project Bible.

Project Intelligence disimpan sebagai AI Project Bible project-local di
`<root project target>/.aether/bible/` (lihat `agent_ai.projects.aether_store`).
Legacy: beberapa jalur (mis. verifier lama) masih memakai storage JSON di bawah
workspace Agent-Ai (J:\\Agent_Ai\\projects\\<id>\\intelligence\\) saat root
project tidak diketahui.

Model di sini provider-agnostic dan hanya representasi data (JSON-friendly).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    """Timestamp UTC dalam format ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    """Identifier unik singkat."""
    return uuid.uuid4().hex


# Kategori intelligence yang didukung storage JSON legacy (semantic lama).
INTELLIGENCE_CATEGORIES = (
    "architecture",
    "facts",
    "decisions",
    "rules",
    "learnings",
    "problems",
)

# Kategori AI Project Bible project-local (file `<kategori>.md` di
# `<root project target>/.aether/bible/`). "conventions" menggantikan "rules"
# agar knowledge tetap terwakili (lihat CATEGORY_ALIASES).
# "known_bugs"/"known_gaps" = persistent knowledge untuk masalah terverifikasi
# (known_bugs.md) dan kekurangan/belum-tersedia yang bukan bug (known_gaps.md).
BIBLE_CATEGORIES = (
    "architecture",
    "ui",
    "conventions",
    "decisions",
    "facts",
    "learnings",
    "problems",
    "known_bugs",
    "known_gaps",
)

# Alias kategori lama -> kategori kanonik Bible (semantic lama tetap terwakili).
CATEGORY_ALIASES = {
    "rules": "conventions",
}

# Skill scope yang dikenal. Task 01 hanya memakai project-level, namun
# desain tetap generic untuk ekstensi masa depan.
SKILL_SCOPE_PROJECT = "project"
SKILL_SCOPES = (SKILL_SCOPE_PROJECT,)


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


@dataclass
class Skill:
    """Model generik untuk sebuah Skill AETHER.

    Skill bersifat dynamic — Core hanya memahami konsep generic
    skill_id / name / description / scope / location. Nama directory
    Skill tidak di-hardcode di Core.

    Attributes:
        skill_id: identifier directory Skill (mis. "vue-ui").
        name: nama tampilan Skill.
        description: deskripsi singkat.
        scope: cakupan Skill (saat ini "project").
        content: isi markdown Skill (body di luar frontmatter).
        location: path absolut file skill.md (string).
        created_at: timestamp pembuatan.
        updated_at: timestamp update terakhir.
    """

    skill_id: str
    name: str
    description: str = ""
    scope: str = SKILL_SCOPE_PROJECT
    content: str = ""
    location: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "scope": self.scope,
            "content": self.content,
            "location": self.location,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Skill":
        return cls(
            skill_id=data.get("skill_id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            scope=data.get("scope", SKILL_SCOPE_PROJECT),
            content=data.get("content", ""),
            location=data.get("location", ""),
            created_at=data.get("created_at", _now_iso()),
            updated_at=data.get("updated_at", _now_iso()),
        )


@dataclass
class SkillCatalogEntry:
    """Metadata ringan untuk Skill Catalog / Discovery (tanpa content).

    Catalog hanya berisi field yang dibutuhkan LLM untuk mengenali Skill:
    skill_id, name, description, scope, location. Tidak memuat isi
    ``skill.md`` (body), references, atau Project Bible.

    Attributes:
        skill_id: identifier directory Skill.
        name: nama tampilan Skill.
        description: deskripsi singkat.
        scope: cakupan Skill (saat ini \"project\").
        location: path absolut file skill.md.
    """

    skill_id: str
    name: str
    description: str = ""
    scope: str = SKILL_SCOPE_PROJECT
    location: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "scope": self.scope,
            "location": self.location,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillCatalogEntry":
        return cls(
            skill_id=data.get("skill_id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            scope=data.get("scope", SKILL_SCOPE_PROJECT),
            location=data.get("location", ""),
        )
