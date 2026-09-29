"""Regression test: Extension `aether.playwright` tools tersedia untuk Agent.

Akar masalah yang dijaga test ini: Extension System memang memuat/registrasi
Extension (`aether.playwright`) dan capability-nya, tetapi tool-nya TIDAK pernah
muncul pada toolset Agent karena registry Extension dan registry Agent
(`build_registry`) sebelumnya terpisah.

Test ini membuktikan (tanpa menjalankan browser / tanpa butuh package
``playwright``):
    1. Extension ter-load & ENABLED;
    2. minimal satu tool ``browser_*`` muncul pada Agent tool definitions;
    3. tool tersebut dapat dieksekusi lewat Agent runtime (ToolExecutor).

State proses-wide Extension di-isolasi per-test oleh ``tests/conftest.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.core.types import ToolCall  # noqa: E402
from agent_ai.extensions.agent_bridge import (  # noqa: E402
    ensure_agent_extensions_loaded,
    get_agent_extension_manager,
)
from agent_ai.tools.registry import build_registry  # noqa: E402

EXT_ID = "aether.playwright"
PREFIX = EXT_ID + "."
SAFE_TOOL = EXT_ID + ".session_state_list"


def _loaded():
    """Load Extension (idempotent) dan kembalikan manager, atau skip bila absen."""
    assert ensure_agent_extensions_loaded() is True
    manager = get_agent_extension_manager()
    if not manager.registry.exists(EXT_ID):
        pytest.skip("Extension 'aether.playwright' tidak ter-discover di env ini")
    return manager


def test_playwright_extension_enabled():
    manager = _loaded()
    status = manager.get_status(EXT_ID)
    assert status["enabled"] is True
    assert status["status"] in ("enabled", "loaded")


def test_playwright_tool_registered_as_extension_capability():
    manager = _loaded()
    cap_tools = [
        rec.id
        for rec in manager.capability_registry.list_by_extension(EXT_ID)
        if rec.type == "tool"
    ]
    assert cap_tools, "Extension tidak mendaftarkan tool apa pun"
    assert any(t.startswith(PREFIX + "browser_") for t in cap_tools)


def test_playwright_tool_on_agent_registry_and_definitions():
    _loaded()
    registry = build_registry(root=str(_ROOT))
    assert any(n.startswith(PREFIX) for n in registry.list()), (
        "registry Agent tidak memuat tool Extension Playwright"
    )

    orchestrator = AgentOrchestrator(
        provider=MagicMock(),
        executor=ToolExecutor(registry=build_registry(root=str(_ROOT))),
    )
    names = {d.name for d in orchestrator._tool_definitions()}
    assert any(n.startswith(PREFIX + "browser_") for n in names), (
        "tidak ada tool browser_* pada Agent tool definitions"
    )


def test_playwright_tool_executes_through_agent_runtime():
    _loaded()
    orchestrator = AgentOrchestrator(
        provider=MagicMock(),
        executor=ToolExecutor(registry=build_registry(root=str(_ROOT))),
    )
    payload = orchestrator.executor.execute_tool_call(
        ToolCall.create(SAFE_TOOL, {"project": "regression-check"})
    )
    assert payload.is_success, payload.output
