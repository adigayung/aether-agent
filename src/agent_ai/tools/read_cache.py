"""Read cache ringan (per task/session) untuk dedup retrieval duplikat.

Tujuan: menghindari (a) physical read ulang untuk sumber yang SUDAH tersedia,
dan (b) mengirim source yang SAMA dua kali ke LLM dalam satu task/session. Ini
BUKAN "second brain" dan BUKAN cache lintas-task:

    - Scope = SATU instance per task/session. Instance dibuat fresh oleh
      `build_registry()` / `build_consultant_registry()` (dipanggil per task),
      BUKAN singleton global. Tool yang dikonstruksi langsung tanpa cache
      (mis. di script verifier) berperilaku persis seperti sebelumnya
      (dedup tidak aktif) -> backward compatible.
    - Exact-duplicate: rentang (start, end) identik + digest konten identik.
    - Covered-range: rentang yang sudah TERCakup penuh oleh rentang yang pernah
      dibaca (mis. 100-200 dibaca, lalu 120-150 diminta) tidak dibaca ulang
      SELAMA file belum berubah (dicek via signature mtime_ns+size).
    - In-flight dedup: `key_lock()` menserialkan permintaan identik untuk path
      yang sama sehingga permintaan paralel tidak melakukan physical read
      berkali-kali (request kedua memakai hasil yang sudah tercatat).
    - Search dedup: `claim_search()` mencegah `search_code(query, scope)` yang
      sama dijalankan fisik berulang.
    - Invalidation: `invalidate()` (dipanggil tool mutasi) melupakan rentang
      baca + hasil search untuk path yang berubah; perubahan file eksternal
      juga dideteksi via perubahan signature.
    - Context-aware (runtime compaction): `sync_from_context()` menyelaraskan
      tanda "tersedia" dengan ISI konteks yang BENAR-BENAR dikirim ke LLM.
      Bila runtime compaction membuang detail sumber dari konteks, rentang itu
      TIDAK lagi diklaim "already_available" (menghindari false positive),
      sehingga Agent dapat mengambil ulang sumber yang sudah tidak terlihat.

Modul TIDAK menyimpan source lintas task: digest konten (bukan isi) dipakai
untuk exact-duplicate; cuplikan span (start, end) angka dipakai untuk covered
range.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

#: Kunci satu rentang baca: (start_line, end_line) 1-based inklusif.
RangeKey = Tuple[Optional[int], Optional[int]]

#: Signature file: (mtime_ns, size_bytes).
FileSignature = Tuple[int, int]


class ToolReadCache:
    """Catatan retrieval per task: read range + search, untuk dedup.

    Struktur internal (semua dilindungi `_lock` untuk akses paralel):
        - _ranges  : path -> {(start,end): digest konten}  (exact-duplicate)
        - _covered : path -> [(start,end), ...] yang tercakup (union)
        - _sig     : path -> (mtime_ns, size, total_lines)
        - _searches: set (query, path, context_lines)
        - _locks   : path -> threading.Lock (in-flight dedup)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ranges: Dict[str, Dict[RangeKey, str]] = {}
        self._covered: Dict[str, List[Tuple[int, int]]] = {}
        self._sig: Dict[str, Tuple[int, int, int]] = {}
        self._searches: Set[Tuple[str, str, int]] = set()
        self._locks: Dict[str, threading.Lock] = {}

    # ------------------------------------------------------------------ #
    # Digest
    # ------------------------------------------------------------------ #
    @staticmethod
    def _digest(content: str) -> str:
        return hashlib.sha1(content.encode("utf-8", "replace")).hexdigest()

    # ------------------------------------------------------------------ #
    # In-flight lock (dedup permintaan paralel)
    # ------------------------------------------------------------------ #
    def key_lock(self, rel_path: str) -> threading.Lock:
        """Lock per-path: menserialkan read identik yang datang bersamaan."""
        with self._lock:
            lock = self._locks.get(rel_path)
            if lock is None:
                lock = threading.Lock()
                self._locks[rel_path] = lock
            return lock

    # ------------------------------------------------------------------ #
    # Exact-duplicate (backward compatible)
    # ------------------------------------------------------------------ #
    def is_duplicate(
        self,
        rel_path: str,
        start: Optional[int],
        end: Optional[int],
        content: str,
    ) -> bool:
        """True bila (path, start, end) sudah pernah dibaca dengan konten IDENTIK."""
        with self._lock:
            entry = self._ranges.get(rel_path)
            if not entry:
                return False
            stored = entry.get((start, end))
            if stored is None:
                return False
            return stored == self._digest(content)

    def record(
        self,
        rel_path: str,
        start: Optional[int],
        end: Optional[int],
        content: str,
    ) -> None:
        """Catat bahwa (path, start, end) sudah dikirim dengan konten `content`."""
        with self._lock:
            self._ranges.setdefault(rel_path, {})[(start, end)] = self._digest(content)

    # ------------------------------------------------------------------ #
    # Covered-range (skip physical read)
    # ------------------------------------------------------------------ #
    def covered(
        self,
        rel_path: str,
        start: Optional[int],
        end: Optional[int],
        signature: FileSignature,
    ) -> Optional[Tuple[int, int, int]]:
        """Kembalikan (start, end, total_lines) bila rentang sudah tersedia.

        Rentang dianggap tersedia bila:
            - signature file SAMA dengan saat terakhir dicatat (file tidak
              berubah), DAN
            - (start, end) tercakup penuh oleh salah satu rentang yang tercatat.

        `start`/`end` None berarti "dari awal" / "sampai akhir" (di-resolve
        memakai total_lines yang tercatat). Mengembalikan None bila tidak
        tersedia (pemanggil harus melakukan read fisik).
        """
        with self._lock:
            rec = self._sig.get(rel_path)
            if rec is None:
                return None
            if (rec[0], rec[1]) != (int(signature[0]), int(signature[1])):
                return None
            total = rec[2]
            if total <= 0:
                return None
            s = 1 if start is None else int(start)
            e = total if end is None else int(end)
            if s < 1 or e < 1 or s > e:
                return None
            if e > total:
                e = total
            if s > e:
                return None
            for span_start, span_end in self._covered.get(rel_path, ()):
                if span_start <= s and e <= span_end:
                    return (s, e, total)
            return None

    def record_read(
        self,
        rel_path: str,
        start: Optional[int],
        end: Optional[int],
        signature: FileSignature,
        total_lines: int,
        content: str,
    ) -> None:
        """Catat read berhasil: digest + span tercakup + signature file.

        Bila signature berbeda dari yang tercatat (file berubah), rentang lama
        untuk path ini dibuang lebih dulu agar tidak dianggap masih valid.
        """
        try:
            start_i = 1 if start is None else int(start)
            end_i = int(end) if end is not None else int(total_lines)
        except (TypeError, ValueError):
            return
        if end_i < start_i or total_lines <= 0:
            return
        with self._lock:
            rec = self._sig.get(rel_path)
            if rec is not None and (rec[0], rec[1]) != (
                int(signature[0]),
                int(signature[1]),
            ):
                # File berubah -> rentang tercatat lama tidak valid.
                self._covered.pop(rel_path, None)
                self._ranges.pop(rel_path, None)
            self._sig[rel_path] = (
                int(signature[0]),
                int(signature[1]),
                int(total_lines),
            )
            self._ranges.setdefault(rel_path, {})[(start, end)] = self._digest(content)
            self._add_covered(rel_path, start_i, end_i)

    @staticmethod
    def _merge_spans(spans: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
        """Gabungkan span yang overlap/bersebelahan menjadi union terurut.

        Deterministis dan murni (tanpa state): dipakai baik oleh penambahan
        covered-range bertahap maupun oleh penyelarasan konteks penuh.
        """
        merged: List[Tuple[int, int]] = []
        for span_start, span_end in sorted(spans):
            if merged and span_start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], span_end))
            else:
                merged.append((span_start, span_end))
        return merged

    @staticmethod
    def _span_covered(spans: Sequence[Tuple[int, int]], start: int, end: int) -> bool:
        """True bila [start, end] tercakup penuh oleh salah satu span."""
        for span_start, span_end in spans:
            if span_start <= start and end <= span_end:
                return True
        return False

    def _add_covered(self, rel_path: str, start: int, end: int) -> None:
        """Tambahkan span [start, end] ke union tanda tercakup (merge overlap)."""
        spans = list(self._covered.get(rel_path, ()))
        spans.append((start, end))
        self._covered[rel_path] = self._merge_spans(spans)

    # ------------------------------------------------------------------ #
    # Context-aware availability (runtime compaction)
    # ------------------------------------------------------------------ #
    def sync_from_context(
        self,
        available_spans: Dict[str, Sequence[Tuple[int, int]]],
        seen_paths: Iterable[str],
    ) -> None:
        """Selaraskan tanda "tersedia" dengan ISI konteks yang dikirim ke LLM.

        Dipanggil runtime SETELAH context compaction menentukan pesan mana yang
        BENAR-BENAR masih terlihat oleh LLM. Kontrak:

            - Untuk SETIAP path di `seen_paths`, tanda covered-range DIGANTI
              dengan union `available_spans[path]` (rentang read_file yang
              masih ada di konteks; belum dipadatkan/dibuang). Path yang seluruh
              hasilnya hilang dari konteks menjadi TIDAK tersedia -> permintaan
              berikutnya melakukan physical read dan mengirim isi lagi
              (menghilangkan false positive "already_available").
            - Entri exact-duplicate (`_ranges`) yang rentangnya TIDAK lagi
              tercakup ikut dibuang, agar dedup exact tidak berbohong.
            - Signature (`_sig`) TIDAK diubah: perubahan file tetap terdeteksi
              dan `covered()` tetap mengembalikan None bila file berubah.

        Aman dipanggil berulang (idempoten): memanggil dengan konteks yang sama
        menghasilkan state yang sama.
        """
        paths = list(seen_paths)
        if not paths:
            return
        with self._lock:
            for rel_path in paths:
                merged = self._merge_spans(available_spans.get(rel_path, ()) or ())
                self._covered[rel_path] = merged
                entry = self._ranges.get(rel_path)
                if not entry:
                    continue
                for key in list(entry.keys()):
                    start, end = key
                    if not self._span_covered(merged, int(start), int(end)):
                        entry.pop(key, None)

    def forget_search(self, query: str, rel_path: str, context_lines: int) -> None:
        """Lupakan satu klaim pencarian (hasilnya tidak lagi ada di konteks).

        Dipakai runtime compaction: bila hasil `search_code(query, path, ctx)`
        sudah TIDAK terlihat di konteks, pencarian yang sama HARUS dapat
        dijalankan ulang (bukan "already_searched" palsu). Mutasi file tetap
        memakai `invalidate()` (membuang semua hasil search).
        """
        key = (str(query), str(rel_path), int(context_lines))
        with self._lock:
            self._searches.discard(key)

    # ------------------------------------------------------------------ #
    # Search dedup
    # ------------------------------------------------------------------ #
    def claim_search(self, query: str, rel_path: str, context_lines: int) -> bool:
        """Klaim satu pencarian secara atomic.

        Returns:
            True bila pencarian (query, path, context_lines) SUDAH pernah
            dijalankan (duplicate); False bila ini yang pertama (dan langsung
            diklaim, sehingga request paralel identik tidak dobel).
        """
        key = (str(query), str(rel_path), int(context_lines))
        with self._lock:
            if key in self._searches:
                return True
            self._searches.add(key)
            return False

    def record_search(self, query: str, rel_path: str, context_lines: int) -> None:
        """Catat pencarian (kompatibilitas; `claim_search` sudah mencatat)."""
        key = (str(query), str(rel_path), int(context_lines))
        with self._lock:
            self._searches.add(key)

    # ------------------------------------------------------------------ #
    # Invalidation
    # ------------------------------------------------------------------ #
    def invalidate(self, rel_path: Optional[str]) -> None:
        """Lupakan satu path (dipakai setelah mutasi file).

        Selain rentang baca path tersebut, SELURUH hasil search dibuang
        (konservatif: search bisa mencakup banyak file/project-wide, jadi setiap
        mutasi file membuat hasil search berpotensi stale).
        """
        if not rel_path:
            return
        with self._lock:
            self._ranges.pop(rel_path, None)
            self._covered.pop(rel_path, None)
            self._sig.pop(rel_path, None)
            self._searches.clear()

    def reset(self) -> None:
        """Kosongkan seluruh cache (scope tetap satu instance/task)."""
        with self._lock:
            self._ranges.clear()
            self._covered.clear()
            self._sig.clear()
            self._searches.clear()

    def __len__(self) -> int:  # pragma: no cover - introspection
        with self._lock:
            return sum(len(v) for v in self._ranges.values())


__all__ = ["ToolReadCache", "RangeKey", "FileSignature"]
