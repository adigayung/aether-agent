"""ImageInputLoader: memuat & memvalidasi input gambar secara aman.

Provider-agnostic. Membaca gambar dari path lokal di dalam workspace,
mendeteksi MIME type, dan membangun ImageInput terstruktur.

Keamanan:
    - Path dibatasi pada workspace root (memakai helper boundary yang sudah ada).
    - Path traversal / symlink keluar workspace ditolak.
    - Ukuran file dibatasi (anti OOM).
    - Tidak menyimpan temporary file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from agent_ai.tools.filesystem import _DEFAULT_ROOT, _resolve_within_root
from agent_ai.vision.models import (
    FORMAT_TO_MIME,
    MIME_TO_FORMAT,
    ImageInput,
    InvalidImageError,
    UnsupportedImageFormatError,
)

# Batas ukuran file input (anti OOM). Dapat di-override per-instance.
_DEFAULT_MAX_INPUT_BYTES = 20_000_000  # 20 MB

# Magic bytes -> MIME (deteksi tanpa Pillow, cepat & aman).
_MAGIC_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"RIFF", "image/webp"),  # WEBP: "RIFF....WEBP"
)


class ImageInputLoader:
    """Memuat input gambar dari path lokal (aman, provider-agnostic).

    Args:
        root: workspace root. Default: project root (helper boundary existing).
        max_input_bytes: batas ukuran file input.
    """

    def __init__(
        self,
        root: Optional[Path] = None,
        max_input_bytes: int = _DEFAULT_MAX_INPUT_BYTES,
    ) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT
        self.max_input_bytes = max_input_bytes

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def load(
        self,
        path: str,
        *,
        mime_type: str = "",
        filename: Optional[str] = None,
    ) -> ImageInput:
        """Muat gambar dari path lokal di dalam workspace.

        Args:
            path: path gambar (relatif terhadap workspace root atau absolut
                di dalam workspace).
            mime_type: MIME type (opsional; dideteksi bila kosong).
            filename: nama file opsional.

        Returns:
            ImageInput dengan data + mime_type terisi.

        Raises:
            InvalidImageError: bila path tidak valid / file tidak ada / kosong.
            UnsupportedImageFormatError: bila format tidak didukung.
        """
        # Boundary: path harus di dalam workspace root.
        target = _resolve_within_root(path, self.root)
        if not target.exists() or not target.is_file():
            raise InvalidImageError(f"File gambar tidak ditemukan: {path}")

        size = target.stat().st_size
        if size <= 0:
            raise InvalidImageError(f"File gambar kosong: {path}")
        if size > self.max_input_bytes:
            raise InvalidImageError(
                f"File gambar terlalu besar ({size} bytes > {self.max_input_bytes})."
            )

        try:
            data = target.read_bytes()
        except OSError as exc:
            raise InvalidImageError(f"Gagal membaca file gambar: {exc}") from exc

        detected = self.detect_mime(data)
        resolved_mime = (mime_type or detected or "").lower()
        if resolved_mime not in MIME_TO_FORMAT:
            raise UnsupportedImageFormatError(
                f"Format gambar tidak didukung: {resolved_mime or 'unknown'}"
            )

        return ImageInput(
            path=str(target),
            mime_type=resolved_mime,
            filename=filename or target.name,
            data=data,
            metadata={"size_bytes": size},
        )

    def from_bytes(
        self,
        data: bytes,
        *,
        mime_type: str = "",
        filename: Optional[str] = None,
    ) -> ImageInput:
        """Bangun ImageInput dari bytes (tanpa path).

        Raises:
            InvalidImageError: bila data kosong.
            UnsupportedImageFormatError: bila format tidak didukung.
        """
        if not data:
            raise InvalidImageError("Data gambar kosong.")
        detected = self.detect_mime(data)
        resolved_mime = (mime_type or detected or "").lower()
        if resolved_mime not in MIME_TO_FORMAT:
            raise UnsupportedImageFormatError(
                f"Format gambar tidak didukung: {resolved_mime or 'unknown'}"
            )
        return ImageInput(
            path="",
            mime_type=resolved_mime,
            filename=filename,
            data=data,
            metadata={"size_bytes": len(data)},
        )

    # ------------------------------------------------------------------ #
    # MIME detection
    # ------------------------------------------------------------------ #
    @staticmethod
    def detect_mime(data: bytes) -> Optional[str]:
        """Deteksi MIME type dari magic bytes (tanpa Pillow).

        Returns:
            MIME type kanonik, atau None bila tidak dikenali.
        """
        if not data:
            return None
        for signature, mime in _MAGIC_SIGNATURES:
            if data.startswith(signature):
                if mime == "image/webp":
                    # WEBP: "RIFF" + 4 byte size + "WEBP".
                    if len(data) >= 12 and data[8:12] == b"WEBP":
                        return "image/webp"
                    continue
                return mime
        return None

    @staticmethod
    def mime_for_format(fmt) -> str:
        """MIME type kanonik untuk ImageFormat."""
        return FORMAT_TO_MIME[fmt]
