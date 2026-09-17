"""Model untuk Context Manager.

Context item merepresentasikan potongan informasi yang bisa dikirim ke
provider AI. Tiga jenis dasar:
    - text  : teks bebas.
    - file  : referensi/isi file.
    - image : referensi image (belum ada processing/vision).

Desain sengaja provider-agnostic: item hanya menyimpan data + metadata,
bukan format spesifik Ollama/DeepSeek/OpenAI. Provider multimodal nantinya
dapat mengubah item ini menjadi payload sesuai kebutuhannya.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ContextType(str, Enum):
    """Jenis context item."""

    TEXT = "text"
    FILE = "file"
    IMAGE = "image"


@dataclass
class ContextItem:
    """Satu potongan context.

    Attributes:
        type: jenis context (text/file/image).
        content: isi utama. Untuk text = teks; untuk file = isi file (opsional);
            untuk image = referensi (path/URL/base64) — belum diproses.
        source: asal item (mis. path file, URL, atau label). Opsional.
        metadata: info tambahan bebas (mis. mime_type, line range, ukuran).
        id: identifier unik item.
    """

    type: ContextType
    content: str = ""
    source: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    # ------------------------------------------------------------------ #
    # Factory helpers
    # ------------------------------------------------------------------ #
    @classmethod
    def text(cls, content: str, source: Optional[str] = None, **metadata: Any) -> "ContextItem":
        """Buat context item bertipe text."""
        return cls(type=ContextType.TEXT, content=content, source=source, metadata=dict(metadata))

    @classmethod
    def file(
        cls,
        source: str,
        content: str = "",
        **metadata: Any,
    ) -> "ContextItem":
        """Buat context item bertipe file (referensi/isi file)."""
        return cls(type=ContextType.FILE, content=content, source=source, metadata=dict(metadata))

    @classmethod
    def image(cls, source: str, **metadata: Any) -> "ContextItem":
        """Buat context item bertipe image (referensi saja, belum diproses)."""
        return cls(type=ContextType.IMAGE, content="", source=source, metadata=dict(metadata))

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        """Representasi netral (provider-agnostic) dari item."""
        return {
            "id": self.id,
            "type": self.type.value,
            "content": self.content,
            "source": self.source,
            "metadata": self.metadata,
        }

    def __repr__(self) -> str:  # pragma: no cover - bantuan debug
        preview = (self.content or self.source or "")[:40]
        return f"<ContextItem type={self.type.value} id={self.id[:8]} {preview!r}>"
