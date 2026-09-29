"""Regresi kecil: tool Extension Playwright tersedia untuk Agent runtime.

Menjaga DUA hal yang menjadi akar masalah integrasi Extension -> Agent:

  1. Registry Extension yang diisi lewat ``ExtensionContext.tools.register``
     harus benar-benar muncul pada registry Agent (``build_registry``) dan
     pada Agent tool definitions — bukan hanya di capability registry.
  2. Nama tool yang dikirim ke API provider (OpenAI-compatible / DeepSeek)
     harus provider-safe (``^[a-zA-Z0-9_-]+$``). Id Extension bertitik
     (mis. ``aether.playwright.browser_click``) ditolak API dengan
     HTTP 400 ``Invalid 'tools[i].function.name'`` sehingga Run Task gagal.

Test tidak menjalankan browser dan tidak memerlukan package ``playwright``
(import engine-nya lazy). State proses-wide Extension di-isolasi per-test oleh
``tests/conftest.py``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from agent_ai.config.settings import OpenAIConfig  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.core.types import ToolCall  # noqa: E402
from agent_ai.extensions.agent_bridge import (  # noqa: E402
    ensure_agent_extensions_loaded,
    get_agent_extension_manager,
)
from agent_ai.providers.base import GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.tools.registry import build_registry  # noqa: E402

EXT_ID = "aether.playwright"
PREFIX = EXT_ID + "."
SAFE_TOOL = EXT_ID + ".session_state_list"

#: Pola nama function yang diterima API OpenAI-compatible (mis. DeepSeek).
PROVIDER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _loaded():
    """Load Extension (idempotent) dan kembalikan manager, atau skip bila absen."""
    assert ensure_agent_extensions_loaded() is True
    manager = get_agent_extension_manager()
    if not manager.registry.exists(EXT_ID):
        pytest.skip("Extension 'aether.playwright' tidak ter-discover di env ini")
    return manager


def _orchestrator() -> AgentOrchestrator:
    return AgentOrchestrator(
        provider=MagicMock(),
        executor=ToolExecutor(registry=build_registry(root=str(_ROOT))),
    )


def test_browser_tools_reach_agent_registry():
    """Tool Extension ENABLED benar-benar ada di registry Agent."""
    _loaded()
    registry = build_registry(root=str(_ROOT))
    browser = [n for n in registry.list() if n.startswith(PREFIX + "browser_")]
    assert browser, "registry Agent tidak memuat tool Extension Playwright"


def test_browser_tools_in_agent_tool_definitions():
    """Minimal satu tool ``browser_*`` muncul pada Agent tool definitions."""
    _loaded()
    names = {d.name for d in _orchestrator()._tool_definitions()}
    assert any(n.startswith(PREFIX + "browser_") for n in names), (
        "tidak ada tool browser_* pada Agent tool definitions"
    )


def test_extension_tool_executes_through_agent_runtime():
    """Minimal satu tool Extension dapat dieksekusi lewat Agent runtime."""
    _loaded()
    payload = _orchestrator().executor.execute_tool_call(
        ToolCall.create(SAFE_TOOL, {"project": "regression-check"})
    )
    assert payload.is_success, payload.output


def test_provider_payload_uses_provider_safe_tool_names():
    """Nama tool pada payload provider WAJIB cocok pola API (regresi DeepSeek)."""
    _loaded()
    tools = _orchestrator()._tool_definitions()
    assert tools, "Agent tool definitions kosong"

    provider = OpenAICompatibleProvider(
        config=OpenAIConfig(
            api_key="test-key",
            base_url="http://localhost.invalid/v1",
            model="deepseek-flash",
        )
    )
    payload = provider._build_payload(
        prompt=None,
        messages=[{"role": "user", "content": "hi"}],
        options=None,
        tools=tools,
    )

    names = [t["function"]["name"] for t in payload["tools"]]
    assert names, "payload provider tidak memuat definisi tool"
    assert any("browser" in n for n in names), (
        "tool browser_* Extension tidak ikut terkirim ke provider"
    )
    invalid = [n for n in names if not PROVIDER_NAME_RE.match(n)]
    assert not invalid, f"nama tool tidak provider-safe: {invalid[:5]}"


def test_provider_payload_message_tool_names_are_provider_safe():
    """Riwayat multi-turn (assistant tool_calls + tool result) juga provider-safe.

    Tanpa ini, request KEDUA (yang membawa riwayat) masih memuat nama bertitik
    dan ditolak API walaupun definisi tool sudah provider-safe.
    """
    provider = OpenAICompatibleProvider(
        config=OpenAIConfig(api_key="k", base_url="http://localhost.invalid/v1", model="m")
    )
    dotted = "aether.playwright.browser_snapshot"
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": dotted, "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "1", "name": dotted, "content": "{}"},
    ]
    payload = provider._build_payload(prompt=None, messages=messages, options=None, tools=None)

    names = []
    for message in payload["messages"]:
        for call in message.get("tool_calls") or []:
            names.append(call["function"]["name"])
        if message.get("role") == "tool" and "name" in message:
            names.append(message["name"])
    assert names, "payload tidak memuat nama tool dari riwayat"
    assert all(PROVIDER_NAME_RE.match(n) for n in names), names
    # Riwayat asli AETHER TIDAK dimutasi.
    assert messages[1]["tool_calls"][0]["function"]["name"] == dotted


def test_normalize_response_decodes_provider_safe_name():
    """Nama provider-safe pada response di-decode balik -> lookup registry benar."""
    from agent_ai.providers.base import to_provider_safe_tool_name

    dotted = "aether.playwright.browser_snapshot"
    safe = to_provider_safe_tool_name(dotted)
    assert safe != dotted

    provider = OpenAICompatibleProvider(
        config=OpenAIConfig(api_key="k", base_url="http://localhost.invalid/v1", model="m")
    )
    response = provider.normalize_response(
        GenerateResult(
            text="",
            model="m",
            provider="deepseek",
            raw={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "1",
                                    "type": "function",
                                    "function": {"name": safe, "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )
    )
    assert response.actions and response.actions[0].name == dotted


def test_provider_safe_name_roundtrips_to_registry_name():
    """Nama tool yang dikembalikan provider di-decode kembali ke identitas asli."""
    _loaded()
    from agent_ai.providers.base import (
        from_provider_safe_tool_name,
        to_provider_safe_tool_name,
    )

    registry = build_registry(root=str(_ROOT))
    for name in registry.list():
        assert from_provider_safe_tool_name(to_provider_safe_tool_name(name)) == name
