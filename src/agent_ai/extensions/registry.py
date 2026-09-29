"""Extension Registry — centralized store for loaded Extensions (Task 02).

Registry uses Extension ID from manifest (not folder name) as key.
Detects duplicate IDs via DuplicateExtensionError.
Stores minimal info needed: ID, Manifest, Extension instance, root/path, status.

Status awareness (Task 02 minimal, forward-compatible with Task 07):
    - "loaded"   : successfully loaded & registered
    - "failed"   : failed to load (tracked separately via loader, not as active)
    - "enabled" / "disabled" / "installed" : reserved for future Task 07,
      not yet persisted here but registry/catalog design allows distinguishing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_ai.extensions.base import Extension
from agent_ai.extensions.discovery import ExtensionEntry
from agent_ai.extensions.manifest import DuplicateExtensionError, Manifest


@dataclass
class ExtensionRecord:
    """One loaded Extension stored in registry."""

    id: str
    manifest: Manifest
    extension: Any
    root: Optional[Path]
    source: str
    status: str = "loaded"
    error: Optional[str] = None
    entry_point: Optional[str] = None

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def version(self) -> str:
        return self.manifest.version


class ExtensionRegistry:
    """Central registry for Extensions that were successfully discovered/loaded.

    Uses Extension ID (manifest.id) as key. Duplicate IDs raise error,
    never silently overwrite.

    Example:
        registry = ExtensionRegistry()
        registry.register(entry)   # entry: ExtensionEntry
        registry.get("someone.browser")
        registry.all()
        registry.exists("someone.browser")
        registry.unregister("someone.browser")
    """

    def __init__(self) -> None:
        self._records: Dict[str, ExtensionRecord] = {}
        # failures are tracked separately for observability; they are not active entries
        self._failures: List[Dict[str, Any]] = []

    # -- registration ---------------------------------------------------

    def register(self, entry: ExtensionEntry) -> ExtensionRecord:
        """Register a discovered ExtensionEntry.

        Args:
            entry: ExtensionEntry with validated Manifest and instance.

        Returns:
            ExtensionRecord stored.

        Raises:
            DuplicateExtensionError: if id already exists (do not overwrite)
            ValueError: if entry or manifest missing id
        """
        if entry is None or entry.manifest is None:
            raise ValueError("ExtensionEntry with valid manifest required")
        ext_id = entry.manifest.id
        if not ext_id:
            raise ValueError("Extension manifest id must not be empty")
        if ext_id in self._records:
            existing = self._records[ext_id]
            raise DuplicateExtensionError(
                f'Duplicate Extension id "{ext_id}" detected '
                f'(existing source: "{existing.source}" and new source: "{entry.source}")'
            )
        # Derive root Path from source if filesystem path
        root: Optional[Path] = None
        try:
            p = Path(entry.source)
            if p.exists() and p.is_dir():
                root = p
            elif p.parent.exists():
                # entry.source may be file path; treat parent
                root = p.parent if p.parent.is_dir() else None
        except Exception:
            root = None

        record = ExtensionRecord(
            id=ext_id,
            manifest=entry.manifest,
            extension=entry.extension,
            root=root if root is not None else (Path(entry.source) if entry.source else None),
            source=entry.source,
            status="loaded",
            error=None,
            entry_point=entry.entry_point,
        )
        # Attach manifest to extension instance if missing
        try:
            if isinstance(record.extension, Extension) and getattr(record.extension, "manifest", None) is None:
                record.extension.manifest = record.manifest
        except Exception:
            pass
        self._records[ext_id] = record
        return record

    # alternative: register from raw components (convenience)
    def register_record(self, record: ExtensionRecord) -> ExtensionRecord:
        if record.id in self._records:
            raise DuplicateExtensionError(f'Duplicate Extension id "{record.id}" detected')
        self._records[record.id] = record
        return record

    # -- access ---------------------------------------------------------

    def get(self, extension_id: str) -> Optional[ExtensionRecord]:
        """Return record for id or None if not found."""
        return self._records.get(extension_id)

    # alias for spec
    def get_extension(self, extension_id: str) -> Optional[ExtensionRecord]:
        return self.get(extension_id)

    def all(self) -> List[ExtensionRecord]:
        """All loaded Extensions sorted by id (deterministic)."""
        return sorted(self._records.values(), key=lambda r: r.id.lower())

    def exists(self, extension_id: str) -> bool:
        return extension_id in self._records

    def extension_exists(self, extension_id: str) -> bool:
        return self.exists(extension_id)

    def remove(self, extension_id: str) -> bool:
        """Remove / unregister extension. Returns True if removed."""
        if extension_id in self._records:
            del self._records[extension_id]
            return True
        return False

    def unregister(self, extension_id: str) -> bool:
        return self.remove(extension_id)

    def clear(self) -> None:
        self._records.clear()
        self._failures.clear()

    @property
    def count(self) -> int:
        return len(self._records)

    def __len__(self) -> int:  # pragma: no cover
        return len(self._records)

    def __contains__(self, extension_id: str) -> bool:
        return extension_id in self._records

    # -- failures tracking (for loader observability) -------------------

    def add_failure(self, source: str, error: str, extension_id: Optional[str] = None) -> None:
        self._failures.append({"source": source, "error": error, "id": extension_id, "status": "failed"})

    def failures(self) -> List[Dict[str, Any]]:
        return list(self._failures)

    def clear_failures(self) -> None:
        self._failures.clear()


__all__ = ["ExtensionRegistry", "ExtensionRecord"]
