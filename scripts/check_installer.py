"""Verifikasi mode SIMULASI installer AETHER (dry-run offline, tanpa mutasi).

DETERMINISTIK & OFFLINE: script ini tidak melakukan network sama sekali dan
tidak mengubah working tree. Ia membuktikan bahwa:

    1. `install_aether.py --simulate` selesai cepat (< 5s), exit 0, dan
       mencetak rencana per-langkah ([DETEKSI]/[CLONE]/[VENV]/[DEPS]/[ENV]/
       [FRONTEND]/[LAUNCH]) + RINGKASAN (butuh koneksi vs LOKAL) + pernyataan
       "tidak ada perubahan file / unduhan".
    2. `--simulate --root <folder kosong>` tetap exit 0 dan melaporkan
       langkah pertama = git clone (butuh koneksi) TANPA menjalankannya.
    3. Anti-network/anti-mutasi: `subprocess.run` TIDAK PERNAH dipanggil
       (tidak ada pip/npm/venv/vite/manage.py/runserver/clone), `launch()` dan
       pembukaan browser tidak pernah dipanggil, dan folder root simulasi tetap
       kosong setelah dijalankan.
    4. Flag lain tetap berfungsi & tidak regresi: --check, --skip-frontend,
       --rebuild-frontend, --host, --port (semuanya lewat jalur simulasi).
    5. run.bat memuat gate simulasi (AETHER_SIMULATE / --simulate), melewati
       `git clone` pada jalur simulasi, dan tidak 'pause' di mode simulasi.
    6. Boundary: tidak ada perubahan pada src/ atau web/django_app/ (backend/core
       Agent tidak tersentuh).

Jalankan (offline):
    python scripts/check_installer.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALLER_PATH = PROJECT_ROOT / "scripts" / "install_aether.py"
RUN_BAT_PATH = PROJECT_ROOT / "run.bat"

MAX_SIMULATION_SECONDS = 5.0

FORBIDDEN_ARGV_TOKENS = (
    "pip",
    "npm",
    "venv",
    "vite",
    "manage.py",
    "runserver",
    "clone",
)


class _SimulationSideEffect(Exception):
    """Dilempar bila mode simulasi mencoba menjalankan/memanggil sesuatu."""


def _load_installer():
    """Muat installer sebagai modul terpisah (TANPA menjalankan main())."""
    assert INSTALLER_PATH.exists(), f"installer tidak ditemukan: {INSTALLER_PATH}"
    spec = importlib.util.spec_from_file_location("install_aether_under_test", INSTALLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture(module, argv: list[str]) -> tuple[str, int, float]:
    """Jalankan module.main(argv) sambil menangkap stdout + waktu eksekusi."""
    buffer = io.StringIO()
    started = time.perf_counter()
    with contextlib.redirect_stdout(buffer):
        code = module.main(argv)
    elapsed = time.perf_counter() - started
    return buffer.getvalue(), code, elapsed


def _simulate(module, argv: list[str]) -> tuple[str, int, float]:
    """_capture + menerjemahkan side-effect simulasi menjadi AssertionError jelas."""
    try:
        return _capture(module, argv)
    except _SimulationSideEffect as exc:
        raise AssertionError(str(exc)) from None


def _check_no_side_effects(recorded: list[list[str]]) -> None:
    assert recorded == [], f"mode simulasi memanggil subprocess.run: {recorded}"
    for argv in recorded:
        blob = " ".join(str(part) for part in argv).lower()
        for token in FORBIDDEN_ARGV_TOKENS:
            assert token not in blob, f"mode simulasi menjalankan perintah terlarang: {argv}"


def _run() -> int:
    module = _load_installer()

    # ------------------------------------------------------------------
    # Instrumentasi: blokir SEMUA eksekusi proses + launch pada mode simulasi.
    # ------------------------------------------------------------------
    recorded: list[list[str]] = []
    real_subprocess_run = subprocess.run
    real_launch = module.launch
    real_open_browser_later = module._open_browser_later

    def _blocked_run(argv, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        recorded.append(list(argv) if isinstance(argv, (list, tuple)) else [str(argv)])
        raise _SimulationSideEffect(f"subprocess.run dipanggil saat mode simulasi: {argv}")

    def _blocked_launch(*args, **kwargs):  # noqa: ANN002, ANN003
        raise _SimulationSideEffect("launch() dipanggil saat mode simulasi")

    def _blocked_open_browser(*args, **kwargs):  # noqa: ANN002, ANN003
        raise _SimulationSideEffect("_open_browser_later() dipanggil saat mode simulasi")

    subprocess.run = _blocked_run
    module.launch = _blocked_launch
    module._open_browser_later = _blocked_open_browser
    try:
        # ==============================================================
        # [1] --simulate pada root nyata: cepat, exit 0, rencana lengkap.
        # ==============================================================
        output, code, elapsed = _simulate(module, ["--simulate", "--root", str(PROJECT_ROOT)])
        assert code == 0, f"exit code harus 0, dapat {code}"
        assert elapsed < MAX_SIMULATION_SECONDS, f"simulasi terlalu lama: {elapsed:.2f}s"
        for label in ("[DETEKSI]", "[CLONE]", "[VENV]", "[DEPS]", "[ENV]", "[FRONTEND]", "[LAUNCH]"):
            assert label in output, f"label langkah {label} tidak ada di output simulasi"
        assert "RINGKASAN" in output, "ringkasan langkah tidak dicetak"
        assert "butuh koneksi" in output, "ringkasan harus memisahkan langkah 'butuh koneksi'"
        assert "LOKAL" in output, "ringkasan harus memisahkan langkah 'LOKAL'"
        assert "Tidak ada perubahan file / unduhan yang dilakukan" in output, (
            "pernyataan eksplisit 'tidak ada perubahan' wajib dicetak"
        )
        # Root nyata sudah lengkap -> langkah setup harus SKIP (idempotent).
        assert "[VENV] SKIP" in output, "venv yang sudah ada harus dilaporkan SKIP"
        assert "Tidak ada perubahan" in output
        print(f"[1] --simulate root nyata OK -> exit 0, {elapsed:.3f}s, rencana lengkap (SKIP idempotent)")

        # ==============================================================
        # [2] --simulate pada root KOSONG: clone dilaporkan, tidak dijalankan.
        # ==============================================================
        with tempfile.TemporaryDirectory(prefix="aether_sim_root_") as tmp:
            empty_root = Path(tmp)
            output, code, elapsed = _simulate(module, ["--simulate", "--root", str(empty_root)])
            assert code == 0, f"exit code harus 0 untuk root kosong, dapat {code}"
            assert elapsed < MAX_SIMULATION_SECONDS, f"simulasi terlalu lama: {elapsed:.2f}s"
            lowered = output.lower()
            assert "clone" in lowered, "langkah pertama (git clone) harus dilaporkan"
            assert "butuh koneksi" in output, "git clone harus ditandai 'butuh koneksi'"
            assert "tidak dijalankan" in lowered, "git clone harus dinyatakan TIDAK dijalankan"
            assert "[VENV] AKAN" in output, "root kosong harus merencanakan pembuatan venv"
            # Root yang dikendalikan script ini harus TETAP KOSONG setelah simulasi.
            assert sorted(os.listdir(empty_root)) == [], (
                f"mode simulasi menulis file ke root simulasi: {sorted(os.listdir(empty_root))}"
            )
            print("[2] --simulate root kosong OK -> clone dilaporkan, tanpa eksekusi, folder tetap kosong")

        # ==============================================================
        # [3] Flag lain tetap berfungsi lewat jalur simulasi (tanpa regresi).
        # ==============================================================
        output, code, _ = _simulate(module, ["--simulate", "--root", str(PROJECT_ROOT), "--skip-frontend"])
        assert code == 0 and "[FRONTEND] SKIP (--skip-frontend)" in output, (
            "--skip-frontend tidak tercermin di rencana simulasi"
        )
        output, code, _ = _simulate(
            module, ["--simulate", "--root", str(PROJECT_ROOT), "--rebuild-frontend"]
        )
        assert code == 0 and "node_modules/vite/bin/vite.js build" in output, (
            "--rebuild-frontend harus merencanakan vite build meski dist sudah ada"
        )
        assert "[FRONTEND] AKAN" in output
        output, code, _ = _simulate(
            module,
            ["--simulate", "--root", str(PROJECT_ROOT), "--host", "0.0.0.0", "--port", "8001"],
        )
        assert code == 0 and "runserver 0.0.0.0:8001" in output, (
            "--host/--port harus tercermin di rencana LAUNCH"
        )
        # run.bat memakai gate case-insensitive (findstr /i) sehingga flag bisa
        # sampai sebagai --SIMULATE; installer harus menormalkannya.
        output, code, _ = _simulate(module, ["--SIMULATE", "--root", str(PROJECT_ROOT)])
        assert code == 0 and "[SIMULATE]" in output, "--SIMULATE (kapital) harus dinormalisasi"
        print("[3] flag --skip-frontend/--rebuild-frontend/--host/--port/--SIMULATE tetap berfungsi OK")

        # ==============================================================
        # [4] Anti-network & anti-mutasi terbukti.
        # ==============================================================
        _check_no_side_effects(recorded)
        try:
            module.launch(PROJECT_ROOT, PROJECT_ROOT / "python", "127.0.0.1", 8000)
        except _SimulationSideEffect:
            pass
        else:
            raise AssertionError("proteksi harness gagal: launch() tidak diblokir")
        print("[4] tidak ada subprocess.run / pip / npm / venv / vite / runserver / clone & launch OK")

        # ==============================================================
        # [5] --check tetap exit 0 dan cepat (tidak regresi).
        # ==============================================================
        subprocess.run = real_subprocess_run
        buffer = io.StringIO()
        started = time.perf_counter()
        with contextlib.redirect_stdout(buffer):
            check_code = module.main(["--check", "--root", str(PROJECT_ROOT)])
        check_elapsed = time.perf_counter() - started
        check_out = buffer.getvalue()
        assert check_code == 0, f"--check harus exit 0, dapat {check_code}"
        assert check_elapsed < MAX_SIMULATION_SECONDS, f"--check terlalu lama: {check_elapsed:.2f}s"
        for token in ("Python", "Venv", "Frontend dist"):
            assert token in check_out, f"output --check harus memuat '{token}'"
        print(f"[5] --check tetap OK -> exit 0, {check_elapsed:.3f}s (tanpa regresi)")
    finally:
        subprocess.run = real_subprocess_run
        module.launch = real_launch
        module._open_browser_later = real_open_browser_later

    # ==================================================================
    # [6] Gate simulasi di run.bat (assertion struktural statis).
    # ==================================================================
    bat = RUN_BAT_PATH.read_text(encoding="utf-8", errors="replace")
    assert "AETHER_SIMULATE" in bat, "run.bat harus mendeteksi env AETHER_SIMULATE"
    assert "--simulate" in bat, "run.bat harus meneruskan --simulate ke installer"
    # Hanya boleh ada SATU baris perintah 'git clone' (komentar REM tidak dihitung).
    clone_lines = [line.strip() for line in bat.splitlines() if line.strip().lower().startswith("git clone")]
    assert len(clone_lines) == 1, f"harus ada tepat SATU baris perintah git clone, dapat {clone_lines}"
    assert "clone dilewati" in bat, "run.bat harus mencetak bahwa clone dilewati di mode simulasi"
    # Gate simulasi harus diperiksa SEBELUM baris 'git clone' dieksekusi.
    gate_pos = bat.index('set "SIMULATE="')
    clone_pos = bat.index(clone_lines[0])
    assert gate_pos < clone_pos, "gate simulasi harus berada sebelum langkah git clone"
    # Jalur simulasi tidak boleh jatuh ke :fail-pause.
    fail_block = bat[bat.index(":fail") :]
    assert "if defined SIMULATE goto :end" in fail_block, (
        ":fail harus melewati 'pause' saat mode simulasi"
    )
    assert "if defined AETHER_NONINTERACTIVE goto :end" in fail_block, (
        ":fail harus melewati 'pause' di konteks non-interaktif"
    )
    print("[6] run.bat: gate simulasi OK (clone dilewati, pause dijaga)")

    # ==================================================================
    # [7] Boundary: backend/core tidak tersentuh.
    # ==================================================================
    git = shutil.which("git")
    if git:
        result = subprocess.run(
            [git, "diff", "--name-only", "--", "src", "web/django_app"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        changed = [line for line in (result.stdout or "").splitlines() if line.strip()]
        assert not changed, f"tidak boleh ada perubahan di src/ atau web/django_app/: {changed}"
    else:  # pragma: no cover - git praktis selalu ada di repo ini
        print("[7] git tidak tersedia; boundary check dilewati")
    print("[7] boundary OK -> tidak ada perubahan pada src/ atau web/django_app/")

    print()
    print("[OK] Installer simulation OK")
    return 0


def main() -> int:
    print("=== Verifikasi Installer AETHER mode SIMULASI (dry-run offline) ===")
    return _run()


if __name__ == "__main__":
    sys.exit(main() or 0)
