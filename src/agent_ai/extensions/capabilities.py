"""Capability Registration System for AETHER Extension (Task 03).

Generic contract for Extension to register capabilities via ExtensionContext
without AETHER Core knowing extension types.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# ------------------------------------------------------------------ #
# Errors
# ------------------------------------------------------------------ #

class CapabilityError(Exception):
    """Base error for capability registration."""

class DuplicateCapabilityError(CapabilityError):
    """Duplicate capability ID."""

class CapabilityValidationError(CapabilityError):
    """Validation failed for capability registration."""

# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #

CAPABILITY_TYPES = (
    "tool",
    "skill",
    "knowledge",
    "config",
    "service",
    "provider",
    "resource",
    "hook",
    "command",
    "ui",
)

UI_TYPES = (
    "modal",
    "form",
    "panel",
    "table",
    "chart",
    "viewer",
    "wizard",
    "result_renderer",
    "action",
    "custom_view",
)

CONFIG_TYPES = (
    "string",
    "integer",
    "number",
    "boolean",
    "enum",
    "path",
    "url",
    "secret",
    "json",
    "list",
)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_id(value: str, field_name: str = "id") -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityValidationError(f"{field_name} must be a non-empty string")
    v = value.strip()
    if not _ID_RE.match(v):
        raise CapabilityValidationError(f'{field_name} "{v}" has invalid format')
    if "/" in v or "\\" in v or " " in v:
        raise CapabilityValidationError(f'{field_name} must not contain path separator or space')
    return v


def _ensure_namespaced(capability_id: str, extension_id: str) -> str:
    if not extension_id:
        raise CapabilityValidationError("Extension id is required for namespaced capability")
    prefix = extension_id + "."
    if not capability_id.startswith(prefix):
        raise CapabilityValidationError(
            f'Capability id "{capability_id}" must be namespaced as "{extension_id}.<capability_id>"'
        )
    _validate_id(capability_id, "capability_id")
    suffix = capability_id[len(prefix):]
    if not suffix:
        raise CapabilityValidationError(f'Capability id "{capability_id}" suffix must not be empty')
    return capability_id


@dataclass
class CapabilityRecord:
    id: str
    type: str
    extension_id: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    source: Optional[str] = None
    audience: Optional[str] = None
    permission: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "extension_id": self.extension_id,
            "metadata": dict(self.metadata),
            "enabled": self.enabled,
            "source": self.source,
            "audience": self.audience,
            "permission": self.permission,
        }


class CapabilityRegistry:
    """Central registry for all capabilities across extensions."""

    def __init__(self) -> None:
        self._by_type_id: Dict[tuple[str, str], CapabilityRecord] = {}
        self._all_by_id: Dict[str, List[CapabilityRecord]] = {}

    def _key(self, cap_type: str, cap_id: str) -> tuple[str, str]:
        return (cap_type, cap_id)

    def register(
        self,
        cap_type: str,
        cap_id: str,
        extension_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        source: Optional[str] = None,
        audience: Optional[str] = None,
        permission: Optional[str] = None,
        enabled: bool = True,
    ) -> CapabilityRecord:
        if cap_type not in CAPABILITY_TYPES:
            raise CapabilityValidationError(f'Unknown capability type "{cap_type}"')
        _validate_id(cap_id, "capability_id")
        if not extension_id:
            raise CapabilityValidationError("extension_id must not be empty")
        _ensure_namespaced(cap_id, extension_id)
        key = self._key(cap_type, cap_id)
        if key in self._by_type_id:
            existing = self._by_type_id[key]
            raise DuplicateCapabilityError(
                f'Duplicate capability ID "{cap_id}" of type "{cap_type}" already registered by "{existing.extension_id}"'
            )
        rec = CapabilityRecord(
            id=cap_id,
            type=cap_type,
            extension_id=extension_id,
            metadata=dict(metadata or {}),
            enabled=enabled,
            source=source,
            audience=audience,
            permission=permission,
        )
        self._by_type_id[key] = rec
        self._all_by_id.setdefault(cap_id, []).append(rec)
        return rec

    def exists(self, cap_type: str, cap_id: str) -> bool:
        return self._key(cap_type, cap_id) in self._by_type_id

    def exists_global(self, cap_id: str) -> bool:
        return cap_id in self._all_by_id

    def get(self, cap_type: str, cap_id: str) -> Optional[CapabilityRecord]:
        return self._by_type_id.get(self._key(cap_type, cap_id))

    def get_global(self, cap_id: str) -> List[CapabilityRecord]:
        return list(self._all_by_id.get(cap_id, []))

    def list(self, cap_type: Optional[str] = None) -> List[CapabilityRecord]:
        if cap_type is None:
            return sorted(self._by_type_id.values(), key=lambda r: r.id.lower())
        if cap_type not in CAPABILITY_TYPES:
            raise CapabilityValidationError(f'Unknown capability type "{cap_type}"')
        return sorted([r for (t, _), r in self._by_type_id.items() if t == cap_type], key=lambda r: r.id.lower())

    def list_by_extension(self, extension_id: str) -> List[CapabilityRecord]:
        return sorted([r for r in self._by_type_id.values() if r.extension_id == extension_id], key=lambda r: (r.type, r.id.lower()))

    def all(self) -> List[CapabilityRecord]:
        return self.list(None)

    def remove(self, cap_type: str, cap_id: str) -> bool:
        key = self._key(cap_type, cap_id)
        if key in self._by_type_id:
            rec = self._by_type_id.pop(key)
            lst = self._all_by_id.get(cap_id, [])
            if rec in lst:
                lst.remove(rec)
                if not lst:
                    self._all_by_id.pop(cap_id, None)
            return True
        return False

    def clear(self) -> None:
        self._by_type_id.clear()
        self._all_by_id.clear()

    def is_enabled(self, cap_type: str, cap_id: str) -> bool:
        rec = self.get(cap_type, cap_id)
        return rec.enabled if rec is not None else False

    def set_enabled(self, cap_type: str, cap_id: str, enabled: bool) -> bool:
        rec = self.get(cap_type, cap_id)
        if rec:
            rec.enabled = bool(enabled)
            return True
        return False

    def count(self, cap_type: Optional[str] = None) -> int:
        return len(self.list(cap_type))

    def __len__(self) -> int:
        return len(self._by_type_id)


# Global capability registry (shared across loader contexts)
global_capability_registry = CapabilityRegistry()

# ------------------------------------------------------------------ #
# Facade base
# ------------------------------------------------------------------ #

class _BaseFacade:
    def __init__(self, extension_id: str, capability_registry: CapabilityRegistry, cap_type: str, extension_root: Optional[Path] = None, source: Optional[str] = None):
        self._extension_id = extension_id or ""
        self._registry = capability_registry
        self._type = cap_type
        self._extension_root = Path(extension_root) if extension_root is not None else None
        self._source = source or (str(extension_root) if extension_root is not None else None)

    def _register(self, cap_id: str, metadata: Optional[Dict[str, Any]] = None, audience: Optional[str] = None, permission: Optional[str] = None) -> CapabilityRecord:
        if audience is not None and audience not in ("agent", "consultant", "both"):
            raise CapabilityValidationError(f'audience must be one of "agent", "consultant", "both", got "{audience}"')
        meta = dict(metadata or {})
        _ensure_namespaced(cap_id, self._extension_id)
        return self._registry.register(
            cap_type=self._type,
            cap_id=cap_id,
            extension_id=self._extension_id,
            metadata=meta,
            source=self._source,
            audience=audience,
            permission=permission,
        )

    def get(self, cap_id: str) -> Optional[CapabilityRecord]:
        return self._registry.get(self._type, cap_id)

    def exists(self, cap_id: str) -> bool:
        return self._registry.exists(self._type, cap_id)

    def list(self) -> List[CapabilityRecord]:
        return self._registry.list(self._type)

    def all(self) -> List[CapabilityRecord]:
        return self.list()

    def count(self) -> int:
        return self._registry.count(self._type)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} extension={self._extension_id!r} type={self._type} count={self.count()}>"

# ------------------------------------------------------------------ #
# Specific facades
# ------------------------------------------------------------------ #

class ToolsFacade(_BaseFacade):
    def __init__(self, extension_id: str, capability_registry: CapabilityRegistry, extension_root: Optional[Path] = None, source: Optional[str] = None, tool_registry: Optional[Any] = None, aether_root: Optional[Path] = None):
        super().__init__(extension_id, capability_registry, "tool", extension_root, source)
        self._tool_registry = tool_registry

    def register(self, tool: Any = None, **kwargs: Any) -> CapabilityRecord:
        meta: Dict[str, Any] = {}
        cap_id: Optional[str] = None
        tool_obj = None

        if tool is not None and kwargs:
            if isinstance(tool, str):
                cap_id = tool
                meta.update(kwargs)
            else:
                tool_obj = tool
                cap_id = getattr(tool_obj, "name", None) or getattr(tool_obj, "id", None) or kwargs.get("id") or kwargs.get("name")
                if not cap_id:
                    raise CapabilityValidationError("Tool must have 'name' (namespaced id)")
                meta["description"] = getattr(tool_obj, "description", kwargs.get("description", ""))
                meta["input_schema"] = getattr(tool_obj, "input_schema", kwargs.get("input_schema", {}))
                meta["permission"] = getattr(tool_obj, "permission", kwargs.get("permission"))
                meta["audience"] = getattr(tool_obj, "audience", kwargs.get("audience", "agent"))
                # Keep the live tool instance so enable-after-disable can restore
                # it into the ToolRegistry (generic re-activation).
                meta.setdefault("_tool_instance", tool_obj)
                for k, v in kwargs.items():
                    if k not in meta or meta[k] is None:
                        meta[k] = v
        elif tool is not None:
            if isinstance(tool, dict):
                cap_id = tool.get("id") or tool.get("name")
                meta.update(tool)
            elif isinstance(tool, str):
                cap_id = tool
            else:
                tool_obj = tool
                cap_id = getattr(tool_obj, "name", None) or getattr(tool_obj, "id", None)
                if not cap_id:
                    raise CapabilityValidationError("Tool must have 'name' (namespaced id)")
                meta["description"] = getattr(tool_obj, "description", "")
                meta["input_schema"] = getattr(tool_obj, "input_schema", {})
                if hasattr(tool_obj, "permission"):
                    meta["permission"] = getattr(tool_obj, "permission")
                if hasattr(tool_obj, "audience"):
                    meta["audience"] = getattr(tool_obj, "audience")
                meta["_tool_instance"] = tool_obj
        else:
            cap_id = kwargs.pop("id", None) or kwargs.pop("name", None)
            if not cap_id:
                raise CapabilityValidationError("Tool registration requires 'name' or 'id'")
            meta.update(kwargs)

        if not cap_id:
            raise CapabilityValidationError("Tool registration requires 'name'/'id'")
        cap_id = str(cap_id).strip()
        _ensure_namespaced(cap_id, self._extension_id)
        audience = meta.get("audience") or kwargs.get("audience")
        if audience is None:
            audience = "agent"
        permission = meta.get("permission") or kwargs.get("permission")
        if tool_obj is not None:
            try:
                if getattr(tool_obj, "name", None) != cap_id:
                    tool_obj.name = cap_id  # type: ignore[attr-defined]
            except Exception:
                pass
        rec = self._register(cap_id, metadata=meta, audience=audience, permission=permission)
        if self._tool_registry is not None and tool_obj is not None:
            try:
                self._tool_registry.register(tool_obj)
            except Exception as exc:
                self._registry.remove(self._type, cap_id)
                if "already" in str(exc).lower() or "duplicate" in str(exc).lower():
                    raise DuplicateCapabilityError(str(exc)) from exc
                raise CapabilityError(str(exc)) from exc
        return rec

    def specs(self) -> List[Dict[str, Any]]:
        if self._tool_registry is not None:
            try:
                return self._tool_registry.specs()
            except Exception:
                pass
        result = []
        for rec in self.list():
            meta = rec.metadata
            result.append({
                "name": rec.id,
                "description": meta.get("description", ""),
                "input_schema": meta.get("input_schema", {}),
                "audience": rec.audience,
                "permission": rec.permission,
                "extension_id": rec.extension_id,
            })
        return result


class SkillsFacade(_BaseFacade):
    def __init__(self, extension_id: str, capability_registry: CapabilityRegistry, extension_root: Optional[Path] = None, source: Optional[str] = None, **kwargs: Any):
        super().__init__(extension_id, capability_registry, "skill", extension_root, source)

    def register(self, skill: Any = None, **kwargs: Any) -> CapabilityRecord:
        meta: Dict[str, Any] = {}
        cap_id: Optional[str] = None
        if skill is not None and isinstance(skill, str):
            cap_id = skill
            meta.update(kwargs)
        elif skill is not None and isinstance(skill, dict):
            cap_id = skill.get("id") or skill.get("skill_id") or skill.get("name")
            meta.update(skill)
            meta.update(kwargs)
        elif skill is not None:
            cap_id = getattr(skill, "skill_id", None) or getattr(skill, "id", None) or getattr(skill, "name", None)
            if cap_id:
                meta["name"] = getattr(skill, "name", "")
                meta["description"] = getattr(skill, "description", "")
                meta["content"] = getattr(skill, "content", "")
                meta["scope"] = getattr(skill, "scope", "project")
                meta["location"] = getattr(skill, "location", "")
                meta.update(kwargs)
            else:
                cap_id = kwargs.get("id") or kwargs.get("skill_id")
                meta.update(kwargs)
        else:
            cap_id = kwargs.pop("id", None) or kwargs.pop("skill_id", None) or kwargs.pop("name", None)
            if not cap_id:
                raise CapabilityValidationError("Skill registration requires 'id'/'skill_id'")
            meta.update(kwargs)
        if not cap_id:
            raise CapabilityValidationError("Skill registration requires id")
        cap_id = str(cap_id).strip()
        _ensure_namespaced(cap_id, self._extension_id)
        audience = meta.get("audience")
        permission = meta.get("permission")
        return self._register(cap_id, metadata=meta, audience=audience, permission=permission)


class KnowledgeFacade(_BaseFacade):
    def __init__(self, extension_id: str, capability_registry: CapabilityRegistry, extension_root: Optional[Path] = None, source: Optional[str] = None, **_: Any):
        super().__init__(extension_id, capability_registry, "knowledge", extension_root, source)

    def register(self, knowledge: Any = None, **kwargs: Any) -> CapabilityRecord:
        meta: Dict[str, Any] = {}
        cap_id: Optional[str] = None
        if knowledge is not None and isinstance(knowledge, str):
            cap_id = knowledge
            meta.update(kwargs)
        elif knowledge is not None and isinstance(knowledge, dict):
            cap_id = knowledge.get("id")
            meta.update(knowledge)
            meta.update(kwargs)
        elif knowledge is not None:
            cap_id = getattr(knowledge, "id", None)
            meta["content"] = getattr(knowledge, "content", str(knowledge))
            meta.update(kwargs)
        else:
            cap_id = kwargs.pop("id", None)
            if not cap_id:
                raise CapabilityValidationError("Knowledge registration requires 'id'")
            meta.update(kwargs)
        if not cap_id:
            raise CapabilityValidationError("Knowledge registration requires id")
        cap_id = str(cap_id).strip()
        _ensure_namespaced(cap_id, self._extension_id)
        return self._register(cap_id, metadata=meta)


class ConfigFacade(_BaseFacade):
    def __init__(
        self,
        extension_id: str,
        capability_registry: CapabilityRegistry,
        extension_root: Optional[Path] = None,
        source: Optional[str] = None,
        config_store: Optional[Any] = None,
        aether_root: Optional[Path] = None,
        **_: Any,
    ):
        super().__init__(extension_id, capability_registry, "config", extension_root, source)
        # Config value store (reuse SQLite). Lazy to avoid cycle.
        if config_store is not None:
            self._config_store = config_store
        else:
            try:
                from agent_ai.extensions.config import get_config_store
                self._config_store = get_config_store()
            except Exception:
                self._config_store = None
        self._aether_root = aether_root

    def _norm_key(self, key: str) -> tuple[str, str]:
        """Normalize key -> (short_key, full_id)."""
        raw = str(key).strip()
        if not raw:
            raise CapabilityValidationError("Config key must not be empty")
        if "." in raw:
            if raw.startswith(self._extension_id + "."):
                _ensure_namespaced(raw, self._extension_id)
                short = raw[len(self._extension_id) + 1:]
                return short, raw
            # need validation that it's namespaced correctly
            _ensure_namespaced(raw, self._extension_id)
            return raw.split(".")[-1], raw
        return raw, f"{self._extension_id}.{raw}"

    def _get_definition(self, key: str) -> CapabilityRecord:
        _, full_id = self._norm_key(key)
        rec = self._registry.get("config", full_id)
        if rec is None:
            raise CapabilityValidationError(f'Unknown config key "{key}" (no definition for "{full_id}")')
        return rec

    def register(self, key: Any = None, **kwargs: Any) -> CapabilityRecord:
        meta: Dict[str, Any] = {}
        cap_key: Optional[str] = None
        if isinstance(key, str):
            cap_key = key
            meta.update(kwargs)
        elif isinstance(key, dict):
            cap_key = key.get("key") or key.get("id")
            meta.update(key)
            meta.update(kwargs)
        elif key is not None:
            cap_key = getattr(key, "key", None) or getattr(key, "id", None)
            meta.update(kwargs)
        else:
            cap_key = kwargs.pop("key", None) or kwargs.pop("id", None)
            if not cap_key:
                raise CapabilityValidationError("Config registration requires 'key'")
            meta.update(kwargs)
        if not cap_key:
            raise CapabilityValidationError("Config registration requires 'key'")
        cap_key = str(cap_key).strip()
        if not cap_key:
            raise CapabilityValidationError("Config key must not be empty")
        if "." not in cap_key:
            full_id = f"{self._extension_id}.{cap_key}"
        else:
            if not cap_key.startswith(self._extension_id + "."):
                _ensure_namespaced(cap_key, self._extension_id)
                full_id = cap_key
            else:
                full_id = cap_key
                _ensure_namespaced(full_id, self._extension_id)
        cfg_type = meta.get("type", "string")
        if cfg_type not in CONFIG_TYPES:
            raise CapabilityValidationError(f'Config type "{cfg_type}" not in {CONFIG_TYPES}')
        # Scope validation
        scope = meta.get("scope", "extension")
        try:
            from agent_ai.extensions.config import SUPPORTED_SCOPES
            if scope not in SUPPORTED_SCOPES:
                raise CapabilityValidationError(f'Config scope "{scope}" not in {SUPPORTED_SCOPES}')
        except CapabilityValidationError:
            raise
        except Exception:
            pass
        # Enum handling
        choices = meta.get("choices")
        if cfg_type == "enum":
            if not isinstance(choices, (list, tuple)) or not choices:
                raise CapabilityValidationError('Config type "enum" requires non-empty "choices"')
        else:
            if choices is not None:
                # choices only valid for enum
                raise CapabilityValidationError(f'Config type "{cfg_type}" must not have "choices" (only enum)')
        # Default validation
        default = meta.get("default")
        if default is not None:
            try:
                from agent_ai.extensions.config import _validate_default
                _validate_default(cfg_type, default, choices)
            except CapabilityValidationError:
                raise
            except Exception as exc:
                from agent_ai.extensions.config import ConfigValidationError as _CVE
                # Re-raise as CapabilityValidationError for consistent caller handling
                if isinstance(exc, _CVE):
                    raise CapabilityValidationError(str(exc)) from exc
                raise CapabilityValidationError(str(exc)) from exc
            # enum default must be in choices
            if cfg_type == "enum" and default not in choices:
                raise CapabilityValidationError(f'Enum default "{default}" must be one of {choices}')
            # secret default warning: we still validate but task says don't include real secret; we just validate type
        # required + default co-existence is allowed
        short_key = cap_key if "." not in cap_key else cap_key.split(".")[-1]
        meta.setdefault("key", short_key)
        meta.setdefault("type", cfg_type)
        meta.setdefault("description", meta.get("description", ""))
        meta.setdefault("required", bool(meta.get("required", False)))
        meta.setdefault("secret", bool(meta.get("secret", False)) or cfg_type == "secret")
        meta.setdefault("scope", scope)
        if choices is not None:
            meta["choices"] = list(choices)
        return self._register(full_id, metadata=meta)

    # -- runtime value API (Task 04) ------------------------------------

    def get(self, key: str, default: Any = None, *, project_id: Optional[str] = None, scope: Optional[str] = None) -> Any:
        """Get config value. Falls back to definition default if not set.
        Raises clear error if unknown key, or required missing.
        Secret is allowed for runtime but never auto-exposed to LLM.
        """
        rec = self._get_definition(key)
        meta = rec.metadata
        cfg_type = meta.get("type", "string")
        required = bool(meta.get("required", False))
        declared_scope = meta.get("scope", "extension")
        effective_scope = scope or declared_scope
        short_key, _ = self._norm_key(key)
        # Try value store per scope
        val = None
        found = False
        if self._config_store is not None:
            # If scope is project/task, try project_id
            try:
                stored = self._config_store.get(self._extension_id, short_key, scope=effective_scope, project_id=project_id)
                if stored is not None:
                    val = stored
                    found = True
            except Exception:
                pass
            # Fallback: try other scopes? For simplicity, if effective_scope is project and we have no project_id, try global?
            # Spec: project scope value is per-project; but we should not auto hide — just return value for effective_scope.
        if found:
            return val
        # Check default
        if "default" in meta and meta["default"] is not None:
            return meta["default"]
        if required:
            # Don't fail startup; but at runtime get should raise clear error
            from agent_ai.extensions.config import RequiredConfigMissingError
            from agent_ai.tools.base import ToolExecutionError as _TE
            raise RequiredConfigMissingError(f'Required config "{short_key}" for extension "{self._extension_id}" is not set')
        if default is not None:
            return default
        # No value, no default, not required -> return None or default
        return None

    def set(self, key: str, value: Any, *, project_id: Optional[str] = None, scope: Optional[str] = None) -> None:
        """Set config value. Validates type, unknown key, enum choices."""
        rec = self._get_definition(key)
        meta = rec.metadata
        cfg_type = meta.get("type", "string")
        choices = meta.get("choices")
        declared_scope = meta.get("scope", "extension")
        effective_scope = scope or declared_scope
        # Scope validation
        try:
            from agent_ai.extensions.config import SUPPORTED_SCOPES
            if effective_scope not in SUPPORTED_SCOPES:
                from agent_ai.extensions.config import ConfigValidationError as _CVE
                raise _CVE(f'Config scope "{effective_scope}" not in {SUPPORTED_SCOPES}')
        except Exception as exc:
            if "not in" in str(exc):
                raise
        # Type validation
        try:
            from agent_ai.extensions.config import _validate_type
            _validate_type(cfg_type, value, choices)
        except Exception as exc:
            from agent_ai.extensions.config import ConfigValidationError as _CVE
            if isinstance(exc, _CVE):
                raise
            raise _CVE(str(exc)) from exc
        short_key, _ = self._norm_key(key)
        if self._config_store is None:
            raise RuntimeError("Config store not available")
        self._config_store.set(self._extension_id, short_key, value, scope=effective_scope, project_id=project_id)

    def has(self, key: str, *, project_id: Optional[str] = None, scope: Optional[str] = None) -> bool:
        try:
            rec = self._get_definition(key)
        except Exception:
            return False
        short_key, _ = self._norm_key(key)
        declared_scope = rec.metadata.get("scope", "extension")
        effective_scope = scope or declared_scope
        if self._config_store is None:
            return False
        try:
            if self._config_store.has(self._extension_id, short_key, scope=effective_scope, project_id=project_id):
                return True
            # also has if default exists
            if "default" in rec.metadata and rec.metadata["default"] is not None:
                return True
            return False
        except Exception:
            return False

    def get_definition(self, key: str) -> Dict[str, Any]:
        rec = self._get_definition(key)
        return dict(rec.metadata)

    def list_definitions(self) -> List[Dict[str, Any]]:
        result = []
        for rec in self._registry.list("config"):
            if rec.extension_id == self._extension_id:
                d = dict(rec.metadata)
                d["_id"] = rec.id
                result.append(d)
        return sorted(result, key=lambda x: x.get("key", ""))

    def delete(self, key: str, *, project_id: Optional[str] = None, scope: Optional[str] = None) -> bool:
        rec = self._get_definition(key)
        short_key, _ = self._norm_key(key)
        declared_scope = rec.metadata.get("scope", "extension")
        effective_scope = scope or declared_scope
        if self._config_store is None:
            return False
        return bool(self._config_store.delete(self._extension_id, short_key, scope=effective_scope, project_id=project_id))

    def is_secret(self, key: str) -> bool:
        rec = self._get_definition(key)
        return bool(rec.metadata.get("secret") or rec.metadata.get("type") == "secret")


class ServicesFacade(_BaseFacade):
    def __init__(self, extension_id: str, capability_registry: CapabilityRegistry, extension_root: Optional[Path] = None, source: Optional[str] = None, **_: Any):
        super().__init__(extension_id, capability_registry, "service", extension_root, source)

    def register(self, service: Any = None, **kwargs: Any) -> CapabilityRecord:
        meta: Dict[str, Any] = {}
        cap_id: Optional[str] = None
        if service is not None and isinstance(service, str):
            cap_id = service
            meta.update(kwargs)
        elif service is not None and isinstance(service, dict):
            cap_id = service.get("id") or service.get("name")
            meta.update(service)
            meta.update(kwargs)
        elif service is not None:
            cap_id = getattr(service, "id", None) or getattr(service, "name", None)
            if not cap_id and hasattr(service, "__class__"):
                short = service.__class__.__name__
                cap_id = f"{self._extension_id}.{short}" if short else None
            if cap_id and "." not in cap_id:
                cap_id = f"{self._extension_id}.{cap_id}"
            meta["service_instance"] = service
            meta.update(kwargs)
        else:
            cap_id = kwargs.pop("id", None) or kwargs.pop("name", None)
            if not cap_id:
                raise CapabilityValidationError("Service registration requires 'id'/'name'")
            meta.update(kwargs)
        if not cap_id:
            raise CapabilityValidationError("Service registration requires id")
        cap_id = str(cap_id).strip()
        if not cap_id.startswith(self._extension_id + "."):
            if "." not in cap_id:
                cap_id = f"{self._extension_id}.{cap_id}"
            else:
                _ensure_namespaced(cap_id, self._extension_id)
        else:
            _ensure_namespaced(cap_id, self._extension_id)
        return self._register(cap_id, metadata=meta)


class ProvidersFacade(_BaseFacade):
    def __init__(self, extension_id: str, capability_registry: CapabilityRegistry, extension_root: Optional[Path] = None, source: Optional[str] = None, **_: Any):
        super().__init__(extension_id, capability_registry, "provider", extension_root, source)

    def register(self, provider: Any = None, **kwargs: Any) -> CapabilityRecord:
        meta: Dict[str, Any] = {}
        cap_id: Optional[str] = None
        if provider is not None and isinstance(provider, str):
            cap_id = provider
            meta.update(kwargs)
        elif provider is not None and isinstance(provider, dict):
            cap_id = provider.get("id") or provider.get("name")
            meta.update(provider)
            meta.update(kwargs)
        elif provider is not None:
            cap_id = getattr(provider, "name", None) or getattr(provider, "id", None)
            if not cap_id:
                cap_id = kwargs.get("id") or kwargs.get("name")
            meta["provider_instance"] = provider
            meta.update(kwargs)
        else:
            cap_id = kwargs.pop("id", None) or kwargs.pop("name", None)
            if not cap_id:
                raise CapabilityValidationError("Provider registration requires 'id'")
            meta.update(kwargs)
        if not cap_id:
            raise CapabilityValidationError("Provider registration requires id")
        cap_id = str(cap_id).strip()
        if not cap_id.startswith(self._extension_id + "."):
            if "." not in cap_id:
                cap_id = f"{self._extension_id}.{cap_id}"
            else:
                _ensure_namespaced(cap_id, self._extension_id)
        else:
            _ensure_namespaced(cap_id, self._extension_id)
        return self._register(cap_id, metadata=meta)


class ResourcesFacade(_BaseFacade):
    def __init__(self, extension_id: str, capability_registry: CapabilityRegistry, extension_root: Optional[Path] = None, source: Optional[str] = None, **_: Any):
        super().__init__(extension_id, capability_registry, "resource", extension_root, source)

    def register(self, resource: Any = None, **kwargs: Any) -> CapabilityRecord:
        meta: Dict[str, Any] = {}
        cap_id: Optional[str] = None
        if resource is not None and isinstance(resource, str):
            cap_id = resource
            meta.update(kwargs)
        elif resource is not None and isinstance(resource, dict):
            cap_id = resource.get("id")
            meta.update(resource)
            meta.update(kwargs)
        elif resource is not None:
            cap_id = getattr(resource, "id", None)
            meta.update(kwargs)
        else:
            cap_id = kwargs.pop("id", None)
            if not cap_id:
                raise CapabilityValidationError("Resource registration requires 'id'")
            meta.update(kwargs)
        if not cap_id:
            raise CapabilityValidationError("Resource registration requires id")
        cap_id = str(cap_id).strip()
        _ensure_namespaced(cap_id, self._extension_id)
        return self._register(cap_id, metadata=meta)


class HooksFacade(_BaseFacade):
    def __init__(self, extension_id: str, capability_registry: CapabilityRegistry, extension_root: Optional[Path] = None, source: Optional[str] = None, **_: Any):
        super().__init__(extension_id, capability_registry, "hook", extension_root, source)

    def register(self, hook: Any = None, **kwargs: Any) -> CapabilityRecord:
        meta: Dict[str, Any] = {}
        cap_id: Optional[str] = None
        if hook is not None and isinstance(hook, str):
            cap_id = hook
            handler = kwargs.get("handler") or kwargs.get("callback")
            event = kwargs.get("event", cap_id)
            meta.update(kwargs)
            meta["event"] = event
            if handler:
                meta["handler"] = handler
        elif hook is not None and isinstance(hook, dict):
            cap_id = hook.get("id")
            meta.update(hook)
        elif hook is not None and callable(hook):
            handler = hook
            cap_id = kwargs.get("id") or getattr(hook, "__name__", None)
            event = kwargs.get("event", cap_id)
            meta["handler"] = handler
            meta["event"] = event
            meta.update(kwargs)
        else:
            cap_id = kwargs.pop("id", None) or kwargs.pop("event", None)
            handler = kwargs.get("handler") or kwargs.get("callback")
            event = kwargs.get("event", cap_id)
            meta.update(kwargs)
            if event:
                meta["event"] = event
            if handler:
                meta["handler"] = handler
        if not cap_id:
            raise CapabilityValidationError("Hook registration requires 'id'/'event'")
        cap_id = str(cap_id).strip()
        if not cap_id.startswith(self._extension_id + "."):
            if "." not in cap_id:
                cap_id = f"{self._extension_id}.{cap_id}"
            else:
                _ensure_namespaced(cap_id, self._extension_id)
        else:
            _ensure_namespaced(cap_id, self._extension_id)
        if "event" not in meta:
            meta["event"] = cap_id.split(".")[-1]
        return self._register(cap_id, metadata=meta)


class CommandsFacade(_BaseFacade):
    def __init__(self, extension_id: str, capability_registry: CapabilityRegistry, extension_root: Optional[Path] = None, source: Optional[str] = None, **_: Any):
        super().__init__(extension_id, capability_registry, "command", extension_root, source)

    def register(self, command: Any = None, **kwargs: Any) -> CapabilityRecord:
        meta: Dict[str, Any] = {}
        cap_id: Optional[str] = None
        if command is not None and isinstance(command, str):
            cap_id = command
            meta.update(kwargs)
        elif command is not None and isinstance(command, dict):
            cap_id = command.get("id") or command.get("name")
            meta.update(command)
            meta.update(kwargs)
        elif command is not None and callable(command):
            cap_id = getattr(command, "__name__", None) or kwargs.get("id")
            meta["handler"] = command
            meta.update(kwargs)
        else:
            cap_id = kwargs.pop("id", None) or kwargs.pop("name", None)
            if not cap_id:
                raise CapabilityValidationError("Command registration requires 'id'")
            meta.update(kwargs)
        if not cap_id:
            raise CapabilityValidationError("Command registration requires id")
        cap_id = str(cap_id).strip()
        _ensure_namespaced(cap_id, self._extension_id)
        return self._register(cap_id, metadata=meta)


class UIFacade(_BaseFacade):
    def __init__(self, extension_id: str, capability_registry: CapabilityRegistry, extension_root: Optional[Path] = None, source: Optional[str] = None, **_: Any):
        super().__init__(extension_id, capability_registry, "ui", extension_root, source)

    def register(self, ui: Any = None, **kwargs: Any) -> CapabilityRecord:
        meta: Dict[str, Any] = {}
        cap_id: Optional[str] = None
        if ui is not None and isinstance(ui, str):
            cap_id = ui
            meta.update(kwargs)
        elif ui is not None and isinstance(ui, dict):
            cap_id = ui.get("id")
            meta.update(ui)
            meta.update(kwargs)
        else:
            cap_id = kwargs.pop("id", None)
            if not cap_id:
                raise CapabilityValidationError("UI registration requires 'id'")
            meta.update(kwargs)
        if not cap_id:
            raise CapabilityValidationError("UI registration requires id")
        cap_id = str(cap_id).strip()
        _ensure_namespaced(cap_id, self._extension_id)
        ui_type = meta.get("type") or kwargs.get("type")
        if ui_type is not None and ui_type not in UI_TYPES:
            raise CapabilityValidationError(f'UI type "{ui_type}" not in {UI_TYPES}')
        meta.setdefault("type", ui_type or "panel")
        meta.setdefault("title", meta.get("title") or meta.get("label") or "")
        meta.setdefault("description", meta.get("description", ""))
        if "entry" not in meta:
            for alias in ("reference", "view", "component", "entry_point"):
                if alias in meta and meta[alias]:
                    meta["entry"] = meta[alias]
                    break
        import json as _json
        for k in ("handler", "callback", "component_instance"):
            if k in meta and callable(meta[k]):
                try:
                    name = getattr(meta[k], "__name__", str(meta[k]))
                except Exception:
                    name = str(meta[k])
                meta[k] = f"<callable:{name}>"
        try:
            _json.dumps(meta, ensure_ascii=False, default=str)
        except Exception as exc:
            raise CapabilityValidationError(f"UI metadata must be JSON serializable: {exc}") from exc
        if "title" in meta and not isinstance(meta["title"], str):
            meta["title"] = str(meta["title"])
        if "description" in meta and not isinstance(meta["description"], str):
            meta["description"] = str(meta["description"])
        return self._register(cap_id, metadata=meta)

__all__ = [
    "CapabilityError",
    "DuplicateCapabilityError",
    "CapabilityValidationError",
    "CapabilityRecord",
    "CapabilityRegistry",
    "global_capability_registry",
    "CAPABILITY_TYPES",
    "UI_TYPES",
    "CONFIG_TYPES",
    "ToolsFacade",
    "SkillsFacade",
    "KnowledgeFacade",
    "ConfigFacade",
    "ServicesFacade",
    "ProvidersFacade",
    "ResourcesFacade",
    "HooksFacade",
    "CommandsFacade",
    "UIFacade",
]
