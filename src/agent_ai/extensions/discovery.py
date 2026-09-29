"""Extension discovery contract (Task 01 minimal).

Contract: Extensions are discoverable via entry-points OR via filesystem
at <AETHER_ROOT>/Extension/<free-folder>/ with manifest.json + pyproject.toml.

Task 01 provides minimal load capability:
    - load_extension_from_dir (explicit contract test)
    - discover_extensions via entry-points (no heavy registry)
Filesystem discovery helper for testing / future Task 02.

Does NOT create ExtensionRegistry / Catalog — that's Task 02.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_ai.extensions.base import Extension
from agent_ai.extensions.context import ExtensionContext
from agent_ai.extensions.manifest import Manifest, load_manifest, parse_manifest
from agent_ai.extensions.paths import get_aether_root, get_extensions_dir

try:
    import importlib.metadata as importlib_metadata
except ImportError:  # pragma: no cover - py<3.8
    import importlib_metadata  # type: ignore


class ExtensionLoadError(Exception):
    """Failed to load extension."""


@dataclass
class ExtensionEntry:
    """One discovered extension (contract minimal).

    Attributes:
        manifest: validated Manifest
        extension: Extension instance (entry object)
        source: filesystem folder or entry-point name
        entry_point: entry-point identifier if discovered via entry-points
    """

    manifest: Manifest
    extension: Extension
    source: str
    entry_point: Optional[str] = None


def _load_py_module_from_dir(extension_dir: Path) -> Any:
    """Load extension.py as module from directory.

    Used when Extension is at filesystem location without package install.
    Loads {dir}/extension.py as temporary module.
    """
    ext_file = extension_dir / "extension.py"
    if not ext_file.is_file():
        raise ExtensionLoadError(f"extension.py not found in '{extension_dir}'")
    # Load as module with unique name
    mod_name = f"_aether_ext_{extension_dir.name}_{id(extension_dir)}"
    spec = importlib.util.spec_from_file_location(mod_name, str(ext_file))
    if spec is None or spec.loader is None:
        raise ExtensionLoadError(f"Cannot load extension.py from '{ext_file}'")
    mod = importlib.util.module_from_spec(spec)
    # Ensure parent package not required; inject into sys.modules temporarily
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    except Exception as exc:
        sys.modules.pop(mod_name, None)
        raise ExtensionLoadError(f"Failed to execute extension.py in '{extension_dir}': {exc}") from exc
    return mod


def load_extension_from_dir(extension_dir: str | Path) -> ExtensionEntry:
    """Load extension from filesystem directory.

    Validates:
        - extension_dir exists
        - manifest.json valid
        - extension.py exists and exposes `extension` object with lifecycle

    Folder name is NOT used as identity — manifest id is.

    Raises:
        ExtensionLoadError / ManifestValidationError
    """
    dir_path = Path(extension_dir).resolve()
    if not dir_path.is_dir():
        raise ExtensionLoadError(f"Extension directory not found: '{dir_path}'")

    manifest_path = dir_path / "manifest.json"
    manifest = load_manifest(str(manifest_path))

    # Strategy 1: try import as package if __init__.py exists and dir added to path
    # Strategy 2: load extension.py directly
    # Prefer extension.py direct load to avoid polluting sys.path permanently.
    # But also support package import: attempt to load extension object.

    mod = None
    ext_obj: Optional[Any] = None

    # If directory looks like a package (has __init__.py), try to use pyproject entry?
    # For Task 01, we support both: if __init__.py exists, try loading via import with temp sys.path.
    init_file = dir_path / "__init__.py"
    if init_file.is_file() and (dir_path / "extension.py").is_file():
        # Load via spec with parent dir on sys.path temporarily to allow `from .extension import`
        parent = str(dir_path.parent)
        added = parent not in sys.path
        if added:
            sys.path.insert(0, parent)
        try:
            # Try importing as package by folder name as module (may not match package name)
            # Fallback to direct file load.
            mod = _load_py_module_from_dir(dir_path)
        finally:
            if added:
                try:
                    sys.path.remove(parent)
                except ValueError:
                    pass
        # After direct load, search for extension object
        if mod is not None:
            ext_obj = getattr(mod, "extension", None)
            if ext_obj is None:
                # Also try class-based discovery: find Extension subclass instance?
                ext_obj = getattr(mod, "ext", None)
    else:
        mod = _load_py_module_from_dir(dir_path)
        ext_obj = getattr(mod, "extension", None)

    if ext_obj is None:
        raise ExtensionLoadError(
            f"Extension entry 'extension' not found in '{dir_path}/extension.py' (expected `extension = ExampleExtension()` or similar)"
        )

    # If extension object is Extension subclass but manifest not attached, attach it.
    if isinstance(ext_obj, Extension):
        if ext_obj.manifest is None:
            ext_obj.manifest = manifest
    else:
        # Plain object with lifecycle methods: wrap check duck-typing
        # Must have at least register or be callable-ish. We accept any object with register/enable/disable attrs.
        # To keep contract, attach manifest attribute if missing.
        if not hasattr(ext_obj, "manifest"):
            try:
                setattr(ext_obj, "manifest", manifest)
            except Exception:
                pass

    # Verify lifecycle interface (duck-typed)
    for method in ("register", "enable", "disable"):
        if hasattr(ext_obj, method) and not callable(getattr(ext_obj, method)):
            raise ExtensionLoadError(f"Extension object attribute '{method}' is not callable in '{dir_path}'")

    return ExtensionEntry(
        manifest=manifest,
        extension=ext_obj,  # type: ignore[arg-type]
        source=str(dir_path),
    )


def discover_extensions(
    *,
    entry_point_group: str = "aether.extensions",
) -> List[ExtensionEntry]:
    """Discover extensions via Python entry-points (contract for Task 01).

    This is the contract that future Extension Catalog will build on.
    It does NOT scan filesystem Extension/ by default — that's filesystem
    loading above. Registry (Task 02) will unify both.

    Returns:
        List of ExtensionEntry for each valid entry-point. Invalid manifests
        or load errors are skipped (with error entry? For Task 01 we skip).
        Duplicate ids raise via caller if needed; here we just collect.
    """
    entries: List[ExtensionEntry] = []
    try:
        eps = importlib_metadata.entry_points()
        # Python 3.10+ returns EntryPoints; 3.12 group param; handle both
        try:
            group_eps = eps.select(group=entry_point_group)  # type: ignore
        except AttributeError:
            # Older API: dict-like
            group_eps = eps.get(entry_point_group, [])  # type: ignore
    except Exception:
        return []

    for ep in group_eps:
        try:
            obj = ep.load()
        except Exception:
            continue
        # obj may be Extension instance or class
        ext_obj = obj
        # If it's a class, instantiate
        if isinstance(obj, type):
            try:
                ext_obj = obj()
            except Exception:
                continue
        manifest = getattr(ext_obj, "manifest", None)
        # Try to infer manifest from nearby file? For now require manifest attr or attached
        if manifest is None:
            # Cannot validate without manifest: skip
            continue
        # If manifest is dict, parse it
        if isinstance(manifest, dict):
            try:
                manifest = parse_manifest(manifest, source_path=f"entry-point:{ep.name}")
            except Exception:
                continue
        if not isinstance(manifest, Manifest):
            continue
        entries.append(ExtensionEntry(manifest=manifest, extension=ext_obj, source=str(ep), entry_point=ep.name))
    return entries


def discover_extensions_from_dir(extensions_root: str | Path) -> List[ExtensionEntry]:
    """Discover extensions from filesystem <AETHER_ROOT>/Extension/ directory.

    Scans immediate subfolders for manifest.json.
    Skips invalid folders; collects valid ones.
    Used for testing contract that folder name != id.
    """
    root = Path(extensions_root)
    if not root.is_dir():
        return []
    results: List[ExtensionEntry] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        # Must have manifest.json to be considered extension
        if not (child / "manifest.json").is_file():
            continue
        try:
            entry = load_extension_from_dir(child)
            results.append(entry)
        except Exception:
            continue
    return results


__all__ = [
    "ExtensionEntry",
    "ExtensionLoadError",
    "load_extension_from_dir",
    "discover_extensions",
    "discover_extensions_from_dir",
]
