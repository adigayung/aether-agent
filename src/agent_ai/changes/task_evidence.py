"""Change Evidence per task: adapter TIPIS di atas ChangeTracker yang sudah ada.

Modul ini TIDAK membuat tracker kedua. Ia hanya menyatukan tiga hal yang
sebelumnya terpisah:

1. **Snapshot** isi workspace saat task dimulai (`ChangeTracker.snapshot`).
2. **Deteksi perubahan** (created/modified/deleted) yang DIBATASI pada path
   yang memang disentuh task — bukan full-project hashing pada setiap round.
3. **Ringkasan ringkas** (compact) hasil deteksi untuk continuous loop
   (`AgentOrchestrator`) dan review berbasis evidence.

Desain:

    `ChangeTracker` (existing)  ->  hashing + snapshot per task_id
    `TaskChangeEvidence`        ->  target path + kapan deteksi dijalankan
    `summary()`                 ->  daftar file ringkas (path + kind + sizes)

Jaminan:
    - Isolasi per `task_id`: satu instance menangani banyak task tanpa
      tercampur (snapshot/target/records di-key oleh `task_id`).
    - Read-only: tidak pernah menulis/mengubah file project.
    - Bounded: deteksi hanya menyentuh path yang terdaftar sebagai kandidat
      (di-register dari aktivitas tool mutasi/command), sehingga biaya selalu
      proporsional terhadap aktivitas task, bukan ukuran project.
    - Tidak mengarang data: `kind` berasal dari perbandingan hash/size
      (`ChangeType`), dan metadata only berisi field yang benar-benar tersedia.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_ai.changes.models import ChangeRecord, ChangeSet, ChangeType
from agent_ai.changes.tracker import ChangeTracker

#: Jenis record yang dianggap "hilang" bila file yang dilaporkan tool mutasi
#: sudah tidak ada saat deteksi dijalankan (mis. dipindah lalu dihapus).
_STATUS_ORDER = {
    ChangeType.CREATED.value: "created",
    ChangeType.MODIFIED.value: "modified",
    ChangeType.DELETED.value: "deleted",
}


def normalize_change_path(path: Any) -> str:
    """Normalisasi path menjadi relative-posix (selaras dengan event live).

    Hanya menyamakan BENTUK path (backslash -> slash, buang prefix './');
    tidak mengubah makna path.
    """
    if not path:
        return ""
    text = str(path).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip()


class TaskChangeEvidence:
    """Bukti perubahan file untuk satu atau banyak task (per `task_id`).

    Args:
        tracker: `ChangeTracker` existing. WAJIB diberikan — kelas ini tidak
            pernah membuat tracker sendiri (tidak ada change index kedua).
        task_id: task yang dilacak. Bila diisi, instance ini terikat pada satu
            task; `begin()` cukup dipanggil tanpa argumen.
    """

    def __init__(
        self,
        tracker: ChangeTracker,
        task_id: Optional[str] = None,
    ) -> None:
        if tracker is None:
            raise ValueError("TaskChangeEvidence butuh ChangeTracker existing.")
        self._tracker = tracker
        self._task_id = task_id
        # task_id -> daftar path (file atau directory) yang menjadi kandidat.
        self._targets: Dict[str, List[str]] = {}
        # task_id -> {path: ChangeRecord} hasil deteksi terakhir.
        self._records: Dict[str, Dict[str, ChangeRecord]] = {}
        # task_id -> jumlah deteksi (observability saja, bukan keputusan).
        self._detections: Dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # Identitas task
    # ------------------------------------------------------------------ #
    @property
    def task_id(self) -> Optional[str]:
        """task_id yang diikat ke instance ini (None bila per-call)."""
        return self._task_id

    def _resolve_task_id(self, task_id: Optional[str]) -> str:
        """Tentukan task_id efektif (raise bila tidak ada)."""
        effective = task_id or self._task_id
        if not effective:
            raise ValueError("TaskChangeEvidence butuh task_id.")
        return effective

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def begin(self, task_id: Optional[str] = None, *paths: str) -> None:
        """Mulai task: buat ChangeSet + snapshot path awal (bounded).

        Snapshot awal HANYA untuk path yang diberikan (biasanya tidak ada),
        karena deteksi berbasis kandidat: file yang belum dikenal dianggap
        CREATED. Root `.` TIDAK di-snapshot di sini agar tidak ada
        full-project hashing saat task dimulai.

        Bila instance dipakai ULANG untuk beberapa task (mis. satu AgentRuntime
        menjalankan task berurutan) dan `task_id` diberikan, instance ini
        REBIND ke task tersebut sehingga `task_id` selalu menunjuk task yang
        sedang berjalan — bukan task pertama. Isolasi antar-task tetap dijaga:
        state tiap task_id disimpan terpisah (tidak saling mencemari).

        Best-effort: kegagalan snapshot tidak menggagalkan task.
        """
        effective = self._resolve_task_id(task_id)
        if task_id:
            # Rebind instance ke task yang sedang berjalan (reuse runtime).
            self._task_id = effective
        self._tracker.start(effective)
        self._targets[effective] = []
        self._records[effective] = {}
        self._detections[effective] = 0
        for path in paths:
            self.register_path(path, task_id=effective)

    def register_path(
        self,
        path: Any,
        task_id: Optional[str] = None,
        *,
        snapshot_baseline: bool = True,
    ) -> None:
        """Catat path sebagai kandidat perubahan + snapshot kondisi SEKARANG.

        Dipanggil dari aktivitas NYATA Agent (tool mutasi / command yang
        menulis file). Snapshot kondisi "sebelum intervensi" diambil di sini
        sehingga deteksi berikutnya tahu apakah file dibuat atau diubah.

        Snapshot juga di-refresh saat path didaftarkan ULANG (mis. operasi
        berikutnya pada file yang sama), sehingga basis perbandingan selalu
        kondisi terakhir yang terdeteksi — bukan kondisi task dimulai.

        Args:
            path: path kandidat (relatif root).
            task_id: task yang dilacak (opsional; default instance).
            snapshot_baseline: False untuk kandidat yang BARU ditemukan pada
                kondisi yang sudah berubah (mis. file yang dibuat `run_command`).
                Dalam kasus itu kondisi "sebelum" memang tidak diketahui,
                sehingga TIDAK diadakan baseline dan deteksi berikutnya
                melaporkannya sebagai CREATED — bukan modified palsu.

        Best-effort: path di luar root / tidak valid diabaikan.
        """
        rel = normalize_change_path(path)
        if not rel:
            return
        effective = self._resolve_task_id(task_id)
        targets = self._targets.setdefault(effective, [])
        if rel not in targets:
            targets.append(rel)
        if not snapshot_baseline:
            # Kandidat yang BARU ditemukan (mis. file yang dibuat `run_command`)
            # tidak diberi baseline: kondisi "sebelum" memang tidak diketahui
            # (file belum ada saat task berjalan), sehingga deteksi berikutnya
            # melaporkannya sebagai CREATED — bukan modified palsu.
            return
        try:
            self._tracker.snapshot(rel, task_id=effective)
        except Exception:  # noqa: BLE001 - snapshot tidak boleh crash task
            return

    def finish(self, task_id: Optional[str] = None) -> Optional[ChangeSet]:
        """Tutup tracking task (set `finished_at`)."""
        effective = self._resolve_task_id(task_id)
        return self._tracker.finish(effective)

    def unregister_path(self, path: Any, task_id: Optional[str] = None) -> None:
        """Hentikan tracking path yang tidak lagi dipantau task.

        Dipakai dua kasus nyata:
            * mutasi tool GAGAL -> path tidak boleh dilaporkan berubah;
            * `move_file` -> source berhenti dipantau (bukan 'deleted').

        Records hasil deteksi untuk path itu dibuang agar tidak ada laporan
        perubahan semu. Snapshot dasar di ChangeTracker dibiarkan apa adanya
        (read-only; tidak ada penghapusan state tracker di luar API-nya).
        """
        rel = normalize_change_path(path)
        if not rel:
            return
        effective = self._resolve_task_id(task_id)
        targets = self._targets.get(effective)
        if targets and rel in targets:
            targets.remove(rel)
        self._forget_records(effective, rel)

    def _forget_records(self, task_id: str, rel: str) -> None:
        """Buang records (dan sub-path) untuk satu path dari daftar hasil."""
        records = self._records.get(task_id)
        if not records:
            return
        prefix = rel + "/"
        for key in list(records.keys()):
            if key == rel or key.startswith(prefix):
                records.pop(key, None)

    def discover(
        self, task_id: Optional[str] = None, *, limit: int = 200
    ) -> List[str]:
        """Catat kandidat BARU dari isi directory root (bounded, sekali).

        Dipakai HANYA ketika evidence benar-benar diminta (mis. review akhir
        task), untuk menangkap file yang dibuat lewat jalur yang tidak
        melaporkan path (mis. script yang menulis file baru). Hasil dibatasi
        `limit` path dan langsung disnapshot agar perubahan tercatat.

        Bukan hashing seluruh project: murni listing + snapshot kandidat.
        """
        effective = self._resolve_task_id(task_id)
        root = Path(self._tracker.root)
        known = set(self._tracker._snapshots.get(effective, {}))  # noqa: SLF001 - API tracker existing
        found: List[str] = []
        try:
            from agent_ai.tools.filesystem import _iter_files

            for file_path in _iter_files(root):
                if len(found) >= limit:
                    break
                try:
                    rel = str(file_path.relative_to(root.resolve()))
                except ValueError:
                    continue
                rel = normalize_change_path(rel)
                if not rel or rel in known:
                    continue
                found.append(rel)
        except Exception:  # noqa: BLE001 - discovery tidak boleh crash task
            found = []
        for rel in found:
            self.register_path(rel, task_id=effective)
        return found

    def discover_from_candidates(
        self, task_id: Optional[str] = None, *, limit: int = 200
    ) -> List[str]:
        """Catat kandidat BARU di SCOPE kandidat task yang sudah ada (bounded).

        Dipakai ketika hasil `run_command` tidak melaporkan path apa pun: file
        baru yang ditulis command hanya dapat ditangkap dengan memindai
        directory yang MEMANG sudah menjadi scope task (parent dari kandidat
        yang sudah terdaftar). Ini TIDAK memindai seluruh project — hanya
        directory kandidat — sehingga biaya tetap proporsional terhadap
        aktivitas task, bukan ukuran project.

        Bila task belum punya kandidat apa pun, TIDAK ada yang dipindai dan
        TIDAK ada perubahan yang dikarang (cakupan command tidak diketahui).

        Returns:
            Daftar path relatif baru yang baru terdaftar (mungkin kosong).
        """
        effective = self._resolve_task_id(task_id)
        known = set(self._tracker._snapshots.get(effective, {}))  # noqa: SLF001 - API tracker existing
        # Scope = parent directory dari kandidat yang sudah terdaftar. Kandidat
        # berupa file -> pakai parent-nya; kandidat berupa directory -> dirinya.
        scopes: List[str] = []
        for rel in known:
            candidate = (rel or "").strip("/")
            if not candidate:
                continue
            # Path kandidat yang BUKAN directory (mis. "app.py") -> pakai root.
            # Path di dalam subdirectory -> pakai subdirectory tersebut.
            parts = candidate.split("/")
            parent = "/".join(parts[:-1])
            scope = parent if parent else "."
            if scope not in scopes:
                scopes.append(scope)
        if not scopes:
            return []
        found: List[str] = []
        try:
            from agent_ai.tools.filesystem import _iter_files, _resolve_within_root

            root = Path(self._tracker.root)
            for scope in scopes:
                if len(found) >= limit:
                    break
                try:
                    base = _resolve_within_root(scope, root)
                except Exception:  # noqa: BLE001 - scope invalid -> lewati
                    continue
                if not base.is_dir():
                    continue
                for file_path in _iter_files(base):
                    if len(found) >= limit:
                        break
                    try:
                        rel = str(file_path.relative_to(root.resolve()))
                    except ValueError:
                        continue
                    rel = normalize_change_path(rel)
                    if not rel or rel in known:
                        continue
                    found.append(rel)
        except Exception:  # noqa: BLE001 - discovery tidak boleh crash task
            found = []
        for rel in found:
            self.register_path(rel, task_id=effective, snapshot_baseline=False)
        return found

    # ------------------------------------------------------------------ #
    # Deteksi
    # ------------------------------------------------------------------ #
    def detect(self, task_id: Optional[str] = None) -> List[ChangeRecord]:
        """Deteksi perubahan pada path yang DICATAT saja (bounded).

        Untuk setiap path kandidat:
            * directory -> deteksi rekursif di dalamnya;
            * file      -> deteksi tepat pada file tersebut.

        Records di-refresh (bukan hanya ditambahkan) untuk setiap path
        kandidat: hasil lama dibuang lebih dulu sehingga perubahan yang sudah
        tidak berlaku (mis. file dikembalikan ke isi semula, atau file yang
        dilaporkan tool ternyata tidak ada) TIDAK dilaporkan sebagai fakta
        usang. Deteksi berulang aman (idempoten).

        Returns:
            Daftar ChangeRecord terurut path (fakta filesystem, read-only).
        """
        effective = self._resolve_task_id(task_id)
        records = self._records.setdefault(effective, {})
        for rel in list(self._targets.get(effective, [])):
            try:
                found = self._tracker.detect_changes(effective, path=rel)
            except Exception:  # noqa: BLE001 - deteksi tidak boleh crash task
                continue
            self._forget_records(effective, rel)
            for record in found:
                records[normalize_change_path(record.path)] = record
        self._detections[effective] = self._detections.get(effective, 0) + 1
        return self.records(effective)

    def records(self, task_id: Optional[str] = None) -> List[ChangeRecord]:
        """Records terdeteksi sampai sekarang, terurut path (tanpa deteksi)."""
        effective = task_id or self._task_id
        if not effective:
            return []
        return sorted(
            self._records.get(effective, {}).values(), key=lambda r: r.path
        )

    @property
    def detection_count(self) -> int:
        """Jumlah deteksi yang sudah dijalankan untuk task yang diikat."""
        if not self._task_id:
            return 0
        return self._detections.get(self._task_id, 0)

    # ------------------------------------------------------------------ #
    # Ringkasan (untuk continuous loop + review)
    # ------------------------------------------------------------------ #
    def summary(self, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Ringkasan ringkas per file: path, kind, dan metadata yang ADA.

        Field disertakan HANYA bila nilainya benar-benar terukur (hash/size
        dari `ChangeRecord`). Tidak ada jenis perubahan atau hasil validasi
        yang dikarang.
        """
        effective = task_id or self._task_id
        if not effective:
            return []
        items: List[Dict[str, Any]] = []
        for record in self.records(effective):
            kind = _STATUS_ORDER.get(
                getattr(record.change_type, "value", str(record.change_type)),
                str(record.change_type),
            )
            item: Dict[str, Any] = {"path": normalize_change_path(record.path), "kind": kind}
            if record.before_size is not None:
                item["before_size"] = record.before_size
            if record.after_size is not None:
                item["after_size"] = record.after_size
            if record.before_hash is not None:
                item["before_hash"] = record.before_hash
            if record.after_hash is not None:
                item["after_hash"] = record.after_hash
            items.append(item)
        return items

    def counts(self, task_id: Optional[str] = None) -> Dict[str, int]:
        """Jumlah file created/modified/deleted yang benar-benar terdeteksi."""
        result = {"created": 0, "modified": 0, "deleted": 0}
        for item in self.summary(task_id):
            kind = item.get("kind")
            if kind in result:
                result[kind] += 1
        return result

    def text(self, task_id: Optional[str] = None) -> str:
        """Ringkasan teks pendek (untuk konteks review/evidence).

        Kosong bila belum ada perubahan terdeteksi — pemanggil TIDAK boleh
        menyimpulkan apa pun dari string kosong selain "belum ada perubahan
        yang terdeteksi".
        """
        effective = task_id or self._task_id
        if not effective:
            return ""
        items = self.summary(effective)
        if not items:
            return ""
        counts = self.counts(effective)
        lines = [
            "Perubahan file terdeteksi untuk task ini (fakta filesystem):",
            (
                f"- total={len(items)} created={counts['created']} "
                f"modified={counts['modified']} deleted={counts['deleted']}"
            ),
        ]
        for item in items:
            detail = ""
            if item.get("before_size") is not None or item.get("after_size") is not None:
                detail = f" (size {item.get('before_size')} -> {item.get('after_size')})"
            lines.append(f"- {item['path']}: {item['kind']}{detail}")
        return "\n".join(lines)


__all__ = ["TaskChangeEvidence", "normalize_change_path"]
