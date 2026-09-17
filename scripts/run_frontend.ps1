# =============================================================================
# AETHER - Jalankan Vue Workbench (frontend, dev server).
# =============================================================================
# Pemakaian:
#     .\scripts\run_frontend.ps1
#     .\scripts\run_frontend.ps1 -Install   # jalankan npm install dulu
#
# Prasyarat:
#   - Node.js + npm terpasang.
#   - Backend Django berjalan di http://127.0.0.1:8000 (lihat run_backend.ps1).
#     Vite mem-proxy /api ke backend (lihat web/frontend/vite.config.js).
# =============================================================================

param(
    [switch]$Install
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Frontend = Join-Path $ProjectRoot "web\frontend"

if (-not (Test-Path $Frontend)) {
    Write-Error "Tidak menemukan web\frontend di $Frontend"
    exit 1
}

Push-Location $Frontend
try {
    if ($Install -or -not (Test-Path (Join-Path $Frontend "node_modules"))) {
        Write-Host "Menjalankan npm install ..." -ForegroundColor Cyan
        npm install
    }
    Write-Host "Menjalankan Vue Workbench (Vite dev server) di http://127.0.0.1:5173 ..." -ForegroundColor Cyan
    Write-Host "Tekan Ctrl+C untuk berhenti."
    npm run dev
}
finally {
    Pop-Location
}
