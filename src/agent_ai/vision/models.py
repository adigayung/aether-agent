"""Model untuk Vision / Multimodal Input Subsystem (#46).

Provider-agnostic. Vision adalah CAPABILITY UMUM (bukan provider-specific).
Model di sini hanya data terstruktur untuk input gambar, metadata, hasil
preprocessing, dan request vision.

    ImageFormat      -> format gambar yang didukung (jpeg/png/webp)
    ImageInput       -> input gambar (path lokal + mime + filename opsional)
    ImageMetadata    -> metadata gambar (dimensi, ukuran, format)
    ProcessedImage   -> hasil preprocessing (bytes + metadata + payload)
    VisionRequest    -> prompt teks + image input(s) + requirement capability
    VisionConfig     -> policy preprocessing (bounded, dari config)

Tidak menyimpan chain-of-thought. Tidak ada OCR / image-to-text / enhancement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional

from agent_ai.capabilities.models import ModelCapability


class ImageFormat(str, Enum):
    """Format gambar yang didukung."""

    JPEG = "jpeg"
    PNG = "png"
    WEBP = "webp"


#: Pemetaan MIME type -> ImageFormat.
MIME_TO_FORMAT: Dict[str, ImageFormat] = {
    "image/jpeg": ImageFormat.JPEG,
    "image/jpg": ImageFormat.JPEG,
    "image/png": ImageFormat.PNG,
    "image/webp": ImageFormat.WEBP,
}

#: Pemetaan ImageFormat -> MIME type kanonik.
FORMAT_TO_MIME: Dict[ImageFormat, str] = {
    ImageFormat.JPEG: "image/jpeg",
    ImageFormat.PNG: "image/png",
    ImageFormat.WEBP: "image/webp",
}


class VisionError(Exception):
    """Base error untuk vision subsystem."""


class InvalidImageError(VisionError):
    """Gambar tidak valid / tidak dapat dibaca."""


class UnsupportedImageFormatError(VisionError):
    """Format gambar tidak didukung."""


@dataclass
class ImageInput:
    """Input gambar (provider-agnostic).

    Attributes:
        path: path lokal gambar (relatif terhadap workspace root atau absolut
            di dalam workspace).
        mime_type: MIME type gambar (mis. "image/png"). Bila kosong, dideteksi.
        filename: nama file opsional (untuk metadata/payload).
        data: bytes gambar (opsional; bila None, dibaca dari path).
        metadata: info tambahan bebas.
    """

    path: str = ""
    mime_type: str = ""
    filename: Optional[str] = None
    data: Optional[bytes] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def format(self) -> Optional[ImageFormat]:
        """ImageFormat dari mime_type (None bila tidak dikenali)."""
        return MIME_TO_FORMAT.get((self.mime_type or "").lower())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "mime_type": self.mime_type,
            "filename": self.filename,
            "has_data": self.data is not None,
            "data_size": len(self.data) if self.data is not None else None,
            "metadata": self.metadata,
        }


@dataclass
class ImageMetadata:
    """Metadata gambar.

    Attributes:
        width: lebar (piksel).
        height: tinggi (piksel).
        format: format gambar.
        mime_type: MIME type.
        size_bytes: ukuran data (bytes).
        mode: mode warna Pillow (mis. "RGB", "RGBA").
        has_alpha: apakah gambar punya channel alpha.
    """

    width: int = 0
    height: int = 0
    format: Optional[ImageFormat] = None
    mime_type: str = ""
    size_bytes: int = 0
    mode: str = ""
    has_alpha: bool = False

    @property
    def aspect_ratio(self) -> float:
        """Rasio aspek (width / height). 0.0 bila tinggi 0."""
        return (self.width / self.height) if self.height else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "format": self.format.value if self.format else None,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "mode": self.mode,
            "has_alpha": self.has_alpha,
            "aspect_ratio": round(self.aspect_ratio, 6),
        }


@dataclass
class ProcessedImage:
    """Hasil preprocessing gambar.

    Attributes:
        data: bytes gambar hasil preprocessing.
        mime_type: MIME type hasil.
        metadata: metadata gambar hasil (dimensi/ukuran).
        original_metadata: metadata gambar asli (sebelum preprocessing).
        resized: apakah gambar di-resize.
        compressed: apakah gambar dikompresi.
        payload: payload provider-agnostic (mis. {"type": "image", "data": ...}).
            Provider adapter yang menerjemahkan ke format API masing-masing.
        reasons: alasan singkat langkah preprocessing (bukan chain-of-thought).
    """

    data: bytes = b""
    mime_type: str = ""
    metadata: Optional[ImageMetadata] = None
    original_metadata: Optional[ImageMetadata] = None
    resized: bool = False
    compressed: bool = False
    payload: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "metadata": self.metadata.to_dict() if self.metadata else None,
            "original_metadata": self.original_metadata.to_dict() if self.original_metadata else None,
            "resized": self.resized,
            "compressed": self.compressed,
            "payload": self.payload,
            "reasons": self.reasons,
        }


@dataclass
class VisionRequest:
    """Permintaan vision: prompt teks + image input(s).

    Attributes:
        prompt: prompt teks.
        images: daftar ImageInput.
        required_capabilities: capability WAJIB (default VISION + MULTIMODAL).
        metadata: info tambahan bebas (mis. preprocessing metadata).
    """

    prompt: str = ""
    images: List[ImageInput] = field(default_factory=list)
    required_capabilities: FrozenSet[ModelCapability] = field(
        default_factory=lambda: frozenset({ModelCapability.VISION, ModelCapability.MULTIMODAL})
    )
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt": self.prompt,
            "images": [img.to_dict() for img in self.images],
            "required_capabilities": sorted(c.value for c in self.required_capabilities),
            "metadata": self.metadata,
        }


@dataclass
class VisionConfig:
    """Policy preprocessing vision (bounded, tidak di-hardcode).

    Attributes:
        enabled: apakah vision aktif.
        max_dimension: dimensi maksimum (piksel) sisi terpanjang setelah resize.
        readability_max_dimension: dimensi maksimum untuk gambar ber-teks/UI/code
            (lebih besar agar teks tetap terbaca).
        jpeg_quality: kualitas JPEG (1..95).
        max_bytes: batas ukuran data hasil (bytes).
        preserve_alpha: pertahankan alpha (PNG/WebP) bila memungkinkan.
        readability_mode: prioritaskan readability (jangan resize ekstrem).
    """

    enabled: bool = True
    max_dimension: int = 1568
    readability_max_dimension: int = 2048
    jpeg_quality: int = 85
    max_bytes: int = 4_000_000
    preserve_alpha: bool = True
    readability_mode: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_dimension": self.max_dimension,
            "readability_max_dimension": self.readability_max_dimension,
            "jpeg_quality": self.jpeg_quality,
            "max_bytes": self.max_bytes,
            "preserve_alpha": self.preserve_alpha,
            "readability_mode": self.readability_mode,
        }
