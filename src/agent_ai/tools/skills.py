"""Skill System tools — shared capability for Agent & Consultant (Task 05).

Satu mekanisme Skill yang sama untuk Agent dan Consultant (thin & generic):

    SkillStore
    Skill Catalog (lightweight metadata)
    load_skill(skill_id) -> skill.md
    load_skill_reference(skill_id, reference) -> references/<reference>

Lifecycle (Agent only, LLM-driven):

    create_skill(skill_id, name, description, scope, content) -> Skill
    update_skill(skill_id, ...) -> Skill
    delete_skill(skill_id) -> void

Prinsip Task 05:
    - Agent & Consultant memakai Skill System yang SAMA (SkillStore existing)
    - LLM melihat catalog via tool, memilih skill_id sendiri, memuat skill.md
      dan reference on-demand (progressive loading).
    - LLM memutuskan apakah perlu create/update/delete Skill (thin tools);
      TIDAK ada auto-create / auto-update / keyword matcher / scoring /
      heuristic / automatic skill selection / automatic generation.
    - Tidak ada automatic selector / keyword heuristic / scoring
    - Skill hanya procedural guidance / context, bukan permission grant
    - Tidak ada auto-load semua skill / semua reference
    - Tidak ada truncation/compression baru
    - Consultant tetap read-only (skill read tools = READ_ONLY; lifecycle
      mutation tools hanya tersedia pada Agent sesuai permission policy)
    - Catalog tetap dynamic (membaca langsung dari storage, tanpa cache stale)

Tidak ada storage/loader baru: hanya wrapper tool di atas SkillStore existing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_ai.tools.base import BaseTool, ToolValidationError, ToolExecutionError


class _SkillToolBase(BaseTool):
    """Base untuk tool Skill: root project + SkillStore delegation."""

    def __init__(self, root: Optional[Any] = None) -> None:
        self.root = Path(root).resolve() if root is not None else None

    def _require_root(self) -> Path:
        if self.root is None:
            raise ToolValidationError(
                "Project root tidak tersedia; Skill System tidak dapat dijalankan "
                "(root project harus di-set saat tool dibuat)."
            )
        return self.root

    def _store(self):
        from agent_ai.projects.skills import SkillStore

        return SkillStore(self._require_root())


class SkillCatalogTool(_SkillToolBase):
    """Melihat katalog Skill (lightweight metadata, tanpa isi skill.md)."""

    name = "skill_catalog"
    description = (
        "Melihat Katalog Skill yang tersedia di project ini (metadata ringan). "
        "Mengembalikan daftar skill_id, name, description, scope, location TANPA isi "
        "skill.md / references (progressive). Gunakan ini dulu untuk discovery; "
        "LLM yang memilih 0, 1, atau beberapa skill_id, lalu panggil load_skill(skill_id) "
        "untuk skill yang diperlukan. Jangan otomatis memuat semua skill; hanya yang dipilih LLM."
    )
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {},
    }

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        store = self._store()
        try:
            catalog = store.get_catalog()
        except Exception as exc:
            raise ToolExecutionError(f"Gagal membaca Skill catalog: {exc}") from exc
        entries = [e.to_dict() for e in catalog]
        return {
            "count": len(entries),
            "skills": entries,
        }


class LoadSkillTool(_SkillToolBase):
    """Memuat skill.md untuk skill_id yang dipilih LLM (progressive)."""

    name = "load_skill"
    description = (
        "Memuat isi skill.md untuk skill_id yang dipilih LLM (progressive, on-demand). "
        "Generic & dynamic: skill_id adalah identifier directory Skill (tidak hardcode). "
        "Hanya membaca skill.md milik Skill tersebut — tidak memuat references atau Skill lain. "
        "Panggil setelah melihat skill_catalog dan memilih skill yang relevan. "
        "Dapat dipanggil 0, 1, atau beberapa kali dalam satu task (LLM yang memutuskan). "
        "Skill hanya memberikan procedural guidance/context, tidak mengambil alih orchestrator."
    )
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "string",
                "description": "Identifier Skill (nama directory skill, mis. 'vue-ui' atau 'python').",
            }
        },
        "required": ["skill_id"],
    }

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        skill_id = arguments.get("skill_id")
        if skill_id is None or not str(skill_id).strip():
            raise ToolValidationError("Argumen 'skill_id' wajib diisi.")
        store = self._store()
        try:
            skill = store.load_skill(str(skill_id).strip())
        except Exception as exc:
            # Map known Skill errors to validation/execution for clear LLM feedback
            from agent_ai.projects.skills import (
                InvalidSkillIdError,
                SkillNotFoundError,
            )

            if isinstance(exc, (InvalidSkillIdError, SkillNotFoundError)):
                raise ToolValidationError(str(exc)) from exc
            raise ToolExecutionError(str(exc)) from exc
        return skill.to_dict()


class LoadSkillReferenceTool(_SkillToolBase):
    """Memuat satu reference spesifik untuk Skill (progressive, on-demand)."""

    name = "load_skill_reference"
    description = (
        "Memuat satu reference spesifik untuk Skill (progressive, on-demand). "
        "Reference adalah file di references/<skill_id>/ (mis. 'api.md' atau 'guides/vue.md'). "
        "Generic: pemilihan berdasarkan nama/path yang diberikan LLM, tanpa heuristic. "
        "Hanya file yang diminta yang dibaca — tidak otomatis membaca semua references. "
        "Panggil hanya bila informasi di skill.md belum cukup dan reference tersebut diperlukan."
    )
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "string",
                "description": "Identifier Skill.",
            },
            "reference": {
                "type": "string",
                "description": "Nama/path relatif file di references/ (mis. 'api.md').",
            },
        },
        "required": ["skill_id", "reference"],
    }

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        skill_id = arguments.get("skill_id")
        reference = arguments.get("reference")
        if skill_id is None or not str(skill_id).strip():
            raise ToolValidationError("Argumen 'skill_id' wajib diisi.")
        if reference is None or not str(reference).strip():
            raise ToolValidationError("Argumen 'reference' wajib diisi.")
        store = self._store()
        try:
            content = store.load_skill_reference(str(skill_id).strip(), str(reference).strip())
        except Exception as exc:
            from agent_ai.projects.skills import (
                InvalidSkillIdError,
                SkillError,
                SkillNotFoundError,
                SkillReferenceNotFoundError,
            )

            if isinstance(exc, (InvalidSkillIdError, SkillError, SkillNotFoundError, SkillReferenceNotFoundError)):
                raise ToolValidationError(str(exc)) from exc
            raise ToolExecutionError(str(exc)) from exc
        return {
            "skill_id": str(skill_id).strip(),
            "reference": str(reference).strip(),
            "content": content,
        }


# ---------------------------------------------------------------------------
# Lifecycle tools (Agent only) — thin wrappers over SkillStore
# ---------------------------------------------------------------------------
class CreateSkillTool(_SkillToolBase):
    """Membuat Skill baru (LLM-driven, generic)."""

    name = "create_skill"
    description = (
        "Membuat Skill baru di project ini (LLM-driven lifecycle). "
        "Generic & dynamic: skill_id adalah identifier directory Skill yang diinginkan LLM. "
        "Menyimpan skill.md dengan frontmatter (name, description, scope) + body markdown. "
        "Gunakan hanya bila kamu menilai prosedur/pengetahuan tersebut memiliki nilai reusable "
        "untuk task berikutnya; jangan membuat Skill baru untuk setiap task atau sekadar karena task selesai. "
        "Tidak otomatis membuat references jika tidak diperlukan. Hormati validasi ID dan atomic write. "
        "Scope saat ini hanya 'project'."
    )
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "string",
                "description": "Identifier Skill (alfanumerik + . _ -, 1..64 char, tanpa path separator).",
            },
            "name": {
                "type": "string",
                "description": "Nama tampilan Skill.",
            },
            "description": {
                "type": "string",
                "description": "Deskripsi singkat Skill (opsional).",
            },
            "content": {
                "type": "string",
                "description": "Isi markdown Skill (body di luar frontmatter).",
            },
            "scope": {
                "type": "string",
                "description": "Cakupan Skill (saat ini hanya 'project').",
            },
        },
        "required": ["skill_id", "name"],
    }

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        skill_id = arguments.get("skill_id")
        name = arguments.get("name")
        description = arguments.get("description", "")
        content = arguments.get("content", "")
        scope = arguments.get("scope", "project")
        if skill_id is None or not str(skill_id).strip():
            raise ToolValidationError("Argumen 'skill_id' wajib diisi.")
        if name is None or not str(name).strip():
            raise ToolValidationError("Argumen 'name' wajib diisi.")
        # scope default handled by store; pass through if empty
        if scope is None or not str(scope).strip():
            scope = "project"
        store = self._store()
        try:
            skill = store.create_skill(
                skill_id=str(skill_id).strip(),
                name=str(name).strip(),
                description=str(description) if description is not None else "",
                content=str(content) if content is not None else "",
                scope=str(scope).strip(),
            )
        except Exception as exc:
            from agent_ai.projects.skills import (
                InvalidSkillIdError,
                SkillAlreadyExistsError,
                SkillError,
            )

            if isinstance(exc, (InvalidSkillIdError, SkillAlreadyExistsError, SkillError)):
                raise ToolValidationError(str(exc)) from exc
            raise ToolExecutionError(str(exc)) from exc
        return skill.to_dict()


class UpdateSkillTool(_SkillToolBase):
    """Memperbarui Skill existing (LLM-driven, partial update)."""

    name = "update_skill"
    description = (
        "Memperbarui Skill existing (LLM-driven lifecycle, partial update). "
        "Hanya field yang diberikan yang diubah; field lain dipertahankan. "
        "Gunakan hanya bila kamu menilai update tersebut memiliki nilai reusable; "
        "jangan menghapus content/reference yang tidak diminta. "
        "Supported fields: name, description, content, scope."
    )
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "string",
                "description": "Identifier Skill yang akan diperbarui.",
            },
            "name": {
                "type": "string",
                "description": "Nama baru Skill (opsional).",
            },
            "description": {
                "type": "string",
                "description": "Deskripsi baru Skill (opsional).",
            },
            "content": {
                "type": "string",
                "description": "Isi markdown baru Skill (opsional).",
            },
            "scope": {
                "type": "string",
                "description": "Scope baru Skill (opsional, saat ini hanya 'project').",
            },
        },
        "required": ["skill_id"],
    }

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        skill_id = arguments.get("skill_id")
        if skill_id is None or not str(skill_id).strip():
            raise ToolValidationError("Argumen 'skill_id' wajib diisi.")
        # Only pass fields that were explicitly provided (not None)
        kwargs: Dict[str, Any] = {}
        if "name" in arguments and arguments["name"] is not None:
            kwargs["name"] = str(arguments["name"])
        if "description" in arguments and arguments["description"] is not None:
            kwargs["description"] = str(arguments["description"])
        if "content" in arguments and arguments["content"] is not None:
            kwargs["content"] = str(arguments["content"])
        if "scope" in arguments and arguments["scope"] is not None:
            kwargs["scope"] = str(arguments["scope"]).strip() if str(arguments["scope"]).strip() else None
            if kwargs["scope"] is None:
                del kwargs["scope"]
        if not kwargs:
            raise ToolValidationError(
                "Setidaknya satu field yang akan diperbarui harus diberikan (name, description, content, scope)."
            )
        store = self._store()
        try:
            skill = store.update_skill(str(skill_id).strip(), **kwargs)
        except Exception as exc:
            from agent_ai.projects.skills import (
                InvalidSkillIdError,
                SkillError,
                SkillNotFoundError,
            )

            if isinstance(exc, (InvalidSkillIdError, SkillError, SkillNotFoundError)):
                raise ToolValidationError(str(exc)) from exc
            raise ToolExecutionError(str(exc)) from exc
        return skill.to_dict()


class DeleteSkillTool(_SkillToolBase):
    """Menghapus Skill berdasarkan skill_id (LLM-driven)."""

    name = "delete_skill"
    description = (
        "Menghapus Skill berdasarkan skill_id (LLM-driven lifecycle). "
        "Menghapus directory Skill beserta references/. "
        "Gunakan hanya bila kamu menilai Skill tersebut tidak lagi memiliki nilai reusable. "
        "Setelah delete, catalog tidak lagi melihat Skill tersebut."
    )
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "string",
                "description": "Identifier Skill yang akan dihapus.",
            }
        },
        "required": ["skill_id"],
    }

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        skill_id = arguments.get("skill_id")
        if skill_id is None or not str(skill_id).strip():
            raise ToolValidationError("Argumen 'skill_id' wajib diisi.")
        store = self._store()
        try:
            store.delete_skill(str(skill_id).strip())
        except Exception as exc:
            from agent_ai.projects.skills import (
                InvalidSkillIdError,
                SkillError,
                SkillNotFoundError,
            )

            if isinstance(exc, (InvalidSkillIdError, SkillError, SkillNotFoundError)):
                raise ToolValidationError(str(exc)) from exc
            raise ToolExecutionError(str(exc)) from exc
        return {
            "deleted": True,
            "skill_id": str(skill_id).strip(),
        }


def build_skill_tools(
    root: Optional[Any] = None,
    include_catalog: bool = True,
    include_lifecycle: bool = False,
) -> List[BaseTool]:
    """Bangun daftar capability Skill (satu sumber konstruksi).

    Args:
        root: root project target (lokasi .aether/bible/skills).
        include_catalog: bila True sertakan skill_catalog (default True).
        include_lifecycle: bila True sertakan create_skill, update_skill,
            delete_skill (Agent only). Default False sehingga Consultant
            tetap read-only tanpa kemampuan mutasi persistent.

    Returns:
        List tool: skill_catalog, load_skill, load_skill_reference,
        dan bila include_lifecycle True ditambah create/update/delete.
    """
    tools: List[BaseTool] = []
    if include_catalog:
        tools.append(SkillCatalogTool(root=root))
    tools.append(LoadSkillTool(root=root))
    tools.append(LoadSkillReferenceTool(root=root))
    if include_lifecycle:
        tools.append(CreateSkillTool(root=root))
        tools.append(UpdateSkillTool(root=root))
        tools.append(DeleteSkillTool(root=root))
    return tools


def build_skill_lifecycle_tools(root: Optional[Any] = None) -> List[BaseTool]:
    """Bangun daftar lifecycle Skill tools saja (Agent only).

    Thin helper untuk Agent registry yang membutuhkan hanya lifecycle tools.
    Tidak membuat storage baru; wrapper di atas SkillStore existing.
    """
    return [
        CreateSkillTool(root=root),
        UpdateSkillTool(root=root),
        DeleteSkillTool(root=root),
    ]


__all__ = [
    "SkillCatalogTool",
    "LoadSkillTool",
    "LoadSkillReferenceTool",
    "CreateSkillTool",
    "UpdateSkillTool",
    "DeleteSkillTool",
    "build_skill_tools",
    "build_skill_lifecycle_tools",
]
