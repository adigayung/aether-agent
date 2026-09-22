@echo off
cd /d J:\Agent_Ai\web\frontend

echo Building AETHER UI...
call npm run build

if errorlevel 1 (
    echo.
    echo [ERROR] Build gagal.
    pause
    exit /b 1
)

echo.
echo [OK] UI berhasil di-build.