# Architecture Audit — AETHER Core

Tanggal: audit tahap "Architecture Audit + Core Hardening".
Scope: seluruh source Python di `src/agent_ai` (tanpa venv/.git/dummy_test/cache).

## Ringkasan

Arsitektur core **sehat**. Tidak ditemukan circular import runtime, tidak ada
provider-specific code yang bocor ke core, tidak ada duplicate command
execution, dan workspace boundary konsisten. Hanya ditemukan satu inkonsistensi
public API (low severity) yang diperbaiki langsung.

## Dependency direction (top-level package)

```
core        -> config, providers, tools, projects(type-only), reliability
providers   -> core(response), config
runtime     -> core, planning, providers, task
task        -> contextbuilder, planning
projects    -> providers, tools
contextbuilder -> codeindex
changes     -> tools
validation  -> tools
mcp         -> tools
reliability -> (mandiri)
capabilities-> (mandiri)
```

## Temuan

### T1 — `core` <-> `providers` coupling dua arah (severity: LOW, tidak diubah)
- `core/orchestrator.py` mengimpor `providers.base` (BaseProvider, dll).
- `providers/base.py` mengimpor `core.response` (LLMResponse, FinishReason).
- **Alasan tidak diubah**: `LLMResponse` adalah protocol internal bersama yang
  memang provider-agnostic. Import cycle sudah dimitigasi dengan import lokal
  di dalam method `normalize_response`. `core/response.py` tidak mengimpor
  providers, sehingga tidak ada circular import runtime (terverifikasi).
- Ini desain yang disengaja, bukan bug.

### T2 — `core/orchestrator.py` -> `projects.brain` (severity: LOW, tidak diubah)
- Import berada di dalam blok `if TYPE_CHECKING`, jadi tidak ada runtime
  dependency core -> projects. Pola yang benar.

### T3 — `providers/__init__.py` tidak mengekspor `OpenRouterProvider` (severity: LOW, DIPERBAIKI)
- Provider bawaan lain (Ollama/DeepSeek/OpenAICompatible) diekspor, tetapi
  `OpenRouterProvider` tidak, padahal sudah terdaftar di registry.
- **Perubahan**: menambahkan ekspor `OpenRouterProvider`, `ToolDefinition`,
  `ToolChoice` di `providers/__init__.py` untuk konsistensi public API.

### T4 — Dua implementasi `os.walk` (severity: LOW, tidak diubah)
- `tools/filesystem.py::_iter_files` dan `codeindex/indexer.py::_iter_source_files`.
- **Alasan tidak diubah**: kebutuhan berbeda (listing generik vs indexing dengan
  filter bahasa + size + sorting + ignore set berbeda). Menggabungkan akan
  menambah coupling tanpa manfaat nyata. Dicatat sebagai technical debt minor.

### T5 — `changes/__init__.py` self-import `from agent_ai.changes import diff` (severity: INFO, tidak diubah)
- Valid Python (submodule), terverifikasi import OK. Bukan masalah arsitektur.

## Verifikasi prinsip arsitektur

| Prinsip | Status |
|---|---|
| Provider-agnostic core | OK (tidak ada import provider konkret di core) |
| Reliability = layer pendukung | OK (reliability tidak impor core) |
| Capabilities = deklaratif | OK (tidak impor core/providers, tidak routing) |
| Changes = observability | OK (tidak impor core/reliability) |
| Workspace boundary | OK (semua tool tulis/hapus pakai `_resolve_within_root`; run_command pakai cwd=root, shell=False) |
| Tidak ada duplicate command execution | OK (hanya `tools/terminal.py`) |
| Responsibility jelas (Runtime/Orchestrator/Loop/Executor) | OK |
| Tidak ada circular import runtime | OK (16 package importable) |

## Perubahan yang dilakukan

- `src/agent_ai/providers/__init__.py`: tambah ekspor `OpenRouterProvider`,
  `ToolDefinition`, `ToolChoice` (public API consistency).
- `scripts/check_architecture.py`: verifier arsitektur baru (12 pemeriksaan).

## Hal yang sengaja tidak diubah

- Coupling `core` <-> `providers` (T1): desain disengaja, sudah aman.
- `TYPE_CHECKING` import core -> projects (T2): pola benar.
- Dua `os.walk` (T4): kebutuhan berbeda.
- Tidak ada rename module/package, tidak ada refactor besar.

## Technical debt yang tersisa

1. **T1**: `core` <-> `providers` coupling dua arah. Idealnya `LLMResponse`
   dipindah ke package netral (mis. `agent_ai.protocol`) agar arah dependency
   satu arah. Belum dilakukan karena berisiko (banyak konsumen) dan tidak
   mendesak.
2. **T4**: potensi konsolidasi iterasi filesystem bila kebutuhan menyatu.
3. **check_provider.py** bergantung pada environment (`.env`). Bila
   `DEEPSEEK_API_KEY` terisi, verifier menganggap deepseek "tersedia" dan
   gagal. Ini bukan bug arsitektur; verifier sebaiknya memakai config terisolasi
   (mock) agar deterministik. Di luar scope audit ini.
