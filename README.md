# Agent AI

Fondasi project coding agent berbasis Python. Tahap ini berisi **core library**
(konfigurasi + provider AI) yang nantinya dipakai oleh Agent Core dan berbagai
interface layer (CLI, Django REST API, Desktop UI).

## Arsitektur

```
Django Web/API   (web/django_app)   <- interface layer (belum dibuat)
      |
Agent Core                          <- belum dibuat
      |
Model Provider  (src/agent_ai/providers)
      |
Ollama / DeepSeek / OpenAI / provider lain
```

Prinsip utama: **`src/agent_ai/` adalah core library dan tidak boleh bergantung
pada Django** atau framework web apa pun. Django hanya menjadi interface layer
yang memakai `agent_ai`.

## Struktur

```
Agent_Ai/
├── .env                 # konfigurasi aktual (jangan di-commit)
├── deployment.template  # template konfigurasi deployment (tanpa credential)
├── .gitignore
├── pyproject.toml       # packaging Python (src-layout, package agent_ai)
├── requirements.txt
├── README.md
├── venv/                # virtual environment (jangan di-commit)
├── src/
│   └── agent_ai/        # core library (tanpa Django)
│       ├── __init__.py
│       ├── config/
│       │   ├── __init__.py
│       │   └── settings.py
│       └── providers/
│           ├── __init__.py
│           ├── base.py      # interface BaseProvider
│           ├── ollama.py    # implementasi Ollama
│           └── registry.py  # registry provider
├── web/
│   ├── django_app/      # Django Gateway (HTTP layer tipis)
│   └── frontend/        # Vue Workbench (Vite)
├── scripts/
│   ├── run_backend.ps1  # jalankan Django Gateway
│   ├── run_frontend.ps1 # jalankan Vue Workbench
│   └── check_packaging.py
└── tests/
    └── __init__.py
```

## Setup

```powershell
# Aktifkan virtual environment
.\venv\Scripts\Activate.ps1

# Install dependency (runtime)
pip install -r requirements.txt

# Alternatif: install sebagai package (dari root project, src-layout)
pip install -e .

# Siapkan konfigurasi
Copy-Item deployment.template .env
# lalu isi nilai di .env (API key HANYA di .env, jangan di source code)
```

## Menjalankan (Run)

AETHER terdiri dari dua bagian: **Django Gateway** (backend) dan **Vue Workbench**
(frontend). Jalankan backend lebih dulu.

```powershell
# 1) Backend: Django Gateway (default http://127.0.0.1:8000)
.\scripts\run_backend.ps1

# 2) Frontend: Vue Workbench (default http://127.0.0.1:5173)
.\scripts\run_frontend.ps1
```

Frontend (Vite) mem-proxy `/api` ke backend, sehingga tidak perlu konfigurasi
host tambahan. Untuk menjalankan backend di host/port lain:

```powershell
.\scripts\run_backend.ps1 -BindHost 0.0.0.0 -Port 8000
```

## Deployment

Deployment dasar (tanpa container/orchestrator):

1. Siapkan environment: `Copy-Item deployment.template .env` lalu isi nilainya.
   Template **tidak** memuat credential/API key apa pun.
2. Install dependency: `pip install -r requirements.txt` (atau `pip install -e .`).
3. Jalankan backend Django (mis. via `scripts/run_backend.ps1` atau
   `python web/django_app/manage.py runserver`).
4. Build frontend untuk produksi lalu sajikan statisnya:

   ```powershell
   cd web\frontend
   npm install
   npm run build   # hasil di web/frontend/dist
   ```

Catatan: `agent_ai` (core library) tetap **tidak** bergantung pada Django;
Django hanya interface layer. Tidak ada database/persistence baru pada tahap ini.

### Konfigurasi production (hardening)

Set `AETHER_ENV=production` dan isi variabel berikut di `.env`:

| Variabel | Deskripsi |
|----------|-----------|
| `AETHER_ENV` | `development` (default) atau `production` |
| `DJANGO_SECRET_KEY` | **Wajib** di production. Hasilkan: `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `DJANGO_DEBUG` | `false` di production |
| `DJANGO_ALLOWED_HOSTS` | **Wajib** di production (daftar host dipisah koma) |

Di production, Django akan **fail-fast** bila `DJANGO_SECRET_KEY` kosong,
`DEBUG=true`, atau `ALLOWED_HOSTS` kosong. Security berikut aktif otomatis
(dapat di-override lewat environment): `SECURE_SSL_REDIRECT`,
`SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, HSTS, `SECURE_CONTENT_TYPE_NOSNIFF`,
`X_FRAME_OPTIONS=DENY`.

Bila `web/frontend/dist` ada (hasil `npm run build`), Django menyajikan
`index.html` + `assets/` sebagai static — cukup satu proses untuk backend +
frontend production.

## Konfigurasi

Konfigurasi dibaca dari `.env` (via `python-dotenv`). Variabel utama:

| Variabel | Deskripsi |
|----------|-----------|
| `OLLAMA_HOST` | URL server Ollama (default `http://127.0.0.1:11434`) |
| `OLLAMA_MODEL` | Model Ollama (default `qwen2.5-coder:7b`) |
| `DEFAULT_PROVIDER` | Provider default (default `ollama`) |
| `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` | Placeholder provider cloud (tahap berikutnya) |

API key cloud **tidak** ditulis di source code, hanya di `.env`.

## Verifikasi provider

```powershell
.\venv\Scripts\python.exe scripts\check_provider.py
```

Script ini mengambil provider dari registry, memanggil `provider.generate(...)`,
dan menampilkan response ke terminal.

## Catatan tahap ini

Belum dibuat (sesuai scope): Agent Core, tools, database, dan project Django.
