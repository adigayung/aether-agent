"""Konversi gambar user -> content parts provider-agnostic (GENERALIZED).

Helper ini SEMULA hanya dipakai jalur Consultant (`_build_image_parts`). Sekarang
digeneralisasi di modul vision bersama agar REUSE yang sama dapat dipakai baik
oleh Consultant maupun oleh jalur Agent Task (create_task -> runtime ->
orchestrator -> provider). TIDAK menulis ulang logika image: seluruh
decoding/preprocessing tetap memakai modul vision existing (`ImagePreprocessor`).

Kontrak part yang dihasilkan (format internal AETHER, provider-agnostic):
    {"type": "image", "mime_type": "image/png", "encoding": "base64", "data": ...}
Provider adapter (mis. OpenAI-compatible) yang menerjemahkannya menjadi
`image_url` data URL. Modul ini TIDAK menyentuh provider/payload.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = ["build_image_parts"]


def build_image_parts(
    images: Optional[List[Dict[str, Any]]],
) -> Optional[List[Dict[str, Any]]]:
    """Proses gambar user -> content parts provider-agnostic (ADDITIVE).

    Memakai modul vision existing (ImagePreprocessor) TANPA menulis ulang
    logika image. Setiap item input: {"data": "<base64>", "mime_type": ...,
    "filename": opsional}. Base64 di-decode, dipreprocess (resize/kompresi
    bounded), lalu dibungkus menjadi content part image AETHER
    ({"type": "image", "mime_type":..., "encoding":"base64", "data":...}).

    Args:
        images: daftar gambar (base64). None/kosong -> None (text-only).

    Returns:
        List content part image, atau None bila tidak ada image.

    Raises:
        UnsupportedImageFormatError / InvalidImageError: gambar tidak valid.
        ValueError: payload gambar tidak berbentuk dict / data tidak valid.
    """
    if not images:
        return None

    import base64
    import binascii

    from agent_ai.vision.preprocessing import ImagePreprocessor

    preprocessor = ImagePreprocessor()
    parts: List[Dict[str, Any]] = []
    for index, item in enumerate(images):
        if not isinstance(item, dict):
            raise ValueError(f"Gambar[{index}] harus berupa object.")
        raw = item.get("data")
        if not raw:
            raise ValueError(f"Gambar[{index}] tidak punya data.")
        mime = str(item.get("mime_type") or "").strip()
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"Gambar[{index}] bukan base64 yang valid: {exc}") from exc
        processed = preprocessor.process(data, mime_type=mime)
        parts.append(dict(processed.payload))
    return parts or None
