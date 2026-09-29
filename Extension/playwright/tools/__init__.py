"""Playwright tools package.

Exports :func:`build_playwright_tools`, a factory that builds the tool layer
sitting between the LLM and the :class:`PlaywrightService`.
"""

from .playwright_tools import build_playwright_tools

__all__ = ["build_playwright_tools"]
