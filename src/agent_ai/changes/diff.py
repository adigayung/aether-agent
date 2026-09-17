"""Unified diff untuk file teks yang berubah.

Provider-agnostic. Menghasilkan unified diff untuk file created/modified/
deleted. Untuk binary/non-text, TIDAK mencoba membuat diff teks palsu.
"""

from __future__ import annotations

import difflib
from typing import Optional

# Batas ukuran untuk mencoba diff teks (hindari file besar).
_MAX_DIFF_BYTES = 1_000_000


def _is_binary(data: bytes) -> bool:
    """Deteksi binary sederhana: ada byte NUL dalam sampel awal."""
    if not data:
        return False
    sample = data[:8192]
    return b"\x00" in sample


def _decode(data: Optional[bytes]) -> Optional[str]:
    """Decode bytes ke teks; kembalikan None bila binary/tidak dapat didecode."""
    if data is None:
        return ""
    if _is_binary(data):
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def generate(
    before: Optional[bytes],
    after: Optional[bytes],
    path: str,
) -> str:
    """Hasilkan unified diff untuk sebuah file.

    Args:
        before: konten sebelum (None bila file baru).
        after: konten sesudah (None bila file dihapus).
        path: path file (untuk header diff).

    Returns:
        Unified diff sebagai string. Untuk binary/non-text, kembalikan pesan
        penanda (bukan diff teks palsu). Untuk file tidak berubah, kembalikan
        string kosong.
    """
    before_text = _decode(before)
    after_text = _decode(after)

    if before_text is None or after_text is None:
        return f"# binary/non-text file, diff tidak tersedia: {path}\n"

    # Batasi ukuran.
    if (before and len(before) > _MAX_DIFF_BYTES) or (after and len(after) > _MAX_DIFF_BYTES):
        return f"# file terlalu besar untuk diff: {path}\n"

    if before_text == after_text:
        return ""

    before_lines = before_text.splitlines(keepends=True)
    after_lines = after_text.splitlines(keepends=True)

    from_label = f"a/{path}" if before is not None else "/dev/null"
    to_label = f"b/{path}" if after is not None else "/dev/null"

    diff_lines = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=from_label,
        tofile=to_label,
    )
    return "".join(diff_lines)
