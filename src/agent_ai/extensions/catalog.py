"""Extension Catalog — lightweight metadata view over ExtensionRegistry (Task 02).

Catalog is metadata-only (like Skill Catalog):
    - id, name, version, description, api_version, status
    - publisher if manifest provides it
    - capabilities metadata if manifest provides it
    - does NOT include Python source, tool impl, skill.md, knowledge, resources

Dynamic: catalog reflects current Registry state (no duplicate source-of-truth).
Reads directly from registry on each call.

Concept API:
    get_catalog() -> {count, extensions: [metadata...]}
    get_extension(id) -> metadata dict or None
    extension_exists(id) -> bool
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from agent_ai.extensions.registry import ExtensionRegistry


@dataclass
class ExtensionCatalogEntry:
    """Lightweight metadata for one extension."""

    id: str
    name: str
    version: str
    description: str
    api_version: str
    status: str = "loaded"
    publisher: Optional[str] = None
    capabilities: Optional[Any] = None
    source: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "api_version": self.api_version,
            "status": self.status,
        }
        if self.publisher is not None:
            d["publisher"] = self.publisher
        if self.capabilities is not None:
            d["capabilities"] = self.capabilities
        # source is useful for debugging but optional; keep if present
        # Not required by spec but harmless; omit if not needed? include for traceability.
        # To keep catalog lightweight and consistent with example, we include status but source only if needed.
        # We will NOT include Python source / tool impl etc.
        return d


def _record_to_entry(record) -> ExtensionCatalogEntry:
    manifest = record.manifest
    raw = getattr(manifest, "raw", {}) or {}
    publisher = raw.get("publisher")
    capabilities = raw.get("capabilities")
    if capabilities is None:
        capabilities = raw.get("provides")
    status = getattr(record, "status", "loaded") or "loaded"
    # Try to reflect lifecycle store if available (enabled/disabled/failed)
    try:
        from agent_ai.extensions.lifecycle import get_lifecycle_store

        _store = get_lifecycle_store()
        _row = _store.get_status_row(manifest.id)
        if _row is not None and _row.get("status"):
            status = _row.get("status")
    except Exception:
        pass
    return ExtensionCatalogEntry(
        id=manifest.id,
        name=manifest.name,
        version=manifest.version,
        description=manifest.description,
        api_version=manifest.api_version,
        status=status,
        publisher=publisher,
        capabilities=capabilities,
        source=getattr(record, "source", None),
    )


class ExtensionCatalog:
    """Dynamic catalog view over a registry (no duplicate state)."""

    def __init__(self, registry: ExtensionRegistry) -> None:
        self._registry = registry

    def list_entries(self) -> List[ExtensionCatalogEntry]:
        entries: List[ExtensionCatalogEntry] = []
        for rec in self._registry.all():
            entries.append(_record_to_entry(rec))
        # already sorted via registry.all() deterministic
        return entries

    def get_catalog(self) -> Dict[str, Any]:
        """Return catalog dict: {count, extensions: [metadata dict...]}

        Metadata-only: no Python source, tool impl, skill content, knowledge.
        Deterministic (sorted by id).
        """
        entries = self.list_entries()
        return {
            "count": len(entries),
            "extensions": [e.to_dict() for e in entries],
        }

    def get_extension(self, extension_id: str) -> Optional[Dict[str, Any]]:
        """Return metadata dict for one extension or None."""
        rec = self._registry.get(extension_id)
        if rec is None:
            return None
        return _record_to_entry(rec).to_dict()

    def extension_exists(self, extension_id: str) -> bool:
        return self._registry.exists(extension_id)

    # aliases
    def get(self, extension_id: str) -> Optional[Dict[str, Any]]:
        return self.get_extension(extension_id)

    def exists(self, extension_id: str) -> bool:
        return self.extension_exists(extension_id)


# Module-level helpers for convenience (require registry)

def get_catalog(registry: ExtensionRegistry) -> Dict[str, Any]:
    return ExtensionCatalog(registry).get_catalog()


def get_extension(registry: ExtensionRegistry, extension_id: str) -> Optional[Dict[str, Any]]:
    return ExtensionCatalog(registry).get_extension(extension_id)


def extension_exists(registry: ExtensionRegistry, extension_id: str) -> bool:
    return ExtensionCatalog(registry).extension_exists(extension_id)


__all__ = [
    "ExtensionCatalog",
    "ExtensionCatalogEntry",
    "get_catalog",
    "get_extension",
    "extension_exists",
]
