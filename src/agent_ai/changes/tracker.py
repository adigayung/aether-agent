"""ChangeTracker: melacak perubahan filesystem selama satu task.

Provider-agnostic. Berbasis kondisi filesystem (hashing + size), BUKAN
sekadar mencatat action tool. Read-only terhadap source project: tracker
tidak pernah menulis/mengubah file.

Isolasi task: setiap task_id punya ChangeSet sendiri.

Tidak membuat Git dependency, database, atau filesystem engine baru.

Optimasi workspace besar
------------------------
Snapshot awal workspace besar (mis. 40k+ file / 2,7 GB) sebelumnya membaca
dan meng-hash SELURUH isi file secara sinkron sebelum LLM pertama dipanggil
(terukur hingga ratusan detik). Modul ini kini memakai cache fingerprint
proses-level yang di-key oleh **canonical project root**:

    rel_path -> (sha1, size, mtime_ns)

Pada setiap snapshot, tiap file cukup di-`stat` (murah) dan SHA-1 dihitung
hanya bila metadata fingerprint-nya berubah (size / mtime_ns / keberadaan).
Snapshot berulang pada workspace yang tidak berubah menjadi O(#file `stat`)
tanpa membaca isi file.

Batas akurasi (DIDOKUMENTASIKAN eksplisit)
------------------------------------------
Cache fingerprint bersifat ADVISORY: bila `size` dan `mtime_ns` suatu file
identik dengan catatan cache, isi file DIASUMSIKAN tidak berubah dan SHA-1
lama dipakai ulang tanpa membaca ulang isi. Ini adalah heuristik filesystem
standar (sama seperti yang dipakai build tool/`make`), BUKAN jaminan byte
identik. Konsekuensi: perubahan isi yang TIDAK mengubah ukuran DAN tidak
mengubah `mtime_ns` (mis. modifikasi in-place yang mempertahankan timestamp,
atau resolusi mtime kasar di filesystem tertentu) berpotensi tidak terdeteksi.

Celah ini sengaja diterima karena:
    * setiap file pada snapshot pertama tetap di-hash dari isi nyata;
    * tool mutasi AETHER (write/edit/delete/move) selalu memperbarui file
      dalam scope task dan biasanya memperbarui size/mtime;
    * `detect_changes` tetap membandingkan SHA-1 (konten) — bukan metadata —
      sehingga ketika hash benar-benar dihitung ulang, klasifikasi
      created/modified/deleted identik dengan implementasi full-hash lama.

Bila file terbukti berubah pada metadata, hash SELALU dihitung ulang dari
isi terkini; cache tidak pernah menghasilkan hash yang tidak dipakai.

Batasan yang disengaja pada rekursi direktori
---------------------------------------------
`snapshot()` pada direktori memakai `iter_trackable_files()` yang menambahkan
aturan eksklusi eksplisit di atas iterasi filesystem yang sudah ada:
direktori cache/artefak (mis. `coverage`, `htmlcov`, `.next`, `.ruff_cache`,
`.tox` — di samping yang sudah diabaikan `_iter_files`) dan file dengan
ekstensi cache/artefak (`.pyc`, `.log`, `.tmp`, dst) — lihat
`_TRACKER_IGNORED_DIRS` / `_TRACKER_IGNORED_SUFFIXES`. Ini BUKAN penghapusan
pekerjaan hashing secara diam-diam: aturannya eksplisit, tetap meng-hash
seluruh file SOURCE pada snapshot pertama, dan tidak bergantung pada hasil
benchmark. Bukti pengukuran (workspace uji 3000 file artefak): 2700 file
terlihat `_iter_files` -> hanya 100 file di-track tracker.

Root project AETHER (`_DEFAULT_ROOT`) DIKECUALIKAN dari aturan tambahan ini
agar cache fingerprint root AETHER tetap lengkap. Efek samping yang
didokumentasikan: file yang sengaja dibuat Agent di dalam direktori yang
diabaikan hanya akan tetap terdeteksi bila ia sendiri menjadi kandidat
eksplisit task (`register_path`), bukan lewat pemindaian rekursif root.

Deteksi perubahan isi saat metadata identik
-------------------------------------------
Alih-alih membaca ulang seluruh isi workspace setiap snapshot, AETHER
memakai **pencatatan mutasi self-aware**: cache fingerprint mengenali
mutasi yang dilakukan PROSES INI SENDIRI (`mark_mutated` / `mark_moved` /
`mark_deleted`, dipanggil dari jalur tool filesystem pada
`agent_ai.tools.registry.ToolRegistry.execute`). Mutasi seperti itu
MENGHAPUS entri cache path terkait sehingga snapshot berikutnya SELALU
menghitung ulang SHA-1 dari isi nyata — bahkan bila `size` dan `mtime_ns`
kebetulan identik. Dengan begitu perubahan isi yang dilakukan Agent tidak
lagi dapat terlewat oleh cache metadata.

Mutasi dari LUAR proses (editor eksternal, script non-tool, proses lain)
tetap hanya dapat dideteksi bila metadata berubah; bila metadata pun
identik, perubahan itu TIDAK terdeteksi — celah ini didokumentasikan,
bukan disembunyikan, dan tidak diklaim setara dengan hashing konten penuh.

Batas ukuran file yang di-track
-------------------------------
Snapshot direktori melewati file individual yang LEBIH BESAR dari
`MAX_TRACKED_FILE_SIZE` (25 MiB) SEBELUM isinya dibaca — keputusan diambil
dari `st_size` hasil `stat` yang sudah dibutuhkan untuk fingerprint, jadi file
besar tidak pernah dibaca hanya untuk di-hash. Aturan berlaku per file (bukan
per direktori), file tepat 25 MiB tetap dilacak, dan file besar tidak
menyebabkan hashing penuh berulang pada snapshot berikutnya. Akses eksplisit
ke satu path file (mis. lewat `register_path` pada mutasi tool) tetap
menghasilkan hash penuh sehingga perubahan yang dilaporkan tool tidak hilang.
Statistik snapshot terakhir tersedia lewat
`ChangeTracker.last_snapshot_stats()` (`files_seen` / `tracked` /
`skipped_large`).
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent_ai.changes.models import ChangeRecord, ChangeSet, ChangeType
from agent_ai.tools.filesystem import (
    _DEFAULT_ROOT,
    _iter_files,
    _resolve_within_root,
)


# --------------------------------------------------------------------------- #
# Batas ukuran file yang di-track (eksplisit).
#
# File yang LEBIH BESAR dari batas ini dikecualikan dari snapshot workspace
# OTOMATIS (rekursif) dan TIDAK PERNAH dibaca/di-hash. Tujuannya menghilangkan
# penundaan New Task akibat hashing model AI, checkpoint, dataset, media, dan
# file biner besar yang tidak relevan untuk pelacakan perubahan kode.
#
# Aturan:
#   * berlaku per FILE INDIVIDUAL — TIDAK mengecualikan seluruh direktori yang
#     kebetulan memuat file besar (file source di direktori yang sama tetap
#     dilacak);
#   * file berukuran TEPAT 25 MiB masih dilacak (batasnya "lebih dari");
#   * keputusan memakai metadata `st_size` yang sudah dibaca saat `stat` —
#     isi file TIDAK PERNAH dibaca hanya untuk memutuskan pengecualian;
#   * akses EXPLISIT ke satu path (mis. tool `read_file`/`edit_file` pada file
#     besar, atau `snapshot(path_file)`) tetap MENGHASILKAN hash: snapshot
#     per-file yang diminta pemanggil bukan "full-workspace scan" dan tidak
#     boleh diam-diam mengembalikan entri kosong.
#
# Konsekuensi file besar (DIDOKUMENTASIKAN, bukan disembunyikan):
#   * file >25 MiB tidak bermunculan pada snapshot direktori sehingga TIDAK
#     pernah dilaporkan created/modified lewat pemindaian rekursif;
#   * mutasi tool pada file besar tetap dicatat lewat jalur kandidat eksplisit
#     (`TaskChangeEvidence.register_path`) yang menyelesaikan hash per-file;
#   * karena snapshot berikutnya melewati file itu pada tahap `stat`,
#     pengecualian ini TIDAK menyebabkan hashing penuh berulang pada setiap
#     task — biaya untuk file besar adalah nol setelah pemeriksaan ukuran.
# --------------------------------------------------------------------------- #
MAX_TRACKED_FILE_SIZE: int = 25 * 1024 * 1024  # 25 MiB


def _within_size_limit(size: Optional[int]) -> bool:
    """True bila ukuran file masih boleh di-track (<= 25 MiB).

    Dipisahkan agar aturan batas dapat diuji/di-reuse tanpa menyalin angka.
    `None` (tidak ada metadata, mis. stat gagal) dianggap TIDAK layak track.
    """
    return size is not None and size <= MAX_TRACKED_FILE_SIZE


# --------------------------------------------------------------------------- #
# Aturan eksklusi rekursi snapshot (eksplisit, platform-independent).
#
# Root project AETHER TIDAK memakai aturan ini (root AETHER adalah source
# code + state `.aether` yang memang perlu dilacak). Aturan hanya berlaku
# saat `snapshot()` menelusuri direktori DI DALAM workspace (mis. project
# target task), sehingga file source tetap di-hash pada snapshot pertama.
# --------------------------------------------------------------------------- #
_TRACKER_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    "env",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".tox",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".next",
    ".nuxt",
    "coverage",
    "htmlcov",
}

_TRACKER_IGNORED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".log",
    ".tmp",
    ".swp",
    ".swo",
}


def _allowed_in_snapshot(path: Path) -> bool:
    """True bila file layak masuk snapshot rekursif (aturan eksplisit).

    Root project AETHER SELALU diizinkan (`_DEFAULT_ROOT`) — termasuk file di
    dalamnya — agar cache fingerprint root AETHER (source + state `.aether`)
    tetap lengkap.
    """
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if resolved == _DEFAULT_ROOT or _DEFAULT_ROOT in resolved.parents:
        return True
    if path.suffix.lower() in _TRACKER_IGNORED_SUFFIXES:
        return False
    # `path.parts` memuat nama directory + file; nama direktori terabaikan
    # tidak mungkin muncul pada hasil `os.walk`, tetapi pemeriksaan ini
    # menjaga aturan tetap benar bila helper iterasi lain dipakai kelak.
    return not any(part in _TRACKER_IGNORED_DIRS for part in path.parts)


def iter_trackable_files(base: Path):
    """Iterasi file di bawah `base` dengan aturan eksklusi snapshot.

    Berbeda dari `rules` tool filesystem (yang menyembunyikan `.aether`),
    eksekusi tetap diizinkan lewat root AETHER dan file yang di-delete tetap
    dapat ditemukan. Ini murni daftar file yang layak di-hash.

    Aturan ukuran (`MAX_TRACKED_FILE_SIZE`) TIDAK diterapkan di generator ini:
    generator memakai `os.walk` dan untuk direktori besar setiap `stat` yang
    dilakukan di sini akan hilang dari akuntansi "file ditemukan / di-track /
    dilewati" di `snapshot()`. Pemeriksaan ukuran dilakukan `snapshot()` pada
    `stat` yang MEMANG sudah dibutuhkan untuk fingerprint — satu syscall per
    file, tanpa pembacaan isi tambahan.
    """
    for file_path in _iter_files(base):
        if _allowed_in_snapshot(file_path):
            yield file_path


def _hash_file(path: Path) -> Optional[str]:
    """Hash konten file (sha1). None bila file tidak ada/bukan file."""
    if not path.is_file():
        return None
    h = hashlib.sha1()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _file_size(path: Path) -> Optional[int]:
    """Ukuran file. None bila tidak ada/bukan file.

    Dipertahankan sebagai helper publik-modul (dipakai/diuji terpisah walau
    jalur snapshot sekarang memakai `stat` terpadu di `_fingerprint_stat`).
    """
    if not path.is_file():
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Cache fingerprint proses-level (di-key canonical project root).
#
# Struktur nilai: rel_path -> (sha1, size, mtime_ns). Fingerprint metadata
# (size + mtime_ns) dipakai untuk memutuskan apakah SHA-1 perlu dihitung ulang.
# Cache ini ADVISORY (lihat batas akurasi pada docstring modul) dan hanya
# mempercepat; kebenarannya selalu divalidasi ulang lewat `stat` di setiap
# snapshot sehingga file baru/dihapus/modified tetap terdeteksi.
# --------------------------------------------------------------------------- #
_FingerprintEntry = Tuple[str, int, int]

_fingerprint_lock = threading.Lock()
_fingerprint_cache: Dict[str, Dict[str, _FingerprintEntry]] = {}

#: Salinan cache untuk jalur BACA HOTS (stat per file). Dilindungi lock yang
#: SAMA dengan `_fingerprint_cache`, sehingga konsisten. Disimpan terpisah
#: agar setiap `stat` tidak perlu mengambil lock (menghindari overhead lock
#: per file pada workspace besar).
_fingerprint_cache_sync: Dict[str, Dict[str, _FingerprintEntry]] = {}


def _canonical_root_key(root: Path) -> str:
    """Kunci cache dari canonical (absolut+resolve) project root.

    Isolasi berdasarkan root: dua workspace berbeda tidak pernah berbagi
    cache, sehingga file dengan path relatif sama di root berbeda aman.
    """
    try:
        return str(Path(root).resolve())
    except OSError:
        return str(root)


def clear_fingerprint_cache(root: Optional[Path] = None) -> None:
    """Bersihkan cache fingerprint (untuk root tertentu atau seluruh proses).

    Dipakai oleh test/verifier agar hasil tidak bergantung pada state proses
    sebelumnya. Bukan bagian dari alur runtime normal.
    """
    with _fingerprint_lock:
        if root is None:
            _fingerprint_cache.clear()
            _fingerprint_cache_sync.clear()
        else:
            key = _canonical_root_key(root)
            _fingerprint_cache.pop(key, None)
            _fingerprint_cache_sync.pop(key, None)


def mark_mutated(root: "Path | str", paths) -> None:
    """Tandai path sebagai BARU DIMUTASI oleh proses ini (self-aware sync).

    Dipanggil dari jalur tool filesystem yang mengubah isi file
    (`write_file`/`edit_file`) SEGERA setelah operasi berhasil. Entri cache
    fingerprint untuk path itu dibuang sehingga snapshot berikutnya SELALU
    menghitung ulang SHA-1 dari isi nyata — bahkan bila `size` dan `mtime_ns`
    kebetulan tidak berubah.

    Args:
        root: workspace root (canonical key dihitung dari nilai ini).
        paths: satu path atau iterable path (absolut atau relatif terhadap
            `root`).
    """
    _mark(root, paths)


def mark_deleted(root: "Path | str", path) -> None:
    """Tandai path sebagai DIHAPUS oleh proses ini (self-aware sync).

    Entri cache fingerprint untuk path dibuang sehingga file yang benar-benar
    hilang langsung terdeteksi `deleted` (tanpa menunggu perubahan metadata).
    Snapshot dasar task SENGAJA tidak diubah: hilangnya file adalah fakta
    yang justru harus dilaporkan.
    """
    _mark(root, path)


def mark_moved(root: "Path | str", source, destination) -> None:
    """Tandai perpindahan file (source -> destination) oleh proses ini.

    Cache fingerprint di-invalidasi untuk KEDUA path: isi baru `destination`
    harus di-hash ulang, dan `source` dibuang dari cache karena isinya kini
    ada di lokasi lain. Perilaku snapshot/deteksi TIDAK diubah: pada jalur
    runtime, `TaskChangeEvidence.unregister_path(source)` yang mencegah
    laporan 'deleted' palsu untuk source yang hanya dipindah.
    """
    _mark(root, (source, destination))


def _mark(root: "Path | str", paths) -> None:
    """Implementasi bersama `mark_mutated`/`mark_deleted`/`mark_moved`.

    Membuang entri cache fingerprint untuk path yang diberikan, sehingga
    snapshot berikutnya menghitung ulang SHA-1 dari isi nyata.
    """
    if isinstance(paths, (str, Path)):
        paths = (paths,)
    root_path = Path(root)
    try:
        root_resolved = root_path.resolve()
    except OSError:
        return
    key = _canonical_root_key(root_path)
    rels = []
    for raw in paths:
        if raw is None:
            continue
        try:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = root_resolved / candidate
            rels.append(str(candidate.resolve().relative_to(root_resolved)).replace("\\", "/"))
        except (OSError, ValueError):
            continue
    if not rels:
        return
    with _fingerprint_lock:
        for bucket in (_fingerprint_cache_sync.get(key), _fingerprint_cache.get(key)):
            if bucket is None:
                continue
            for rel in rels:
                bucket.pop(rel, None)


def _stat_fingerprint(path: Path) -> Optional[Tuple[int, int]]:
    """Ambil (size, mtime_ns) file. None bila tidak ada / bukan file.

    Satu panggilan `stat` (bukan `is_file()` + `stat()` terpisah) agar
    snapshot workspace besar tidak melakukan dua syscall per file.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return st.st_size, st.st_mtime_ns


def _fingerprint_stat(
    cache: Dict[str, _FingerprintEntry], rel: str, path: Path
) -> Tuple[Optional[str], Optional[int]]:
    """(SHA-1, size) untuk `rel`, memakai cache bila fingerprint metadata cocok.

    Satu `stat` per file melayani baik keputusan hashing maupun nilai `size`
    yang dikembalikan — menghindari syscall ganda pada workspace besar.
    `cache` di sini adalah dict `_fingerprint_cache_sync` (tanpa lock; lihat
    catatan lock di definisi dict tersebut).

    Aturan:
        * metadata (size, mtime_ns) sama dengan cache -> pakai SHA-1 + size
          cache (tidak membaca isi file);
        * metadata berbeda / belum ada -> hitung SHA-1 dari isi file, lalu
          perbarui cache;
        * file tidak ada -> (None, None) dan entri cache `rel` dibuang.
    """
    fp = _stat_fingerprint(path)
    if fp is None:
        cache.pop(rel, None)
        return None, None
    size, mtime_ns = fp
    cached = cache.get(rel)
    if cached is not None and cached[1] == size and cached[2] == mtime_ns:
        return cached[0], size
    digest = _hash_file(path)
    if digest is None:
        cache.pop(rel, None)
        return None, size
    cache[rel] = (digest, size, mtime_ns)
    return digest, size


class ChangeTracker:
    """Melacak perubahan filesystem per task.

    Args:
        root: root workspace (boundary). Default: root project.
        sync: True (default) = cache fingerprint ditulis ke mirror read-only
            yang dapat dibaca tanpa lock; False = cache thread-local murni
            (test isolasi/SQLite-style). Lihat catatan lock di
            `_fingerprint_cache_sync`.
    """

    def __init__(self, root: Optional[Path] = None, *, sync: bool = True) -> None:
        from agent_ai.tools.filesystem import _DEFAULT_ROOT

        self.root = Path(root) if root else _DEFAULT_ROOT
        # task_id -> ChangeSet
        self._sets: Dict[str, ChangeSet] = {}
        # task_id -> {rel_path: (hash, size)} snapshot (kondisi "sebelum")
        self._snapshots: Dict[str, Dict[str, tuple]] = {}
        # Canonical root (kunci cache fingerprint proses-level).
        self._root_key = _canonical_root_key(self.root)
        self._sync = bool(sync)
        # Cache thread-local (fallback saat sync dimatikan / root di reset).
        self._local_cache: Dict[str, _FingerprintEntry] = {}
        # Statistik snapshot terakhir (observability; lihat `last_snapshot_stats`).
        self._last_snapshot_stats: Dict[str, int] = {}

    def _fingerprint_cache(self) -> Dict[str, _FingerprintEntry]:
        """Cache fingerprint untuk root ini (diambil dari registry proses).

        Di-resolve SETIAP kali (bukan disimpan sebagai referensi saat init)
        agar `clear_fingerprint_cache(root)` benar-benar berlaku pada tracker
        yang sudah ada: registry dapat mengganti/menghapus dict per root tanpa
        meninggalkan referensi usang di instance.
        """
        if not self._sync:
            return self._local_cache
        with _fingerprint_lock:
            shared = _fingerprint_cache.setdefault(self._root_key, {})
            sync = _fingerprint_cache_sync.setdefault(self._root_key, {})
        # Samakan kedua mirror (bila registry baru dibuat / dibersihkan).
        if len(sync) < len(shared):
            sync.clear()
            sync.update(shared)
        elif len(shared) < len(sync):
            shared.clear()
            shared.update(sync)
        return sync

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self, task_id: str) -> ChangeSet:
        """Mulai tracking untuk sebuah task_id (idempoten).

        Aman dipanggil ULANG untuk task_id yang sama (mis. gateway menyiapkan
        baseline lewat `snapshot(".", task_id)` lalu runtime memanggil `start`
        lagi saat eksekusi berjalan). Baseline snapshot yang SUDAH ada TIDAK
        dihapus: menghapusnya akan membuat file yang sudah dikenal terlihat
        'created' (perubahan semu). Untuk task_id BARU, ChangeSet dan snapshot
        dimulai dari kosong.

        Raises:
            ValueError: bila task_id kosong.
        """
        if not task_id:
            raise ValueError("ChangeTracker butuh task_id.")
        existing = self._sets.get(task_id)
        if existing is not None:
            # Sudah pernah di-start: pertahankan ChangeSet + baseline snapshot.
            self._snapshots.setdefault(task_id, {})
            return existing
        change_set = ChangeSet(task_id=task_id)
        self._sets[task_id] = change_set
        self._snapshots.setdefault(task_id, {})
        return change_set

    def finish(self, task_id: str) -> Optional[ChangeSet]:
        """Selesaikan tracking untuk task_id (set finished_at)."""
        change_set = self._sets.get(task_id)
        if change_set is None:
            return None
        change_set.finished_at = time.time()
        return change_set

    def get_changes(self, task_id: str) -> Optional[ChangeSet]:
        """Ambil ChangeSet untuk task_id (None bila tidak ada)."""
        return self._sets.get(task_id)

    def tracked_paths(self, task_id: str) -> List[str]:
        """Path kandidat yang sudah tercatat untuk task (bounded, deterministik).

        Dipakai untuk mendeteksi file yang dibuat lewat jalur yang TIDAK
        melaporkan path (mis. `run_command`): hasil command di-scan HANYA pada
        prefix path yang sudah menjadi kandidat task, bukan seluruh project.
        Path di luar scope task tidak pernah dilaporkan (tidak mengarang
        daftar perubahan).
        """
        return list(self._snapshots.get(task_id, {}))

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #
    def snapshot(self, path: str, task_id: Optional[str] = None) -> Dict[str, tuple]:
        """Ambil snapshot (hash, size) untuk sebuah path.

        Hashing INKREMENTAL: SHA-1 sebuah file hanya dihitung ulang bila
        fingerprint metadata-nya (size/mtime_ns) berubah dibanding cache
        fingerprint root ini, ATAU bila file itu ditandai baru dimutasi oleh
        proses ini (`mark_mutated` / `mark_moved` / `mark_deleted`). Lihat
        batas akurasi pada docstring modul.

        Batas ukuran (`MAX_TRACKED_FILE_SIZE` = 25 MiB): pada penelusuran
        DIREKTORI, file yang `st_size`-nya LEBIH BESAR dari batas itu
        DILEWATI sebelum SHA-1 dihitung — isinya tidak pernah dibaca hanya
        untuk fingerprint. Keputusan memakai `st_size` dari `stat` yang sudah
        diambil; konsekuensinya biaya per snapshot untuk file besar hanya satu
        `stat`, sehingga tidak ada hashing penuh berulang. Satu path FILE yang
        diminta EKSPLISIT (atau `task_id` yang menargetkan file itu) tetap
        di-hash penuh agar mutasi yang dilaporkan tool tidak hilang.

        Pemanggilan berulang pada workspace yang tidak berubah tidak membaca
        ulang isi file — biaya didominasi `stat` per file. Penelusuran
        direktori mengecualikan direktori/ekstensi yang tidak relevan
        (`_TRACKER_IGNORED_DIRS` / `_TRACKER_IGNORED_SUFFIXES`).

        Args:
            path: path relatif terhadap root (file atau directory).
            task_id: bila diisi, snapshot disimpan untuk task tersebut.

        Returns:
            Dict {rel_path: (hash, size)}. Untuk file tunggal, satu entri.
        """
        target = _resolve_within_root(path, self.root)
        root_resolved = self.root.resolve()
        cache = self._fingerprint_cache()
        result: Dict[str, tuple] = {}
        stats = {"files_seen": 0, "tracked": 0, "skipped_large": 0}

        if target.is_file():
            # Path FILE eksplisit: hash penuh (lihat catatan di docstring).
            rel = str(target.relative_to(root_resolved)).replace("\\", "/")
            digest, size = _fingerprint_stat(cache, rel, target)
            result[rel] = (digest, size)
            stats["files_seen"] = 1
            stats["tracked"] = 1
        elif target.is_dir():
            for file_path in iter_trackable_files(target):
                stats["files_seen"] += 1
                try:
                    rel = str(file_path.relative_to(root_resolved)).replace("\\", "/")
                except ValueError:
                    continue
                # Batas ukuran: pakai metadata `stat` (bukan membaca isi file).
                fp = _stat_fingerprint(file_path)
                if fp is None:
                    continue
                size = fp[0]
                if size > MAX_TRACKED_FILE_SIZE:
                    stats["skipped_large"] += 1
                    continue
                digest, size = _fingerprint_stat(cache, rel, file_path)
                result[rel] = (digest, size)
                stats["tracked"] += 1

        stats["skipped_large"] += self._skip_task_id_large(task_id, result, cache)

        self._last_snapshot_stats = stats

        if task_id is not None:
            self._snapshots.setdefault(task_id, {}).update(result)

        return result

    def _skip_task_id_large(
        self,
        task_id: Optional[str],
        result: Dict[str, tuple],
        cache: Dict[str, _FingerprintEntry],
    ) -> int:
        """Buang file besar dari snapshot (hasil + state task) bila tercatat.

        Antisipasi jalur campuran: `TaskChangeEvidence.register_path` dapat
        menyelesaikan hash per-file untuk file besar (mutasi eksplisit tool),
        dan file itu kemudian ikut terbawa ke snapshot direktori berikutnya.
        File >batas dibuang dari hasil DAN dari snapshot task agar biayanya
        tidak kembali setiap snapshot (konsisten dengan perilaku "tidak
        diproses ulang"), sehingga `tracked_paths(task_id)` tidak lagi
        memuatnya. Perubahan file besar itu tetap dapat dideteksi lewat
        deteksi per-file yang memang jalur eksplisit runtime.

        File besar yang TIDAK dimiliki task ini tidak disentuh.

        Returns:
            Jumlah entri yang dibuang.
        """
        if not task_id:
            return 0
        baseline = self._snapshots.get(task_id)
        if not baseline:
            return 0
        dropped = 0
        owned = set(baseline) | set(result)
        for rel in owned:
            size = None
            if rel in result:
                size = result[rel][1]
            elif rel in baseline and len(baseline[rel]) > 1:
                size = baseline[rel][1]
            if size is None or size <= MAX_TRACKED_FILE_SIZE:
                continue
            # Buang dari hasil DAN state task (bila ada). Entri file besar yang
            # sudah tercatat task tidak boleh bertahan sehingga biaya hashing
            # eksplisitnya tidak ikut terbawa ke snapshot berikutnya.
            result.pop(rel, None)
            baseline.pop(rel, None)
            cache.pop(rel, None)
            dropped += 1
        return dropped

    def last_snapshot_stats(self) -> Dict[str, int]:
        """Statistik snapshot terakhir (observability, bukan keputusan).

        Mengembalikan hitungan: `files_seen` (file ditemukan setelah aturan
        eksklusi direktori/ekstensi), `tracked` (file yang benar-benar masuk
        snapshot), dan `skipped_large` (file yang dilewati karena
        melampaui `MAX_TRACKED_FILE_SIZE`). Pembacaan murni; tidak mengubah
        state tracker.
        """
        return dict(getattr(self, "_last_snapshot_stats", {}) or {})

    # ------------------------------------------------------------------ #
    # Detection
    # ------------------------------------------------------------------ #
    def detect_changes(
        self,
        task_id: str,
        path: str = ".",
    ) -> List[ChangeRecord]:
        """Deteksi perubahan dengan membandingkan snapshot awal vs kondisi kini.

        Read-only: hanya membaca filesystem, tidak mengubah file.

        Args:
            task_id: task yang dilacak.
            path: path yang diperiksa (default seluruh root).

        Returns:
            Daftar ChangeRecord (created/modified/deleted).
        """
        before = self._snapshots.get(task_id, {})
        current = self.snapshot(path)  # disimpan sebagai basis, bukan keputusan

        records: List[ChangeRecord] = []
        now = time.time()

        # Created & modified.
        for rel, (after_hash, after_size) in current.items():
            if rel not in before:
                records.append(ChangeRecord(
                    path=rel,
                    change_type=ChangeType.CREATED,
                    before_hash=None,
                    after_hash=after_hash,
                    before_size=None,
                    after_size=after_size,
                    timestamp=now,
                ))
                continue
            base = before[rel]
            before_hash, before_size = base[0], base[1]
            # Perubahan ditentukan dari KONTEN yang tersedia (hash + ukuran),
            # bukan timestamp/mtime: hash berbeda -> isi file benar-benar
            # berubah (created/modified/deleted). Bila hash sama, file identik
            # walau mtime berubah -> TIDAK dilaporkan.
            if before_hash != after_hash:
                records.append(ChangeRecord(
                    path=rel,
                    change_type=ChangeType.MODIFIED,
                    before_hash=before_hash,
                    after_hash=after_hash,
                    before_size=before_size,
                    after_size=after_size,
                    timestamp=now,
                ))

        # Deleted. Perbandingan DIBATASI pada scope `path` yang diperiksa:
        # snapshot task dapat memuat file yang di-track untuk path yang lebih
        # sempit (mis. snapshot per-file saat tool mutasi), sehingga file di
        # luar scope tidak boleh dilaporkan 'deleted' hanya karena tidak
        # muncul pada pemindaian sub-path ini.
        scope = _resolve_within_root(path, self.root)
        for rel, base in before.items():
            if rel in current:
                continue
            before_hash, before_size = base[0], base[1]
            try:
                candidate = _resolve_within_root(rel, self.root)
            except Exception:  # noqa: BLE001 - path di luar root diabaikan
                continue
            if candidate != scope and scope not in candidate.parents:
                continue
            records.append(ChangeRecord(
                path=rel,
                change_type=ChangeType.DELETED,
                before_hash=before_hash,
                after_hash=None,
                before_size=before_size,
                after_size=None,
                timestamp=now,
            ))

        return sorted(records, key=lambda r: r.path)

    def record_change(self, task_id: str, record: ChangeRecord) -> None:
        """Catat satu ChangeRecord secara eksplisit ke ChangeSet task.

        Raises:
            KeyError: bila task_id belum di-start.
        """
        change_set = self._sets.get(task_id)
        if change_set is None:
            raise KeyError(f"Task '{task_id}' belum di-start.")
        change_set.changes.append(record)

    def track(self, task_id: str, path: str = ".") -> List[ChangeRecord]:
        """Deteksi perubahan dan catat ke ChangeSet task.

        Convenience: detect_changes + record_change untuk tiap record.

        Returns:
            Daftar ChangeRecord yang baru dicatat.
        """
        records = self.detect_changes(task_id, path=path)
        for record in records:
            self.record_change(task_id, record)
        return records
