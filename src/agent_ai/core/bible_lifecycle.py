"""Bible Context Lifecycle (Task 4).

Masalah yang diselesaikan
-------------------------
Context Project Bible/Intelligence (hasil retrieval existing:
``_retrieve_knowledge_context`` -> ``_fit_knowledge_context``) sebelumnya
disisipkan ke riwayat percakapan pada setiap pemanggilan jalur context, dan
tidak ada catatan bahwa context itu SUDAH dikirim pada task yang sedang
berjalan. Akibatnya isi Bible yang sama ikut dikirim berulang (boros token)
pada task yang sama.

Modul ini TIDAK menggantikan retrieval Bible dan TIDAK mengubah isi Bible.
Ia hanya menyimpan identitas context Bible yang sudah pernah dikirim pada
scope SATU task/runtime context:

    Task
     +-- BibleContextState
          +-- identitas context yang sudah dikirim (fingerprint konten)
          +-- revision/fingerprint sumber Bible (murah: nama + size + mtime)
          +-- query yang dipakai saat retrieval terakhir
          +-- invalidation

Aturan perilaku:
    * Round 1  -> belum pernah dikirim  -> inject + mark sent
    * Round 2  -> sama (revision sama)   -> skip (tanpa retrieval, tanpa inject)
    * Round 3  -> Bible berubah          -> retrieve ulang -> inject bila isi
                                            memang berubah -> mark sent
    * Bible berubah lalu isi identik     -> tetap tidak di-inject (anti duplikat)

State bersifat INSTANCE/LOCAL (satu scope = satu task). Tidak ada cache global
lintas task: task lain tetap dianggap belum menerima context Bible.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

# Lokasi Bible project-local (sesuai BibleStore/ProjectIntelligence):
#   <root project target>/.aether/bible/<kategori>.md
_BIBLE_DIR = ".aether"
_BIBLE_SUBDIR = "bible"

# Atribut root yang mungkin dipakai objek brain/intelligence (best-effort).
_ROOT_ATTRS: Tuple[str, ...] = ("root", "project_root", "root_dir", "project_dir")


def fingerprint_text(text: str) -> str:
    """Fingerprint deterministik untuk konten konteks (sha1, hex).

    Dipakai sebagai 'identitas context' yang sudah terkirim. Teks kosong
    menghasilkan string kosong supaya pemanggil dapat membedakan
    'belum ada context' dari 'context ada tapi kosong'.
    """
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def resolve_bible_root(brain: Any = None, root: Any = None) -> Optional[Path]:
    """Cari root project target dari objek brain/intelligence (best-effort).

    Mengembalikan ``None`` bila root tidak dapat ditentukan. Pencarian bersifat
    murah (hanya atribut) dan tidak mengimpor apa pun dari paket ``projects``
    supaya tetap bebas dependency.
    """
    if root is not None:
        try:
            return Path(root)
        except (TypeError, ValueError):
            return None

    candidates: List[Any] = []
    if brain is not None:
        candidates.append(brain)
        intelligence = getattr(brain, "intelligence", None)
        if intelligence is not None:
            candidates.append(intelligence)

    for obj in candidates:
        for attr in _ROOT_ATTRS:
            value = getattr(obj, attr, None)
            if not value:
                continue
            try:
                return Path(value)
            except (TypeError, ValueError):
                continue
    return None


def bible_sources_dir(root: Any = None) -> Optional[Path]:
    """Direktori sumber Bible (``<root>/.aether/bible``) atau ``None``."""
    base = resolve_bible_root(root=root)
    if base is None:
        return None
    return base / _BIBLE_DIR / _BIBLE_SUBDIR


def bible_source_revision(root: Any = None) -> Optional[str]:
    """Revision murah dari sumber Bible (TANPA membaca isi file).

    Revision dihitung dari daftar ``*.md`` beserta size + mtime. Perubahan isi
    Bible (file ditulis ulang, ditambah, atau dihapus) hampir selalu mengubah
    revision ini, sehingga context lama dapat dianggap tidak valid.

    Status khusus:
        * ``None``     -> root tidak diketahui (revision tak bisa dinilai)
        * ``"missing"``-> direktori Bible belum ada
        * ``"empty"``  -> direktori ada tapi belum ada file Bible
    """
    directory = bible_sources_dir(root)
    if directory is None:
        return None
    if not directory.is_dir():
        return "missing"

    entries: List[str] = []
    try:
        for path in sorted(directory.glob("*.md")):
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
    except OSError:
        return None

    if not entries:
        return "empty"
    return fingerprint_text("\n".join(entries))


@dataclass
class BibleContextState:
    """State task-scoped untuk context Bible yang sudah dikirim.

    Satu instance = satu scope task. Instance lain (task lain) TIDAK berbagi
    state ini, jadi context Bible tetap dianggap belum dikirim pada task baru.
    """

    scope: str = ""
    revision: Optional[str] = None
    content_fingerprint: str = ""
    query: str = ""
    injected_count: int = 0
    skipped_count: int = 0
    invalidated: bool = False
    pending_scope: bool = True
    reason: str = ""

    # ---------------------------------------------------------------- checks
    def needs_retrieval(self, revision: Optional[str]) -> bool:
        """Apakah context Bible perlu di-retrieve (dan mungkin di-inject)?

        ``True`` bila scope ini belum mengevaluasi Bible sama sekali, bila state
        di-invalidate eksplisit, atau bila revision sumber Bible berbeda dari
        revision yang tercatat saat terakhir diproses.
        """
        if self.pending_scope or self.invalidated:
            return True
        return revision != self.revision

    def has_sent_context(self) -> bool:
        """True bila setidaknya ada satu context Bible yang sudah dikirim."""
        return bool(self.content_fingerprint)

    # -------------------------------------------------------------- mutation
    def mark_injected(
        self,
        content_fingerprint: str,
        revision: Optional[str],
        query: str = "",
    ) -> bool:
        """Catat bahwa context Bible dikirim. Return False bila isinya identik.

        Return ``False`` berarti pemanggil TIDAK boleh mengirim context itu
        (duplikat isi pada scope ini) meskipun revision berubah.
        """
        self.pending_scope = False
        self.invalidated = False
        self.reason = ""
        self.revision = revision
        if query:
            self.query = query

        if not content_fingerprint:
            return False
        if content_fingerprint == self.content_fingerprint:
            return False

        self.content_fingerprint = content_fingerprint
        self.injected_count += 1
        return True

    def mark_checked(self, revision: Optional[str]) -> None:
        """Catat bahwa Bible sudah diperiksa (tanpa ada context yang dikirim).

        Dipakai agar task dengan Bible kosong tidak melakukan retrieval berulang
        di setiap round; bila Bible berubah, ``needs_retrieval`` akan True lagi.
        """
        self.pending_scope = False
        self.invalidated = False
        self.reason = ""
        self.revision = revision

    def record_skip(self) -> None:
        """Hitung satu round yang tidak perlu retrieval/inject (observability)."""
        self.skipped_count += 1

    def invalidate(self, reason: str = "") -> "BibleContextState":
        """Tandai context Bible pada scope ini tidak valid (harus dikirim ulang)."""
        self.invalidated = True
        self.pending_scope = True
        self.reason = reason or ""
        return self

    # -------------------------------------------------------------- reporting
    def to_dict(self) -> Dict[str, Any]:
        """Ringkasan state untuk observability/verifikasi (tanpa isi context)."""
        return {
            "scope": self.scope,
            "revision": self.revision,
            "has_context": self.has_sent_context(),
            "content_fingerprint": self.content_fingerprint,
            "query": self.query,
            "injected": self.injected_count,
            "skipped": self.skipped_count,
            "invalidated": self.invalidated,
            "pending_scope": self.pending_scope,
            "reason": self.reason,
        }
