# Audit: Sistem Konfigurasi LLM (API Key → Provider Instance → Model)

Dokumen ini adalah hasil audit/desain fitur konfigurasi LLM untuk AETHER:
UI untuk menyimpan **API key**, membuat **provider instance**, dan mengelola
**model** per instance dengan relasi yang jelas:

```
API Key (.env)  ->  Provider Instance (SQLite)  ->  Model (SQLite)
  1 nama var         0/1 api_key_env              1 provider_id
  secret                (hanya NAMA var)           banyak model
```

## 1. Prinsip Desain

1. **Reuse, bukan layer baru.** Konfigurasi disimpan pada database GLOBAL yang
   sudah ada (`data/aether.db`) — database yang sama dipakai gateway/launcher
   (`web/django_app/api/project_store.py`). Tidak membuat database kedua.
2. **API key TIDAK pernah masuk SQLite.** Yang disimpan hanya **nama variabel
   `.env`** (`api_key_env`, mis. `OPENROUTER_API_KEY`). Nilai secret tetap di
   `.env` (sudah dimuat oleh `agent_ai/config/settings.py` via `python-dotenv`).
3. **Relasi eksplisit.** Provider type menentukan prefix env key, default API
   URL, dan apakah API key wajib. Ini memvalidasi relasi
   `provider instance ↔ nama variabel .env` sehingga konfigurasi tidak bisa
   "nyambung ke key yang salah".
4. **Aman untuk UI.** Pembacaan default tidak menyertakan nilai secret; hanya
   `masked`, `is_set`, `api_key_env`. Nilai asli hanya keluar lewat
   `resolve_runtime_config()` (untuk runtime provider).
5. **Enkapsulasi.** Modul lain cukup memanggil `LLMConfigService`; tidak perlu
   tahu detail SQLite atau isi `.env`.

## 2. Modul Baru

```
src/agent_ai/llm_config/
├── __init__.py      # Public API (facade) + contoh pemakaian
├── errors.py        # LLMConfigError, NotFound, Validation, Conflict
├── providers.py     # Katalog provider type + pola env API key
├── models.py        # ProviderInstance, ModelConfig, CredentialInfo
├── store.py         # Persistensi SQLite (tabel llm_provider_instances, llm_models)
├── env_file.py      # Editor .env aman + masking secret
└── service.py       # LLMConfigService (CRUD + credential + pembacaan runtime)
```

Verifier: `scripts/check_llm_config.py` (fixture sementara di `dummy_test/`).

## 3. Skema Database (pada `data/aether.db`)

Tabel ditambahkan secara idempotent (`CREATE TABLE IF NOT EXISTS`), tanpa
menyentuh tabel existing (`projects`, `app_state`).

### `llm_provider_instances`
| kolom | tipe | keterangan |
|---|---|---|
| `id` | TEXT PK | uuid hex |
| `name` | TEXT UNIQUE | nama instance (mis. "OpenRouter Utama") |
| `provider_type` | TEXT | kunci spec (openrouter/deepseek/openai/ollama) |
| `api_url` | TEXT | base URL (default dari provider type bila kosong) |
| `api_key_env` | TEXT | **NAMA** variabel `.env` (bukan nilai secret) |
| `enabled` | INTEGER | 0/1 |
| `created_at` / `updated_at` | TEXT | ISO-8601 UTC |

### `llm_models`
| kolom | tipe | keterangan |
|---|---|---|
| `id` | TEXT PK | uuid hex |
| `provider_id` | TEXT FK | → `llm_provider_instances(id)` **ON DELETE CASCADE** |
| `model_name` | TEXT | mis. `openai/gpt-4o-mini` |
| `enabled` | INTEGER | 0/1 |
| `created_at` / `updated_at` | TEXT | ISO-8601 UTC |

`UNIQUE(provider_id, model_name)` mencegah model duplikat per instance.
`PRAGMA foreign_keys = ON` di setiap koneksi mengaktifkan cascade.

## 4. Katalog Provider Type (`providers.py`)

| key | label | prefix env | default api_url | wajib key |
|---|---|---|---|---|
| `openrouter` | OpenRouter | `OPENROUTER` | `https://openrouter.ai/api/v1` | ya |
| `deepseek` | DeepSeek | `DEEPSEEK` | `https://api.deepseek.com/v1` | ya |
| `openai` | OpenAI | `OPENAI` | `https://api.openai.com/v1` | ya |
| `ollama` | Ollama (lokal) | `OLLAMA` | `http://127.0.0.1:11434` | **tidak** |

Pola env API key: `^<PREFIX>_API_KEY(_SUFFIX)?$` (case-insensitive).
- `OPENROUTER_API_KEY` → cocok.
- `OPENROUTER_API_KEY_AKUN_TEMAN` → cocok (multi-akun), label suffix
  "akun teman".
- `OPENROUTER_MODEL`, `OLLAMA_HOST`, `PATH` → **tidak** dianggap credential.

Kunci provider type ini sengaja **sama** dengan nama provider pada
`agent_ai/providers/registry.py` (`ollama|deepseek|openrouter|openai`), sehingga
`resolve_runtime_config()["provider_type"]` dapat langsung dipetakan ke
`registry.get(provider_type)` bila integrasi runtime ditambahkan.

## 5. API Publik (`LLMConfigService`)

CRUD provider instance:
- `create_provider_instance(name, provider_type, api_key_env="", api_url="", enabled=True)`
- `get_provider_instance(id)` / `list_provider_instances()`
- `update_provider_instance(id, *, name?, provider_type?, api_key_env?, api_url?, enabled?)`
- `delete_provider_instance(id)` → cascade ke model

CRUD model:
- `add_model(provider_id, model_name, enabled=True)`
- `get_model(id)` / `list_models(provider_id=None)`
- `update_model(id, *, model_name?, enabled?)`
- `delete_model(id)`

Credential `.env`:
- `list_credentials()` → daftar credential (discovery dari `.env` + yang
  dirujuk instance), dengan `masked`, `is_set`, `used_by`.
- `set_api_key(env_name, value)` → menulis baris terkait di `.env` + env proses.
- `get_api_key(env_name)` / `has_api_key(env_name)` (internal/runtime).
- `delete_api_key(env_name, force=False)` → ditolak bila masih dipakai instance
  kecuali `force=True`.

Pembacaan untuk modul lain:
- `get_provider_config(id, include_api_key=False)` → instance + model + status
  key (aman untuk UI).
- `resolve_runtime_config(id, model_id=..., model_name=..., include_api_key=True)`
  → provider_type, api_url, api_key, model siap pakai.
- `get_full_config()` → seluruh konfigurasi (masked).

Error: `LLMConfigError` (base) → `LLMConfigNotFoundError`,
`LLMConfigValidationError`, `LLMConfigConflictError` (nama/model duplikat).

## 6. Peta Dependensi

```
                  +-----------------------------+
                  | .env  (API key, nilai asli) |
                  +--------------+--------------+
                                 | api_key_env (nama var)
                                 v
   LLMConfigService  <---->  LLMConfigStore  --->  data/aether.db
   (service.py)                (store.py)          (DB GLOBAL reuse)
        |                          |
        | env_file.py              | models.py (ProviderInstance/ModelConfig)
        v
   .env (editor aman)

   Dikonsumsi oleh: (runtime/provider, UI, endpoint) — BELUM di-wire di tahap ini.
```

Hubungan dengan kode existing (TIDAK diubah):
- `agent_ai/config/settings.py` — sumber `PROJECT_ROOT`/`ENV_PATH` + sub-config
  provider existing (`OllamaConfig`, `DeepSeekConfig`, `OpenAIConfig`,
  `OpenRouterConfig`). Layer baru memakai `PROJECT_ROOT` untuk lokasi DB/`.env`.
- `agent_ai/providers/registry.py` — registry provider existing; `provider_type`
  layer baru selaras dengan nama di sini.
- `web/django_app/api/project_store.py` — pemilik `data/aether.db`; layer ini
  menambah tabel pada DB yang sama.

Tidak ada dependensi runtime AETHER terhadap `dummy_test/`. Verifier bersifat
opsional dan fixture-nya dibersihkan otomatis.

## 7. Alur (Flow)

**Menambah provider (UI → storage):**
1. User pilih provider type + isi nama (mis. "OpenRouter Utama") + pilih/ isi
   nama variabel key (`OPENROUTER_API_KEY`).
2. `create_provider_instance` validasi: nama unik, provider type dikenal,
   prefix env cocok/tidak wajib untuk lokal.
3. Baris disimpan di `llm_provider_instances`; `api_url` diisi default bila kosong.
4. Bila user memasukkan nilai key → `set_api_key("OPENROUTER_API_KEY", value)`
   menulis ke `.env` (baris itu saja) + memperbarui env proses.

**Menambah model:** `add_model(instance_id, "openai/gpt-4o-mini")` → baris di
`llm_models` dengan FK ke instance.

**Runtime membaca konfigurasi:** `resolve_runtime_config(instance_id)` →
`{provider_type, api_url, api_key, model}`; di sini (dan hanya di sini) nilai
secret dibaca dari `.env`.

## 8. Keamanan

- Nilai secret **tidak** pernah ditulis ke SQLite (diverifikasi: byte database
  tidak memuat secret).
- `list_credentials()`/`get_provider_config()` default tidak mengembalikan
  secret; hanya `masked` (mis. `sk-o***...7890`), `is_set`, `api_key_env`.
- Editor `.env` hanya mengubah **baris variabel terkait**; komentar dan variabel
  lain dipertahankan persis (write atomik via file `.tmp` + `os.replace`).
- `delete_api_key` menolak menghapus credential yang masih direferensikan
  instance kecuali `force=True`.

## 9. Verifikasi

```
python scripts/check_llm_config.py
python scripts/check_llm_runtime_integration.py
```

`check_llm_runtime_integration.py` menguji integrasi konfigurasi tersimpan ke
runtime: metadata task (`provider_instance_id` + `model_id`) dirakit menjadi
provider konkret (api_url/api_key/model dari SQLite) lewat
`agent_ai.providers.factory`, `model_id` benar-benar dipakai runtime
(`GenerateOptions.model`), validasi relasi ditolak `400 validation_error`,
instance tanpa API key membuat task `failed` dengan pesan jelas, dan jalur
default (tanpa `provider_instance_id`) tetap backward compatible. Fixture
(DB/.env/project sementara) dibuat di `dummy_test/` lalu dihapus otomatis.

Menguji: katalog provider type, pola env, validasi relasi, CRUD instance/model,
cascade delete, ketidak-bocoran secret ke SQLite, discovery credential + masking,
edit `.env` aman, dan pembacaan runtime. Fixture (DB + `.env` sementara) dibuat
di `dummy_test/` lalu dihapus otomatis. `data/aether.db` asli dan `.env` asli
tidak disentuh.

## 10. Batasan / Non-Goals (tahap ini)

- **Wiring runtime SUDAH ada** (di luar dokumen ini): `api/execution.py`
  merakit provider dari konfigurasi tersimpan lewat
  `agent_ai.providers.factory`, dan endpoint `POST /api/tasks` menerima
  `provider_instance_id` + `model_id` via metadata. Batch pemanggilan provider
  tetap memakai provider existing (`providers/*`), tanpa layer provider baru.
- **Satu credential → banyak instance** didukung (mis. `OPENROUTER_API_KEY`
  dipakai beberapa instance); satu instance menunjuk satu `api_key_env`.
- Tidak ada migrasi data dari `settings.py` ke SQLite: sub-config provider
  existing tetap berfungsi (backward compatible).
