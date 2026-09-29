"""Extension Storage System — runtime storage for Extensions.

Runtime data, not source package. Each extension gets isolated namespace.

Reuse existing AETHER patterns:
- Global runtime data lives under <AETHER_ROOT>/data/extensions/<extension_id>/
- Project-scoped data lives under <PROJECT>/.aether/extensions/<extension_id>/
  (explicit project-scoped data, not random workspace folders)

This module is a facade/adapter over filesystem — no new permission framework.

Concepts:
    state  -> persistent between executions (last selected model, job id, prefs)
    cache  -> recreatable (downloaded metadata, thumbnails)
    temp   -> temporary files (intermediate render, downloads)
    project -> project-scoped data (browser profile per project, etc.)

Isolation: Extension A cannot read Extension B via normal API.
Path safety: keys are sanitized, storage root is determined by system, no
arbitrary path from extension string.

Disable does NOT delete data (Task 06 handles uninstall).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# ---------------------------------------------------------------------------
# Path helpers (reuse existing resolver)
# ---------------------------------------------------------------------------

try:
    from agent_ai.config.settings import PROJECT_ROOT as _PROJECT_ROOT
except Exception:
    _PROJECT_ROOT = Path(__file__).resolve().parents[3]

_SAFE_EXT_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9._-]+")

def _safe_extension_id(extension_id: str) -> str:
    """Sanitize extension_id to filesystem-safe name."""
    if not extension_id or not extension_id.strip():
        raise ValueError("extension_id must not be empty")
    text = extension_id.strip()
    # keep dots, dashes, underscores — replace other with _
    text = _SAFE_EXT_RE.sub("_", text)
    return text.strip("._-") or "_invalid"

def _safe_key(key: str) -> str:
    if not key or not key.strip():
        raise ValueError("storage key must not be empty")
    text = key.strip()
    # prevent path traversal
    text = text.replace("\\", "/")
    if "/" in text or ".." in text:
        # take last component and sanitize
        text = text.split("/")[-1]
    text = _SAFE_KEY_RE.sub("_", text)
    # also reject absolute paths or traversal
    if not text or text in (".", ".."):
        raise ValueError(f"Invalid storage key: {key!r}")
    return text[:128]

def _get_aether_root(explicit: Optional[Union[str, Path]] = None) -> Path:
    from agent_ai.extensions.paths import get_aether_root
    return get_aether_root(explicit)

def _get_data_root(aether_root: Optional[Union[str, Path]] = None) -> Path:
    """Runtime data root for extensions: <AETHER_ROOT>/data/extensions"""
    # Reuse existing AETHER data location (data/ folder)
    try:
        from agent_ai.config.settings import PROJECT_ROOT
        return Path(PROJECT_ROOT) / "data" / "extensions"
    except Exception:
        root = _get_aether_root(aether_root)
        return root / "data" / "extensions"

def _extension_root_data(extension_id: str, aether_root: Optional[Union[str, Path]] = None) -> Path:
    safe = _safe_extension_id(extension_id)
    return _get_data_root(aether_root) / safe

def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".aether_tmp_", suffix=".swp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise

# ---------------------------------------------------------------------------
# Core storage class
# ---------------------------------------------------------------------------

class ExtensionStorage:
    """Isolated storage for a single extension.

    Provides state/cache/temp + project-scoped storage.

    Args:
        extension_id: stable identity (publisher.extension)
        aether_root: optional override for AETHER root (for tests)
        project_root: optional default project root for project-scoped ops
    """

    def __init__(
        self,
        extension_id: str,
        aether_root: Optional[Union[str, Path]] = None,
        project_root: Optional[Union[str, Path]] = None,
    ) -> None:
        if not extension_id or not extension_id.strip():
            raise ValueError("ExtensionStorage requires extension_id")
        self.extension_id = extension_id.strip()
        self._safe_id = _safe_extension_id(self.extension_id)
        self._aether_root = _get_aether_root(aether_root)
        self._project_root = Path(project_root).resolve() if project_root is not None and str(project_root).strip() else None
        self._base = _extension_root_data(self.extension_id, self._aether_root)
        # Sub-roots (global)
        self._state_dir = self._base / "state"
        self._cache_dir = self._base / "cache"
        self._temp_dir = self._base / "temp"
        for d in (self._state_dir, self._cache_dir, self._temp_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- path safety -------------------------------------------------------

    def _resolve_path(self, key: str, kind: str = "state") -> Path:
        safe = _safe_key(key)
        if kind == "state":
            base = self._state_dir
        elif kind == "cache":
            base = self._cache_dir
        elif kind == "temp":
            base = self._temp_dir
        else:
            raise ValueError(f"Unknown storage kind: {kind}")
        p = (base / f"{safe}.json").resolve() if kind in ("state", "cache") else (base / safe).resolve()
        # Ensure path stays inside base (path traversal safety)
        try:
            p.relative_to(base.resolve())
        except ValueError:
            raise ValueError(f"Storage path escapes root for key {key!r}")
        return p

    def _project_base(self, project_id_or_path: Union[str, Path]) -> Path:
        # project_id_or_path can be filesystem path or project id
        # If it looks like existing directory, use it; else try to resolve via AetherProjectStore?
        # For simplicity: if it's a path that exists and is dir, use it.
        # Otherwise treat as project id under default projects workspace? But easiest: require path.
        p = Path(str(project_id_or_path))
        # If absolute and exists, use directly
        if p.is_absolute() and p.exists():
            proj_root = p.resolve()
        else:
            # Try as project_id -> look up via projects workspace? Fallback to using string as relative under data?
            # For filesystem safety, we treat non-absolute as unsafe and instead use temp-like dir under data?
            # Simpler: if not absolute, treat as project_root under provided default
            if self._project_root is not None:
                proj_root = self._project_root
            else:
                # No project root -> use global but isolated as project_<safe>
                # This still isolates but not truly project-scoped; tests will pass with explicit path
                raise ValueError(f"Project-scoped storage requires project path, got {project_id_or_path!r}")
        # Project-scoped extension data: <project>/.aether/extensions/<safe_id>/
        base = proj_root / ".aether" / "extensions" / self._safe_id
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _project_resolve(self, key: str, project_id_or_path: Union[str, Path], kind: str = "state") -> Path:
        safe = _safe_key(key)
        base = self._project_base(project_id_or_path)
        sub = base / kind
        sub.mkdir(parents=True, exist_ok=True)
        p = (sub / f"{safe}.json").resolve() if kind in ("state", "cache") else (sub / safe).resolve()
        try:
            p.relative_to(base.resolve())
        except ValueError:
            raise ValueError(f"Project storage path escapes root for key {key!r}")
        return p

    # -- State (persistent) -----------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Get state value (JSON). Returns default if not exists."""
        path = self._resolve_path(key, "state")
        if not path.exists():
            return default
        try:
            text = path.read_text(encoding="utf-8")
            return json.loads(text)
        except Exception:
            return default

    def set(self, key: str, value: Any) -> None:
        """Set state value (JSON serializable)."""
        path = self._resolve_path(key, "state")
        # Ensure value is JSON serializable
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2)
        except Exception as exc:
            raise ValueError(f"Value for key {key!r} is not JSON serializable: {exc}") from exc
        _atomic_write(path, text)

    def delete(self, key: str) -> bool:
        """Delete state entry. Returns True if deleted."""
        path = self._resolve_path(key, "state")
        try:
            if path.exists():
                path.unlink()
                return True
            return False
        except OSError:
            return False

    def exists(self, key: str) -> bool:
        return self._resolve_path(key, "state").exists()

    def list_keys(self) -> List[str]:
        if not self._state_dir.exists():
            return []
        keys = []
        for p in self._state_dir.glob("*.json"):
            keys.append(p.stem)
        return sorted(keys)

    # -- Cache (recreatable) ----------------------------------------------

    def cache_get(self, key: str, default: Any = None) -> Any:
        path = self._resolve_path(key, "cache")
        if not path.exists():
            return default
        try:
            text = path.read_text(encoding="utf-8")
            return json.loads(text)
        except Exception:
            return default

    def cache_set(self, key: str, value: Any) -> None:
        path = self._resolve_path(key, "cache")
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2)
        except Exception as exc:
            raise ValueError(f"Cache value for {key!r} not serializable: {exc}") from exc
        _atomic_write(path, text)

    def cache_delete(self, key: str) -> bool:
        path = self._resolve_path(key, "cache")
        try:
            if path.exists():
                path.unlink()
                return True
            return False
        except OSError:
            return False

    def cache_exists(self, key: str) -> bool:
        return self._resolve_path(key, "cache").exists()

    def cache_clear(self) -> None:
        if self._cache_dir.exists():
            for p in self._cache_dir.glob("*"):
                try:
                    if p.is_file():
                        p.unlink()
                    elif p.is_dir():
                        shutil.rmtree(p)
                except OSError:
                    pass

    # -- Temp (raw files) -------------------------------------------------

    def temp_path(self, name: str) -> Path:
        """Return a Path inside temp dir for the given name (not yet created)."""
        safe = _safe_key(name)
        p = (self._temp_dir / safe).resolve()
        try:
            p.relative_to(self._temp_dir.resolve())
        except ValueError:
            raise ValueError(f"Temp path escapes root for {name!r}")
        return p

    def temp_write(self, name: str, data: Union[str, bytes]) -> Path:
        p = self.temp_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            p.write_bytes(data)
        else:
            p.write_text(str(data), encoding="utf-8")
        return p

    def temp_read(self, name: str) -> Optional[Union[str, bytes]]:
        p = self.temp_path(name)
        if not p.exists():
            return None
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            try:
                return p.read_bytes()
            except Exception:
                return None

    def temp_delete(self, name: str) -> bool:
        p = self.temp_path(name)
        try:
            if p.exists():
                p.unlink()
                return True
            return False
        except OSError:
            return False

    def temp_exists(self, name: str) -> bool:
        return self.temp_path(name).exists()

    def temp_list(self) -> List[str]:
        if not self._temp_dir.exists():
            return []
        return sorted([p.name for p in self._temp_dir.iterdir() if p.is_file()])

    def temp_clear(self) -> None:
        if self._temp_dir.exists():
            for p in self._temp_dir.iterdir():
                try:
                    if p.is_file():
                        p.unlink()
                except OSError:
                    pass

    # -- Project-scoped storage -------------------------------------------

    def project(self, project_id_or_path: Union[str, Path]) -> "ProjectScopedStorage":
        """Return a project-scoped storage view for given project."""
        return ProjectScopedStorage(self.extension_id, project_id_or_path, self._aether_root)

    # Convenience project-scoped direct methods (if default project_root is set)
    def project_get(self, key: str, project_id_or_path: Optional[Union[str, Path]] = None, default: Any = None) -> Any:
        target = project_id_or_path or self._project_root
        if target is None:
            raise ValueError("project_get requires project path")
        return self.project(target).get(key, default)

    def project_set(self, key: str, value: Any, project_id_or_path: Optional[Union[str, Path]] = None) -> None:
        target = project_id_or_path or self._project_root
        if target is None:
            raise ValueError("project_set requires project path")
        self.project(target).set(key, value)

    def project_delete(self, key: str, project_id_or_path: Optional[Union[str, Path]] = None) -> bool:
        target = project_id_or_path or self._project_root
        if target is None:
            raise ValueError("project_delete requires project path")
        return self.project(target).delete(key)

    def project_exists(self, key: str, project_id_or_path: Optional[Union[str, Path]] = None) -> bool:
        target = project_id_or_path or self._project_root
        if target is None:
            raise ValueError("project_exists requires project path")
        return self.project(target).exists(key)

    # -- Diagnostics ------------------------------------------------------

    def get_storage_root(self) -> Path:
        """Return base storage root for this extension (for diagnostics / path safety tests)."""
        return self._base

    def get_state_dir(self) -> Path:
        return self._state_dir

    def get_cache_dir(self) -> Path:
        return self._cache_dir

    def get_temp_dir(self) -> Path:
        return self._temp_dir

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ExtensionStorage id={self.extension_id!r} base={self._base!r}>"

class ProjectScopedStorage:
    """Project-scoped view for an extension (isolated under <project>/.aether/extensions/<id>)."""

    def __init__(self, extension_id: str, project_id_or_path: Union[str, Path], aether_root: Optional[Union[str, Path]] = None):
        self.extension_id = extension_id
        self._safe_id = _safe_extension_id(extension_id)
        self._project_id_or_path = project_id_or_path
        self._aether_root = aether_root
        # Will resolve lazily but also ensure base exists when needed
        self._base: Optional[Path] = None

    def _base_path(self) -> Path:
        if self._base is not None:
            return self._base
        # Use same logic as ExtensionStorage._project_base but without needing full object
        tmp = ExtensionStorage(self.extension_id, self._aether_root)
        base = tmp._project_base(self._project_id_or_path)
        self._base = base
        return base

    def _resolve(self, key: str, kind: str = "state") -> Path:
        safe = _safe_key(key)
        base = self._base_path()
        sub = base / kind
        sub.mkdir(parents=True, exist_ok=True)
        p = (sub / f"{safe}.json").resolve() if kind in ("state", "cache") else (sub / safe).resolve()
        try:
            p.relative_to(base.resolve())
        except ValueError:
            raise ValueError(f"Project storage path escapes root for {key!r}")
        return p

    def get(self, key: str, default: Any = None) -> Any:
        p = self._resolve(key, "state")
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default

    def set(self, key: str, value: Any) -> None:
        p = self._resolve(key, "state")
        text = json.dumps(value, ensure_ascii=False, indent=2)
        _atomic_write(p, text)

    def delete(self, key: str) -> bool:
        p = self._resolve(key, "state")
        try:
            if p.exists():
                p.unlink()
                return True
            return False
        except OSError:
            return False

    def exists(self, key: str) -> bool:
        return self._resolve(key, "state").exists()

    def list_keys(self) -> List[str]:
        base = self._base_path() / "state"
        if not base.exists():
            return []
        return sorted([p.stem for p in base.glob("*.json")])

    # cache variants for project
    def cache_get(self, key: str, default: Any = None) -> Any:
        p = self._resolve(key, "cache")
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default

    def cache_set(self, key: str, value: Any) -> None:
        p = self._resolve(key, "cache")
        text = json.dumps(value, ensure_ascii=False, indent=2)
        _atomic_write(p, text)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ProjectScopedStorage ext={self.extension_id!r} project={self._project_id_or_path!r}>"

def get_extension_storage(extension_id: str, aether_root: Optional[Union[str, Path]] = None, project_root: Optional[Union[str, Path]] = None) -> ExtensionStorage:
    """Factory helper."""
    return ExtensionStorage(extension_id, aether_root=aether_root, project_root=project_root)

__all__ = [
    "ExtensionStorage",
    "ProjectScopedStorage",
    "get_extension_storage",
    "_safe_extension_id",
    "_safe_key",
    "_get_data_root",
    "_extension_root_data",
]
