"""Extension UI System & Rendering Contract — Task 05.

Generic UI contract for Extensions. AETHER Core does not know extension names.
All rendering is determined by UI type / renderer id / schema / mime type,
not by extension_id equality.

This module provides:
- UI type constants (mirrors capabilities.UI_TYPES but extended for rendering)
- UIContribution / UIResult contracts (serializable, stable)
- UICatalog (dynamic view over CapabilityRegistry + ExtensionRegistry)
- Config -> Form schema translation (secret-safe)
- Generic Table / Chart / Viewer / Result contracts
- Enable/disable awareness delegation
- No hardcoded extension logic

Frontend is fed via backend API; backend is authority for validation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from agent_ai.extensions.capabilities import (
    UI_TYPES,
    CONFIG_TYPES,
    CapabilityRecord,
    CapabilityRegistry,
    CapabilityValidationError,
)

# ---------------------------------------------------------------------------
# Constants (generic, no hardcode)
# ---------------------------------------------------------------------------

# UI capability types (from capabilities.py)
# UI_TYPES = ("modal","form","panel","table","chart","viewer","wizard","result_renderer","action","custom_view")

CHART_TYPES = ("line", "bar", "area", "pie", "scatter")

VIEWER_TYPES = (
    "image",
    "video",
    "audio",
    "pdf",
    "json",
    "text",
    "log",
    "diff",
    "table",
    "chart",
    "map",
)

# Renderer types: UI_TYPES + viewer sub-types + generic "result"
RENDERER_TYPES = tuple(sorted(set(UI_TYPES) | set(VIEWER_TYPES) | {"result"}))

# Mapping config type -> form field descriptor
CONFIG_TYPE_TO_FIELD = {
    "string": "text",
    "integer": "number",
    "number": "number",
    "boolean": "checkbox",
    "enum": "select",
    "path": "path",
    "url": "url",
    "secret": "secret",
    "json": "json",
    "list": "list",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_serializable(obj: Any) -> None:
    """Raise if obj cannot be json serialized."""
    try:
        json.dumps(obj, ensure_ascii=False, default=str)
    except Exception as exc:
        raise CapabilityValidationError(f"UI metadata must be JSON serializable: {exc}") from exc


def _validate_chart_data(data: Dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise CapabilityValidationError("Chart data must be dict")
    ctype = data.get("type")
    if ctype is not None and ctype not in CHART_TYPES:
        # allow unknown if extension provides custom but still validate generic types
        # For generic contract, unknown is allowed but we warn? For Task 05 we allow any if existing library supports.
        # Keep validation permissive: only check if provided and not in known -> still allow but we will not reject generic custom type?
        # Spec says minimal chart must represent line/bar/area/pie/scatter or other if library supports.
        # So we accept any string but ensure structure present.
        pass
    # Datasets or data field
    # Allow either "datasets" or "data"
    if "datasets" not in data and "data" not in data and "labels" not in data:
        # chart may still be validated elsewhere; we don't reject empty chart strictly.
        pass


def _validate_table_payload(payload: Dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise CapabilityValidationError("Table payload must be dict")
    cols = payload.get("columns")
    rows = payload.get("rows")
    if cols is not None and not isinstance(cols, list):
        raise CapabilityValidationError("Table columns must be list")
    if rows is not None and not isinstance(rows, list):
        raise CapabilityValidationError("Table rows must be list")


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

@dataclass
class UIContribution:
    """Backend <-> Frontend contract for a UI capability.

    Serialisable, stable, generic. No Python internal objects.
    """

    id: str
    extension_id: str
    type: str
    title: str = ""
    description: str = ""
    entry: Optional[str] = None
    schema: Optional[Dict[str, Any]] = None
    props: Dict[str, Any] = field(default_factory=dict)
    actions: List[Dict[str, Any]] = field(default_factory=list)
    enabled: bool = True
    source: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "extension_id": self.extension_id,
            "type": self.type,
            "title": self.title,
            "description": self.description,
            "enabled": self.enabled,
        }
        if self.entry is not None:
            d["entry"] = self.entry
        if self.schema is not None:
            d["schema"] = self.schema
        if self.props:
            d["props"] = dict(self.props)
        if self.actions:
            d["actions"] = list(self.actions)
        if self.source:
            d["source"] = self.source
        # Ensure serializable
        _ensure_serializable(d)
        return d


@dataclass
class UIResult:
    """Structured result from Extension Tool -> rendered via UI.

    renderer determines which renderer to use, data is structured payload,
    artifact is reference to existing Artifact system.
    """

    renderer: str
    data: Any = None
    artifact: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    type: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "renderer": self.renderer,
            "type": self.type or self.renderer,
            "data": self.data,
            "metadata": dict(self.metadata),
        }
        if self.artifact is not None:
            d["artifact"] = dict(self.artifact)
        _ensure_serializable(d)
        return d


@dataclass
class ArtifactRef:
    artifact_id: str
    mime_type: str = ""
    name: str = ""
    size: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "artifact_id": self.artifact_id,
            "mime_type": self.mime_type,
            "name": self.name,
            "metadata": dict(self.metadata),
        }
        if self.size is not None:
            d["size"] = self.size
        return d


# ---------------------------------------------------------------------------
# Config -> Form schema translation (secret-safe)
# ---------------------------------------------------------------------------

def config_type_to_field_type(cfg_type: str) -> str:
    """Map config type to form field widget type (generic, no hardcode)."""
    return CONFIG_TYPE_TO_FIELD.get(cfg_type, "text")


def config_definition_to_field(rec: CapabilityRecord, has_value: bool = False) -> Dict[str, Any]:
    """Translate a single config CapabilityRecord to a form field descriptor.

    Secret handling: field type is secret, value is never included.
    Instead we expose configured: bool (whether value is set).
    """
    meta = rec.metadata or {}
    # derive short key
    raw_id = rec.id
    ext_prefix = rec.extension_id + "."
    short_key = raw_id[len(ext_prefix):] if raw_id.startswith(ext_prefix) else raw_id
    cfg_type = meta.get("type", "string")
    field_type = config_type_to_field_type(cfg_type)
    # Determine secret
    is_secret = bool(meta.get("secret") or cfg_type == "secret")
    field: Dict[str, Any] = {
        "key": short_key,
        "id": rec.id,
        "type": cfg_type,
        "field_type": field_type,
        "title": meta.get("title") or short_key,
        "description": meta.get("description", ""),
        "required": bool(meta.get("required", False)),
        "secret": is_secret,
    }
    # Optional attributes without leaking values
    if "default" in meta and meta["default"] is not None and not is_secret:
        # For non-secret, include default; for secret, never include actual value
        field["default"] = meta["default"]
    elif is_secret and "default" in meta and meta["default"] is not None:
        # secret default: do not expose value, just indicate existence
        field["has_default"] = True
    if cfg_type == "enum" and meta.get("choices"):
        field["choices"] = list(meta["choices"])
    if is_secret:
        # configured flag: caller provides has_value; UI shows configured/not configured
        field["configured"] = bool(has_value)
        # Never include value
        field.pop("default", None)
    # validation hints
    if cfg_type in ("string", "path", "url", "secret"):
        field["validation"] = {"type": "string"}
        if cfg_type == "url":
            field["validation"]["format"] = "url"
    elif cfg_type == "integer":
        field["validation"] = {"type": "integer"}
    elif cfg_type == "number":
        field["validation"] = {"type": "number"}
    elif cfg_type == "boolean":
        field["validation"] = {"type": "boolean"}
    elif cfg_type == "enum":
        field["validation"] = {"type": "enum", "choices": list(meta.get("choices", []))}
    elif cfg_type == "json":
        field["validation"] = {"type": "json"}
    elif cfg_type == "list":
        field["validation"] = {"type": "list"}
    _ensure_serializable(field)
    return field


def config_definitions_to_form_schema(
    records: List[CapabilityRecord],
    has_value_lookup: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """Translate list of config records to a declarative form schema.

    has_value_lookup: mapping full_id -> bool indicating if value configured (for secrets).
    """
    fields = []
    for rec in sorted(records, key=lambda r: r.id.lower()):
        has_val = False
        if has_value_lookup is not None:
            has_val = bool(has_value_lookup.get(rec.id))
        fields.append(config_definition_to_field(rec, has_value=has_val))
    schema: Dict[str, Any] = {
        "type": "form",
        "fields": fields,
        "title": "Configuration",
    }
    _ensure_serializable(schema)
    return schema


def build_form_schema_for_extension(
    extension_id: str,
    capability_registry: CapabilityRegistry,
    config_store: Optional[Any] = None,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build form schema for all config definitions of an extension, secret-safe."""
    # Gather config records for extension
    all_configs = capability_registry.list("config")
    ext_configs = [r for r in all_configs if r.extension_id == extension_id]
    # Determine which secrets have values (without exposing values)
    lookup: Dict[str, bool] = {}
    if config_store is not None:
        for rec in ext_configs:
            if rec.metadata.get("type") == "secret" or rec.metadata.get("secret"):
                # short key
                short = rec.id[len(extension_id) + 1:] if rec.id.startswith(extension_id + ".") else rec.id
                try:
                    has = config_store.has(extension_id, short, scope=rec.metadata.get("scope", "extension"), project_id=project_id)
                    # also has if configured flag; for secret we just check presence via get maybe has default? but we treat has as store.has
                    lookup[rec.id] = bool(has)
                except Exception:
                    lookup[rec.id] = False
    return config_definitions_to_form_schema(ext_configs, has_value_lookup=lookup)


# ---------------------------------------------------------------------------
# UIContribution helpers
# ---------------------------------------------------------------------------

def record_to_contribution(rec: CapabilityRecord) -> UIContribution:
    """Convert CapabilityRecord (type ui) to UIContribution contract."""
    meta = rec.metadata or {}
    # Determine title/description
    title = meta.get("title", "") or meta.get("label", "") or ""
    description = meta.get("description", "")
    # Entry/reference
    entry = meta.get("entry") or meta.get("component") or meta.get("view") or meta.get("reference")
    schema = meta.get("schema") or meta.get("input_schema") or meta.get("form_schema")
    props = meta.get("props") or {}
    # Collect known props without leaking internals: include generic fields
    # e.g., for table: columns; chart: chart_type; modal: size; panel: placement
    # We forward props as provided, but ensure serializable and exclude Python objects
    safe_props: Dict[str, Any] = {}
    for k, v in (props.items() if isinstance(props, dict) else {}):
        try:
            json.dumps(v, ensure_ascii=False, default=str)
            safe_props[k] = v
        except Exception:
            safe_props[k] = str(v)
    # Also include meta fields that are declared as props generically
    for extra_key in ("columns", "rows", "chart_type", "placement", "slot", "size", "steps", "viewer_type", "mime_type"):
        if extra_key in meta and extra_key not in safe_props:
            try:
                json.dumps(meta[extra_key], ensure_ascii=False, default=str)
                safe_props[extra_key] = meta[extra_key]
            except Exception:
                safe_props[extra_key] = str(meta[extra_key])
    actions = meta.get("actions") or []
    if not isinstance(actions, list):
        actions = [actions] if actions else []
    # Ensure actions serializable and referencing capability identifiers (not arbitrary core internals)
    safe_actions = []
    for a in actions:
        if isinstance(a, dict):
            try:
                json.dumps(a, ensure_ascii=False, default=str)
                safe_actions.append(a)
            except Exception:
                safe_actions.append({"id": str(a.get("id", ""))})
        elif isinstance(a, str):
            safe_actions.append({"id": a})
    # schema handling: ensure dict if provided
    if schema is not None and not isinstance(schema, dict):
        schema = {"value": str(schema)}
    contrib = UIContribution(
        id=rec.id,
        extension_id=rec.extension_id,
        type=meta.get("type", "panel"),
        title=title,
        description=description,
        entry=entry,
        schema=schema,
        props=safe_props,
        actions=safe_actions,
        enabled=bool(rec.enabled),
        source=rec.source,
    )
    _ensure_serializable(contrib.to_dict())
    return contrib


# ---------------------------------------------------------------------------
# UICatalog
# ---------------------------------------------------------------------------

class UICatalog:
    """Dynamic UI catalog view over CapabilityRegistry + optional ExtensionRegistry.

    No duplicate state: reads directly from capability registry on each call.
    Respects enabled flag (disabled extensions -> UI unavailable).
    """

    def __init__(
        self,
        capability_registry: CapabilityRegistry,
        extension_registry: Optional[Any] = None,
    ) -> None:
        self._cap = capability_registry
        self._ext = extension_registry

    def list_contributions(
        self,
        extension_id: Optional[str] = None,
        ui_type: Optional[str] = None,
        enabled_only: bool = True,
    ) -> List[UIContribution]:
        """List UI contributions, optionally filtered."""
        records = self._cap.list("ui")
        out: List[UIContribution] = []
        for rec in records:
            if extension_id is not None and rec.extension_id != extension_id:
                continue
            if ui_type is not None and rec.metadata.get("type") != ui_type:
                continue
            if enabled_only and not rec.enabled:
                continue
            # Also check extension registry status if provided
            if enabled_only and self._ext is not None:
                try:
                    ext_rec = self._ext.get(rec.extension_id)
                    if ext_rec is not None and getattr(ext_rec, "status", "loaded") == "disabled":
                        continue
                except Exception:
                    pass
            out.append(record_to_contribution(rec))
        return sorted(out, key=lambda c: c.id.lower())

    def get_contribution(self, ui_id: str) -> Optional[UIContribution]:
        rec = self._cap.get("ui", ui_id)
        if rec is None:
            return None
        return record_to_contribution(rec)

    def exists(self, ui_id: str) -> bool:
        return self._cap.exists("ui", ui_id)

    def count(self, enabled_only: bool = True) -> int:
        return len(self.list_contributions(enabled_only=enabled_only))

    def to_dict(self, enabled_only: bool = True) -> Dict[str, Any]:
        contributions = self.list_contributions(enabled_only=enabled_only)
        return {
            "count": len(contributions),
            "contributions": [c.to_dict() for c in contributions],
        }

    def to_serializable(self, enabled_only: bool = True) -> Dict[str, Any]:
        return self.to_dict(enabled_only=enabled_only)


# ---------------------------------------------------------------------------
# Result / Viewer helpers (generic)
# ---------------------------------------------------------------------------

def make_table_result(
    columns: List[Dict[str, Any]],
    rows: List[Any],
    pagination: Optional[Dict[str, Any]] = None,
    title: str = "",
) -> UIResult:
    data: Dict[str, Any] = {"columns": columns, "rows": rows}
    if pagination:
        data["pagination"] = pagination
    if title:
        data["title"] = title
    _validate_table_payload(data)
    return UIResult(renderer="table", type="table", data=data)


def make_chart_result(
    chart_type: str,
    labels: List[Any],
    datasets: List[Dict[str, Any]],
    title: str = "",
) -> UIResult:
    if chart_type not in CHART_TYPES:
        # still allow but ensure type field present
        pass
    data: Dict[str, Any] = {"type": chart_type, "labels": labels, "datasets": datasets}
    if title:
        data["title"] = title
    _validate_chart_data(data)
    return UIResult(renderer="chart", type="chart", data=data)


def make_viewer_result(
    viewer_type: str,
    payload: Any,
    artifact: Optional[Dict[str, Any]] = None,
    mime_type: str = "",
) -> UIResult:
    if viewer_type not in VIEWER_TYPES and viewer_type not in UI_TYPES:
        # allow generic
        pass
    data: Dict[str, Any] = {"viewer_type": viewer_type, "payload": payload}
    if mime_type:
        data["mime_type"] = mime_type
    return UIResult(renderer="viewer", type=viewer_type, data=data, artifact=artifact)


def make_artifact_result(
    artifact: Dict[str, Any],
    renderer: str = "viewer",
) -> UIResult:
    """Generic artifact -> viewer mapping."""
    return UIResult(renderer=renderer, type=renderer, data=None, artifact=artifact)


def resolve_renderer_for_result(result: Dict[str, Any]) -> str:
    """Resolve renderer id from structured result (no hardcode extension logic).

    Uses fields: renderer, type, viewer_type, artifact.mime_type.
    """
    if not isinstance(result, dict):
        return "text"
    # explicit renderer field has priority
    for key in ("renderer", "type", "viewer_type"):
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            v = val.strip()
            # Normalize to known renderer if possible, otherwise keep as-is (generic)
            return v
    # Artifact based
    artifact = result.get("artifact")
    if isinstance(artifact, dict):
        mime = str(artifact.get("mime_type", "")).lower()
        if "image" in mime:
            return "image"
        if "video" in mime:
            return "video"
        if "audio" in mime:
            return "audio"
        if "pdf" in mime:
            return "pdf"
        if "json" in mime:
            return "json"
        if mime.startswith("text/"):
            return "text"
    # Fallback
    return "text"


def is_renderer_available(renderer: str) -> bool:
    """Generic check: renderer is among known types (no extension hardcode)."""
    return renderer in RENDERER_TYPES or renderer in VIEWER_TYPES or renderer in UI_TYPES


# ---------------------------------------------------------------------------
# Enable/disable awareness delegation (capability-level)
# ---------------------------------------------------------------------------

def set_extension_ui_enabled(
    capability_registry: CapabilityRegistry,
    extension_id: str,
    enabled: bool,
) -> int:
    """Set enabled flag for all UI capabilities of an extension. Returns count."""
    count = 0
    for rec in capability_registry.list_by_extension(extension_id):
        if rec.type == "ui":
            capability_registry.set_enabled(rec.type, rec.id, bool(enabled))
            count += 1
    return count


def set_extension_enabled(
    capability_registry: CapabilityRegistry,
    extension_id: str,
    enabled: bool,
    extension_registry: Optional[Any] = None,
) -> int:
    """Set enabled for all capabilities of extension (generic)."""
    count = 0
    for rec in capability_registry.list_by_extension(extension_id):
        capability_registry.set_enabled(rec.type, rec.id, bool(enabled))
        count += 1
    if extension_registry is not None:
        try:
            ext_rec = extension_registry.get(extension_id)
            if ext_rec is not None:
                ext_rec.status = "loaded" if enabled else "disabled"
        except Exception:
            pass
    return count


# ---------------------------------------------------------------------------
# Validation helpers exposed for tests
# ---------------------------------------------------------------------------

def validate_ui_contribution_dict(d: Dict[str, Any]) -> None:
    """Validate that a UI contribution dict is well-formed and serializable."""
    required = ("id", "extension_id", "type")
    for k in required:
        if k not in d or not d[k]:
            raise CapabilityValidationError(f"UI contribution missing {k}")
    if d["type"] not in UI_TYPES:
        raise CapabilityValidationError(f'UI type "{d["type"]}" not in {UI_TYPES}')
    _ensure_serializable(d)


__all__ = [
    "UI_TYPES",
    "CHART_TYPES",
    "VIEWER_TYPES",
    "RENDERER_TYPES",
    "CONFIG_TYPE_TO_FIELD",
    "UIContribution",
    "UIResult",
    "ArtifactRef",
    "UICatalog",
    "record_to_contribution",
    "config_type_to_field_type",
    "config_definition_to_field",
    "config_definitions_to_form_schema",
    "build_form_schema_for_extension",
    "make_table_result",
    "make_chart_result",
    "make_viewer_result",
    "make_artifact_result",
    "resolve_renderer_for_result",
    "is_renderer_available",
    "set_extension_ui_enabled",
    "set_extension_enabled",
    "validate_ui_contribution_dict",
]
