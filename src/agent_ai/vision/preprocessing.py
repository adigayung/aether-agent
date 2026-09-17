"""Preprocessing gambar (provider-agnostic, deterministik).

Membaca gambar secara aman, mempertahankan aspect ratio, melakukan
resize/downscale bila perlu, dan kompresi agar ukuran data lebih kecil bila
memungkinkan. TIDAK melakukan AI/image enhancement, OCR, atau image-to-text.

Prinsip:
    - Aspect ratio dipertahankan (tidak pernah stretch).
    - Readability diprioritaskan: untuk gambar ber-teks/UI/code, resize tidak
      ekstrem (memakai readability_max_dimension).
    - JPEG/PNG/WebP ditangani dengan aman.
    - Tidak menyimpan temporary file sembarangan (semua in-memory).
    - Tidak ada branching provider-specific.
"""

from __future__ import annotations

import io
from typing import List, Optional, Tuple

from agent_ai.vision.models import (
    FORMAT_TO_MIME,
    ImageFormat,
    ImageMetadata,
    InvalidImageError,
    ProcessedImage,
    UnsupportedImageFormatError,
    VisionConfig,
)

# Format Pillow -> ImageFormat kita.
_PIL_FORMAT_MAP = {
    "JPEG": ImageFormat.JPEG,
    "PNG": ImageFormat.PNG,
    "WEBP": ImageFormat.WEBP,
}


class ImagePreprocessor:
    """Preprocessing gambar sebelum inference.

    Args:
        config: VisionConfig (bounded). Default: VisionConfig().
    """

    def __init__(self, config: Optional[VisionConfig] = None) -> None:
        self.config = config or VisionConfig()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def process(
        self,
        data: bytes,
        *,
        mime_type: str = "",
        readability: bool = False,
    ) -> ProcessedImage:
        """Preprocess gambar dari bytes.

        Args:
            data: bytes gambar asli.
            mime_type: MIME type (opsional; dideteksi dari data bila kosong).
            readability: True bila gambar ber-teks/UI/code (prioritaskan
                readability, hindari resize ekstrem).

        Returns:
            ProcessedImage (bytes hasil + metadata + payload).

        Raises:
            InvalidImageError: bila data bukan gambar valid.
            UnsupportedImageFormatError: bila format tidak didukung.
        """
        if not data:
            raise InvalidImageError("Data gambar kosong.")

        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - Pillow wajib
            raise InvalidImageError("Pillow tidak tersedia untuk preprocessing gambar.") from exc

        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception as exc:  # noqa: BLE001 - data bukan gambar valid
            raise InvalidImageError(f"Gambar tidak valid: {type(exc).__name__}") from exc

        pil_format = (img.format or "").upper()
        fmt = _PIL_FORMAT_MAP.get(pil_format)
        if fmt is None:
            raise UnsupportedImageFormatError(f"Format gambar tidak didukung: {pil_format or 'unknown'}")

        original = self._metadata(img, fmt, len(data))
        reasons: List[str] = []

        # 1) Resize/downscale (pertahankan aspect ratio).
        target_dim = (
            self.config.readability_max_dimension if readability else self.config.max_dimension
        )
        resized = False
        new_size = self._fit_within(img.width, img.height, target_dim)
        if new_size != (img.width, img.height):
            img = img.resize(new_size, Image.LANCZOS)
            resized = True
            reasons.append(f"resize {original.width}x{original.height} -> {new_size[0]}x{new_size[1]}")

        # 2) Encode + kompresi (bounded oleh max_bytes).
        out_data, out_mime, compressed = self._encode(img, fmt, reasons)

        # 3) Bila masih melebihi max_bytes, turunkan kualitas/dimensi bertahap.
        if len(out_data) > self.config.max_bytes:
            out_data, out_mime, compressed, extra = self._shrink_to_fit(img, fmt, reasons)
            resized = resized or extra

        result_meta = self._metadata_from_bytes(out_data, out_mime)
        payload = self._build_payload(out_data, out_mime)

        return ProcessedImage(
            data=out_data,
            mime_type=out_mime,
            metadata=result_meta,
            original_metadata=original,
            resized=resized,
            compressed=compressed,
            payload=payload,
            reasons=reasons,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fit_within(width: int, height: int, max_dim: int) -> Tuple[int, int]:
        """Hitung ukuran baru agar sisi terpanjang <= max_dim (aspect ratio tetap).

        Tidak pernah memperbesar gambar (upscale) dan tidak pernah menghasilkan
        dimensi < 1 piksel.
        """
        longest = max(width, height)
        if longest <= max_dim or longest == 0:
            return (width, height)
        scale = max_dim / float(longest)
        new_w = max(1, int(round(width * scale)))
        new_h = max(1, int(round(height * scale)))
        return (new_w, new_h)

    def _encode(self, img, fmt: ImageFormat, reasons: List[str]) -> Tuple[bytes, str, bool]:
        """Encode gambar ke format target (JPEG/PNG/WebP) dengan kompresi."""
        from PIL import Image

        buf = io.BytesIO()
        compressed = False
        if fmt == ImageFormat.JPEG:
            rgb = img.convert("RGB") if img.mode not in ("RGB", "L") else img
            rgb.save(buf, format="JPEG", quality=self.config.jpeg_quality, optimize=True)
            compressed = True
            reasons.append(f"jpeg quality={self.config.jpeg_quality}")
        elif fmt == ImageFormat.PNG:
            # PNG: pertahankan alpha bila diminta; optimasi lossless.
            png = img
            if not self.config.preserve_alpha and img.mode in ("RGBA", "LA", "P"):
                png = img.convert("RGB")
            png.save(buf, format="PNG", optimize=True)
            reasons.append("png optimize (lossless)")
        elif fmt == ImageFormat.WEBP:
            webp = img
            if not self.config.preserve_alpha and img.mode in ("RGBA", "LA", "P"):
                webp = img.convert("RGB")
            webp.save(buf, format="WEBP", quality=self.config.jpeg_quality, method=4)
            compressed = True
            reasons.append(f"webp quality={self.config.jpeg_quality}")
        else:  # pragma: no cover - dijaga oleh pemanggil
            raise UnsupportedImageFormatError(f"Format tidak didukung: {fmt}")
        return buf.getvalue(), FORMAT_TO_MIME[fmt], compressed

    def _shrink_to_fit(
        self, img, fmt: ImageFormat, reasons: List[str]
    ) -> Tuple[bytes, str, bool, bool]:
        """Turunkan ukuran bertahap sampai <= max_bytes (bounded).

        Strategi deterministik:
            1. Turunkan kualitas JPEG/WebP bertahap.
            2. Bila masih besar, turunkan dimensi bertahap (aspect ratio tetap).

        Returns:
            (data, mime, compressed, resized)
        """
        from PIL import Image

        resized = False
        # 1) Turunkan kualitas (hanya untuk format lossy).
        if fmt in (ImageFormat.JPEG, ImageFormat.WEBP):
            for quality in (75, 65, 55, 45):
                buf = io.BytesIO()
                target = img.convert("RGB") if img.mode not in ("RGB", "L") else img
                if fmt == ImageFormat.JPEG:
                    target.save(buf, format="JPEG", quality=quality, optimize=True)
                else:
                    target.save(buf, format="WEBP", quality=quality, method=4)
                data = buf.getvalue()
                if len(data) <= self.config.max_bytes:
                    reasons.append(f"kompresi tambahan quality={quality}")
                    return data, FORMAT_TO_MIME[fmt], True, resized

        # 2) Turunkan dimensi bertahap (aspect ratio tetap).
        current = img
        for _ in range(6):
            new_size = self._fit_within(current.width, current.height, max(1, int(max(current.width, current.height) * 0.8)))
            if new_size == (current.width, current.height):
                break
            current = current.resize(new_size, Image.LANCZOS)
            resized = True
            data, mime, _ = self._encode(current, fmt, [])
            if len(data) <= self.config.max_bytes:
                reasons.append(f"downscale -> {new_size[0]}x{new_size[1]}")
                return data, mime, True, resized

        # Fallback terakhir: kembalikan hasil terkecil yang bisa dibuat.
        data, mime, _ = self._encode(current, fmt, [])
        reasons.append("ukuran minimum tercapai (max_bytes mungkin tidak terpenuhi)")
        return data, mime, True, resized

    @staticmethod
    def _metadata(img, fmt: ImageFormat, size_bytes: int) -> ImageMetadata:
        """Bangun ImageMetadata dari objek Pillow."""
        return ImageMetadata(
            width=img.width,
            height=img.height,
            format=fmt,
            mime_type=FORMAT_TO_MIME[fmt],
            size_bytes=size_bytes,
            mode=img.mode,
            has_alpha=img.mode in ("RGBA", "LA", "P"),
        )

    @staticmethod
    def _metadata_from_bytes(data: bytes, mime_type: str) -> ImageMetadata:
        """Bangun ImageMetadata dari bytes hasil (dimensi/ukuran final)."""
        from PIL import Image

        try:
            with Image.open(io.BytesIO(data)) as img:
                fmt = _PIL_FORMAT_MAP.get((img.format or "").upper())
                return ImageMetadata(
                    width=img.width,
                    height=img.height,
                    format=fmt,
                    mime_type=mime_type,
                    size_bytes=len(data),
                    mode=img.mode,
                    has_alpha=img.mode in ("RGBA", "LA", "P"),
                )
        except Exception:  # noqa: BLE001 - metadata gagal -> minimal
            return ImageMetadata(mime_type=mime_type, size_bytes=len(data))

    @staticmethod
    def _build_payload(data: bytes, mime_type: str) -> dict:
        """Payload provider-agnostic (diterjemahkan provider adapter nantinya).

        Format internal AETHER, BUKAN format API provider tertentu.
        """
        import base64

        return {
            "type": "image",
            "mime_type": mime_type,
            "encoding": "base64",
            "data": base64.b64encode(data).decode("ascii"),
        }
