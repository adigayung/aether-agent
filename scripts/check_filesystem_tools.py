"""Verifikasi filesystem tools (read-only) terhadap project saat ini.

Menguji: list_files, read_file, search_code, dan penolakan path traversal.

Jalankan:
    python scripts/check_filesystem_tools.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.tools import registry  # noqa: E402
from agent_ai.tools.base import ToolValidationError  # noqa: E402


def main() -> int:
    print("=== Verifikasi Filesystem Tools (read-only) ===")
    print(f"Tool terdaftar : {', '.join(registry.list())}")
    print()

    # 1) list_files terhadap directory project.
    listing = registry.execute("list_files", {"path": "src/agent_ai"})
    print(f"list_files('src/agent_ai') -> {listing['count']} entri")
    for entry in listing["entries"]:
        print(f"  - [{entry['type']}] {entry['name']}")
    print()

    # 2) read_file terhadap satu file kecil.
    read = registry.execute("read_file", {"path": "src/agent_ai/tools/__init__.py", "end_line": 5})
    print("read_file('src/agent_ai/tools/__init__.py', end_line=5):")
    print(read["content"])
    print()

    # 3) search_code terhadap symbol yang sudah ada.
    search = registry.execute("search_code", {"query": "class BaseTool", "path": "src"})
    print(f"search_code('class BaseTool') -> {search['count']} hasil")
    for match in search["matches"]:
        print(f"  - {match['file']}:{match['line']}  {match['text']}")
    print()

    # 4) Tolak path traversal.
    try:
        registry.execute("read_file", {"path": "..\\..\\..\\Windows\\win.ini"})
        print("[ERROR] path traversal seharusnya ditolak")
        return 1
    except ToolValidationError as exc:
        print(f"Path traversal ditolak OK -> {exc}")

    print()
    print("[OK] Filesystem tools (list_files, read_file, search_code) bekerja & aman.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
