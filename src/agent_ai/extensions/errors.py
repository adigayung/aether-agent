"""Extension lifecycle/installer error contract (Task 06).

Clear, LLM-readable errors with extension_id, operation, root cause.
Hierarchy kept small, no excessive nesting.
"""

from __future__ import annotations


class ExtensionError(Exception):
    """Base for all extension lifecycle/install errors."""


class ExtensionInstallError(ExtensionError):
    """Install failed."""


class ExtensionValidationError(ExtensionError):
    """Validation failed (manifest, structure, API, entry point, etc.)."""


class ExtensionDependencyError(ExtensionError):
    """Dependency installation failed."""


class ExtensionCompatibilityError(ExtensionError):
    """API compatibility failed."""


class ExtensionLifecycleError(ExtensionError):
    """Enable/disable lifecycle failed."""


class ExtensionUpdateError(ExtensionError):
    """Update failed."""


class ExtensionUninstallError(ExtensionError):
    """Uninstall failed."""


__all__ = [
    "ExtensionError",
    "ExtensionInstallError",
    "ExtensionValidationError",
    "ExtensionDependencyError",
    "ExtensionCompatibilityError",
    "ExtensionLifecycleError",
    "ExtensionUpdateError",
    "ExtensionUninstallError",
]
