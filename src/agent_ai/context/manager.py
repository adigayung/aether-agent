"""Context Manager.

Mengelola kumpulan ContextItem yang diberikan/diminta Agent. Manager ini
TIDAK membaca repository otomatis dan TIDAK terikat provider tertentu.

    from agent_ai.context import ContextManager

    ctx = ContextManager()
    ctx.add_text("Halo")
    ctx.add_file("src/main.py", content="...")
    ctx.add_image("screenshot.png")
    payload = ctx.to_provider_context()
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent_ai.context.models import ContextItem, ContextType


class ContextManager:
    """Kumpulan context item dengan operasi add/remove/list/get."""

    def __init__(self) -> None:
        self._items: List[ContextItem] = []

    # ------------------------------------------------------------------ #
    # Add
    # ------------------------------------------------------------------ #
    def add(self, item: ContextItem) -> ContextItem:
        """Tambahkan sebuah ContextItem."""
        self._items.append(item)
        return item

    def add_text(self, content: str, source: Optional[str] = None, **metadata: Any) -> ContextItem:
        """Tambah context teks."""
        return self.add(ContextItem.text(content, source=source, **metadata))

    def add_file(self, source: str, content: str = "", **metadata: Any) -> ContextItem:
        """Tambah context file (referensi/isi)."""
        return self.add(ContextItem.file(source, content=content, **metadata))

    def add_image(self, source: str, **metadata: Any) -> ContextItem:
        """Tambah context image (referensi saja)."""
        return self.add(ContextItem.image(source, **metadata))

    # ------------------------------------------------------------------ #
    # Remove / clear
    # ------------------------------------------------------------------ #
    def remove(self, item_id: str) -> bool:
        """Hapus item berdasarkan id.

        Returns:
            True bila item ditemukan dan dihapus, False bila tidak ada.
        """
        for index, item in enumerate(self._items):
            if item.id == item_id:
                del self._items[index]
                return True
        return False

    def clear(self) -> None:
        """Hapus semua context item."""
        self._items.clear()

    # ------------------------------------------------------------------ #
    # Get / list
    # ------------------------------------------------------------------ #
    def get(self, item_id: str) -> Optional[ContextItem]:
        """Ambil item berdasarkan id (None bila tidak ada)."""
        for item in self._items:
            if item.id == item_id:
                return item
        return None

    def list(self, type: Optional[ContextType] = None) -> List[ContextItem]:
        """Daftar item, opsional difilter berdasarkan tipe."""
        if type is None:
            return list(self._items)
        return [item for item in self._items if item.type == type]

    def __len__(self) -> int:
        return len(self._items)

    # ------------------------------------------------------------------ #
    # Provider context
    # ------------------------------------------------------------------ #
    def to_provider_context(self) -> List[Dict[str, Any]]:
        """Context netral (provider-agnostic) untuk dikirim ke provider.

        Mengembalikan daftar dict hasil `ContextItem.to_dict()`. Provider
        multimodal nantinya dapat mengubah representasi ini sesuai formatnya.
        """
        return [item.to_dict() for item in self._items]

    def __repr__(self) -> str:  # pragma: no cover - bantuan debug
        return f"<ContextManager items={len(self._items)}>"
