@echo off
REM ===========================================================================
REM  AETHER - Self-bootstrapping Launcher
REM ===========================================================================
REM  Cukup double-click file ini untuk menjalankan AETHER. Launcher akan:
REM    1) Menyiapkan folder AETHER (git clone bila belum ada).
REM    2) Menyiapkan environment (venv + dependency + frontend production build).
REM    3) Menjalankan AETHER (Django Gateway + frontend production build).
REM
REM  Mode: PRODUCTION BUILD. Hanya SATU proses dan SATU URL:
REM        http://127.0.0.1:8000/
REM
REM  Logika instalasi/verifikasi yang berat berada di skrip Python portable:
REM        scripts\install_aether.py
REM  Batch ini hanya: deteksi folder AETHER -> git clone bila perlu -> panggil
REM  installer -> teruskan exit code. Aman dijalankan berulang (idempotent).
REM
REM  Anda bisa override folder instalasi lewat environment variable:
REM        set AETHER_INSTALL_DIR=D:\apps\aether-agent
REM  Anda juga bisa meneruskan argumen tambahan ke installer:
REM        set AETHER_ARGS=--check         (verifikasi prasyarat saja)
REM        set AETHER_ARGS=--rebuild-frontend
REM
REM  MODE SIMULASI (dry-run, offline) — untuk menguji alur instalasi TANPA
REM  mengunduh/mengubah apa pun. Git clone dilewati dan installer dicetak
REM  sebagai rencana langkah saja:
REM        set AETHER_SIMULATE=1           && run.bat
REM        set AETHER_ARGS=--simulate      && run.bat
REM  Set AETHER_NONINTERACTIVE=1 untuk mencegah 'pause' (konteks otomatis/CI).
REM ===========================================================================

setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
REM %~dp0 selalu berakhir dengan "\". Buang trailing backslash agar path aman
REM dipakai sebagai argumen berkutip ("<path>\"" akan merusak parsing quote).
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "REPO_URL=https://github.com/adigayung/aether-agent.git"
set "HOST=127.0.0.1"
set "PORT=8000"

REM Folder default hasil clone bila AETHER belum ada di samping run.bat.
set "INSTALL_DIR=%SCRIPT_DIR%\aether-agent"
if defined AETHER_INSTALL_DIR set "INSTALL_DIR=%AETHER_INSTALL_DIR%"
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"

echo [AETHER] Launcher...

REM --- 0) Deteksi mode simulasi (dry-run) ------------------------------------
REM Aktif bila AETHER_SIMULATE diset ATAU AETHER_ARGS memuat "--simulate".
REM Dalam mode ini: git clone dilewati, installer dipanggil dengan --simulate,
REM dan 'pause' tidak dijalankan (agar tidak menggantung di konteks otomatis).
set "SIMULATE="
if defined AETHER_SIMULATE set "SIMULATE=1"
if defined AETHER_ARGS (
    echo(%AETHER_ARGS%| findstr /i /c:"--simulate" >nul 2>&1 && set "SIMULATE=1"
)
if defined SIMULATE echo [AETHER] [SIMULATE] Dry-run aktif: unduhan/mutasi dilewati.

REM --- 1) Deteksi folder AETHER (marker khas) --------------------------------
call :detect_aether "%SCRIPT_DIR%"
if defined AETHER_DIR goto :aether_found

call :detect_aether "%INSTALL_DIR%"
if defined AETHER_DIR goto :aether_found

REM --- 2a) Folder AETHER belum ada + mode simulasi -> jangan git clone -------
if defined SIMULATE goto :simulate_no_root

REM --- 2) Folder AETHER belum ada -> git clone -------------------------------
echo [AETHER] Folder AETHER belum ditemukan. Menyiapkan di:
echo          %INSTALL_DIR%

where git >nul 2>&1
if errorlevel 1 (
    echo [AETHER] ERROR: git tidak ditemukan di PATH.
    echo [AETHER] Instal Git for Windows ^(https://git-scm.com/download/win^) lalu jalankan ulang run.bat.
    goto :fail
)

if exist "%INSTALL_DIR%" (
    echo [AETHER] ERROR: folder "%INSTALL_DIR%" sudah ada tetapi bukan instalasi AETHER.
    echo [AETHER] Hapus/pindahkan folder tersebut lalu jalankan ulang, atau set AETHER_INSTALL_DIR.
    goto :fail
)

echo [AETHER] Mengambil AETHER dari %REPO_URL% ...
git clone "%REPO_URL%" "%INSTALL_DIR%"
if errorlevel 1 (
    echo [AETHER] ERROR: git clone gagal. Periksa koneksi internet/akses repo.
    goto :fail
)

call :detect_aether "%INSTALL_DIR%"
if not defined AETHER_DIR (
    echo [AETHER] ERROR: hasil clone tidak dikenali sebagai instalasi AETHER.
    goto :fail
)

:aether_found
echo [AETHER] Folder AETHER: %AETHER_DIR%

REM --- 3) Validasi Python (untuk bootstrap installer) ------------------------
call :detect_python
if not defined SYS_PY (
    echo [AETHER] ERROR: Python tidak ditemukan di PATH.
    echo [AETHER] Instal Python 3.10+ ^(https://www.python.org/downloads/windows/^).
    echo [AETHER] Saat instalasi, centang "Add python.exe to PATH", lalu jalankan ulang run.bat.
    goto :fail
)

set "INSTALLER=%AETHER_DIR%\scripts\install_aether.py"
if not exist "%INSTALLER%" (
    echo [AETHER] ERROR: installer tidak ditemukan di "%INSTALLER%".
    goto :fail
)

REM --- 4) Jalankan installer Python (setup + launch) -------------------------
REM Argumen tambahan opsional dari user (mis. --check, --rebuild-frontend).
set "EXTRA_ARGS="
if defined AETHER_ARGS set "EXTRA_ARGS=%AETHER_ARGS%"

REM Mode simulasi: installer dipanggil sebagai dry-run (tanpa unduhan/mutasi).
set "SIM_ARGS="
if defined SIMULATE set "SIM_ARGS=--simulate"

pushd "%AETHER_DIR%"
%SYS_PY% "%INSTALLER%" --root "%AETHER_DIR%" --host %HOST% --port %PORT% %SIM_ARGS% %EXTRA_ARGS%
set "EXITCODE=%ERRORLEVEL%"
popd

if not "%EXITCODE%"=="0" goto :fail
goto :end

REM --- Subroutine: deteksi interpreter Python untuk bootstrap ----------------
:detect_python
set "SYS_PY="
where python >nul 2>&1 && set "SYS_PY=python"
if not defined SYS_PY (
    where py >nul 2>&1 && set "SYS_PY=py -3"
)
exit /b 0

REM --- Subroutine: mode simulasi saat folder AETHER belum ada ----------------
REM git clone DILEWATI (dry-run offline); installer dijalankan dengan
REM --simulate --root "%INSTALL_DIR%" bila skrip installer tersedia.
:simulate_no_root
echo [AETHER] [SIMULATE] clone dilewati: %REPO_URL% -^> %INSTALL_DIR%
set "INSTALLER=%SCRIPT_DIR%\scripts\install_aether.py"
if not exist "%INSTALLER%" (
    echo [AETHER] [SIMULATE] installer belum tersedia ^(folder AETHER belum ada^); tidak ada aksi.
    goto :end
)
call :detect_python
if not defined SYS_PY (
    echo [AETHER] [SIMULATE] Python tidak ditemukan; simulasi dilewati.
    goto :end
)
%SYS_PY% "%INSTALLER%" --simulate --root "%INSTALL_DIR%"
set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" goto :fail
goto :end

REM --- Subroutine: deteksi marker AETHER pada sebuah folder ------------------
REM Normalisasi path (buang trailing backslash) agar aman dipakai sebagai
REM argumen berkutip. %%~fI TIDAK membuang trailing backslash, jadi di-strip manual.
:detect_aether
set "AETHER_DIR="
set "CANDIDATE="
if exist "%~1\pyproject.toml" if exist "%~1\web\django_app\manage.py" set "CANDIDATE=%~1"
if exist "%~1\web\django_app\manage.py" if exist "%~1\scripts\install_aether.py" set "CANDIDATE=%~1"
if not defined CANDIDATE exit /b 0
set "AETHER_DIR=%CANDIDATE%"
if "%AETHER_DIR:~-1%"=="\" set "AETHER_DIR=%AETHER_DIR:~0,-1%"
exit /b 0

:fail
echo.
echo [AETHER] Gagal menjalankan AETHER. Lihat pesan di atas untuk penyebabnya.
echo.
REM Mode simulasi / non-interaktif tidak boleh menggantung di 'pause'.
if defined SIMULATE goto :end
if defined AETHER_NONINTERACTIVE goto :end
echo [AETHER] Jendela ini TIDAK ditutup otomatis agar Anda dapat membaca error.
echo.
pause
goto :end

:end
endlocal
