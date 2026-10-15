@echo off
REM ===========================================================================
REM AETHER - CRITICAL TEST SUITE (default/fast gate)
REM ---------------------------------------------------------------------------
REM Jalur uji paling KECIL yang tetap menjaga jalur eksekusi inti AETHER:
REM   task lifecycle & pembuatan task -> scheduler/executor -> Agent Runtime
REM   -> provider request & tool calling -> permission/isolasi project ->
REM   keamanan filesystem -> ChangeTracker & deteksi perubahan file.
REM
REM Ini BUKAN pengganti full regression. Jalankan full regression terpisah:
REM   python -m pytest tests/ -q
REM
REM Pemakaian:
REM   scripts\test_critical.bat            (ringkas, default)
REM   scripts\test_critical.bat -v         (verbose; argumen lain diteruskan)
REM
REM Tidak ada file test yang dihapus/dinonaktifkan; script ini hanya MEMILIH
REM berkas test yang dijalankan (daftar eksplisit di bawah).
REM ===========================================================================
setlocal

set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%.."
pushd "%PROJECT_ROOT%"

echo === AETHER CRITICAL TEST SUITE ===
echo root: %CD%
echo.

python -m pytest ^
  --rootdir=%PROJECT_ROOT% ^
  tests/test_config_separation.py ^
  tests/test_permission_matrix.py ^
  scripts/check_workspace_write_tools.py ^
  tests/test_change_tracker_runtime_integration.py ^
  tests/test_change_tracker_incremental_snapshot.py ^
  tests/test_changes_persistence.py ^
  tests/test_agent_execution_mode.py ^
  tests/test_agent_execution_policy.py ^
  tests/test_read_dedup.py ^
  tests/test_bug4_completion_gate.py ^
  tests/test_provider_fallback_continuous.py ^
  tests/test_provider_retry_lifecycle.py ^
  -q %*

set "EXIT_CODE=%ERRORLEVEL%"
popd

endlocal & exit /b %EXIT_CODE%
