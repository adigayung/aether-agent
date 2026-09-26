"""Tests: global conversation compression switch (`data/settings.json` -> compression.enabled).

Membuktikan bahwa `compression.enabled` benar-benar memengaruhi messages yang
diberikan ke provider (boundary `_compile_context_messages`, sebelum
`provider.generate()`):

- ON  -> jalur compaction existing (pesan dipadatkan saat budget ketat).
- OFF -> riwayat dikirim PENUH (tanpa compaction; identik to_provider_format).
- default (tanpa config) -> sama dengan ON (backward compatible).
"""
import json
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.config import settings as settings_mod
from agent_ai.config.settings import compression_enabled
from agent_ai.core.history import ConversationHistory
from agent_ai.core.orchestrator import AgentOrchestrator
from agent_ai.core.types import ChatMessage, ChatRole, ToolCall

COMPACT_MARKER = "[dipadatkan]"


class _StubProvider:
    """Dummy provider minimal (tidak dipakai untuk generate; hanya __init__)."""

    name = "stub"
    model = "stub-model"


def _tool_msg(tcid: str, content: str, name: str = "read_file") -> ChatMessage:
    return ChatMessage(role=ChatRole.TOOL.value, content=content, name=name, tool_call_id=tcid)


def _big_history() -> ConversationHistory:
    """History besar (hasil tool panjang) agar compaction nyata vs full jelas."""
    h = ConversationHistory()
    h.append_system_message("Kamu adalah coding agent.")
    h.append_user_message("Baca lalu edit file.")
    h.append_assistant_message(
        content="saya baca",
        tool_calls=[ToolCall(id="big1", function={"name": "read_file", "arguments": {"path": "a.txt"}}, type="function")],
    )
    h.append_message(_tool_msg("big1", "A" * 6000))  # hasil tool SANGAT besar
    h.append_assistant_message(
        content="saya baca lagi",
        tool_calls=[ToolCall(id="big2", function={"name": "read_file", "arguments": {"path": "b.txt"}}, type="function")],
    )
    h.append_message(_tool_msg("big2", "B" * 6000))
    h.append_assistant_message(content="Selesai.")
    return h


def _orch() -> AgentOrchestrator:
    return AgentOrchestrator(provider=_StubProvider())


def _full_dicts(h: ConversationHistory):
    return [m.to_provider_dict() for m in h.messages]


# --------------------------------------------------------------------------- #
# Loader (data/settings.json -> compression_enabled)
# --------------------------------------------------------------------------- #
def test_loader_on_true(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps({"compression": {"enabled": True}}), encoding="utf-8"
    )
    assert compression_enabled() is True


def test_loader_off_false(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps({"compression": {"enabled": False}}), encoding="utf-8"
    )
    assert compression_enabled() is False


def test_loader_default_no_file(tmp_path, monkeypatch):
    # File tidak ada -> default True.
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "tidak_ada.json")
    assert compression_enabled() is True


def test_loader_default_missing_section(tmp_path, monkeypatch):
    # compression / enabled tidak ada -> default True.
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(json.dumps({"lain": 1}), encoding="utf-8")
    assert compression_enabled() is True
    (tmp_path / "settings.json").write_text(json.dumps({"compression": {}}), encoding="utf-8")
    assert compression_enabled() is True


# --------------------------------------------------------------------------- #
# Integrasi: ON/OFF/default memengaruhi messages ke provider
# --------------------------------------------------------------------------- #
def _compile(orch, h, enable):
    # Patch switch + budget tetap kecil agar compaction mesti terjadi saat ON.
    orch._context_budget_decision = lambda: (900, "test")  # type: ignore[method-assign]
    compiled, _stats = orch._compile_context_messages(h, tools=[])
    return compiled, enable


def test_integration_on_compacts(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_ai.config.settings.compression_enabled", lambda: True)
    msgs, _ = _compile(_orch(), _big_history(), True)
    # Status ON: budget ketat -> hasil tool yang panjang dipadatkan (marker hadir,
    # isi jauh lebih pendek dari full).
    tool_contents = [m["content"] for m in msgs if m.get("role") == "tool"]
    assert tool_contents, "harus ada tool result"
    assert all(str(c).strip() for c in tool_contents)
    assert any(COMPACT_MARKER in str(c) for c in tool_contents), "ON harus memadatkan"
    assert sum(len(str(c)) for c in tool_contents) < 6000 * 2, "ON harus jauh lebih pendek dr full"


def test_integration_off_sends_full(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_ai.config.settings.compression_enabled", lambda: False)
    h = _big_history()
    msgs, _ = _compile(_orch(), h, False)
    # Status OFF: riwayat dikirim PENUH, IDENTIK dengan to_provider_format.
    assert msgs == _full_dicts(h), "OFF harus = riwayat penuh (tanpa compaction)"
    full_len = sum(len(str(m["content"] or "")) for m in _full_dicts(h) if m.get("role") == "tool")
    off_len = sum(len(str(m["content"] or "")) for m in msgs if m.get("role") == "tool")
    assert off_len == full_len
    assert not any(COMPACT_MARKER in str(m["content"]) for m in msgs if m.get("role") == "tool")


def test_integration_default_equals_on(tmp_path, monkeypatch):
    # Default (tanpa config) harus = ON. Patch SETTINGS_PATH ke file tidak ada ->
    # compression_enabled() (asli) mengembalikan True -> sama dengan ON.
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "no_such_settings.json")
    h = _big_history()
    orch = _orch()
    orch._context_budget_decision = lambda: (900, "test")  # type: ignore[method-assign]
    msgs_default, _ = orch._compile_context_messages(h, tools=[])
    # Setara dengan jalur ON (ada compaction / bukan full).
    full = _full_dicts(h)
    assert msgs_default != full, "default harus memadatkan (sama dengan ON)"
    assert any(COMPACT_MARKER in str(m["content"]) for m in msgs_default if m.get("role") == "tool")