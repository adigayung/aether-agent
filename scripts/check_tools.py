"""Verifikasi Tool System dengan dummy tool sederhana.

Tidak menyentuh filesystem dan tidak menjalankan command apa pun.

Jalankan:
    python scripts/check_tools.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.tools import (  # noqa: E402
    BaseTool,
    ToolExecutionError,
    ToolNotFoundError,
    ToolRegistry,
    ToolValidationError,
)


class AddTool(BaseTool):
    """Dummy tool: menjumlahkan dua angka."""

    name = "add"
    description = "Menjumlahkan dua angka."
    input_schema = {
        "type": "object",
        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
        "required": ["a", "b"],
    }

    def execute(self, **arguments):
        return arguments["a"] + arguments["b"]


class BoomTool(BaseTool):
    """Dummy tool: selalu gagal, untuk menguji error handling."""

    name = "boom"
    description = "Selalu gagal."
    input_schema = {"type": "object", "properties": {}}

    def execute(self, **arguments):
        raise RuntimeError("kegagalan internal")


def main() -> int:
    print("=== Verifikasi Tool System ===")
    registry = ToolRegistry()
    registry.register(AddTool())
    registry.register(BoomTool())

    print(f"Tool terdaftar : {', '.join(registry.list())}")
    print(f"Specs          : {registry.specs()}")
    print()

    # 1) Lookup & eksekusi normal.
    result = registry.execute("add", {"a": 2, "b": 3})
    print(f"execute('add', 2, 3) = {result}")
    assert result == 5, "hasil add salah"

    # 2) Tool tidak ditemukan.
    try:
        registry.execute("tidak_ada", {})
        print("[ERROR] seharusnya ToolNotFoundError")
        return 1
    except ToolNotFoundError as exc:
        print(f"ToolNotFoundError OK -> {exc}")

    # 3) Validasi argumen wajib.
    try:
        registry.execute("add", {"a": 1})
        print("[ERROR] seharusnya ToolValidationError")
        return 1
    except ToolValidationError as exc:
        print(f"ToolValidationError OK -> {exc}")

    # 4) Error eksekusi dibungkus, error asli tidak ditelan.
    try:
        registry.execute("boom", {})
        print("[ERROR] seharusnya ToolExecutionError")
        return 1
    except ToolExecutionError as exc:
        print(f"ToolExecutionError OK -> {exc}")
        print(f"  __cause__ = {type(exc.__cause__).__name__}: {exc.__cause__}")

    print()
    print("[OK] Tool System (registry, lookup, execute, error handling) bekerja.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
