"""Bridge: tool Extension ENABLED -> toolset Agent (integration seam).

Latar belakang (akar masalah): AETHER Extension System sudah punya mekanisme
registration tool yang lengkap (``ExtensionContext.tools`` -> ``ToolsFacade`` ->
``ToolRegistry``). Namun registry yang dipakai Agent dibangun TERPISAH oleh
``agent_ai.tools.registry.build_registry`` (registry baru per task), sehingga
tool Extension — mis. ``aether.playwright.browser_*`` — tidak pernah muncul
pada toolset Agent walau Extension-nya ter-load & ENABLED.

Modul ini menutup celah itu TANPA subsystem baru dan TANPA mengubah Extension:

* memakai ``ExtensionLoader`` / ``ExtensionManager`` yang sudah ada;
* mengarahkan keduanya ke SATU registry bersama
  (``agent_ai.tools.registry.get_extension_tool_registry``) yang dibaca oleh
  ``build_registry()``;
* mengekspos SINGLETON manager proses-wide agar layer gateway (UI Extension) dan
  jalur eksekusi Agent berbagi state yang sama — tidak ada Extension yang ter-load
  dua kali (sehingga tidak ada dua service/browser dari Extension yang sama).

Extension tetap package mandiri: ia hanya memakai ``context.tools.register`` /
``context.config.register`` yang sudah ada. Tidak ada import balik dari modul
core ke sini; arah ketergantungan selalu core -> (dipakai) bridge, dan bridge
memakai registry bersama yang didefinisikan di core tanpa menarik Extension.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

#: Manager proses-wide (lazy). Dibuat sekali, dibagi oleh gateway + Agent.
_manager: Optional[Any] = None
_lock = threading.Lock()


def get_agent_extension_manager():
    """ExtensionManager proses-wide yang terikat registry tool bersama Agent.

    Memuat seluruh Extension yang ter-discover (filesystem ``<AETHER_ROOT>/Extension``)
    lewat ``ExtensionLoader`` yang sudah ada, dengan ``tool_registry`` diarahkan ke
    registry bersama. Extension yang ENABLED akan mendaftarkan tool-nya ke registry
    bersama itu pada fase aktivasi (``on_enable``).

    Returns:
        ExtensionManager (instance tunggal per proses).
    """
    global _manager
    if _manager is not None:
        return _manager
    with _lock:
        if _manager is not None:
            return _manager

        from agent_ai.extensions.capabilities import CapabilityRegistry
        from agent_ai.extensions.lifecycle import get_lifecycle_store
        from agent_ai.extensions.loader import ExtensionLoader
        from agent_ai.extensions.manager import ExtensionManager
        from agent_ai.extensions.registry import ExtensionRegistry
        from agent_ai.tools.registry import get_extension_tool_registry

        # SATU registry tool bersama: ditulis oleh Extension (enable) dan dibaca
        # oleh build_registry() (Agent).
        tool_registry = get_extension_tool_registry()
        capability_registry = CapabilityRegistry()
        extension_registry = ExtensionRegistry()
        lifecycle_store = get_lifecycle_store()

        config_store = None
        try:
            from agent_ai.extensions.config import get_config_store

            config_store = get_config_store()
        except Exception:
            config_store = None

        loader = ExtensionLoader(
            registry=extension_registry,
            capability_registry=capability_registry,
            lifecycle_store=lifecycle_store,
            tool_registry=tool_registry,
            config_store=config_store,
            enable_entry_points=False,
        )
        try:
            loader.load_all()
        except Exception:
            # Satu Extension bermasalah tidak boleh menggagalkan yang lain;
            # loader sudah mengisolasi kegagalan per-Extension.
            pass

        _manager = ExtensionManager(
            registry=extension_registry,
            capability_registry=capability_registry,
            lifecycle_store=lifecycle_store,
            tool_registry=tool_registry,
            config_store=config_store,
        )
        return _manager


def ensure_agent_extensions_loaded() -> bool:
    """Pastikan Extension ter-load ke registry bersama (idempotent, best-effort).

    Dipanggil sebelum Agent membangun tool registry-nya (lihat
    ``TaskExecutor._build_runtime``) agar tool Extension ENABLED benar-benar
    tersedia di toolset Agent. Tidak pernah melempar exception.

    Returns:
        True bila manager registry bersama siap (walau tanpa Extension),
        False bila gagal total (Agent tetap berjalan tanpa tool Extension).
    """
    try:
        get_agent_extension_manager()
        return True
    except Exception:
        return False


__all__ = ["get_agent_extension_manager", "ensure_agent_extensions_loaded"]
