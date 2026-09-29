"""Regresi: tool Extension yang ENABLED otomatis ALLOW (tanpa approval popup).

Root masalah: `PermissionPolicy` mengklasifikasi nama tool. Tool Extension
(mis. `aether.playwright.browser_launch`) tidak dikenal classifier -> jatuh ke
`ActionClass.UNKNOWN` -> `REQUIRE_APPROVAL` -> PermissionManager MENOLAK-nya
sebelum sempat dieksekusi.

Perbaikan: `PermissionPolicy` mengizinkan (ALLOW) tool yang berasal dari
Extension yang ENABLED. Identifikasi memakai MEKANISME REGISTRASI Extension yang
SUDAH ADA (keanggotaan registry tool Extension bersama yang diisi saat extension
ENABLED dan dicabut saat DISABLE) — BUKAN daftar nama Extension (mis.
"playwright"). Tool bawaan AETHER tidak ada di registry itu, sehingga tetap
mengikuti permission policy normal.

Test ini mengunci tiga jaminan:
  1. Tool Extension enabled -> ALLOW dan dapat dieksekusi.
  2. Tool Extension disabled / tidak terdaftar -> TIDAK di-auto-allow (perilaku
     tidak berubah: tetap mengikuti policy normal; UNKNOWN -> require_approval).
  3. Tool bawaan AETHER tetap mengikuti permission policy yang berlaku.

`aether.playwright.browser_launch` dipakai sebagai contoh KONKRET untuk
skenario 1, tetapi implementasinya generik (lihat juga uji tool demo generik).

State proses-wide Extension di-isolasi per-test oleh `tests/conftest.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.types import ToolCall  # noqa: E402
from agent_ai.permission.manager import PermissionManager  # noqa: E402
from agent_ai.permission.models import (  # noqa: E402
    PermissionConfig,
    PermissionRequest,
    PolicyMode,
)
from agent_ai.permission.policy import PermissionPolicy  # noqa: E402
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.registry import (  # noqa: E402
    build_registry,
    get_extension_tool_registry,
    is_extension_tool,
)

EXT_ID = "aether.playwright"
PREFIX = EXT_ID + "."

#: Contoh KONKRET dari task: tool Extension nyata (nama bertitik).
PW_LAUNCH = "aether.playwright.browser_launch"
#: Tool Playwright yang AMAN dieksekusi tanpa menjalankan browser (hanya membaca
#: daftar storage-state) — dipakai untuk membuktikan tool Extension benar-benar
#: dapat dieksekusi lewat runtime tanpa approval.
PW_SAFE = "aether.playwright.session_state_list"

#: Nama tool Extension generik (bukan Playwright) untuk membuktikan mekanisme
#: berlaku untuk SELURUH Extension, bukan logic khusus satu Extension.
GENERIC_TOOL = "demo.sample.echo"


class _RecordingTool(BaseTool):
    """Tool Extension dummy yang mencatat apakah ia benar-benar dieksekusi."""

    name = "base"
    description = "Dummy tool Extension untuk verifikasi permission."
    input_schema = {"type": "object", "properties": {"value": {"type": "string"}}}

    def __init__(self, name: str) -> None:
        self.name = name
        self.executed = False

    def execute(self, **arguments):  # noqa: D102 - kontrak BaseTool
        self.executed = True
        return {"echo": arguments.get("value"), "tool": self.name}


def _default_policy_manager() -> PermissionManager:
    """PermissionManager dengan policy default (efektif = settings default).

    Default penting: `UNKNOWN -> REQUIRE_APPROVAL`, sehingga tool yang TIDAK
    dikenali (termasuk tool Extension sebelum perbaikan ini) akan ditolak.
    """
    return PermissionManager(policy=PermissionPolicy(PermissionConfig()))


def _loaded_manager():
    """Load Extension (idempotent) -> manager, atau skip bila tidak ada di env."""
    from agent_ai.extensions.agent_bridge import (
        ensure_agent_extensions_loaded,
        get_agent_extension_manager,
    )

    assert ensure_agent_extensions_loaded() is True
    manager = get_agent_extension_manager()
    if not manager.registry.exists(EXT_ID):
        pytest.skip(f"Extension '{EXT_ID}' tidak ter-discover di env ini")
    return manager


# --------------------------------------------------------------------------- #
# 1) Tool Extension ENABLED -> ALLOW & dapat dieksekusi (generik)
# --------------------------------------------------------------------------- #
def test_enabled_extension_tool_is_allowed_generic():
    """Tool Extension (generik, bukan Playwright) yang terdaftar -> ALLOW."""
    tool = _RecordingTool(GENERIC_TOOL)
    # Mensimulasikan Extension ENABLED yang mendaftarkan tool-nya lewat
    # mekanisme registrasi existing (registry tool Extension bersama).
    get_extension_tool_registry().register(tool)

    assert is_extension_tool(GENERIC_TOOL) is True

    manager = _default_policy_manager()
    decision = manager.check(GENERIC_TOOL, {"value": "hi"})
    assert decision.allowed is True, decision.reason
    assert decision.mode == PolicyMode.ALLOW
    assert decision.metadata.get("source") == "extension"


def test_enabled_extension_tool_is_executable_through_executor():
    """Tool Extension enabled benar-benar dieksekusi (bukan ditolak permission)."""
    tool = _RecordingTool(GENERIC_TOOL)
    get_extension_tool_registry().register(tool)

    registry = build_registry(root=str(_ROOT))
    executor = ToolExecutor(registry=registry, permission_manager=_default_policy_manager())
    payload = executor.execute_tool_call(ToolCall.create(GENERIC_TOOL, {"value": "ok"}))

    assert payload.is_success, payload.output
    assert tool.executed is True


def test_playwright_browser_launch_example_is_allowed():
    """Contoh KONKRET `aether.playwright.browser_launch` -> ALLOW (tanpa popup)."""
    _loaded_manager()
    assert is_extension_tool(PW_LAUNCH) is True

    decision = _default_policy_manager().check(PW_LAUNCH, {})
    assert decision.allowed is True, decision.reason
    assert decision.metadata.get("source") == "extension"


def test_playwright_extension_tool_is_executable():
    """Tool Playwright aman dapat dieksekusi lewat executor tanpa approval."""
    _loaded_manager()
    registry = build_registry(root=str(_ROOT))
    executor = ToolExecutor(registry=registry, permission_manager=_default_policy_manager())
    payload = executor.execute_tool_call(ToolCall.create(PW_SAFE, {"project": "perm-check"}))
    assert payload.is_success, payload.output


# --------------------------------------------------------------------------- #
# 2) Tool Extension DISABLED / tidak terdaftar -> TIDAK di-auto-allow
# --------------------------------------------------------------------------- #
def test_unregistered_tool_is_not_extension_allowed():
    """Nama tool yang TIDAK terdaftar bukan "extension tool" -> policy normal."""
    assert is_extension_tool(PW_LAUNCH) is False

    decision = _default_policy_manager().check(PW_LAUNCH, {})
    assert decision.metadata.get("source") != "extension"
    assert decision.allowed is False, decision.reason


def test_disabled_extension_tool_is_not_extension_allowed():
    """Setelah extension DISABLE, tool-nya tidak lagi di-auto-allow."""
    manager = _loaded_manager()

    cap_tools = [
        rec.id
        for rec in manager.capability_registry.list_by_extension(EXT_ID)
        if rec.type == "tool" and rec.id.startswith(PREFIX + "browser_")
    ]
    if not cap_tools:
        pytest.skip(f"Extension '{EXT_ID}' tidak mendaftarkan tool browser_*")
    tool_name = sorted(cap_tools)[0]

    # Sebelum disable: diakui sebagai tool Extension (auto ALLOW).
    assert is_extension_tool(tool_name) is True
    assert _default_policy_manager().check(tool_name, {}).allowed is True

    try:
        manager.disable(EXT_ID)
        # Setelah disable: tidak lagi dikenali -> policy normal -> ditolak.
        assert is_extension_tool(tool_name) is False
        decision = _default_policy_manager().check(tool_name, {})
        assert decision.metadata.get("source") != "extension"
        assert decision.allowed is False, decision.reason
    finally:
        manager.enable(EXT_ID)

    # Enable kembali memulihkan auto ALLOW.
    assert is_extension_tool(tool_name) is True


def test_disabled_extension_tool_not_executed_as_extension():
    """Executor tidak menjalankan tool Extension yang sudah di-disable."""
    manager = _loaded_manager()
    cap_tools = [
        rec.id
        for rec in manager.capability_registry.list_by_extension(EXT_ID)
        if rec.type == "tool" and rec.id.startswith(PREFIX + "browser_")
    ]
    if not cap_tools:
        pytest.skip(f"Extension '{EXT_ID}' tidak mendaftarkan tool browser_*")
    tool_name = sorted(cap_tools)[0]

    try:
        manager.disable(EXT_ID)
        registry = build_registry(root=str(_ROOT))
        executor = ToolExecutor(
            registry=registry, permission_manager=_default_policy_manager()
        )
        payload = executor.execute_tool_call(ToolCall.create(tool_name, {}))
        assert payload.is_success is False, "tool Extension disabled tidak boleh sukses"
    finally:
        manager.enable(EXT_ID)


# --------------------------------------------------------------------------- #
# 3) Tool bawaan AETHER tetap mengikuti permission policy normal
# --------------------------------------------------------------------------- #
def test_builtin_tool_is_never_treated_as_extension():
    """Tool bawaan tidak pernah dianggap tool Extension (tidak ada auto ALLOW)."""
    for name in ("read_file", "write_file", "run_command", "search_code"):
        assert is_extension_tool(name) is False
        decision = _default_policy_manager().check(name, {})
        assert decision.metadata.get("source") != "extension"


def test_builtin_tool_follows_permission_deny_and_allow():
    """Policy tetap berlaku untuk tool bawaan: DENY menolak, ALLOW mengizinkan."""
    deny = PermissionManager(
        policy=PermissionPolicy(PermissionConfig(workspace_write=PolicyMode.DENY))
    )
    d_deny = deny.check("write_file", {"path": "a.txt", "content": "x"})
    assert d_deny.allowed is False
    assert d_deny.mode == PolicyMode.DENY
    assert d_deny.metadata.get("source") != "extension"

    allow = PermissionManager(
        policy=PermissionPolicy(PermissionConfig(workspace_write=PolicyMode.ALLOW))
    )
    d_allow = allow.check("write_file", {"path": "a.txt", "content": "x"})
    assert d_allow.allowed is True
    assert d_allow.mode == PolicyMode.ALLOW
    assert d_allow.metadata.get("source") != "extension"


def test_builtin_tool_denied_is_not_executed():
    """Executor tetap TIDAK menjalankan tool bawaan yang ditolak policy."""
    registry = build_registry(root=str(_ROOT))
    deny = PermissionManager(
        policy=PermissionPolicy(PermissionConfig(read_only=PolicyMode.DENY))
    )
    executor = ToolExecutor(registry=registry, permission_manager=deny)
    payload = executor.execute_tool_call(ToolCall.create("list_files", {}))
    assert payload.is_success is False


def test_policy_accepts_custom_extension_resolver():
    """Policy dapat di-inject resolver (seam tanpa membuat sistem permission baru)."""
    policy = PermissionPolicy(
        PermissionConfig(),
        extension_tool_resolver=lambda action: action == "custom.tool.enabled",
    )
    assert policy.is_extension_tool("custom.tool.enabled") is True
    assert policy.is_extension_tool("write_file") is False
    d = policy.evaluate(PermissionRequest(action="custom.tool.enabled"))
    assert d.allowed is True
    assert d.metadata.get("source") == "extension"
