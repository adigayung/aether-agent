"""Extension API version contract for AETHER.

Single source of truth for current Extension API version.
Task 01 defines only version "1" without complex compatibility matrix.
"""

from __future__ import annotations

#: Current Extension API version that AETHER Core implements.
CURRENT_API_VERSION: str = "1"

#: Supported versions (Task 01: only "1").
SUPPORTED_API_VERSIONS = (CURRENT_API_VERSION,)

__all__ = ["CURRENT_API_VERSION", "SUPPORTED_API_VERSIONS"]
