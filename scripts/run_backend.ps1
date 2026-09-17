# =============================================================================
# AETHER - Jalankan Django Gateway (backend).
# =============================================================================
# Pemakaian:
#     .\scripts\run_backend.ps1
#     .\scripts\run_backend.ps1 -Host 0.0.0.0 -Port 8000
#
# Prasyarat:
#   - Dependency terpasang: pip install -r requirements.txt
#   - (Opsional) file .env sudah disiapkan dari deployment.template
# =============================================================================

param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

# Root project = parent dari folder scripts/.
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DjangoApp = Join-Path $ProjectRoot "web\django_app"

if (-not (Test-Path $DjangoApp)) {
    Write-Error "Tidak menemukan web\django_app di $DjangoApp"
    exit 1
}

Write-Host "Menjalankan AETHER Django Gateway di http://${BindHost}:${Port} ..." -ForegroundColor Cyan
Write-Host "  DJANGO_SETTINGS_MODULE=config.settings"
Write-Host "  Working dir: $DjangoApp"
Write-Host "Tekan Ctrl+C untuk berhenti."

Push-Location $DjangoApp
try {
    $env:DJANGO_SETTINGS_MODULE = "config.settings"
    python manage.py runserver "${BindHost}:${Port}"
}
finally {
    Pop-Location
}
