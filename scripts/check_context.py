"""Verifikasi Context Manager.

Menguji: text context, file context, image reference, add/remove/list/get,
dan representasi context untuk provider.

Jalankan:
    python scripts/check_context.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.context import ContextManager, ContextType  # noqa: E402


def main() -> int:
    print("=== Verifikasi Context Manager ===")
    ctx = ContextManager()

    # 1) Text context.
    text_item = ctx.add_text("Tolong jelaskan fungsi ini.", source="user")
    print(f"add_text    -> {text_item}")

    # 2) File context/reference.
    file_item = ctx.add_file("src/agent_ai/tools/base.py", content="class BaseTool: ...")
    print(f"add_file    -> {file_item}")

    # 3) Image reference.
    image_item = ctx.add_image("screenshots/error.png", mime_type="image/png")
    print(f"add_image   -> {image_item}")
    print()

    # 4) List & filter.
    print(f"Total items : {len(ctx)}")
    print(f"list()      : {[i.type.value for i in ctx.list()]}")
    print(f"list(IMAGE) : {[i.source for i in ctx.list(ContextType.IMAGE)]}")
    print()

    # 5) Get.
    fetched = ctx.get(file_item.id)
    print(f"get(file)   -> {fetched}")
    assert fetched is file_item, "get() mengembalikan item yang salah"

    # 6) Remove.
    removed = ctx.remove(text_item.id)
    print(f"remove(text)-> {removed}, sisa {len(ctx)} item")
    assert removed is True, "remove() gagal"
    assert ctx.get(text_item.id) is None, "item masih ada setelah remove"

    # 7) Provider context (netral).
    payload = ctx.to_provider_context()
    print(f"to_provider_context() -> {len(payload)} item")
    for entry in payload:
        print(f"  - {entry['type']}: {entry['source']}")
    print()

    print("[OK] Context Manager (text/file/image, add/remove/list/get) bekerja.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
