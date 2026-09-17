"""Duplicate Read Prevention.

Melacak file yang sudah diberikan ke LLM. Bila file sudah dibaca dan hash
tidak berubah, full content tidak dikirim lagi. Bila file berubah, perubahan
dideteksi dan content terbaru dikirim.

In-memory saja (TIDAK membuat cache/persistence database baru).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from agent_ai.tools.filesystem import _resolve_within_root


def _hash_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


@dataclass
class ReadRecord:
    """Catatan satu file yang pernah dibaca.

    Attributes:
        path: path relatif.
        content_hash: hash konten saat terakhir dikirim.
        bytes_len: ukuran bytes saat terakhir dikirim.
        tokens: estimasi token saat terakhir dikirim.
        count: berapa kali file ini dikirim.
    """

    path: str
    content_hash: str
    bytes_len: int
    tokens: int
    count: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "content_hash": self.content_hash,
            "bytes_len": self.bytes_len,
            "tokens": self.tokens,
            "count": self.count,
        }


class ReadTracker:
    """Melacak file yang sudah dibaca untuk mencegah duplikasi.

    Args:
        root: root workspace (boundary).
        enabled: bila False, semua file dianggap belum dibaca (selalu kirim).
    """

    def __init__(self, root: Path, enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = enabled
        self._records: Dict[str, ReadRecord] = {}

    def _current_hash(self, path: str) -> Optional[str]:
        target = _resolve_within_root(path, self.root)
        if not target.is_file():
            return None
        try:
            data = target.read_bytes()
        except OSError:
            return None
        return hashlib.sha1(data).hexdigest()

    def is_unchanged(self, path: str) -> bool:
        """True bila file sudah pernah dibaca dan hash-nya tidak berubah."""
        if not self.enabled:
            return False
        record = self._records.get(path)
        if record is None:
            return False
        current = self._current_hash(path)
        return current is not None and current == record.content_hash

    def has_changed(self, path: str) -> bool:
        """True bila file pernah dibaca tetapi hash-nya berubah."""
        if not self.enabled:
            return False
        record = self._records.get(path)
        if record is None:
            return False
        current = self._current_hash(path)
        return current is not None and current != record.content_hash

    def record(self, path: str, content: str, tokens: int = 0) -> ReadRecord:
        """Catat bahwa file `path` sudah dikirim ke LLM.

        Hash yang disimpan adalah hash FILE di disk (sumber kebenaran untuk
        deteksi perubahan), bukan hash potongan `content` yang dikirim. Ini
        agar partial read tetap terdeteksi "unchanged" selama file tidak
        berubah.
        """
        file_hash = self._current_hash(path)
        content_hash = file_hash if file_hash is not None else _hash_text(content)
        existing = self._records.get(path)
        if existing is not None and existing.content_hash == content_hash:
            existing.count += 1
            return existing
        record = ReadRecord(
            path=path,
            content_hash=content_hash,
            bytes_len=len(content.encode("utf-8")),
            tokens=tokens,
            count=1,
        )
        self._records[path] = record
        return record

    def get(self, path: str) -> Optional[ReadRecord]:
        return self._records.get(path)

    def reset(self) -> None:
        self._records.clear()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "tracked": len(self._records),
            "records": {k: v.to_dict() for k, v in self._records.items()},
        }
