#!/usr/bin/env python
"""AETHER installer + bootstrap resmi (helper untuk run.bat).

Skrip ini HANYA memakai standard library sehingga dapat dijalankan SEBELUM
dependency AETHER terpasang. Tujuannya membuat distribusi AETHER ramah-user:
cukup jalankan run.bat, sisanya (venv, dependency, frontend build) disiapkan
di sini secara otomatis.

Tanggung jawab:
    1. Verifikasi prasyarat (Python >= 3.10, git tersedia).
    2. Siapkan virtual environment portable di <root>/venv.
    3. Pastikan dependency runtime terpasang (pip install -r requirements.txt).
    4. Pastikan file konfigurasi .env ada (disalin dari template; TANPA credential).
    5. Pastikan frontend production build ada (web/frontend/dist), build bila perlu.
    6. Jalankan AETHER (Django Gateway) di http://127.0.0.1:8000/.

Sifat IDEMPOTENT: aman dijalankan berulang. Langkah yang sudah selesai
dilewati (dicek dulu), sehingga menjalankan ulang tidak merusak instalasi.

MODE SIMULASI (--simulate): dry-run OFFLINE. Seluruh alur dicetak sebagai
rencana berpenanda state (SKIP / AKAN) TANPA efek samping apa pun: tidak
menjalankan git clone, `python -m venv`, `pip install`, `npm install`,
`vite build`, maupun `manage.py runserver`; tidak membuka browser; dan tidak
menulis/menyalin file apa pun (termasuk .env). Mode ini hanya membaca state
(keberadaan file/folder, `shutil.which`, `sys.version_info`, cek port via
socket) sehingga aman dijalankan di lingkungan tanpa koneksi internet.
Kapitalisasi flag ditoleransi (mis. `--SIMULATE`), karena gate simulasi di
run.bat mencocokkan argumen secara case-insensitive.

Batasan arsitektur yang dipertahankan:
    - Entry backend tetap web/django_app/manage.py.
    - Frontend tetap dibangun dari web/frontend memakai Vite; build memakai
      `node node_modules/vite/bin/vite.js build` (tidak bergantung pada npm
      script) sesuai konvensi proyek.
    - Tidak ada credential yang ditulis ke file mana pun.
    - Tidak meng-install Python/Node secara otomatis; hanya memberi pesan jelas.
    - Tidak ada path machine-specific (semua path relatif ke root instalasi).

Jalankan (biasanya lewat run.bat):
    python scripts/install_aether.py
    python scripts/install_aether.py --check          # verifikasi saja
    python scripts/install_aether.py --no-launch      # setup saja
    python scripts/install_aether.py --simulate       # dry-run offline (tanpa unduhan/mutasi)
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------------------
# Konstanta (portable; JANGAN hardcode path mesin)
# ---------------------------------------------------------------------------
MIN_PYTHON = (3, 10)
REPO_URL = "https://github.com/adigayung/aether-agent.git"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
LOG_PREFIX = "[AETHER]"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def info(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", flush=True)


def warn(message: str) -> None:
    print(f"{LOG_PREFIX} WARNING: {message}", flush=True)


def error(message: str) -> None:
    print(f"{LOG_PREFIX} ERROR: {message}", flush=True)


# ---------------------------------------------------------------------------
# Path helpers (semua relatif ke root instalasi)
# ---------------------------------------------------------------------------
def project_root_default() -> Path:
    """Root AETHER = parent dari folder scripts/ (lokasi file ini)."""
    return Path(__file__).resolve().parent.parent


def venv_dir(root: Path) -> Path:
    return root / "venv"


def venv_python(root: Path) -> Path:
    """Path python di dalam venv (Windows: Scripts, POSIX: bin)."""
    win_python = venv_dir(root) / "Scripts" / "python.exe"
    if win_python.exists():
        return win_python
    posix_python = venv_dir(root) / "bin" / "python"
    if posix_python.exists():
        return posix_python
    # Belum dibuat: default Windows.
    return win_python


def requirements_file(root: Path) -> Path:
    return root / "requirements.txt"


def frontend_dir(root: Path) -> Path:
    return root / "web" / "frontend"


def frontend_dist_index(root: Path) -> Path:
    return frontend_dir(root) / "dist" / "index.html"


def django_app_dir(root: Path) -> Path:
    return root / "web" / "django_app"


def _deps_stamp(venv: Path) -> Path:
    return venv / ".aether_deps_stamp"


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------
def _run_command(argv: list[str], cwd: Path | None = None, env: dict | None = None):
    """Jalankan executable. Batch/CMD script di Windows dirutekan lewat cmd.exe.

    hal ini diperlukan karena CreateProcess tidak dapat mengeksekusi file
    .cmd/.bat secara langsung (mis. npm.CMD).
    """
    exe = argv[0]
    suffix = Path(exe).suffix.lower()
    if os.name == "nt" and suffix in (".cmd", ".bat"):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return subprocess.run([comspec, "/c", exe, *argv[1:]], cwd=str(cwd) if cwd else None, env=env)
    return subprocess.run(argv, cwd=str(cwd) if cwd else None, env=env)


# ---------------------------------------------------------------------------
# 1) Prasyarat
# ---------------------------------------------------------------------------
def verify_python() -> bool:
    """Validasi interpreter yang sedang berjalan (dipakai run.bat untuk bootstrap)."""
    version = sys.version_info
    if (version.major, version.minor) < MIN_PYTHON:
        error(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ wajib, terdeteksi "
            f"{version.major}.{version.minor}.{version.micro}."
        )
        error("Instal Python 3.10+ (https://www.python.org/downloads/windows/) lalu jalankan ulang run.bat.")
        return False
    info(f"Python {version.major}.{version.minor}.{version.micro} OK ({sys.executable})")
    return True


def verify_git() -> bool:
    """git diperlukan untuk bootstrap (clone) dan oleh Agent. Wajib ada."""
    git = shutil.which("git")
    if not git:
        error("git tidak ditemukan di PATH.")
        error("Instal Git for Windows (https://git-scm.com/download/win) lalu jalankan ulang run.bat.")
        return False
    try:
        result = subprocess.run([git, "--version"], capture_output=True, text=True)
    except OSError as exc:  # pragma: no cover - sangat jarang
        error(f"gagal menjalankan git: {exc}")
        return False
    version = (result.stdout or result.stderr).strip() or "git"
    info(f"{version} OK")
    return True


# ---------------------------------------------------------------------------
# 2) Virtual environment
# ---------------------------------------------------------------------------
def ensure_venv(root: Path) -> Path | None:
    python = venv_python(root)
    if python.exists():
        info(f"Virtual environment OK ({python})")
        return python

    info("Membuat virtual environment (venv)...")
    try:
        result = _run_command([sys.executable, "-m", "venv", str(venv_dir(root))])
    except OSError as exc:
        error(f"gagal membuat venv: {exc}")
        return None
    if result.returncode != 0 or not venv_python(root).exists():
        error("gagal membuat virtual environment. Pastikan modul 'venv' tersedia pada Python Anda.")
        return None
    info(f"Virtual environment dibuat ({venv_python(root)})")
    return venv_python(root)


# ---------------------------------------------------------------------------
# 3) Dependency runtime
# ---------------------------------------------------------------------------
def _requirements_hash(root: Path) -> str | None:
    req = requirements_file(root)
    if not req.exists():
        return None
    return hashlib.sha256(req.read_bytes()).hexdigest()


def deps_satisfied(root: Path, python: Path) -> bool:
    """Cek stamp hash requirements + import nyata; idempotensi & cepat."""
    expected = _requirements_hash(root)
    if expected is None:
        return False
    stamp = _deps_stamp(venv_dir(root))
    if not stamp.exists():
        return False
    try:
        if stamp.read_text(encoding="utf-8").strip() != expected:
            return False
    except OSError:
        return False
    try:
        probe = subprocess.run(
            [str(python), "-c", "import django, dotenv, requests, PIL"],
            capture_output=True,
        )
    except OSError:
        return False
    return probe.returncode == 0


def install_deps(root: Path, python: Path, force: bool = False) -> bool:
    if not requirements_file(root).exists():
        warn("requirements.txt tidak ditemukan; melewati instalasi dependency.")
        return True
    if not force and deps_satisfied(root, python):
        info("Dependency runtime sudah terpasang (skip).")
        return True

    info("Memasang dependency runtime (pip install -r requirements.txt)...")
    argv = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "-r",
        str(requirements_file(root)),
    ]
    try:
        result = _run_command(argv)
    except OSError as exc:
        error(f"gagal menjalankan pip: {exc}")
        return False
    if result.returncode != 0:
        error("instalasi dependency gagal (pip). Periksa koneksi internet lalu jalankan ulang run.bat.")
        return False

    try:
        _deps_stamp(venv_dir(root)).write_text(_requirements_hash(root) or "", encoding="utf-8")
    except OSError:  # pragma: no cover - stamp hanya optimasi
        pass
    info("Dependency runtime terpasang.")
    return True


# ---------------------------------------------------------------------------
# 4) Konfigurasi .env (tanpa credential)
# ---------------------------------------------------------------------------
def ensure_env_file(root: Path) -> bool:
    env_file = root / ".env"
    if env_file.exists():
        info("Konfigurasi .env ditemukan (tidak diubah).")
        return True
    for candidate in ("deployment.template", ".env.example"):
        source = root / candidate
        if source.exists():
            try:
                shutil.copyfile(source, env_file)
            except OSError as exc:
                warn(f"gagal menyalin {candidate} ke .env: {exc}")
                return True
            info(f"Konfigurasi .env dibuat dari {candidate} (isi API key hanya bila diperlukan).")
            return True
    warn("template konfigurasi tidak ditemukan (.env.example / deployment.template); .env dilewati.")
    return True


# ---------------------------------------------------------------------------
# 5) Frontend production build
# ---------------------------------------------------------------------------
def ensure_frontend(root: Path, force: bool = False) -> bool:
    frontend = frontend_dir(root)
    dist_index = frontend_dist_index(root)

    if not frontend.exists():
        error(f"folder frontend tidak ditemukan ({frontend}).")
        return False

    if dist_index.exists() and not force:
        info("Frontend production build sudah ada (skip).")
        return True

    node = shutil.which("node")
    if not node:
        error("Node.js diperlukan untuk build frontend, tetapi 'node' tidak ditemukan di PATH.")
        error("Instal Node.js LTS (https://nodejs.org) lalu jalankan ulang run.bat.")
        error("AETHER TIDAK meng-install Node secara otomatis (butuh persetujuan Anda).")
        return False

    node_modules = frontend / "node_modules"
    if not node_modules.exists():
        npm = shutil.which("npm")
        if not npm:
            error("'npm' tidak ditemukan di PATH; dibutuhkan untuk 'npm install' di web/frontend.")
            error("Instal Node.js LTS (yang menyertakan npm) lalu jalankan ulang run.bat.")
            return False
        info("Memasang dependency frontend (npm install)...")
        result = _run_command([npm, "install"], cwd=frontend)
        if result.returncode != 0:
            error("'npm install' gagal. Periksa koneksi internet lalu jalankan ulang run.bat.")
            return False

    vite = node_modules / "vite" / "bin" / "vite.js"
    if not vite.exists():
        error("vite tidak ditemukan di node_modules. Hapus web/frontend/node_modules lalu jalankan ulang run.bat.")
        return False

    info("Membangun frontend production build (vite build)...")
    result = _run_command([node, str(vite), "build"], cwd=frontend)
    if result.returncode != 0 or not dist_index.exists():
        error("build frontend gagal. Periksa pesan di atas lalu jalankan ulang run.bat.")
        return False
    info("Frontend production build siap (web/frontend/dist).")
    return True


# ---------------------------------------------------------------------------
# 6) Jalankan AETHER (Django Gateway)
# ---------------------------------------------------------------------------
def _open_browser_later(url: str, delay: float = 3.0) -> None:
    """Buka browser setelah delay singkat agar server sempat siap."""

    def _open() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - pembukaan browser bersifat best-effort
            pass

    threading.Thread(target=_open, daemon=True).start()


def launch(root: Path, python: Path, host: str, port: int, open_browser: bool = True) -> int:
    app_dir = django_app_dir(root)
    if not (app_dir / "manage.py").exists():
        error(f"manage.py tidak ditemukan di {app_dir}.")
        return 1

    env = os.environ.copy()
    env.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    # ALLOWED_HOSTS: jangan timpa bila user sudah menyetelnya sendiri.
    env.setdefault("DJANGO_ALLOWED_HOSTS", f"{host},localhost")

    url = f"http://{host}:{port}/"
    info("Backend started")
    info("Frontend ready")
    info(f"URL: {url}")
    info("Tekan Ctrl+C di jendela ini untuk menghentikan AETHER.")

    if open_browser:
        _open_browser_later(url)

    argv = [str(python), "manage.py", "runserver", f"{host}:{port}"]
    try:
        return _run_command(argv, cwd=app_dir, env=env).returncode
    except KeyboardInterrupt:
        return 0


# ---------------------------------------------------------------------------
# 7) Mode SIMULASI (dry-run, offline, tanpa mutasi)
# ---------------------------------------------------------------------------
def _aether_markers(root: Path) -> list[str]:
    """Marker yang menandai sebuah folder sebagai instalasi AETHER (read-only)."""
    found: list[str] = []
    if (root / "pyproject.toml").exists():
        found.append("pyproject.toml")
    if (django_app_dir(root) / "manage.py").exists():
        found.append("web/django_app/manage.py")
    if (frontend_dir(root) / "package.json").exists():
        found.append("web/frontend/package.json")
    return found


def looks_like_aether(root: Path) -> bool:
    """True bila root memuat marker instalasi AETHER (tanpa efek samping)."""
    markers = _aether_markers(root)
    return "pyproject.toml" in markers or "web/django_app/manage.py" in markers


def port_in_use(host: str, port: int) -> bool:
    """Cek apakah port sudah terpakai — read-only, TIDAK menjalankan server."""
    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex((target, port)) == 0
    except OSError:
        return False


def deps_stamp_state(root: Path) -> tuple[str, str]:
    """Status dependency TANPA menjalankan proses apa pun (dipakai mode simulasi)."""
    expected = _requirements_hash(root)
    if expected is None:
        return "skip", "requirements.txt tidak ditemukan"
    stamp = _deps_stamp(venv_dir(root))
    if not stamp.exists():
        return "install", "stamp dependency belum ada"
    try:
        if stamp.read_text(encoding="utf-8").strip() != expected:
            return "install", "requirements.txt berubah sejak instalasi terakhir"
    except OSError:
        return "install", "stamp dependency tidak dapat dibaca"
    return "skip", "stamp cocok dengan requirements.txt"


def run_simulation(
    root: Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    skip_frontend: bool = False,
    rebuild_frontend: bool = False,
) -> int:
    """Cetak rencana instalasi (dry-run) tanpa unduhan/mutasi apa pun.

    Hanya operasi read-only: `shutil.which`, `Path.exists`, `sys.version_info`,
    dan cek port via socket. Tidak ada subprocess = tidak ada git clone / pip /
    npm / vite / runserver, dan tidak ada file yang ditulis.
    """
    counters = {"koneksi": 0, "lokal": 0, "skip": 0}

    info("[SIMULATE] Mode simulasi (dry-run, offline) — tidak ada unduhan/mutasi dijalankan.")

    # --- DETEKSI ----------------------------------------------------------
    info(f"[DETEKSI] root: {root}")
    markers = _aether_markers(root)
    if markers:
        info(f"[DETEKSI] marker AETHER: DITEMUKAN ({', '.join(markers)})")
    else:
        info("[DETEKSI] marker AETHER: TIDAK DITEMUKAN (root belum ada / bukan instalasi AETHER)")

    version = sys.version_info
    if (version.major, version.minor) < MIN_PYTHON:
        warn(
            f"[DETEKSI] Python {version.major}.{version.minor}.{version.micro} < "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}; instalasi nyata akan berhenti."
        )
    else:
        info(f"[DETEKSI] Python {version.major}.{version.minor}.{version.micro} OK ({sys.executable})")

    git = shutil.which("git")
    node = shutil.which("node")
    npm = shutil.which("npm")
    info(f"[DETEKSI] git: {'ADA (' + git + ')' if git else 'TIDAK DITEMUKAN'}")
    info(f"[DETEKSI] Node.js: {'ADA (' + node + ')' if node else 'TIDAK DITEMUKAN'}")
    info(f"[DETEKSI] npm: {'ADA (' + npm + ')' if npm else 'TIDAK DITEMUKAN'}")

    # --- CLONE (dieksekusi oleh run.bat, bukan oleh installer) -------------
    if looks_like_aether(root):
        info("[CLONE] SKIP (instalasi AETHER sudah ada di root; git clone tidak diperlukan)")
        counters["skip"] += 1
    else:
        info(
            f"[CLONE] root belum ada; langkah pertama = git clone {REPO_URL} -> {root} "
            "(butuh koneksi, tidak dijalankan)"
        )
        counters["koneksi"] += 1

    # --- VENV -------------------------------------------------------------
    python = venv_python(root)
    if python.exists():
        info(f"[VENV] SKIP (sudah ada: {python})")
        counters["skip"] += 1
    else:
        info(f"[VENV] AKAN: {sys.executable} -m venv {venv_dir(root)} (LOKAL)")
        counters["lokal"] += 1

    # --- DEPS -------------------------------------------------------------
    req = requirements_file(root)
    deps_state, deps_reason = deps_stamp_state(root)
    if not req.exists():
        info("[DEPS] SKIP (requirements.txt tidak ditemukan)")
        counters["skip"] += 1
    elif not python.exists():
        info(
            f"[DEPS] AKAN: {python} -m pip install --disable-pip-version-check -r {req} "
            "(butuh koneksi; setelah venv dibuat)"
        )
        counters["koneksi"] += 1
    elif deps_state == "skip":
        info(f"[DEPS] SKIP ({deps_reason}; mode simulasi tidak menjalankan probe import)")
        counters["skip"] += 1
    else:
        info(
            f"[DEPS] AKAN: {python} -m pip install --disable-pip-version-check -r {req} "
            f"(butuh koneksi; alasan: {deps_reason})"
        )
        counters["koneksi"] += 1

    # --- ENV --------------------------------------------------------------
    env_file = root / ".env"
    if env_file.exists():
        info("[ENV] SKIP (file .env sudah ada; tidak diubah)")
        counters["skip"] += 1
    else:
        template = next(
            (name for name in ("deployment.template", ".env.example") if (root / name).exists()),
            None,
        )
        if template:
            info(f"[ENV] AKAN: salin {root / template} -> {env_file} (LOKAL)")
            counters["lokal"] += 1
        else:
            info("[ENV] TIDAK DAPAT DILANJUTKAN: template .env tidak ditemukan di root simulasi")

    # --- FRONTEND ---------------------------------------------------------
    dist_index = frontend_dist_index(root)
    node_modules = frontend_dir(root) / "node_modules"
    if skip_frontend:
        info("[FRONTEND] SKIP (--skip-frontend)")
        counters["skip"] += 1
    elif not frontend_dir(root).exists():
        info(f"[FRONTEND] TIDAK DAPAT DILANJUTKAN: folder {frontend_dir(root)} tidak ditemukan")
    elif dist_index.exists() and not rebuild_frontend:
        info(f"[FRONTEND] SKIP (sudah ada: {dist_index})")
        counters["skip"] += 1
    else:
        if node_modules.exists():
            info("[FRONTEND] SKIP (node_modules sudah ada; npm install tidak diperlukan)")
            counters["skip"] += 1
        else:
            info("[FRONTEND] AKAN: npm install (butuh koneksi)")
            counters["koneksi"] += 1
            if not node:
                warn("[FRONTEND] Node.js tidak ditemukan; npm install tidak akan berjalan.")
            elif not npm:
                warn("[FRONTEND] npm tidak ditemukan; npm install tidak akan berjalan.")
        info(
            f"[FRONTEND] AKAN: {node or 'node'} node_modules/vite/bin/vite.js build "
            f"(LOKAL, cwd={frontend_dir(root)})"
        )
        counters["lokal"] += 1
        if not node:
            warn("[FRONTEND] Node.js tidak ditemukan; build frontend tidak akan berjalan.")

    # --- LAUNCH -----------------------------------------------------------
    app_dir = django_app_dir(root)
    if not (app_dir / "manage.py").exists():
        info(f"[LAUNCH] TIDAK DAPAT DILANJUTKAN: manage.py tidak ditemukan di {app_dir}")
    else:
        info(
            f"[LAUNCH] AKAN: {python} manage.py runserver {host}:{port} "
            "(LOKAL, BLOCKING di foreground; dijalankan saat instalasi nyata)"
        )
        counters["lokal"] += 1
        if port_in_use(host, port):
            warn(f"[LAUNCH] port {port} sedang dipakai; saat instalasi nyata gunakan --port <alternatif>.")
        else:
            info(f"[LAUNCH] port {port} bebas (tidak ada server lain yang memakai).")

    # --- RINGKASAN --------------------------------------------------------
    info(
        f"[SIMULATE] RINGKASAN: {counters['koneksi']} langkah butuh koneksi, "
        f"{counters['lokal']} langkah LOKAL, {counters['skip']} langkah SKIP."
    )
    info("[SIMULATE] Tidak ada perubahan file / unduhan yang dilakukan (dry-run murni).")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AETHER installer + launcher (portable, idempotent).",
    )
    parser.add_argument("--root", default=None, help="Folder root AETHER (default: parent dari scripts/).")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host bind server (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port server (default 8000).")
    parser.add_argument("--check", action="store_true", help="Hanya verifikasi prasyarat (tanpa mengubah apa pun).")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Dry-run OFFLINE: cetak rencana langkah tanpa unduhan/mutasi apa pun.",
    )
    parser.add_argument("--no-launch", action="store_true", help="Siapkan instalasi saja, jangan jalankan server.")
    parser.add_argument("--skip-frontend", action="store_true", help="Jangan build frontend.")
    parser.add_argument("--rebuild-frontend", action="store_true", help="Paksa build ulang frontend.")
    parser.add_argument("--force-deps", action="store_true", help="Paksa pip install ulang dependency.")
    parser.add_argument("--no-browser", action="store_true", help="Jangan buka browser otomatis.")
    return parser


def _normalize_argv(argv: list[str]) -> list[str]:
    """Normalisasi kapitalisasi flag yang dikirim run.bat.

    Gate simulasi di run.bat memakai pencocokan case-insensitive (findstr /i),
    sehingga user bisa menyetel AETHER_ARGS=--SIMULATE. Flag tersebut
    dinormalisasi ke bentuk kanonik agar argparse tetap menerimanya.
    """
    return ["--simulate" if arg.lower() == "--simulate" else arg for arg in argv]


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(_normalize_argv(raw_argv))
    root = Path(args.root).resolve() if args.root else project_root_default()

    # Mode simulasi (dry-run): offline, read-only, tanpa mutasi. Root boleh
    # belum ada karena langkah pertama (git clone) memang belum dijalankan.
    if args.simulate:
        return run_simulation(
            root,
            args.host,
            args.port,
            skip_frontend=args.skip_frontend,
            rebuild_frontend=args.rebuild_frontend,
        )

    info(f"AETHER root: {root}")
    if not looks_like_aether(root):
        error("folder ini tidak terlihat seperti instalasi AETHER (marker tidak ditemukan).")
        return 1

    python_ok = verify_python()
    git_ok = verify_git()

    if args.check:
        node = shutil.which("node")
        info(f"Node.js: {'OK (' + node + ')' if node else 'TIDAK DITEMUKAN (diperlukan untuk build frontend)'}")
        info(f"Venv: {'ADA' if venv_python(root).exists() else 'BELUM ADA (akan dibuat saat setup)'}")
        info(f"Frontend dist: {'ADA' if frontend_dist_index(root).exists() else 'BELUM ADA (akan di-build saat setup)'}")
        return 0 if (python_ok and git_ok) else 1

    if not python_ok or not git_ok:
        return 1

    python = ensure_venv(root)
    if python is None:
        return 1
    if not install_deps(root, python, force=args.force_deps):
        return 1
    ensure_env_file(root)

    if not args.skip_frontend and not ensure_frontend(root, force=args.rebuild_frontend):
        return 1

    if args.no_launch:
        info("Setup selesai (--no-launch). Jalankan run.bat untuk memulai AETHER.")
        return 0

    code = launch(root, python, args.host, args.port, open_browser=not args.no_browser)
    if code != 0:
        error(f"Backend berhenti dengan kode {code}.")
    return code


if __name__ == "__main__":
    sys.exit(main())
