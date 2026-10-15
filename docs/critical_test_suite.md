# AETHER — Critical Test Suite & Scope-Based Test Splitting

Dokumen ini menjelaskan pengelompokan test AETHER: mana yang masuk **critical
suite** (gate cepat sehari-hari) dan mana yang cukup dijalankan sebagai
**full regression** atau **targeted test** sesuai modul yang berubah.

> Prinsip: task ini **tidak** mengubah implementasi runtime, tidak menghapus
> test, tidak melonggarkan assertion, dan tidak menonaktifkan test.

---

## 1. Perintah

| Keperluan | Perintah |
| --- | --- |
| Critical suite (cepat, default) | `scripts\test_critical.bat` |
| Critical suite verbose | `scripts\test_critical.bat -v` |
| Critical suite (langsung pytest) | `python -m pytest tests/test_config_separation.py tests/test_permission_matrix.py scripts/check_workspace_write_tools.py tests/test_change_tracker_runtime_integration.py tests/test_change_tracker_incremental_snapshot.py tests/test_changes_persistence.py tests/test_agent_execution_mode.py tests/test_agent_execution_policy.py tests/test_read_dedup.py tests/test_bug4_completion_gate.py tests/test_provider_fallback_continuous.py tests/test_provider_retry_lifecycle.py -q` |
| Full regression (semua test) | `python -m pytest tests/ -q` |
| Targeted (satu modul) | `python -m pytest tests/<test_terkait>.py -q` |
| Verifier end-to-end (scripts) | `python scripts\check_<area>.py` |

`scripts\test_critical.bat` hanya **memilih** berkas test yang dijalankan
(daftar eksplisit). Tidak ada konfigurasi pytest global (`addopts`,
`markers`, `-k`, `--ignore`) yang diubah, sehingga test di luar daftar tetap
terkumpul & tetap dijalankan oleh `python -m pytest tests/ -q`.

---

## 2. Isi critical suite (12 berkas, 166 test)

| # | Test | Area yang dijaga | Alasan dipilih |
| --- | --- | --- | --- |
| 1 | `tests/test_config_separation.py` | Global Settings vs Project Policy + layer gateway (`api.services`) + isolasi antar-project | Menjaga isolasi state/konfigurasi per-project pada jalur gateway; juga satu-satunya yang menyentuh `GatewayService` di critical suite |
| 2 | `tests/test_permission_matrix.py` | Permission matrix project, wiring `TaskExecutor` + matrix, backward compatible | Gerbang keamanan sebelum tool dieksekusi (write/command ke luar project) |
| 3 | `scripts/check_workspace_write_tools.py` | Tool tulis workspace (`write_file`/`edit_file`/`delete_file`/`move_file`): path confinement, penolakan keluar workspace, isolasi antar-root | Keamanan filesystem & isolasi antar-project pada jalur tulis Agent. Verifier standalone; ditambah entry point pytest (assertion sama) agar ikut critical suite — `python scripts\check_workspace_write_tools.py` tetap jalan seperti sebelumnya |
| 4 | `tests/test_change_tracker_runtime_integration.py` | `ChangeTracker` + runtime: evidence perubahan tanpa mencampur dua task | Deteksi perubahan file yang dilaporkan ke user, terikat task |
| 5 | `tests/test_change_tracker_incremental_snapshot.py` | Snapshot incremental + cache/root terisolasi | Jaminan tidak ada perubahan hilang/bocor antar-root |
| 6 | `tests/test_changes_persistence.py` | Persistensi change set ke task log (`TaskLogReader`) | Hasil perubahan harus tetap ada setelah task selesai |
| 7 | `tests/test_agent_execution_mode.py` | Propagasi execution mode ke `ToolExecutor`/runtime + `build_registry()` | Titik masuk eksekusi task Agent (preparation -> execution) |
| 8 | `tests/test_agent_execution_policy.py` | `AgentRuntime`, `ToolExecutor`, `AgentOrchestrator`, registry, policy | Jalur runtime/orchestrator inti |
| 9 | `tests/test_read_dedup.py` | `ToolReadCache`: dedup read, invalidation oleh mutasi, `force=true` | Retrieval Agent yang efisien & anti-stale |
| 10 | `tests/test_bug4_completion_gate.py` | Completion gate, loop/orchestrator, truncation recovery | Menentukan kapan task boleh dinyatakan selesai vs lanjut loop |
| 11 | `tests/test_provider_fallback_continuous.py` | Fallback provider pada loop kontinu | Ketahanan jalur provider saat model/endpoint gagal |
| 12 | `tests/test_provider_retry_lifecycle.py` | Retry/lifecycle provider & konsistensi status | Request provider tetap terikat pada lifecycle task |

---

## 3. Pemetaan modul -> test (scope-based splitting)

Jalankan **targeted** dulu untuk modul yang diubah; naikkan ke critical suite
bila perubahannya menyentuh lebih dari satu area.

| Modul/area yang berubah | Targeted test |
| --- | --- |
| `src/agent_ai/permission/**`, `src/agent_ai/projects/permissions.py`, `matrix.py` | `tests/test_permission_matrix.py`, `tests/test_project_policy.py`, `tests/test_config_separation.py` |
| `src/agent_ai/changes/**` | `tests/test_change_tracker_incremental_snapshot.py`, `tests/test_change_tracker_runtime_integration.py`, `tests/test_changes_persistence.py`, `tests/test_file_change_metadata.py` |
| `src/agent_ai/tools/workspace.py`, `filesystem.py`, `read_cache.py` | `scripts/check_workspace_write_tools.py`, `tests/test_read_dedup.py`, `tests/test_repeated_read_eviction.py`, `tests/test_context_aware_retrieval_cache.py` |
| `src/agent_ai/core/executor.py`, `orchestrator.py`, `loop.py` | `tests/test_agent_execution_mode.py`, `tests/test_agent_execution_policy.py`, `tests/test_bug4_completion_gate.py` |
| `src/agent_ai/runtime/**` | `tests/test_agent_execution_policy.py`, `tests/test_provider_fallback_continuous.py`, `tests/test_max_steps_batch_bound.py`, `tests/test_verification_strategy.py` |
| `src/agent_ai/providers/**` | `tests/test_provider_retry_lifecycle.py`, `tests/test_provider_fallback_continuous.py`, `tests/test_api_retry_config.py` |
| `src/agent_ai/tasks/**`, `src/agent_ai/task/**` | `tests/test_working_state_integration.py`, `tests/test_working_state_multround.py`, `tests/test_agent_execution_mode.py` |
| `src/agent_ai/config/settings.py`, `projects/registry.py` | `tests/test_config_separation.py`, `tests/test_global_settings.py`, `tests/test_project_settings.py` |
| `src/agent_ai/extensions/**` | `tests/test_extension_*.py` (bukan bagian critical suite) |
| `src/agent_ai/consultant/**` | `tests/test_consultant_*.py` (bukan bagian critical suite) |
| `web/django_app/api/**` | `python scripts\check_task_queue_scheduler.py`, `python scripts\check_task_lifecycle.py`, `tests/test_statics_api.py` |
| Frontend `web/frontend/**` | `python scripts\check_*.py` terkait (UI/dashboard/analytics/export), bukan pytest |

---

## 4. Yang sengaja TIDAK masuk critical suite

Cakupan sekunder/berat yang tidak menentukan apakah alur eksekusi Agent masih
berfungsi, atau memiliki verifier sendiri di `scripts/check_*.py`:

- Extension (13 berkas `tests/test_extension_*.py`) dan Playwright bridge;
- Consultant (6 berkas `tests/test_consultant_*.py`);
- Frontend/dashboard/analytics/export: `tests/test_statics_api.py`,
  `tests/test_statics_integration.py`, `tests/test_extension_ui.py`;
- Konfigurasi lanjutan yang tidak dipakai jalur runtime inti:
  `tests/test_agent_settings.py`, `tests/test_global_settings.py`,
  `tests/test_project_settings.py`, `tests/test_api_retry_config.py`,
  `tests/test_llm_*` (bila ada), `tests/test_usage_telemetry.py`;
- Compaction/context lanjutan: `tests/test_context_compaction.py`,
  `tests/test_context_continuity.py`, `tests/test_context_aware_retrieval_cache.py`,
  `tests/test_retrieval_dedup_vs_compaction.py`, `tests/test_retrieval_efficiency.py`,
  `tests/test_tool_result_compaction.py`,
  `tests/test_compression_switch.py`, `tests/test_repeated_read_eviction.py`,
  `tests/test_max_steps_batch_bound.py`, `tests/test_verification_strategy.py`,
  `tests/test_venv_resolver.py`, `tests/test_agent_view_image.py`,
  `tests/test_agent_policy_gateway_propagation.py`, `tests/test_review_capability.py`,
  `tests/test_response_api_logging.py`, `tests/test_truncation_*.py`,
  `tests/test_working_state_multround.py`, `tests/test_playwright_*.py`.

Semua berkas di atas **tetap hidup** dan tetap dijalankan oleh
`python -m pytest tests/ -q`.

---

## 5. Hasil eksekusi & waktu

Diukur pada environment ini (`J:\AETHER_DI_EDIT\aether-agent`, Windows, venv aktif):

| Suite | Hasil | Waktu |
| --- | --- | --- |
| Critical suite (`scripts\test_critical.bat`) | **166 passed** | **±27 s** (27,15 s) |
| Full regression (`python -m pytest tests/ -q`) | seluruh berkas test (59 berkas) | jauh lebih lambat (menit) |

Distribusi test per berkas di critical suite (dari `--collect-only`):
`test_agent_execution_policy.py` 33, `test_bug4_completion_gate.py` 31,
`test_permission_matrix.py` 15, `test_change_tracker_runtime_integration.py` 13,
`test_config_separation.py` 13, `test_read_dedup.py` 11,
`test_provider_retry_lifecycle.py` 11, `test_agent_execution_mode.py` 10,
`test_change_tracker_incremental_snapshot.py` 9,
`test_provider_fallback_continuous.py` 8,
`scripts/check_workspace_write_tools.py` 7, `test_changes_persistence.py` 5.

Test penting yang **belum** tercakup critical suite (kandidat pengembangan
berikutnya): task lifecycle & queue scheduler end-to-end (verifier
`scripts/check_task_lifecycle.py`, `scripts/check_task_queue_scheduler.py` —
berbasis script, bukan pytest), permission approval interaktif
(`tests/test_permission_approval.py`), isolasi sesi Consultant
(`tests/test_consultant_session_isolation.py`), serta jalur gateway penuh
(`tests/test_statics_api.py`, `tests/test_statics_integration.py`).

---

## 6. Catatan environment

`tests/conftest.py` menambahkan secara aditif (di `pytest_configure`):
`src` dan `web/django_app` ke `sys.path`, plus satu kali `django.setup()`
(`DJANGO_SETTINGS_MODULE=config.settings`). Ini membuat perintah pytest
bekerja dari direktori mana pun; tanpa ini sebagian test gagal saat runtime
import. Tidak ada test yang dilewati/dinonaktifkan karena perubahan ini.
