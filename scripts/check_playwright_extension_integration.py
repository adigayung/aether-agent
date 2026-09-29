"""Validasi minimal integrasi Extension Playwright -> toolset Agent.

Membuktikan 3 hal (tanpa memasang package `playwright` ke global/venv):

  1. Extension `aether.playwright` TER-LOAD & ENABLED.
  2. Minimal satu tool `browser_*` MUNCUL pada Agent tool definitions.
  3. Tool tersebut DAPAT DIEKSEKUSI lewat Agent runtime (ToolExecutor).

Selain itu diverifikasi bahwa lifecycle ENABLE/DISABLE Extension benar-benar
mengubah isi toolset Agent (disable -> tool hilang; enable -> tool kembali).

Tidak menjalankan browser dan tidak menyentuh project lain.

Jalankan:
    python scripts/check_playwright_extension_integration.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from unittest.mock import MagicMock  # noqa: E402

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
#: Tool Playwright yang aman dieksekusi tanpa menjalankan browser (murni
#: membaca daftar storage-state yang tersimpan). Nama `browser_*` tidak wajib;
#: yang dibuktikan adalah tool Extension benar-benar bisa dipanggil Agent.
SAFE_TOOL = EXT_ID + ".session_state_list"


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    print("=== Validasi Integrasi Extension Playwright -> Agent ===")

    # Blokir import engine `playwright` untuk MEMBUKTIKAN extension tetap
    # ter-deteksi & tool-nya tetap tersedia TANPA memasang package Playwright.
    import importlib.abc

    class _BlockPlaywrightEngine(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):  # noqa: D401
            if fullname == "playwright" or fullname.startswith("playwright."):
                raise ImportError(
                    "engine 'playwright' sengaja diblokir untuk validasi integrasi"
                )
            return None

    sys.meta_path.insert(0, _BlockPlaywrightEngine())
    print("    engine 'playwright' diblokir (simulasi: package TIDAK terpasang)")
    _expect(
        "playwright" not in sys.modules,
        "engine playwright ter-import lebih awal; validasi env tidak bersih",
    )

    # --- [1] Extension ter-load & enabled ---------------------------------
    _expect(
        ensure_agent_extensions_loaded(),
        "registry Extension bersama gagal disiapkan",
    )
    manager = get_agent_extension_manager()
    status = manager.get_status(EXT_ID)
    print(f"[1] {EXT_ID}: status={status['status']} enabled={status['enabled']}")
    _expect(status["enabled"] is True, f"{EXT_ID} harus ENABLED")

    # Tool terdaftar pada capability registry Extension (mekanisme registration
    # Extension yang sudah ada: ExtensionContext.tools -> ToolsFacade).
    cap_tools = [
        rec.id
        for rec in manager.capability_registry.list_by_extension(EXT_ID)
        if rec.type == "tool"
    ]
    _expect(len(cap_tools) > 0, "Extension tidak mendaftarkan tool apa pun")
    print(f"    capability tool terdaftar={len(cap_tools)} (mis. {cap_tools[0]})")

    # Tidak butuh package `playwright` untuk deteksi: registry tool Extension
    # sudah terisi meski engine Playwright belum diimpor (import lazy).
    try:
        import importlib.util

        has_pkg = importlib.util.find_spec("playwright") is not None
    except Exception:
        has_pkg = False
    print(f"    package 'playwright' terpasang di env: {has_pkg} (tidak wajib)")

    def _browser_tools_on_agent() -> list[str]:
        registry = build_registry(root=str(PROJECT_ROOT))
        return sorted(n for n in registry.list() if n.startswith(PREFIX + "browser_"))

    # --- [2] Tool browser_* muncul pada Agent tool definitions ------------
    registry = build_registry(root=str(PROJECT_ROOT))
    agent_pw = sorted(n for n in registry.list() if n.startswith(PREFIX))
    print(f"[2] build_registry memuat {len(agent_pw)} tool Extension Playwright")

    orchestrator = AgentOrchestrator(
        provider=MagicMock(),
        executor=ToolExecutor(registry=build_registry(root=str(PROJECT_ROOT))),
    )
    definition_names = {d.name for d in orchestrator._tool_definitions()}
    browser_names = sorted(n for n in definition_names if n.startswith(PREFIX + "browser_"))
    _expect(
        len(browser_names) >= 1,
        "tidak ada tool 'browser_*' pada Agent tool definitions",
    )
    print(
        f"    browser_* pada Agent tool definitions={len(browser_names)} "
        f"(mis. {browser_names[0]})"
    )

    # --- [3] Tool dapat dieksekusi lewat Agent runtime --------------------
    executor = orchestrator.executor
    call = ToolCall.create(SAFE_TOOL, {"project": "integration-check"})
    payload = executor.execute_tool_call(call)
    print(f"[3] execute '{SAFE_TOOL}' -> status={payload.status.value} output={payload.output}")
    _expect(payload.is_success, f"tool Extension gagal dieksekusi: {payload.output}")

    # --- [bonus] lifecycle ENABLE/DISABLE ikut menggerakkan toolset Agent --
    try:
        manager.disable(EXT_ID)
        after_disable = _browser_tools_on_agent()
        print(f"[lifecycle] setelah DISABLE -> browser_* pada Agent={len(after_disable)}")
        _expect(
            len(after_disable) == 0,
            "DISABLE Extension harus menghapus tool-nya dari toolset Agent",
        )
    finally:
        manager.enable(EXT_ID)
    after_enable = _browser_tools_on_agent()
    print(f"[lifecycle] setelah ENABLE  -> browser_* pada Agent={len(after_enable)}")
    _expect(
        len(after_enable) >= 1,
        "ENABLE Extension harus mengembalikan tool-nya ke toolset Agent",
    )

    print()
    print(
        "[OK] Extension Playwright ter-load & enabled, tool browser_* muncul pada "
        "toolset Agent, dan dapat dieksekusi lewat Agent runtime."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
