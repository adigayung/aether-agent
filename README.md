# AETHER — AI Coding Agent

AETHER is a three-layer AI coding agent system: a pure-Python agent engine (`src/agent_ai/`), a thin Django API gateway (`web/django_app/`), and a Vue 3 + Vite single-page application frontend (`web/frontend/`). It supports local (Ollama) and cloud (DeepSeek, OpenRouter, OpenAI-compatible) LLM providers, a serial task queue, cooperative cancellation, persistent task logging, a Consultant reasoning layer, and a project knowledge base (Project Bible).

---

## Features

- **Agent execution loop** — continuous reasoning loop with tool-calling: file read/write, code search, terminal execution, and project map queries.
- **Global Task Queue** — serial execution (concurrency = 1). Tasks are submitted to a queue with states: pending, running, disabled, done. FIFO ordering with move-up/move-down, disable/enable, and remove controls.
- **Consultant Chat** — a reasoning layer (separate from the Agent) with two modes: **Quick** (Project Bible + Project Map read-only) and **Investigate** (Bible + Map + source/runtime tools). Generates Task Proposals that can be submitted to the Agent with per-task provider/model overrides. Supports image attachments.
- **Project Map** — code navigation via **Atlas** (Python symbol/file/method/class lookup with callers/callees/inheritance) and **RIG** (code relationship graph with edges: calls, imports, inherits, contains, depends_on, external). Queried on-demand by the LLM through tools. Only the Agent may call `refresh_project_map`.
- **Project Bible** — persistent knowledge store in `.aether/bible/<category>.md`, maintained by the Consultant.
- **Persistent task logging** — all task events are appended to `.aether/log/<task_id>.log` (JSONL format). Served via REST APIs: task history, per-task chronological activity, and final Agent report.
- **Live SSE events** — real-time event streaming from backend to frontend during task execution (`/api/events`).
- **GitHub Backup** — per-project backup to a Git remote. Configuration stored in `.aether/github/config.json`; GitHub token encrypted via Windows DPAPI.
- **Code Editor** — Monaco editor (lazy-loaded) embedded in a modal with syntax highlighting, dirty-state tracking, and save/close confirmation.
- **File Explorer** — tree-view file browser with recursive expansion, context menu (open, copy path, reveal in Explorer, rename, delete), and incremental live updates on filesystem changes.
- **Changes panel** — compact single-row-per-file display of workspace changes (add/modify/delete/move) with accordion diff.
- **Audio feedback** — sound effects on task start, success, failure, and cancellation (`assets/audio/`).
- **Task Card** — lifecycle stepper (Planning → Inspecting → Editing → Running → Validating → Completed) plus duration, provider, and model metadata.
- **Lifecycle: Task Queue | Task History** — QUEUE tab shows live pending/running/disabled tasks; HISTORY tab shows completed tasks with report viewing.

---

## Architecture

```
web/frontend/  (Vue 3 + Vite SPA)     ── served statically from dist/ ──┐
                                                                         │
                    ┌─────────────────────────────────────────────────────┤
                    │  HTTP /api/* + SSE /api/events                     │
                    ▼                                                     │
web/django_app/api/  (Django gateway)                                     │
    services.py  — GatewayService (task queue, scheduler, CRUD)           │
    views.py     — HTTP handlers                                          │
    urls.py      — route definitions                                      │
    execution.py — TaskExecutor (background daemon thread)                │
    streaming.py — SSE event dispatcher                                   │
    project_store.py — project registry                                   │
    github_backup.py — GitHub backup service                              │
                    │                                                     │
                    ▼                                                     │
src/agent_ai/  (pure Python core library — NO Django dependency)           │
    core/       — AgentOrchestrator, AgentLoop, ToolExecutor,             │
                  CancellationToken, ConversationHistory                  │
    runtime/    — AgentRuntime (task lifecycle, event emission)           │
    tools/      — file I/O, terminal, project-map, workspace mutation    │
    providers/  — Ollama, DeepSeek, OpenRouter, OpenAI-compatible        │
    consultant/ — reasoning layer (modes, tools, prompts, service)       │
    session/    — InMemorySessionStore, event types, SSE model           │
    projects/   — Project Bible, Project Map service, Aether Store      │
    vision/     — image input loader, preprocessor, vision policy        │
    git/        — GitRepositoryFacade (read-only)                        │
    permission/ — PermissionPolicy classifier                            │
```

**Data flow (task execution):**

```
User input → create_task → queue (pending) → scheduler (promotes to running)
  → TaskExecutor.run()
    → AgentOrchestrator.run_continuous_loop()
      → LLM generate() ↔ tool calls ↔ event emission
    → AgentRuntime._lifecycle_finalize()
  → slot released → scheduler picks next pending task

Events → InMemorySessionStore → SSE → frontend
Events → .aether/log/<task_id>.log (persistent JSONL)
```

---

## Repository Layout

```
aether-agent/
├── .aether/                  # Project-local runtime data
│   ├── bible/                # Project Bible (knowledge base)
│   ├── log/                  # Persistent task logs (JSONL)
│   ├── map/                  # Atlas + RIG maps
│   └── github/               # GitHub backup config + encrypted token
├── assets/
│   └── audio/                # Sound files (start, succeed, failed, stop)
├── data/
│   └── aether.db             # SQLite: projects, LLM provider instances, models
├── projects/                 # Workspace project folders (gitignored)
├── scripts/                  # Verification & utility scripts
│   ├── install_aether.py     # Portable installer (stdlib-only)
│   └── check_*.py            # 50+ verifiers (architecture, components, integration)
├── src/
│   └── agent_ai/             # Core library (Python, Django-free)
│       ├── core/             # Orchestrator, loop, executor, models
│       ├── runtime/          # Task lifecycle & event emission
│       ├── tools/            # File I/O, terminal, project-map, workspace
│       ├── providers/        # Ollama, DeepSeek, OpenRouter, OpenAI-compatible
│       ├── consultant/       # Reasoning layer (modes, tools, prompts, service)
│       ├── session/          # InMemorySessionStore, event types
│       ├── projects/         # Bible, map service, aether store
│       └── vision/           # Image input & preprocessing
├── web/
│   ├── django_app/           # Django gateway (API, executor, SSE)
│   │   ├── api/              # services, views, urls, execution, streaming
│   │   └── config/           # Django settings, wsgi
│   └── frontend/             # Vue 3 + Vite SPA
│       └── src/
│           ├── components/   # 13 Vue components
│           ├── App.vue       # Root component
│           ├── api.js         # API client
│           ├── styles.css    # Global styles
│           └── audioRegistry.js # Audio mapping
├── .env                      # Local configuration (gitignored)
├── deployment.template       # Environment template (no credentials)
├── .env.example              # Quick-start example
├── .gitignore
├── pyproject.toml            # Python packaging (src-layout)
├── requirements.txt          # Runtime dependencies
├── run.bat                   # Self-bootstrapping launcher
└── README.md                 # This file
```

---

## Prerequisites

| Tool      | Version       | Notes                                         |
|-----------|---------------|-----------------------------------------------|
| Windows   | 10 / 11       | AETHER is developed and tested on Windows      |
| Python    | >= 3.10       | Must be available on PATH                      |
| Git       | any recent    | Required for clone and backup features         |
| Node.js   | >= 18         | Required for frontend build (npm is used only when `node_modules` is missing) |
| Port 8000 | free          | Default AETHER gateway port                     |

AETHER does **not** install Python, Git, or Node.js automatically. If a tool is missing, the installer prints a clear message and stops.

---

## Installation & First Run

### Quick start

Double-click `run.bat` in the repository root, or run from a terminal:

```bat
run.bat
```

This self-bootstrapping launcher will:

1. Detect whether an AETHER folder exists (signatures: `pyproject.toml` + `web\django_app\manage.py`, or `manage.py` + `scripts\install_aether.py`).
2. If not found: `git clone https://github.com/adigayung/aether-agent.git` into a sibling folder.
3. Validate Python and Git availability.
4. Delegate all setup to `scripts/install_aether.py`:
   - Create a Python virtual environment (`<root>/venv/`).
   - Install runtime dependencies (`pip install -r requirements.txt`).
   - Copy `deployment.template` to `.env` (no credentials included).
   - Build the frontend via Vite (`node node_modules/vite/bin/vite.js build`).
   - Launch the Django gateway at `http://127.0.0.1:8000/`.

The launcher is **idempotent** — running it again skips completed steps.

### Dry-run simulation

Test the installer flow **without network access or any mutations**:

```bat
set AETHER_SIMULATE=1
run.bat
```

Or directly:

```powershell
python scripts\install_aether.py --simulate
python scripts\install_aether.py --simulate --root D:\empty\folder
```

### Installer CLI flags

| Flag                  | Description                                                      |
|-----------------------|------------------------------------------------------------------|
| `--root <path>`       | AETHER installation root (default: parent of `scripts/`)          |
| `--host <host>`       | Gateway bind host (default: `127.0.0.1`)                         |
| `--port <port>`       | Gateway port (default: `8000`)                                    |
| `--check`             | Verify prerequisites only; do not change anything                 |
| `--simulate`          | Dry-run offline (no network, no mutations)                        |
| `--no-launch`         | Setup only; do not start the server                               |
| `--skip-frontend`     | Skip frontend build                                               |
| `--rebuild-frontend`  | Force frontend rebuild even if `dist/` exists                     |
| `--force-deps`        | Reinstall Python dependencies even if already satisfied           |
| `--no-browser`        | Do not open browser automatically on launch                       |

### Environment variables

The installer accepts `AETHER_INSTALL_DIR` (override installation folder) and `AETHER_ARGS` (pass extra arguments to the installer). Non-interactive mode: `AETHER_NONINTERACTIVE=1` suppresses pauses.

---

## Configuration

### .env file

AETHER reads configuration from `.env` (via `python-dotenv`). A template is provided as `deployment.template` — copy it and fill secrets:

```powershell
Copy-Item deployment.template .env
```

Key environment variables:

| Variable                  | Description                                              |
|---------------------------|----------------------------------------------------------|
| `OLLAMA_HOST`             | Ollama server URL (default: `http://127.0.0.1:11434`)     |
| `OLLAMA_MODEL`            | Default Ollama model                                     |
| `OLLAMA_TIMEOUT`          | Ollama request timeout (seconds)                          |
| `DEEPSEEK_API_KEY`        | DeepSeek API key (optional)                               |
| `OPENROUTER_API_KEY`      | OpenRouter API key (optional)                             |
| `OPENAI_API_KEY`          | OpenAI API key (optional)                                 |
| `LOG_LEVEL`               | Logging level (default: `INFO`)                           |
| `AETHER_ENV`              | `development` or `production` (controls Django security)  |
| `DJANGO_SECRET_KEY`       | Django secret key (required in production)                |
| `DJANGO_DEBUG`            | `true` (dev) or `false` (prod)                            |
| `DJANGO_ALLOWED_HOSTS`    | Comma-separated host list (required in production)        |
| `PERMISSION_*`            | Action permission overrides (allow / deny / require_approval) |

Additional environment variables for Project Map engine paths:

| Variable                      | Description                                      |
|-------------------------------|--------------------------------------------------|
| `AETHER_CODE_ATLAS_DIR`       | Path to Atlas engine (default: `<AETHER_ROOT>\vendor\CODE_ATLAS`) |
| `AETHER_MAP_CODE_RIG_DIR`     | Path to RIG engine (default: `<AETHER_ROOT>\vendor\MAP_CODE_RIG`) |
| `AETHER_DIR`                  | AETHER root directory (used by gateway at runtime) |
| `OLLAMA_NUM_CTX`              | Ollama context window size (default: 32768)       |

### LLM Provider & Model configuration

Provider instances and models are managed through the AETHER Settings UI and stored in `data/aether.db` (tables `llm_provider_instances` and `llm_models`). The `.env` file only stores API secrets; the active provider is selected from the UI.

---

## Usage

### Workbench

The main interface is the Workbench (http://127.0.0.1:8000/). It contains:

- **Agent Input** — textarea for submitting new tasks to the Agent. Multiline support (Enter = newline). Send submits the task to the global queue.
- **Agent Activity** — chronological timeline of agent events (commentary, tool calls, tool results, observations, final report).
- **Task Card** — displays the latest task with lifecycle stepper, duration, provider, and model metadata.
- **TASKS page** — two tabs: **QUEUE** (live pending/running/disabled tasks with controls: Stop, Disable/Enable, Move Up/Down, Remove) and **HISTORY** (completed tasks with report viewing).
- **Projects page** — project registry listing with GitHub Backup status and per-project GitHub configuration. Delete removes the project from the registry only (files on disk are untouched).
- **Backup page** — GitHub backup configuration for the active project (repository, branch, token, checkpoints, restore).
- **Settings page** — LLM provider instance and model management, API key input.
- **File Explorer** — tree-view file browser with recursive expansion, context menu, and live updates on filesystem changes.
- **Changes panel** — workspace change summary with accordion diff.
- **Code Editor** — Monaco-based editor modal for viewing/editing files.

### Consultant Chat

Open the Consultant Chat (from the sidebar or Agent Input area) to interact with the AETHER reasoning layer:

- **Quick mode** — answers questions from Project Bible + Project Map (read-only). No source/runtime tools. Suitable for architectural and structural questions.
- **Investigate mode** — has access to source/runtime tools (`list_files`, `read_file`, `search_code`, `run_command`) in addition to Bible + Map, for in-depth investigation.
- The Consultant can generate a **Task Proposal** (a structured ````task block) with a **Run Task** button that submits it to the Agent. The proposal card includes per-task provider/model selection dropdowns and a copy button.
- Image attachments (JPEG, PNG, WebP) are supported.

### Task Queue

- All tasks (from Consultant Run Task or Agent Input) enter a **single global queue**.
- Execution is serial (concurrency = 1). The scheduler promotes the first `pending` task when the execution slot is free.
- Tasks can be `disabled` (skipped by the scheduler without removal) or `removed` from the queue.
- Stopping a running task triggers cooperative cancellation at the next safe boundary.

---

## Data & State

| Data                              | Location                             | Format               | Notes                                      |
|-----------------------------------|--------------------------------------|----------------------|--------------------------------------------|
| Project registry                  | `data/aether.db`                     | SQLite               | Tables: `projects`, `app_state`            |
| LLM configuration                 | `data/aether.db`                     | SQLite               | Tables: `llm_provider_instances`, `llm_models` |
| Task logs (persistent)            | `.aether/log/<task_id>.log`          | JSONL                | Append-only, line-delimited JSON           |
| Project Bible                     | `.aether/bible/<category>.md`        | Markdown             | Consultant-maintained knowledge            |
| Project Map (Atlas)               | `.aether/map/atlas.json`             | JSON                 | Code structure + navigation                |
| Project Map (RIG)                 | `.aether/map/rig.json`               | JSON                 | Code relationship graph                    |
| GitHub backup config              | `.aether/github/config.json`         | JSON                 | Per-project config                         |
| GitHub backup token (encrypted)   | `.aether/github/credential.enc`      | DPAPI-encrypted      | Bound to Windows user + machine            |
| Workspace projects                | `projects/<uuid>/`                   | Various              | User-created project workspaces            |
| Audio assets                      | `assets/audio/*.wav`                 | WAV                  | start, succeed, failed, stop               |

---

## API Endpoints

The Django gateway exposes the following API routes (all under `/api/`):

| Endpoint                                    | Method(s) | Description                          |
|---------------------------------------------|-----------|--------------------------------------|
| `health`                                    | GET       | Health check                         |
| `config`                                    | GET       | Gateway configuration                |
| `llm/config`                                | GET/POST  | LLM configuration                    |
| `llm/credentials`                           | GET/POST  | LLM API credentials                  |
| `llm/credentials/delete`                    | POST      | Delete a credential                  |
| `llm/providers`                             | GET       | List LLM provider instances          |
| `llm/providers/<id>`                        | GET       | Provider instance detail             |
| `llm/models`                                | GET       | List LLM models                      |
| `llm/models/<id>`                           | GET       | Model detail                         |
| `projects`                                  | GET       | List projects                        |
| `projects/<id>/github`                      | GET/POST  | GitHub backup config per project     |
| `projects/<id>/github/test`                 | POST      | Test GitHub connection               |
| `projects/<id>/github/checkpoints`          | GET       | List checkpoints (git log)           |
| `projects/<id>/github/restore`              | POST      | Restore a checkpoint                 |
| `projects/<id>`                             | DELETE    | Delete project from registry         |
| `active-project`                            | GET/POST  | Get/set active project               |
| `open-in-explorer`                          | POST      | Open folder in Windows Explorer      |
| `reveal-in-explorer`                        | POST      | Reveal file in Windows Explorer      |
| `delete-entry`                              | POST      | Delete file/folder from workspace    |
| `files`                                     | GET       | List files in a directory            |
| `files/content`                             | GET/POST  | Read/write file content              |
| `tasks`                                     | GET/POST  | List/create tasks                    |
| `tasks/queue`                               | GET       | List queue tasks                     |
| `tasks/queue/<id>/disable`                  | POST      | Disable a pending task               |
| `tasks/queue/<id>/enable`                   | POST      | Enable a disabled task               |
| `tasks/queue/<id>/move`                     | POST      | Reorder a queue task                 |
| `tasks/queue/<id>/remove`                   | POST      | Remove a task from queue             |
| `tasks/history`                             | GET       | List task history (from `.aether/log`). Sorted newest → oldest |
| `tasks/history/<id>`                        | GET       | Task history detail                  |
| `tasks/<id>`                                | GET       | Get task status                      |
| `tasks/<id>/cancel`                         | POST      | Cancel a running task                |
| `tasks/<id>/activity`                       | GET       | Chronological task events            |
| `tasks/<id>/report`                         | GET       | Final Agent report                   |
| `consultant/consult`                        | POST      | Consultant chat request              |
| `events`                                    | GET       | SSE event stream                     |

**Note:** literal route segments (e.g., `tasks/queue`, `tasks/history`) are registered before parameterized segments (`tasks/<task_id>`) to avoid shadowing.

---

## Verification

AETHER includes 50+ verification scripts under `scripts/check_*.py`. Run them from the repository root with the AETHER virtual environment active:

```powershell
.\venv\Scripts\python.exe scripts\check_workbench.py
.\venv\Scripts\python.exe scripts\check_task_queue_scheduler.py
.\venv\Scripts\python.exe scripts\check_consultant.py
.\venv\Scripts\python.exe scripts\check_cancellation.py
.\venv\Scripts\python.exe scripts\check_final_report.py
```

Some scripts require the Django gateway and a configured LLM provider. Pre-existing failures (e.g., check_architecture item [5], check_workbench step [2] with stale string assertions) are unrelated to current changes.

Example — offline verification of the installer flow:

```powershell
python scripts\check_installer.py
```

---

## Development Notes

### Frontend rebuild

The Vue frontend is served as a static production build from `web/frontend/dist/`. After any UI change, rebuild with:

```powershell
cd web\frontend
node node_modules\vite\bin\vite.js build
```

(`npm` is not always available in the AETHER subprocess; use the Node.js entrypoint directly.)

After rebuilding, perform a hard refresh (Ctrl+F5) in the browser if the running instance serves a cached bundle.

### Architecture constraints

- `src/agent_ai/` is a pure Python library and **must not depend on Django** or any web framework.
- The Django gateway (`web/django_app/api/`) is the only layer that imports Django.
- All changes must maintain this separation.
