"""Isolasi state Extension proses-wide antar-test.

Integrasi Extension -> Agent (Task: Playwright) memakai state proses-wide:

* registry tool Extension bersama
  (``agent_ai.tools.registry.get_extension_tool_registry``) yang dibaca oleh
  ``build_registry()`` agar tool Extension ENABLED muncul pada toolset Agent;
* manager Extension singleton (``agent_ai.extensions.agent_bridge``) yang
  memuat Extension sekali per proses.

Keduanya SENGAJA proses-wide pada produksi (satu proses Django: UI Extension dan
jalur eksekusi Agent berbagi state yang sama). Namun untuk TEST, state ini
di-reset sebelum/sesudah setiap test agar hasil test tidak bergantung urutan —
mis. ``build_registry()`` di satu test tidak "mewarisi" tool Extension yang
ter-load oleh test sebelumnya.
"""

from __future__ import annotations

import pytest


def _reset_extension_process_state() -> None:
    try:
        import agent_ai.extensions.agent_bridge as _bridge

        _bridge._manager = None
    except Exception:
        pass
    try:
        from agent_ai.tools.registry import get_extension_tool_registry

        get_extension_tool_registry()._tools.clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_extension_process_state():
    _reset_extension_process_state()
    try:
        yield
    finally:
        _reset_extension_process_state()
