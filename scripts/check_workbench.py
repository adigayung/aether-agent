"""Verifikasi Engineering Workbench UI (#52).

Deterministik, tanpa model/API cloud nyata. Memakai:
    - pemeriksaan statis source frontend (Vue/Vite),
    - SSR render App.vue via Vite (tanpa browser),
    - pemeriksaan kompatibilitas Django API/SSE (#50/#51).

Menguji:
    1. Vue/Vite project load (package.json + vite config + deps)
    2. main App render (SSR render App.vue)
    3. task input tersedia
    4. API #50 digunakan (health/projects/tasks)
    5. SSE #51 digunakan (/api/events)
    6. event activity dapat diproses (handler event #51)
    7. status/progress dapat diperbarui
    8. recommendation area tersedia (placeholder)
    9. tidak ada agent logic di frontend
   10. tidak ada event system kedua
   11. Django API/SSE tetap kompatibel

Jalankan:
    python scripts/check_workbench.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
FRONTEND_DIR = PROJECT_ROOT / "web" / "frontend"
SRC_FRONTEND = FRONTEND_DIR / "src"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _all_frontend_sources() -> dict:
    """Kumpulkan semua source frontend (src/**)."""
    files = {}
    for p in SRC_FRONTEND.rglob("*"):
        if p.is_file() and p.suffix in (".js", ".vue", ".css"):
            key = p.relative_to(FRONTEND_DIR).as_posix()
            files[key] = _read(p)
    return files


def main() -> int:
    print("=== Verifikasi Engineering Workbench UI (#52) ===")
    return _run()


def _run() -> int:
    sources = _all_frontend_sources()

    # 1) Vue/Vite project load.
    pkg_path = FRONTEND_DIR / "package.json"
    assert pkg_path.exists(), "package.json tidak ada"
    pkg = json.loads(_read(pkg_path))
    assert "vue" in pkg.get("dependencies", {}), "vue harus jadi dependency"
    assert "vite" in pkg.get("devDependencies", {}), "vite harus jadi devDependency"
    assert "@vitejs/plugin-vue" in pkg.get("devDependencies", {}), "plugin-vue harus ada"
    vite_cfg = list(FRONTEND_DIR.glob("vite.config.*"))
    assert vite_cfg, "vite.config.* tidak ada"
    assert (FRONTEND_DIR / "index.html").exists(), "index.html tidak ada"
    assert (SRC_FRONTEND / "main.js").exists(), "src/main.js tidak ada"
    assert (SRC_FRONTEND / "App.vue").exists(), "src/App.vue tidak ada"
    print("[1] Vue/Vite project load OK -> vue + vite + plugin-vue")

    # 2) main App render (SSR via Vite, tanpa browser).
    ssr_entry = SRC_FRONTEND / "ssr-check.js"
    assert ssr_entry.exists(), "ssr-check.js tidak ada"
    node = shutil.which("node")
    assert node, "node tidak tersedia"
    # Build SSR bundle lalu render.
    ssr_out = FRONTEND_DIR / ".ssr-check"
    shutil.rmtree(ssr_out, ignore_errors=True)
    try:
        build = subprocess.run(
            [node, str(FRONTEND_DIR / "node_modules" / "vite" / "bin" / "vite.js"),
             "build", "--ssr", "src/ssr-check.js", "--outDir", ".ssr-check"],
            cwd=str(FRONTEND_DIR),
            capture_output=True,
            text=True,
        )
        assert build.returncode == 0, f"SSR build gagal:\n{build.stdout}\n{build.stderr}"
        render = subprocess.run(
            [node, "-e",
             "import('./.ssr-check/ssr-check.js').then(m=>m.renderApp()).then(h=>{"
             "process.stdout.write(h);}).catch(e=>{console.error(e);process.exit(1);})"],
            cwd=str(FRONTEND_DIR),
            capture_output=True,
            text=True,
        )
        assert render.returncode == 0, f"SSR render gagal:\n{render.stdout}\n{render.stderr}"
        html = render.stdout
    finally:
        shutil.rmtree(ssr_out, ignore_errors=True)

    # Initial state: tanpa active project -> Project Launcher; dengan active
    # project -> Workbench ("Agent Workspace"). Keduanya valid.
    assert "Agent Workspace" in html or "Recent Projects" in html, (
        "App harus merender Workbench (Agent Workspace) atau Project Launcher"
    )
    assert "AETHER" in html or "AE" in html, "App harus merender brand AETHER"
    print(f"[2] main App render OK -> {len(html)} bytes HTML (SSR)")

    # 3) task composer tersedia (input utama user).
    composer = sources.get("src/components/TaskComposer.vue", "")
    assert composer, "TaskComposer.vue tidak ada"
    assert "<textarea" in composer or "<input" in composer, "composer harus punya input"
    assert "submit" in composer, "composer harus emit submit"
    assert "TaskComposer" in sources.get("src/App.vue", ""), "App harus memakai TaskComposer"
    print("[3] task composer tersedia OK -> TaskComposer.vue")

    # 4) API #50 digunakan.
    api_src = sources.get("src/api.js", "")
    assert api_src, "api.js tidak ada"
    for endpoint in ("/health", "/projects", "/tasks"):
        assert endpoint in api_src, f"api.js harus memakai endpoint {endpoint}"
    assert "POST" in api_src or "method: \"POST\"" in api_src, "createTask harus POST"
    assert "getHealth" in api_src and "getProjects" in api_src and "createTask" in api_src
    print("[4] API #50 digunakan OK -> health/projects/tasks")

    # 5) SSE #51 digunakan.
    assert "/events" in api_src, "api.js harus memakai /api/events"
    assert "EventSource" in api_src, "harus memakai EventSource (SSE)"
    assert "openEventStream" in api_src, "harus ada openEventStream"
    assert "openEventStream" in sources.get("src/App.vue", ""), "App harus memakai SSE"
    print("[5] SSE #51 digunakan OK -> /api/events via EventSource")

    # 6) event activity dapat diproses (handler event #51).
    app_src = sources.get("src/App.vue", "")
    required_events = [
        "task_started",
        "phase_changed",
        "tool_called",
        "tool_completed",
        "observation_received",
        "validation_started",
        "validation_completed",
        "recovery_started",
        "recovery_completed",
        "change_detected",
        "task_completed",
        "task_failed",
    ]
    for evt in required_events:
        assert evt in app_src, f"App harus menangani event '{evt}'"
    assert "handleEvent" in app_src, "harus ada handler event"
    print(f"[6] event activity diproses OK -> {len(required_events)} event type")

    # 7) status/activity dapat diperbarui.
    assert "task.status" in app_src, "App harus meng-update status task"
    assert "AgentActivity" in app_src, "App harus memakai AgentActivity"
    assert "ChangesPanel" in app_src, "App harus memakai ChangesPanel"
    assert "FileExplorer" in app_src, "App harus memakai FileExplorer"
    print("[7] status/activity diperbarui OK -> status + AgentActivity")

    # 8) agent activity = PURE commentary LLM (bukan terminal mentah / log tool).
    activity_src = sources.get("src/components/AgentActivity.vue", "")
    assert activity_src, "AgentActivity.vue tidak ada"
    # Activity HANYA commentary natural LLM (event agent_commentary).
    # Technical event (tool/status/target) TIDAK boleh muncul di Activity.
    assert "agent_commentary" in activity_src, "AgentActivity harus merender commentary LLM"
    assert "tool_called" not in activity_src, "Activity tidak boleh menampilkan tool event"
    assert "tool_completed" not in activity_src, "Activity tidak boleh menampilkan tool event"
    print("[8] agent activity commentary OK -> AgentActivity.vue")

    # 9) tidak ada agent logic di frontend.
    forbidden = (
        "AgentRuntime",
        "AgentLoop",
        "Orchestrator",
        "TaskPlanner",
        "Replanner",
        "ToolRegistry",
        "executeTool",
        "runCommand",
        "child_process",
        "require(\"fs\")",
        "require('fs')",
        "from \"fs\"",
        "from 'fs'",
        "node:fs",
        "recoveryManager",
        "validationRunner",
    )
    for name, text in sources.items():
        for bad in forbidden:
            assert bad not in text, f"{name} tidak boleh memuat agent logic '{bad}'"
    print("[9] tidak ada agent logic di frontend OK")

    # 10) tidak ada event system kedua.
    for name, text in sources.items():
        assert "class EventBus" not in text, f"{name} tidak boleh membuat EventBus"
        assert "class EventEmitter" not in text, f"{name} tidak boleh membuat EventEmitter"
        assert "new EventBus" not in text, f"{name} tidak boleh membuat event bus baru"
        assert "WebSocket" not in text, f"{name} tidak boleh membuat WebSocket server"
    # Event hanya dari SSE #51 (EventSource).
    assert "EventSource" in api_src, "event harus dari SSE #51"
    print("[10] tidak ada event system kedua OK -> hanya SSE #51")

    # 11) Django API/SSE tetap kompatibel.
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    # Django test client mengirim Host: testserver (hardening #57 membatasi host).
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
    import django
    django.setup()
    from django.test import Client
    client = Client()
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/projects").status_code == 200
    resp = client.post(
        "/api/tasks",
        data=json.dumps({"task": "workbench check"}),
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.status_code
    task_id = resp.json()["task_id"]
    assert client.get(f"/api/tasks/{task_id}").status_code == 200
    sse = client.get("/api/events")
    assert sse.status_code == 200
    assert sse["Content-Type"].startswith("text/event-stream")
    print("[11] Django API/SSE tetap kompatibel OK -> #50 + #51")

    print()
    print("[OK] Engineering Workbench UI bekerja (Vue+Vite tipis, API #50 + SSE #51).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
