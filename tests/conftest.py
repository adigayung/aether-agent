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

import os
from pathlib import Path

import pytest

# Root project (tests/conftest.py -> parents[1]); project ini memakai src-layout
# (`src/agent_ai`) dan gateway Django di `web/django_app` (paket `api`).
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def pytest_configure(config) -> None:  # noqa: ARG001
    """Pastikan import bekerja apa pun direktori pemanggil pytest.

    Tanpa ini, `python -m pytest tests/...` dari root project GAGAL import
    `agent_ai` (src-layout belum di sys.path) dan sebagian test (mis.
    test_config_separation.py) juga butuh `api` dari web/django_app.
    Perilaku ini ADITIF: path tetap sama seperti saat menjalankan pytest
    dengan cwd root project.
    """
    import sys

    for rel in ("src", "web/django_app"):
        path = str(PROJECT_ROOT / rel)
        if path not in sys.path:
            sys.path.insert(0, path)

    # Django setup SEBELUM test apa pun yang meng-import `api.*` (web/django_app)
    # berjalan; hanya sekali per sesi pytest.
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
    try:
        import django

        django.setup()
    except Exception:
        # Bila Django tidak tersedia / sudah di-setup, biarkan test yang
        # benar-benar butuh Django melaporkan kegagalannya sendiri.
        pass


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
