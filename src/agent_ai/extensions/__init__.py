"""AETHER Extension System — Foundation & Contract (Task 01-05).

UI system (Task 05) adds generic UI registry, declarative rendering, viewer
and result contracts — all without hardcoding extension names.
"""

from agent_ai.extensions.api_version import CURRENT_API_VERSION, SUPPORTED_API_VERSIONS
from agent_ai.extensions.base import Extension
from agent_ai.extensions.context import ExtensionContext
from agent_ai.extensions.manifest import (
    DuplicateExtensionError,
    Manifest,
    ManifestError,
    ManifestValidationError,
    detect_duplicate_ids,
    load_manifest,
    parse_manifest,
    validate_manifest,
    validate_manifest_data,
)
from agent_ai.extensions.paths import get_aether_root, get_extensions_dir, get_aether_data_dir, get_extensions_data_dir
from agent_ai.extensions.discovery import (
    ExtensionEntry,
    ExtensionLoadError,
    discover_extensions,
    discover_extensions_from_dir,
    load_extension_from_dir,
)
from agent_ai.extensions.registry import ExtensionRecord, ExtensionRegistry
from agent_ai.extensions.catalog import ExtensionCatalog, ExtensionCatalogEntry
from agent_ai.extensions.loader import ExtensionLoader, ExtensionLoadResult, StartupLoadResult, load_all_extensions
from agent_ai.extensions.capabilities import (
    CAPABILITY_TYPES,
    CONFIG_TYPES,
    UI_TYPES,
    CapabilityError,
    CapabilityRecord,
    CapabilityRegistry,
    CapabilityValidationError,
    DuplicateCapabilityError,
    global_capability_registry,
)
from agent_ai.extensions.ui import (
    ArtifactRef,
    UICatalog,
    UIContribution,
    UIResult,
    build_form_schema_for_extension,
    config_definition_to_field,
    config_definitions_to_form_schema,
    make_artifact_result,
    make_chart_result,
    make_table_result,
    make_viewer_result,
    resolve_renderer_for_result,
    set_extension_enabled,
    set_extension_ui_enabled,
    validate_ui_contribution_dict,
)
from agent_ai.extensions.lifecycle import ExtensionLifecycleStore, get_lifecycle_store
from agent_ai.extensions.manager import ExtensionManager
from agent_ai.extensions.errors import (
    ExtensionCompatibilityError,
    ExtensionDependencyError,
    ExtensionError,
    ExtensionInstallError,
    ExtensionLifecycleError,
    ExtensionUninstallError,
    ExtensionUpdateError,
    ExtensionValidationError,
)

__all__ = [
    "Extension",
    "ExtensionContext",
    "ExtensionEntry",
    "ExtensionLoadError",
    "ExtensionRecord",
    "ExtensionRegistry",
    "ExtensionCatalog",
    "ExtensionCatalogEntry",
    "ExtensionLoader",
    "ExtensionLoadResult",
    "StartupLoadResult",
    "Manifest",
    "ManifestError",
    "ManifestValidationError",
    "DuplicateExtensionError",
    "CapabilityError",
    "CapabilityRecord",
    "CapabilityRegistry",
    "CapabilityValidationError",
    "DuplicateCapabilityError",
    "global_capability_registry",
    "CURRENT_API_VERSION",
    "SUPPORTED_API_VERSIONS",
    "get_aether_root",
    "get_extensions_dir",
    "get_aether_data_dir",
    "get_extensions_data_dir",
    "load_manifest",
    "parse_manifest",
    "validate_manifest",
    "validate_manifest_data",
    "detect_duplicate_ids",
    "discover_extensions",
    "discover_extensions_from_dir",
    "load_extension_from_dir",
    "load_all_extensions",
    "ExtensionLifecycleStore",
    "get_lifecycle_store",
    "ExtensionManager",
    "ExtensionError",
    "ExtensionInstallError",
    "ExtensionValidationError",
    "ExtensionDependencyError",
    "ExtensionCompatibilityError",
    "ExtensionLifecycleError",
    "ExtensionUpdateError",
    "ExtensionUninstallError",
]
