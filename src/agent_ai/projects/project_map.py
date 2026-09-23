"""Project Map foundation: integrasi CODE ATLAS + MAP_CODE_RIG.

Struktur yang dikelola (di ROOT project target):

    <root project target>/
        .aether/
            map/
                atlas.json    # output CODE ATLAS  (project navigation map)
                rig.json      # output MAP_CODE_RIG (Repository Intelligence Graph)

Inti modul:
    - Satu adapter terpusat (`ProjectMapService`) di layer `agent_ai.projects`
      (mengikuti pola project-local `.aether/<subdir>/` seperti github_backup.py).
    - Memakai engine Atlas/RIG yang SUDAH ADA di repo masing-masing
      (TIDAK menyalin / mengimplementasikan ulang engine).
    - Mengecek keberadaan map, memuat map ter-parse, menghitung freshness, dan
      menjalankan generation (satu/paralel) secara atomik.

Termasuk (Task 4 - freshness + refresh terkontrol):
    - Freshness/stale detection DETERMINISTIK berbasis fingerprint source code
      (SHA-256 atas path + isi file `.py`). Metadata kecil per map disimpan di
      `<project>/.aether/map/<type>.meta.json`; isi `atlas.json` / `rig.json`
      TIDAK diubah.
    - Generation PARALEL Atlas + RIG (`generate_maps`) dengan penulisan atomik
      per map; kegagalan satu map tidak merusak map lain / map lama.
    - TIDAK ada auto-regenerate: perubahan source hanya mengubah STATUS menjadi
      `stale`. LLM/Agent yang memutuskan kapan refresh (lihat
      `agent_ai.tools.project_map`).

Belum termasuk (batas modul ini):
    - Modul ini hanya menyediakan service (path/status/load/freshness/generate).
      Query engine & capability LLM berada di layer terpisah
      (`agent_ai.projects.project_map_query`, `agent_ai.tools.project_map`).
    - Tidak ada background generation / auto-refresh.
    - Map TIDAK pernah di-inject otomatis ke context LLM.

Cara AETHER mengakses Atlas/RIG:
    Engine dijalankan sebagai CLI yang SUDAH ADA (subprocess, stdlib-only):

        python <CODE_ATLAS>/atlas.py  <project_path> <output_path>
        python <MAP_CODE_RIG>/rig.py  <project_path> <output_path> --overwrite

    Lokasi engine dapat di-override (urutan prioritas):
        1. argumen konstruktor `atlas_dir` / `rig_dir`
        2. environment variable `AETHER_CODE_ATLAS_DIR` / `AETHER_MAP_CODE_RIG_DIR`
        3. default repo path (relatif root AETHER:
           `<AETHER_ROOT>/vendor/CODE_ATLAS` + `<AETHER_ROOT>/vendor/MAP_CODE_RIG`;
           lihat DEFAULT_ENGINE_DIRS)

    Modul ini TIDAK meng-import engine ke proses AETHER (tidak memodifikasi
    `sys.path`), sehingga engine tetap terisolasi dan tidak bisa membuat
    proses AETHER crash.

Prinsip:
    - Project-local: semua map ditulis di dalam root project target.
    - Additive: hanya menambah subfolder `.aether/map/`; tidak mengubah
      penulisan Bible/log/github yang sudah ada.
    - Idempotent: instansiasi service TIDAK membuat file/folder apa pun.
      Folder `.aether/map/` hanya dibuat saat benar-benar menyimpan map.
    - Tanpa dependency baru.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from agent_ai.projects import scan_policy

#: Nama folder root metadata project (sama dengan aether_store.AETHER_DIR_NAME).
AETHER_DIR_NAME = ".aether"
#: Subfolder hasil Project Map.
MAP_DIR_NAME = "map"

#: Tipe map yang didukung.
MAP_TYPE_ATLAS = "atlas"
MAP_TYPE_RIG = "rig"
#: Urutan tipe map (deterministik).
MAP_TYPES: Tuple[str, ...] = (MAP_TYPE_ATLAS, MAP_TYPE_RIG)

#: Nama file hasil per tipe map (di dalam `.aether/map/`).
MAP_FILE_NAMES: Dict[str, str] = {
    MAP_TYPE_ATLAS: "atlas.json",
    MAP_TYPE_RIG: "rig.json",
}

#: Entry point CLI engine per tipe map (di dalam folder repo engine).
ENGINE_ENTRY_FILES: Dict[str, str] = {
    MAP_TYPE_ATLAS: "atlas.py",
    MAP_TYPE_RIG: "rig.py",
}

#: Environment variable untuk lokasi repo engine (opsional).
ENV_ENGINE_DIRS: Dict[str, str] = {
    MAP_TYPE_ATLAS: "AETHER_CODE_ATLAS_DIR",
    MAP_TYPE_RIG: "AETHER_MAP_CODE_RIG_DIR",
}

#: Root repository AETHER. File ini berada di
#: `src/agent_ai/projects/project_map.py`, sehingga `parents[3]` = root repo.
#: Dipakai agar engine default dihitung RELATIF terhadap repository
#: (tidak ada drive/path machine-specific yang di-hardcode).
_AETHER_ROOT: Path = Path(__file__).resolve().parents[3]

#: Default lokasi repo engine (bila tidak di-override via env/konstruktor).
#: Engine Atlas/RIG dibundel DI DALAM repository AETHER (vendored):
#:     <AETHER_ROOT>/vendor/CODE_ATLAS
#:     <AETHER_ROOT>/vendor/MAP_CODE_RIG
DEFAULT_ENGINE_DIRS: Dict[str, str] = {
    MAP_TYPE_ATLAS: str(_AETHER_ROOT / "vendor" / "CODE_ATLAS"),
    MAP_TYPE_RIG: str(_AETHER_ROOT / "vendor" / "MAP_CODE_RIG"),
}

#: Status keberadaan/validitas map (berbasis file lokal).
STATUS_MISSING = "missing"
STATUS_AVAILABLE = "available"
STATUS_INVALID = "invalid"

#: Status freshness map (apakah map masih merepresentasikan source terbaru).
STATUS_FRESH = "fresh"
STATUS_STALE = "stale"

#: Ekstensi source yang memengaruhi struktur code. Atlas & RIG menganalisis
#: Python, jadi default = `.py`. File non-code TIDAK membuat map stale.
SOURCE_EXTENSIONS: Tuple[str, ...] = (".py",)

#: Folder yang TIDAK dihitung sebagai source (regenerable / non-code / metadata).
#:
#: Satu sumber policy untuk AETHER + engine vendored (Atlas/RIG):
#: `agent_ai.projects.scan_policy`. Daftar ini dipakai untuk menghitung
#: fingerprint freshness dan juga dikirim ke engine lewat environment variable
#: supaya boundary scan Atlas/RIG identik dengan AETHER.
_IGNORED_DIR_NAMES = scan_policy.EXCLUDED_DIR_NAMES

#: Penanda algoritma fingerprint (naikkan bila cara hitung freshness berubah).
FINGERPRINT_ALGORITHM = "sha256-code-v1"

#: Versi metadata freshness.
META_VERSION = 1

#: Nama file metadata freshness per tipe map (di dalam `.aether/map/`).
META_FILE_NAMES: Dict[str, str] = {
    MAP_TYPE_ATLAS: "atlas.meta.json",
    MAP_TYPE_RIG: "rig.meta.json",
}

ProjectPath = Union[str, "os.PathLike[str]"]

#: Lock proses-level untuk operasi FILE MAP (baca utuh + replace atomik).
#:
#: Di Windows, `os.replace` gagal (`WinError 5`) bila file target sedang dibuka
#: pembaca lain. Lock ini hanya melindungi bagian singkat (buka+baca, atau
#: replace) - BUKAN seluruh generation - sehingga engine Atlas/RIG tetap boleh
#: berjalan paralel di luar lock. Ini bukan subsystem locking baru: hanya satu
#: primitive yang menjaga reader selalu melihat file utuh.
_MAP_IO_LOCK = threading.RLock()


def _utc_now_iso() -> str:
    """Timestamp UTC ISO-8601 (detik) untuk metadata freshness."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _atomic_replace(src: str, dst: str, attempts: int = 6, delay: float = 0.03) -> None:
    """`os.replace` dengan retry singkat untuk error transien (Windows).

    Pada Windows, mengganti file yang sedang dibuka pembaca bisa gagal
    sementara (`PermissionError`/`OSError`). Retry bounded membuat concurrent
    refresh + query tetap aman TANPA subsystem locking. Hasil akhir tetap atomik:
    pembaca selalu melihat file lama ATAU file baru yang utuh (rename), tidak
    pernah file setengah tertulis.
    """
    last_error: Optional[BaseException] = None
    for attempt in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:  # pragma: no cover - bergantung timing OS
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(delay * (attempt + 1))
    if last_error is not None:
        raise last_error


class ProjectMapError(Exception):
    """Base error Project Map."""


class InvalidMapTypeError(ProjectMapError):
    """`map_type` tidak dikenal."""


class MapNotFoundError(ProjectMapError):
    """File map belum tersedia (belum digenerate)."""


class MapInvalidError(ProjectMapError):
    """File map ada tetapi tidak dapat dibaca / bukan JSON object valid."""


class MapEngineError(ProjectMapError):
    """Engine Atlas/RIG gagal dijalankan atau tidak menghasilkan output."""


def _normalize_map_type(map_type: str) -> str:
    """Normalisasi + validasi `map_type` (raise bila tidak dikenal)."""
    text = str(map_type).strip().lower()
    if text in MAP_TYPES:
        return text
    raise InvalidMapTypeError(
        "map_type tidak dikenal: {!r} (pilihan: {})".format(
            map_type, ", ".join(MAP_TYPES)
        )
    )


def _resolve_engine_dir(map_type: str, explicit: Optional[ProjectPath]) -> Path:
    """Tentukan folder repo engine (konstruktor > env > default)."""
    if explicit is not None and str(explicit).strip():
        return Path(str(explicit).strip())
    env_value = os.environ.get(ENV_ENGINE_DIRS[map_type], "")
    if isinstance(env_value, str) and env_value.strip():
        return Path(env_value.strip())
    return Path(DEFAULT_ENGINE_DIRS[map_type])


class ProjectMapService:
    """Adapter terpusat Project Map (CODE ATLAS + MAP_CODE_RIG).

    Service ini hanya mengurus lokasi file, status dasar, loading, dan
    pemanggilan engine. Ia tidak menyimpan state global dan tidak membuat
    file/folder saat diinstansiasi.

    Args:
        atlas_dir: folder repo CODE ATLAS (opsional; default env/default).
        rig_dir: folder repo MAP_CODE_RIG (opsional; default env/default).
    """

    def __init__(
        self,
        atlas_dir: Optional[ProjectPath] = None,
        rig_dir: Optional[ProjectPath] = None,
    ) -> None:
        self._engine_dirs: Dict[str, Path] = {
            MAP_TYPE_ATLAS: _resolve_engine_dir(MAP_TYPE_ATLAS, atlas_dir),
            MAP_TYPE_RIG: _resolve_engine_dir(MAP_TYPE_RIG, rig_dir),
        }

    # ------------------------------------------------------------------ #
    # Path helpers
    # ------------------------------------------------------------------ #
    def get_map_dir(self, project_path: ProjectPath) -> Path:
        """Folder penyimpanan map: `<project>/.aether/map/` (tidak dibuat)."""
        return Path(project_path) / AETHER_DIR_NAME / MAP_DIR_NAME

    def get_map_path(self, project_path: ProjectPath, map_type: str) -> Path:
        """Path file map: `<project>/.aether/map/<atlas|rig>.json`."""
        map_type = _normalize_map_type(map_type)
        return self.get_map_dir(project_path) / MAP_FILE_NAMES[map_type]

    def get_meta_path(self, project_path: ProjectPath, map_type: str) -> Path:
        """Path metadata freshness: `<project>/.aether/map/<type>.meta.json`."""
        map_type = _normalize_map_type(map_type)
        return self.get_map_dir(project_path) / META_FILE_NAMES[map_type]

    # ------------------------------------------------------------------ #
    # Freshness (deterministik, tanpa database/index baru)
    # ------------------------------------------------------------------ #
    def _iter_source_files(self, project: Path) -> List[Path]:
        """Daftar file source relevan (deterministik, urut menurut path).

        Memakai policy scan bersama (`agent_ai.projects.scan_policy`):
        directory environment/dependency/cache/build/metadata dipangkas
        sebelum rekursi, symlink/junction tidak ditelusuri, dan traversal
        tidak keluar dari `project`.
        """
        return scan_policy.iter_source_files(project, SOURCE_EXTENSIONS)

    def compute_source_fingerprint(self, project_path: ProjectPath) -> Tuple[str, int]:
        """Fingerprint deterministik dari source code project.

        SHA-256 atas (path relatif + isi) seluruh file source
        (`SOURCE_EXTENSIONS`). Perubahan `.py` (buat/ubah/hapus/rename) mengubah
        fingerprint; perubahan file non-code TIDAK. Tidak ada database/index
        baru - nilai ini hanya dipakai untuk membandingkan freshness map.

        Returns:
            Tuple `(fingerprint_hex, jumlah_file_source)`.
        """
        project = Path(project_path)
        hasher = hashlib.sha256()
        count = 0
        if not project.is_dir():
            return hasher.hexdigest(), 0
        for path in self._iter_source_files(project):
            try:
                rel = path.relative_to(project).as_posix()
            except ValueError:  # pragma: no cover - path di luar root
                rel = path.as_posix()
            hasher.update(rel.encode("utf-8"))
            hasher.update(b"\x00")
            try:
                hasher.update(path.read_bytes())
            except OSError:
                hasher.update(b"<unreadable>")
            hasher.update(b"\x00")
            count += 1
        return hasher.hexdigest(), count

    def load_meta(
        self, project_path: ProjectPath, map_type: str
    ) -> Optional[Dict[str, Any]]:
        """Muat metadata freshness (None bila belum ada / bukan format kita)."""
        path = self.get_meta_path(project_path, map_type)
        if not path.is_file():
            return None
        try:
            with _MAP_IO_LOCK:
                raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("algorithm") != FINGERPRINT_ALGORITHM:
            return None
        return data

    def _write_meta(
        self,
        project_path: ProjectPath,
        map_type: str,
        fingerprint: str,
        file_count: int,
    ) -> None:
        """Tulis metadata freshness secara ATOMIK (temp + os.replace).

        Best-effort: kegagalan metadata tidak boleh menggagalkan map yang sudah
        berhasil ditulis (map akan dianggap stale pada pengecekan berikutnya).
        """
        path = self.get_meta_path(project_path, map_type)
        tmp: Optional[Path] = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": META_VERSION,
                "algorithm": FINGERPRINT_ALGORITHM,
                "fingerprint": fingerprint,
                "file_count": file_count,
                "generated_at": _utc_now_iso(),
            }
            tmp = path.with_name(
                "{}.tmp-{}-{}{}".format(
                    path.stem, os.getpid(), uuid.uuid4().hex, path.suffix or ".json"
                )
            )
            tmp.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
            with _MAP_IO_LOCK:
                _atomic_replace(str(tmp), str(path))
        except OSError:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:  # pragma: no cover
                    pass
            return

    def get_freshness(self, project_path: ProjectPath, map_type: str) -> str:
        """Freshness satu map: `missing` | `fresh` | `stale` | `invalid`.

        - `missing` : file map belum ada.
        - `invalid` : file map ada tetapi tidak terbaca / bukan JSON object.
        - `stale`   : file map valid tetapi fingerprint source sudah berubah
                      (atau belum ada baseline freshness sama sekali).
        - `fresh`   : file map valid dan fingerprint source cocok.

        Operasi READ-ONLY: TIDAK pernah meregenerasi map.
        """
        map_type = _normalize_map_type(map_type)
        path = self.get_map_path(project_path, map_type)
        if not path.is_file():
            return STATUS_MISSING
        try:
            with _MAP_IO_LOCK:
                raw = path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("isi map bukan JSON object")
        except (OSError, ValueError):
            return STATUS_INVALID
        meta = self.load_meta(project_path, map_type)
        if not meta:
            return STATUS_STALE
        current, _ = self.compute_source_fingerprint(project_path)
        if meta.get("fingerprint") == current:
            return STATUS_FRESH
        return STATUS_STALE

    # ------------------------------------------------------------------ #
    # Engine
    # ------------------------------------------------------------------ #
    def get_engine_dir(self, map_type: str) -> Path:
        """Folder repo engine untuk tipe map tertentu."""
        return self._engine_dirs[_normalize_map_type(map_type)]

    def engine_available(self, map_type: str) -> bool:
        """Apakah entry point CLI engine tersedia di folder repo-nya."""
        map_type = _normalize_map_type(map_type)
        entry = self._engine_dirs[map_type] / ENGINE_ENTRY_FILES[map_type]
        return entry.is_file()

    # ------------------------------------------------------------------ #
    # Existence / loading / status
    # ------------------------------------------------------------------ #
    def map_exists(self, project_path: ProjectPath, map_type: str) -> bool:
        """Apakah file map tersedia di disk."""
        return self.get_map_path(project_path, map_type).is_file()

    def load_map(self, project_path: ProjectPath, map_type: str) -> Dict[str, Any]:
        """Muat dan parse file map (read-only, tidak mengubah isi).

        Returns:
            dict hasil `json.loads` dari file map.

        Raises:
            MapNotFoundError: file map belum ada.
            MapInvalidError: file ada tetapi tidak bisa dibaca / bukan JSON
                object.
        """
        map_type = _normalize_map_type(map_type)
        path = self.get_map_path(project_path, map_type)
        if not path.is_file():
            raise MapNotFoundError(
                "Map '{}' belum tersedia: {}".format(map_type, path)
            )
        try:
            with _MAP_IO_LOCK:
                raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MapInvalidError(
                "Gagal membaca map '{}' ({}): {}".format(map_type, path, exc)
            ) from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise MapInvalidError(
                "Map '{}' bukan JSON valid ({}): {}".format(map_type, path, exc)
            ) from exc
        if not isinstance(data, dict):
            raise MapInvalidError(
                "Map '{}' harus berupa JSON object ({})".format(map_type, path)
            )
        return data

    def get_status(self, project_path: ProjectPath) -> Dict[str, Any]:
        """Status map: keberadaan/validitas + freshness (lokal, read-only).

        Returns:
            dict JSON-friendly::

                {
                  "project": "<project path>",
                  "map_dir": "<.aether/map>",
                  "status": "available" | "missing" | "invalid",
                  "freshness": "fresh" | "stale" | "missing" | "invalid",
                  "maps": {
                     "atlas": {
                        "status": "available" | "missing" | "invalid",
                        "freshness": "fresh" | "stale" | "missing" | "invalid",
                        "path": ..., "size": ..., "error": ...,
                        "reason": ..., "tracked": bool, "generated_at": ...,
                     },
                     "rig": {...},
                  },
                  "available": [...], "invalid": [...], "missing": [...],
                  "fresh": [...], "stale": [...],
                }

        `status` = status keberadaan (backward compatible: invalid > available >
        missing). `freshness` = status freshness gabungan (invalid > stale >
        fresh > missing). TIDAK ada isi map di sini.
        """
        map_dir = self.get_map_dir(project_path)
        maps: Dict[str, Dict[str, Any]] = {}
        fingerprint: Optional[str] = None

        for map_type in MAP_TYPES:
            path = self.get_map_path(project_path, map_type)
            entry: Dict[str, Any] = {
                "status": STATUS_MISSING,
                "freshness": STATUS_MISSING,
                "path": str(path),
                "size": None,
                "error": None,
                "reason": None,
                "tracked": False,
                "generated_at": None,
            }
            if path.is_file():
                try:
                    with _MAP_IO_LOCK:
                        raw = path.read_text(encoding="utf-8")
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict):
                        raise ValueError("isi map bukan JSON object")
                except (OSError, ValueError) as exc:
                    entry["status"] = STATUS_INVALID
                    entry["freshness"] = STATUS_INVALID
                    entry["error"] = str(exc)
                    try:
                        entry["size"] = path.stat().st_size
                    except OSError:
                        entry["size"] = None
                else:
                    entry["status"] = STATUS_AVAILABLE
                    entry["size"] = len(raw.encode("utf-8"))
                    meta = self.load_meta(project_path, map_type)
                    if not meta:
                        entry["freshness"] = STATUS_STALE
                        entry["reason"] = (
                            "belum ada baseline freshness (map belum digenerate "
                            "AETHER versi ini)"
                        )
                    else:
                        entry["tracked"] = True
                        entry["generated_at"] = meta.get("generated_at")
                        if fingerprint is None:
                            fingerprint, _ = self.compute_source_fingerprint(
                                project_path
                            )
                        if meta.get("fingerprint") == fingerprint:
                            entry["freshness"] = STATUS_FRESH
                        else:
                            entry["freshness"] = STATUS_STALE
                            entry["reason"] = "source code berubah sejak map digenerate"
            maps[map_type] = entry

        available = [t for t in MAP_TYPES if maps[t]["status"] == STATUS_AVAILABLE]
        invalid = [t for t in MAP_TYPES if maps[t]["status"] == STATUS_INVALID]
        missing = [t for t in MAP_TYPES if maps[t]["status"] == STATUS_MISSING]
        fresh = [t for t in MAP_TYPES if maps[t]["freshness"] == STATUS_FRESH]
        stale = [t for t in MAP_TYPES if maps[t]["freshness"] == STATUS_STALE]

        if invalid:
            overall = STATUS_INVALID
        elif available:
            overall = STATUS_AVAILABLE
        else:
            overall = STATUS_MISSING

        if invalid:
            overall_freshness = STATUS_INVALID
        elif stale:
            overall_freshness = STATUS_STALE
        elif fresh:
            overall_freshness = STATUS_FRESH
        else:
            overall_freshness = STATUS_MISSING

        return {
            "project": str(Path(project_path)),
            "map_dir": str(map_dir),
            "status": overall,
            "freshness": overall_freshness,
            "maps": maps,
            "available": available,
            "invalid": invalid,
            "missing": missing,
            "fresh": fresh,
            "stale": stale,
        }

    # ------------------------------------------------------------------ #
    # Generation (memakai engine yang sudah ada; sinkron, bukan background)
    # ------------------------------------------------------------------ #
    def generate_map(self, project_path: ProjectPath, map_type: str) -> Path:
        """Generate satu map via engine Atlas/RIG dan simpan ke `.aether/map/`.

        Engine dijalankan sebagai CLI yang sudah ada (subprocess). Folder
        `.aether/map/` dibuat di sini (bukan saat inisialisasi service).

        Penulisan bersifat ATOMIK (temporary file di folder yang sama +
        `os.replace`): bila engine gagal / tidak menghasilkan output, map lama
        yang sudah ada TIDAK rusak dan file sementara dibersihkan.

        Setelah sukses, metadata freshness (`<type>.meta.json`) ditulis dengan
        fingerprint source yang dihitung SEBELUM engine dijalankan - sehingga
        bila source berubah saat generation berjalan, map akan terdeteksi
        `stale` pada pengecekan berikutnya (aman/konservatif).

        Args:
            project_path: root project yang akan dipetakan.
            map_type: "atlas" atau "rig".

        Returns:
            Path file map hasil.

        Raises:
            ProjectMapError: project path bukan directory.
            MapEngineError: engine tidak ditemukan atau gagal menghasilkan map.
        """
        map_type = _normalize_map_type(map_type)
        project = Path(project_path)
        if not project.is_dir():
            raise ProjectMapError(
                "Project path bukan directory: {}".format(project)
            )

        engine_dir = self._engine_dirs[map_type]
        entry = engine_dir / ENGINE_ENTRY_FILES[map_type]
        if not entry.is_file():
            raise MapEngineError(
                "Engine '{}' tidak ditemukan: {}".format(map_type, entry)
            )

        out_path = self.get_map_path(project_path, map_type)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Engine menulis ke file sementara (suffix tetap .json); map lama baru
        # diganti setelah engine sukses.
        tmp_path = out_path.with_name(
            "{}.tmp-{}-{}{}".format(
                out_path.stem, os.getpid(), uuid.uuid4().hex, out_path.suffix or ".json"
            )
        )

        cmd: List[str] = [
            sys.executable,
            str(entry.resolve()),
            str(project.resolve()),
            str(tmp_path.resolve()),
        ]
        if map_type == MAP_TYPE_RIG:
            cmd.append("--overwrite")

        # Fingerprint source dihitung SEBELUM engine dijalankan.
        fingerprint, file_count = self.compute_source_fingerprint(project)

        # Boundary scan yang sama diteruskan ke engine vendored (Atlas/RIG).
        # Satu sumber policy: `agent_ai.projects.scan_policy`.
        engine_env = os.environ.copy()
        try:
            engine_env[scan_policy.AETHER_SCAN_POLICY_ENV] = json.dumps(
                scan_policy.to_payload(), ensure_ascii=True
            )
            engine_env[scan_policy.AETHER_SCAN_POLICY_PATH_ENV] = str(
                scan_policy.module_path()
            )
        except (TypeError, ValueError, OSError):
            pass

        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, env=engine_env
            )
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise MapEngineError(
                "Gagal menjalankan engine '{}': {}".format(map_type, exc)
            ) from exc

        if completed.returncode != 0 or not tmp_path.is_file():
            detail = (completed.stderr or completed.stdout or "").strip()
            tmp_path.unlink(missing_ok=True)
            raise MapEngineError(
                "Engine '{}' gagal (exit {}): {}".format(
                    map_type, completed.returncode, detail[:600] or "tanpa detail"
                )
            )

        try:
            with _MAP_IO_LOCK:
                _atomic_replace(str(tmp_path), str(out_path))
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise MapEngineError(
                "Gagal menyimpan map '{}' ke {}: {}".format(map_type, out_path, exc)
            ) from exc

        # Catat freshness baseline (best-effort, tidak menggagalkan generation).
        self._write_meta(project_path, map_type, fingerprint, file_count)
        return out_path

    def _generate_one(
        self, project_path: ProjectPath, map_type: str
    ) -> Dict[str, Any]:
        """Generate satu map dan bungkus hasil/errornya (TIDAK raise)."""
        try:
            path = self.generate_map(project_path, map_type)
        except Exception as exc:  # noqa: BLE001 - hasil per map, bukan kegagalan total
            return {
                "map": map_type,
                "ok": False,
                "path": None,
                "size": None,
                "error": "{}: {}".format(type(exc).__name__, exc),
            }
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        return {"map": map_type, "ok": True, "path": str(path), "size": size, "error": None}

    def generate_maps(
        self,
        project_path: ProjectPath,
        map_types: Sequence[str],
        parallel: bool = True,
    ) -> Dict[str, Dict[str, Any]]:
        """Generate beberapa map sekaligus (opsional PARALEL).

        Setiap map ditulis atomik dan independen: kegagalan satu map TIDAK
        menghapus / merusak map lain maupun map lama. Method ini TIDAK raise
        untuk kegagalan per map - hasil setiap tipe dikembalikan sebagai dict
        (`{"ok": bool, "path": ..., "size": ..., "error": ...}`).

        Args:
            project_path: root project.
            map_types: daftar tipe map ("atlas"/"rig").
            parallel: bila True dan lebih dari satu map, generate bersamaan
                (tiap engine berjalan sebagai subprocess terpisah).

        Returns:
            dict berurutan sesuai `map_types`: {"atlas": {...}, "rig": {...}}.
        """
        types = [_normalize_map_type(t) for t in map_types]
        if not types:
            return {}
        results: Dict[str, Dict[str, Any]] = {}
        if parallel and len(types) > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=len(types)) as pool:
                futures = {
                    pool.submit(self._generate_one, project_path, t): t for t in types
                }
                for future in as_completed(futures):
                    map_type = futures[future]
                    results[map_type] = future.result()
        else:
            for map_type in types:
                results[map_type] = self._generate_one(project_path, map_type)
        return {t: results[t] for t in types}


__all__ = [
    "ProjectMapService",
    "ProjectMapError",
    "InvalidMapTypeError",
    "MapNotFoundError",
    "MapInvalidError",
    "MapEngineError",
    "MAP_TYPE_ATLAS",
    "MAP_TYPE_RIG",
    "MAP_TYPES",
    "MAP_FILE_NAMES",
    "META_FILE_NAMES",
    "AETHER_DIR_NAME",
    "MAP_DIR_NAME",
    "SOURCE_EXTENSIONS",
    "FINGERPRINT_ALGORITHM",
    "META_VERSION",
    "STATUS_MISSING",
    "STATUS_AVAILABLE",
    "STATUS_INVALID",
    "STATUS_FRESH",
    "STATUS_STALE",
]
