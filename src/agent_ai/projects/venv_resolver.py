"""Dynamic discovery virtualenv project + PATH prioritas untuk eksekusi command.

Tujuan:
    Saat task berjalan, project bisa saja TIDAK punya virtualenv di awal
    (hasil discovery = tidak ditemukan). Jika virtualenv dibuat di tengah
    task (mis. `python -m venv test_venv`), command BERIKUTNYA harus
    otomatis memakai interpreter venv tersebut tanpa restart AETHER dan
    tanpa aktivasi manual.

Prinsip:
    - Cache TIDAK pernah "final" saat hasil discovery kosong (None).
      Cache negatif hanya dipakai sebagai pemercekat: sebelum menerima
      cache negatif, resolver melakukan re-scan. Bila venv baru muncul,
      cache langsung di-refresh.
    - Cache positif dipakai langsung (tanpa filesystem scan penuh) selama
      environment tidak berubah. Perubahan dideteksi lewat signature
      ringan (path interpreter + mtime pyvenv.cfg).
    - Prioritas PATH: folder bin venv (Windows: `Scripts`, POSIX: `bin`)
      diletakkan di depan PATH warisan, sehingga `python`/`pip`/`pytest`
      memakai interpreter venv tersebut.
    - Tidak ada dependency baru; murni `pathlib` + `os` + `threading`.
    - Best-effort & aman: kegagalan deteksi menghasilkan `None` (perilaku
      lama: PATH apa adanya), TIDAK pernah melempar exception.

Modul ini read-only terhadap filesystem dan tidak pernah mencetak/menyalin
secret ke mana pun selain dict environment subprocess.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: Nama folder virtualenv konvensi yang dicari di root project, urut prioritas.
_VENV_DIR_NAMES: Tuple[str, ...] = (
    ".venv",
    "venv",
    "env",
    "ENV",
    ".env-venv",
    "virtualenv",
)

#: Folder root yang TIDAK pernah dipindai sebagai kandidat venv.
#: Kandidat non-konvensi (mis. `test_venv`) ditemukan lewat scan entry root,
#: sehingga folder ini harus dikecualikan agar scan tetap murah dan akurat.
_IGNORED_SCAN_DIRS = frozenset({
    "__pycache__",
    "node_modules",
    ".git",
    ".aether",
    "site-packages",
    "src",
    "lib",
    "Lib",
    "dist",
    "build",
})

#: Batas jumlah venv yang diprioritaskan dalam PATH (mencegah PATH terlalu panjang).
_MAX_DISCOVERED_VENVS = 8

#: File penanda virtualenv (PEP 405). Keberadaannya = venv valid.
_PYVENV_CFG = "pyvenv.cfg"


def _bin_dir_name() -> str:
    """Nama folder bin/scripts sesuai platform."""
    return "Scripts" if os.name == "nt" else "bin"


def _python_exe_name() -> str:
    """Nama executable Python sesuai platform."""
    return "python.exe" if os.name == "nt" else "python"


def _candidate_dirs(root: Path) -> List[Path]:
    """Daftar kandidat path virtualenv di `root` (urutan prioritas konvensi)."""
    return [root / name for name in _VENV_DIR_NAMES]


def _is_venv_dir(path: Path) -> bool:
    """True bila `path` Looks like a valid Python virtualenv.

    Kriteria: `path/pyvenv.cfg` ada DAN interpreter Python di
    `path/<Scripts|bin>/python[.exe]` ada. Folder kosong yang baru dibuat
    (mis. `python -m venv` masih berjalan) TIDAK dianggap valid, sehingga
    venv setengah jadi tidak pernah masuk PATH.
    """
    try:
        if not path.is_dir():
            return False
        if not (path / _PYVENV_CFG).is_file():
            return False
        return (path / _bin_dir_name() / _python_exe_name()).is_file()
    except OSError:
        return False


def _discover_venv_dirs(root: Optional[Path]) -> List[Path]:
    """Scan root project untuk folder virtualenv (urutan prioritas).

    Selain nama konvensi, dilakukan scan ringan atas entry root untuk
    menangkap folder custom seperti `test_venv` yang dibuat saat task
    berjalan. Entry yang gagal dibaca (permission) diabaikan.
    """
    if root is None:
        return []

    found: List[Path] = []
    seen: set = set()
    bin_hint = _bin_dir_name().lower()

    def _consider(candidate: Path) -> None:
        try:
            key = str(candidate.resolve()).lower()
        except OSError:
            return
        if key in seen:
            return
        if _is_venv_dir(candidate):
            seen.add(key)
            found.append(candidate)

    # 1) Nama konvensi (prioritas tertinggi, deterministik).
    for candidate in _candidate_dirs(root):
        _consider(candidate)

    # 2) Scan entry root untuk folder venv custom (mis. `test_venv`).
    #    Dibatasi jumlah entry agar root besar tidak mahal.
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        entries = []

    for entry in entries:
        if len(found) >= _MAX_DISCOVERED_VENVS:
            break
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if entry.name in _IGNORED_SCAN_DIRS:
            continue
        # Hemat I/O: hanya folder yang punya subfolder bin/Scripts yang
        # mungkin berisi venv.
        try:
            if not (entry / bin_hint).is_dir():
                continue
        except OSError:
            continue
        _consider(entry)

    return found


def venv_signature(venv_dir: Optional[Path]) -> Tuple[Any, ...]:
    """Signature ringan environment untuk mendeteksi perubahan.

    Returns:
        Tuple hashable; `(None, 0.0)` bila tidak ada venv.
    """
    if venv_dir is None:
        return (None, 0.0)
    try:
        exe = venv_dir / _bin_dir_name() / _python_exe_name()
        cfg = venv_dir / _PYVENV_CFG
        mtime = cfg.stat().st_mtime if cfg.is_file() else 0.0
        return (str(exe), float(mtime))
    except OSError:
        return (str(venv_dir), 0.0)


def prepend_path(env: Dict[str, str], entry: str) -> Dict[str, str]:
    """Sisipkan `entry` di depan PATH pada `env` (idempotent, Windows/POSIX aman).

    Entri PATH yang sudah menunjuk `entry` dihapus lebih dulu supaya tidak
    duplikat. Pada Windows, key `Path` (mixed-case) ikut disinkronkan karena
    beberapa proses membaca `Path` alih-alih `PATH`.
    """
    current = env.get("PATH") or env.get("Path") or ""
    parts: List[str] = [p for p in current.split(os.pathsep) if p]
    try:
        entry_norm = os.path.normcase(os.path.abspath(entry))
        parts = [
            p
            for p in parts
            if os.path.normcase(os.path.abspath(p)) != entry_norm
        ]
    except (OSError, ValueError):
        pass
    env["PATH"] = os.pathsep.join([entry, *parts])
    if os.name == "nt":
        for key in list(env.keys()):
            if key != "PATH" and key.lower() == "path":
                env[key] = env["PATH"]
    return env


class VenvResolver:
    """Resolver virtualenv project dengan cache yang TIDAK pernah final-None.

    Cache negatif (`None`) diperlakukan sebagai "belum ada venv saat ini",
    bukan "selalu tidak ada venv". Sebelum memakai cache negatif, resolver
    melakukan scan ulang; bila venv baru muncul, cache di-refresh dan
    command berikutnya langsung memakainya.

    Cache positif dipakai langsung selama signature environment tidak
    berubah, sehingga tidak ada filesystem scan penuh per command.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self._root: Optional[Path] = Path(root) if root is not None else None
        self._cached: Optional[Path] = None
        self._cached_sig: Tuple[Any, ...] = (None, 0.0)
        self._resolved = False
        # Statistik ringan untuk verifikasi/test (tidak ditulis ke log).
        self.scans = 0
        self.cache_hits = 0

    # ------------------------------------------------------------------ #
    # API publik
    # ------------------------------------------------------------------ #
    @property
    def root(self) -> Optional[Path]:
        """Root project yang di-resolve."""
        return self._root

    def set_root(self, root: Optional[Path]) -> None:
        """Ganti root project dan RESET cache (root baru = environment baru)."""
        with self._lock:
            self._root = Path(root) if root is not None else None
            self._invalidate()

    def invalidate(self) -> None:
        """Paksa refresh pada resolve berikutnya (API eksplisit)."""
        with self._lock:
            self._invalidate()

    def _invalidate(self) -> None:
        self._cached = None
        self._cached_sig = (None, 0.0)
        self._resolved = False

    def resolve(self) -> Optional[Path]:
        """Kembalikan path virtualenv project aktif, atau None.

        Never final-None: bila cache kosong, selalu scan sebelum
        mengembalikan None. Cache hanya dihemat ketika venv sudah ketemu
        DAN signature-nya tidak berubah.
        """
        with self._lock:
            # Cache hit: venv pernah ketemu + signature tidak berubah.
            if self._resolved and self._cached is not None:
                current_sig = venv_signature(self._cached)
                if current_sig == self._cached_sig:
                    self.cache_hits += 1
                    return self._cached
                # Environment berubah -> refresh signature cache sekali.
                self._apply(self._cached, current_sig)
                return self._cached

            # Cache kosong / negatif: JANGAN percaya, lakukan scan.
            found = self._scan()
            if found is not None:
                return found

            # Tetap tidak ada venv. Cache negatif disimpan sebagai
            # pemercepat BUKAN final: resolve berikutnya akan scan lagi.
            self._cached = None
            self._cached_sig = (None, 0.0)
            self._resolved = False
            return None

    def environment_path(self) -> Optional[Path]:
        """Folder bin/Scripts venv (untuk disisipkan ke depan PATH)."""
        venv = self.resolve()
        if venv is None:
            return None
        return venv / _bin_dir_name()

    def resolve_executable(self, name: str) -> Optional[str]:
        """Resolusi nama program terhadap PATH venv-aware.

        Mengembalikan path absolut `name` bila bisa ditemukan DI DALAM
        folder venv (Scripts/bin), tanpa menyentuh PATH sistem. Dipakai
        `RunCommandTool` agar `python` (tanpa ekstensi) ter-resolve ke
        venv interpreter. `None` bila tidak ada di venv (biarkan
        resolusi normal/os).
        """
        venv = self.resolve()
        if venv is None or not name:
            return None
        bin_dir = venv / _bin_dir_name()
        try:
            found = shutil.which(name, path=str(bin_dir))
        except (TypeError, ValueError, OSError):
            found = None
        return found or None

    def build_env(
        self,
        base_env: Optional[Dict[str, str]] = None,
        venv: Optional[Path] = None,
    ) -> Dict[str, str]:
        """Bangun environment subprocess dengan venv di depan PATH.

        Args:
            base_env: environment dasar (default `os.environ`).
            venv: hasil `resolve()` bila caller sudah me-resolve (menghemat
                satu resolve ulang). `None` berarti resolve di sini.

        Returns:
            Dict environment siap pakai untuk subprocess. Bila tidak ada
            venv, kembalikan salinan `base_env` (perilaku lama).
        """
        env = dict(os.environ if base_env is None else base_env)
        if venv is None:
            venv = self.resolve()
        if venv is None:
            return env
        env = prepend_path(env, str(venv / _bin_dir_name()))
        # Set VIRTUAL_ENV saja. JANGAN set PYTHONHOME: pada Windows
        # PYTHONHOME menunjuk folder root venv dan merusak pencarian stdlib
        # (`encodings` tidak ditemukan) karena stdlib tetap milik interpreter
        # basis. Interpreter venv sudah menemukan dirinya sendiri lewat
        # `pyvenv.cfg` + lokasi executable (Scripts/python.exe).
        env["VIRTUAL_ENV"] = str(venv)
        return env

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    def _apply(self, venv: Path, sig: Optional[Tuple[Any, ...]] = None) -> None:
        self._cached = venv
        self._cached_sig = venv_signature(venv) if sig is None else sig
        self._resolved = True

    def _scan(self) -> Optional[Path]:
        """Scan filesystem untuk venv project; return path pertama atau None."""
        self.scans += 1
        dirs = _discover_venv_dirs(self._root)
        if not dirs:
            return None
        chosen = dirs[0]
        self._apply(chosen)
        return chosen


def build_venv_env(
    root: Optional[Path],
    base_env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """One-shot: build env subprocess dengan venv project di depan PATH.

    Dipakai bila caller tidak ingin mengelola cache jangka panjang.
    """
    return VenvResolver(root).build_env(base_env)
