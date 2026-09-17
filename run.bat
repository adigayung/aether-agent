@echo off
REM ===========================================================================
REM  AETHER - Single Launcher
REM ===========================================================================
REM  Cukup double-click file ini untuk menjalankan seluruh aplikasi AETHER.
REM
REM  Mode: PRODUCTION BUILD.
REM    Django Gateway menyajikan frontend production build (web\frontend\dist)
REM    sekaligus API/SSE, sehingga hanya perlu SATU proses dan SATU URL:
REM        http://127.0.0.1:8000/
REM
REM  Bila web\frontend\dist belum ada, launcher akan memberi tahu dan berhenti
REM  (jalankan "npm run build" di web\frontend terlebih dahulu).
REM
REM  Tidak ada installer, service Windows, dependency baru, atau launcher
REM  system baru. Hanya memakai venv + Django yang sudah ada.
REM ===========================================================================

setlocal EnableExtensions EnableDelayedExpansion

REM --- 1) Working directory = root project (folder file .bat ini) -----------
cd /d "%~dp0"
set "PROJECT_ROOT=%~dp0"
set "DJANGO_APP=%PROJECT_ROOT%web\django_app"
set "FRONTEND_DIST=%PROJECT_ROOT%web\frontend\dist"
set "VENV_PY=%PROJECT_ROOT%venv\Scripts\python.exe"
set "HOST=127.0.0.1"
set "PORT=8000"
set "URL=http://%HOST%:%PORT%/"

echo [AETHER] Starting...

REM --- 2) Validasi prasyarat ------------------------------------------------
if not exist "%VENV_PY%" (
    echo [AETHER] ERROR: Python venv tidak ditemukan di "%VENV_PY%".
    echo [AETHER] Pastikan folder venv AETHER sudah ada.
    goto :fail
)

if not exist "%DJANGO_APP%\manage.py" (
    echo [AETHER] ERROR: manage.py tidak ditemukan di "%DJANGO_APP%".
    goto :fail
)

if not exist "%FRONTEND_DIST%\index.html" (
    echo [AETHER] ERROR: Frontend production build tidak ditemukan di "%FRONTEND_DIST%".
    echo [AETHER] Jalankan dulu:  cd web\frontend ^&^& npm run build
    goto :fail
)

REM --- 3) Konfigurasi AETHER yang sudah ada ---------------------------------
set "DJANGO_SETTINGS_MODULE=config.settings"
set "DJANGO_ALLOWED_HOSTS=%HOST%,localhost"

REM --- 4) Jalankan Django backend (menyajikan API + frontend dist) ----------
echo [AETHER] Backend started
echo [AETHER] Frontend ready
echo [AETHER] Opening Workbench...
echo.
echo [AETHER] URL: %URL%
echo [AETHER] Tekan Ctrl+C di jendela ini untuk menghentikan AETHER.
echo.

REM Buka browser setelah jeda singkat agar server sempat siap.
start "" cmd /c "timeout /t 3 /nobreak >nul & start "" "%URL%""

REM Jalankan server di foreground. Ctrl+C akan menghentikan proses ini.
pushd "%DJANGO_APP%"
"%VENV_PY%" manage.py runserver %HOST%:%PORT%
set "EXITCODE=%ERRORLEVEL%"
popd

if not "%EXITCODE%"=="0" (
    echo.
    echo [AETHER] ERROR: Backend berhenti dengan kode %EXITCODE%.
    goto :fail
)

echo.
echo [AETHER] AETHER dihentikan.
goto :end

:fail
echo.
echo [AETHER] Gagal menjalankan AETHER. Lihat pesan di atas untuk penyebabnya.
echo [AETHER] Jendela ini TIDAK ditutup otomatis agar Anda dapat membaca error.
echo.
pause

:end
endlocal
