"""Manifest contract for AETHER Extension.

Manifest is readable without importing extension implementation.
Validates required fields and supports duplicate detection.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Union

from agent_ai.extensions.api_version import CURRENT_API_VERSION, SUPPORTED_API_VERSIONS

# Extension id: letters, digits, dot, underscore, dash. Must start with alphanum.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Version: generic non-empty, no spaces, allow semver.
_VERSION_RE = re.compile(r"^[0-9A-Za-z._+*-]+$")


class ManifestError(Exception):
    """Base error for manifest."""


class ManifestValidationError(ManifestError):
    """Manifest validation failed."""


class DuplicateExtensionError(ManifestError):
    """Duplicate extension identity detected."""


@dataclass(frozen=True)
class Manifest:
    """Validated manifest data.

    Attributes:
        id: stable unique identity (publisher.extension)
        name: display name
        version: semver-ish
        description: short description
        api_version: extension API version string
        raw: original dict (may contain extras)
    """

    id: str
    name: str
    version: str
    description: str
    api_version: str
    raw: Dict[str, Any]
    source_path: str = ""


def _require_str(data: Dict[str, Any], field: str) -> str:
    if field not in data:
        raise ManifestValidationError(f'Invalid Extension manifest: missing required field "{field}"')
    value = data[field]
    if not isinstance(value, str):
        raise ManifestValidationError(f'Invalid Extension manifest: field "{field}" must be a string')
    text = value.strip()
    if not text:
        raise ManifestValidationError(f'Invalid Extension manifest: field "{field}" must not be empty')
    return text


def validate_manifest_data(data: Any) -> Dict[str, str]:
    """Validate raw manifest dict. Returns normalized fields or raises."""
    if not isinstance(data, dict):
        raise ManifestValidationError("Invalid Extension manifest: must be a JSON object")
    mid = _require_str(data, "id")
    if not _ID_RE.match(mid):
        raise ManifestValidationError(
            f'Invalid Extension manifest: field "id" has invalid format "{mid}" (allowed: letters, digits, ., _, -; must start with alphanumeric)'
        )
    if "/" in mid or "\\" in mid or " " in mid:
        raise ManifestValidationError(f'Invalid Extension manifest: field "id" must not contain path separator or space')
    name = _require_str(data, "name")
    version = _require_str(data, "version")
    # Version format: must not contain spaces, allow common semver chars.
    if " " in version:
        raise ManifestValidationError(f'Invalid Extension manifest: field "version" must not contain space')
    if not _VERSION_RE.match(version):
        raise ManifestValidationError(f'Invalid Extension manifest: field "version" has invalid format "{version}"')
    description = _require_str(data, "description")
    api_version = _require_str(data, "api_version")
    # api_version should be non-empty; warn if unsupported but still valid for future
    # Keep error clear if missing; if present but unsupported, allow? For Task 01 strict.
    # We allow any non-empty but ensure it's string.
    return {
        "id": mid,
        "name": name,
        "version": version,
        "description": description,
        "api_version": api_version,
    }


def parse_manifest(data: Dict[str, Any], source_path: str = "") -> Manifest:
    """Parse and validate dict into Manifest."""
    fields = validate_manifest_data(data)
    return Manifest(
        id=fields["id"],
        name=fields["name"],
        version=fields["version"],
        description=fields["description"],
        api_version=fields["api_version"],
        raw=dict(data),
        source_path=source_path,
    )


def load_manifest(path: Union[str, Path]) -> Manifest:
    """Load manifest.json from path and validate.

    Args:
        path: path to manifest.json

    Raises:
        ManifestValidationError: if JSON invalid or required fields missing
        ManifestError: if file cannot be read
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"Invalid Extension manifest: cannot read '{p}': {exc}") from exc
    try:
        data = json.loads(text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ManifestValidationError(f"Invalid Extension manifest: invalid JSON in '{p}': {exc}") from exc
    try:
        return parse_manifest(data, source_path=str(p))
    except ManifestValidationError:
        raise
    except Exception as exc:
        raise ManifestValidationError(f"Invalid Extension manifest: {exc}") from exc


def validate_manifest(path: Union[str, Path]) -> Manifest:
    """Alias for load_manifest."""
    return load_manifest(path)


def detect_duplicate_ids(manifests: List[Manifest]) -> None:
    """Detect duplicate extension ids.

    Raises:
        DuplicateExtensionError: if duplicate id found, with clear message
    """
    seen: Dict[str, Manifest] = {}
    for m in manifests:
        if m.id in seen:
            raise DuplicateExtensionError(
                f'Duplicate Extension id "{m.id}" detected (sources: "{seen[m.id].source_path}" and "{m.source_path}")'
            )
        seen[m.id] = m


# Compatibility alias
load_manifest_file = load_manifest

__all__ = [
    "Manifest",
    "ManifestError",
    "ManifestValidationError",
    "DuplicateExtensionError",
    "validate_manifest_data",
    "parse_manifest",
    "load_manifest",
    "validate_manifest",
    "detect_duplicate_ids",
    "load_manifest_file",
    "CURRENT_API_VERSION",
    "SUPPORTED_API_VERSIONS",
]
