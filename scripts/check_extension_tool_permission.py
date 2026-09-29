"""Verifikasi: tool Extension ENABLED otomatis ALLOW (tanpa approval popup).

Membuktikan tiga hal (deterministik, tanpa model/API cloud, tanpa approval UI):

  1. Tool Extension yang ENABLED -> ALLOW dan dapat dieksekusi. Contoh KONKRET:
     `aether.playwright.browser_launch` (dicek keputusannya) + satu tool Playwright
     aman (`session_state_list`) yang benar-benar dieksekusi.
  2. Tool Extension yang DISABLED / tidak terdaftar -> TIDAK di-auto-allow
     (tetap mengikuti policy normal: UNKNOWN -> require_approval -> ditolak).
  3. Tool bawaan AETHER tetap mengikuti permission policy (deny tetap deny).

Identifikasi tool Extension memakai MEKANISME REGISTRASI Extension yang SUDAH ADA
(keanggotaan registry tool Extension bersama), BUKAN daftar nama Extension —
sehingga berlaku GENERIC untuk semua Extension.

Jalankan:
    python scripts/check_extension_tool_permission.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.types import ToolCall  # noqa: E402
from agent_ai.permission import (  # noqa: E402
    PermissionConfig,
    PermissionManager,
    PermissionPolicy,
    PolicyMode,
)
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.registry import (  # noqa: E402
    build_registry,
    get_extension_tool_registry,
    is_extension_tool,
)

EXT_ID = "aether.playwright"
PREFIX = EXT_ID + "."
PW_LAUNCH = "aether.playwright.browser_launch"
PW_SAFE = "aether.playwright.session_state_list"
GENERIC_TOOL = "demo.sample.echo"


class _DummyExtensionTool(BaseTool):
    name = "base"
    description = "Dummy tool Extension untuk verifikasi permission."
    input_schema = {"type": "object", "properties": {"value": {"type": "string"}}}

    def __init__(self, name: str) -> None:
        self.name = name
        self.executed = False

    def execute(self, **arguments):
        self.executed = True
        return {"echo": arguments.get("value"), "tool": self.name}


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _default_manager() -> PermissionManager:
    """PermissionManager default: UNKNOWN -> require_approval (menolak yang tak dikenal)."""
    return PermissionManager(policy=PermissionPolicy(PermissionConfig()))


def _load_extension_manager():
    from agent_ai.extensions.agent_bridge import (
        ensure_agent_extensions_loaded,
        get_agent_extension_manager,
    )

    ensure_agent_extensions_loaded()
    return get_agent_extension_manager()


def main() -> int:
    print("=== Verifikasi Permission: tool Extension ENABLED otomatis ALLOW ===")

    # --- [0] GENERIK: tool Extension (bukan Playwright) yang terdaftar --------
    dummy = _DummyExtensionTool(GENERIC_TOOL)
    get_extension_tool_registry().register(dummy)  # simulasi Extension ENABLED
    _expect(is_extension_tool(GENERIC_TOOL), "tool Extension terdaftar tidak dikenali")
    d = _default_manager().check(GENERIC_TOOL, {"value": "hi"})
    _expect(d.allowed and d.metadata.get("source") == "extension", "tool Extension generik harus ALLOW")
    print(f"[0] GENERIK '{GENERIC_TOOL}' -> allowed={d.allowed} source={d.metadata.get('source')}")

    # --- [1] Tool Extension ENABLED -> ALLOW & executable --------------------
    manager = _load_extension_manager()
    if not manager.registry.exists(EXT_ID):
        print(f"[SKIP] Extension '{EXT_ID}' tidak ter-discover di env ini")
    else:
        _expect(is_extension_tool(PW_LAUNCH), f"{PW_LAUNCH} bukan tool Extension yang dikenali")
        dec = _default_manager().check(PW_LAUNCH, {})
        _expect(
            dec.allowed and dec.metadata.get("source") == "extension",
            f"{PW_LAUNCH} harus ALLOW tanpa approval",
        )
        print(
            f"[1a] contoh '{PW_LAUNCH}' -> allowed={dec.allowed} "
            f"mode={dec.mode.value} source={dec.metadata.get('source')}"
        )

        registry = build_registry(root=str(PROJECT_ROOT))
        executor = ToolExecutor(registry=registry, permission_manager=_default_manager())
        payload = executor.execute_tool_call(ToolCall.create(PW_SAFE, {"project": "perm-check"}))
        _expect(payload.is_success, f"tool Extension gagal dieksekusi: {payload.output}")
        print(f"[1b] execute '{PW_SAFE}' -> status={payload.status.value}")

    # --- [2] Tool Extension DISABLED / tidak terdaftar -> tidak di-auto-allow -
    not_registered = "aether.unknown_extension.some_tool"
    _expect(
        is_extension_tool(not_registered) is False,
        "nama tak terdaftar tidak boleh dianggap tool Extension",
    )
    dec_u = _default_manager().check(not_registered, {})
    _expect(
        dec_u.allowed is False and dec_u.metadata.get("source") != "extension",
        "tool tak terdaftar harus mengikuti policy normal (ditolak)",
    )
    print(
        f"[2a] tidak terdaftar '{not_registered}' -> allowed={dec_u.allowed} "
        f"mode={dec_u.mode.value} requires_approval={dec_u.requires_approval}"
    )

    if manager.registry.exists(EXT_ID):
        cap_tools = [
            rec.id
            for rec in manager.capability_registry.list_by_extension(EXT_ID)
            if rec.type == "tool" and rec.id.startswith(PREFIX + "browser_")
        ]
        tool_name = sorted(cap_tools)[0]
        try:
            manager.disable(EXT_ID)
            _expect(
                is_extension_tool(tool_name) is False,
                "setelah DISABLE, tool Extension tidak boleh dikenali",
            )
            dec_d = _default_manager().check(tool_name, {})
            _expect(
                dec_d.allowed is False and dec_d.metadata.get("source") != "extension",
                "setelah DISABLE, tool harus mengikuti policy normal (ditolak)",
            )
            print(f"[2b] setelah DISABLE '{tool_name}' -> allowed={dec_d.allowed}")
        finally:
            manager.enable(EXT_ID)
        _expect(is_extension_tool(tool_name), "setelah ENABLE, tool Extension harus dikenali lagi")
        print("[2c] setelah ENABLE kembali -> tool dikenali (auto ALLOW pulih)")

    # --- [3] Tool bawaan AETHER tetap mengikuti permission policy -------------
    for name in ("read_file", "write_file", "run_command", "search_code"):
        _expect(is_extension_tool(name) is False, f"tool bawaan '{name}' tidak boleh dianggap Extension")
    deny = PermissionManager(
        policy=PermissionPolicy(PermissionConfig(workspace_write=PolicyMode.DENY))
    )
    d_deny = deny.check("write_file", {"path": "a.txt", "content": "x"})
    _expect(d_deny.allowed is False, "policy DENY harus tetap berlaku untuk tool bawaan")
    allow = PermissionManager(
        policy=PermissionPolicy(PermissionConfig(workspace_write=PolicyMode.ALLOW))
    )
    _expect(
        allow.check("write_file", {"path": "a.txt", "content": "x"}).allowed is True,
        "policy ALLOW harus tetap berlaku untuk tool bawaan",
    )
    print("[3] tool bawaan tetap mengikuti policy (deny/allowed) OK")

    print()
    print(
        "[OK] Tool Extension ENABLED otomatis ALLOW (generic); tool Extension "
        "disabled/tak terdaftar & tool bawaan tetap mengikuti permission policy."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
