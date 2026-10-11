"""Verifikasi Project Settings AETHER (per project): permissions + Agents.

Membuktikan konfigurasi per-project terpusat di `.aether/settings/`:

    1. Project Permission Matrix disimpan di
       `<root>/.aether/settings/permissions.json` (bukan `.aether/permissions.json`).
    2. MIGRASI AMAN: file LAMA (`.aether/permissions.json`) dipindahkan ke lokasi
       baru saat file baru BELUM ada; bila keduanya ada, file BARU dipertahankan
       (tidak ditimpa).
    3. System Prompt Agent & Consultant per-project disimpan di
       `<root>/.aether/settings/agent.json` / `consultant.json`.
    4. Runtime memakai override prompt project (bila ada) dan jatuh ke default
       existing bila tidak ada (tanpa sistem prompt kedua).
    5. Provider/model/base URL/API key TIDAK masuk konfigurasi project (tetap
       GLOBAL) — endpoint Agents menolak key global.
    6. Boundary frontend: Project Settings punya tab Security + Agents; System
       Prompt tidak lagi di Settings -> Agent (global).

Deterministik & offline: Django test client dengan SETTINGS_PATH sementara dan
registry/SQLite di fixture `dummy_test` (tidak menyentuh data produksi).

Jalankan:
    python scripts/check_project_settings.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
FRONTEND_DIR = PROJECT_ROOT / "web" / "frontend" / "src"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIX_ROOT = DUMMY_ROOT / "project_settings_fixture"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _solution_dir(root: Path) -> Path:
    return root / ".aether" / "settings"


def _run() -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"

    import django

    django.setup()
    from django.test import Client

    from agent_ai.config import settings as settings_mod
    from agent_ai.llm_config import LLMConfigService
    from agent_ai.projects.project_settings import ProjectSettingsStore
    from agent_ai.projects.registry import ProjectRegistry

    import api.services as services_mod
    from api.project_store import ProjectStore

    shutil.rmtree(FIX_ROOT, ignore_errors=True)
    root_a = FIX_ROOT / "proj_a"
    root_b = FIX_ROOT / "proj_b"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)

    # --- [1] Lokasi kanonik terpusat di `.aether/settings/` ----------------
    store_probe = ProjectSettingsStore(root=root_a)
    assert store_probe.settings_dir == root_a / ".aether" / "settings"
    assert store_probe.agent_path.name == "agent.json"
    assert store_probe.consultant_path.name == "consultant.json"
    from agent_ai.projects.permissions import ProjectPermissionStore

    assert (
        ProjectPermissionStore(root=root_a).path
        == root_a / ".aether" / "settings" / "permissions.json"
    )
    print("[1] lokasi kanonik project settings OK -> .aether/settings/*")

    # --- Setup gateway terisolasi ------------------------------------------
    tmp_dir = Path(tempfile.mkdtemp(prefix="proj_settings_", dir=str(DUMMY_ROOT)))
    settings_path = tmp_dir / "settings.json"
    settings_path.write_text(json.dumps({"port": 8123}), encoding="utf-8")
    previous_path = settings_mod.SETTINGS_PATH
    settings_mod.SETTINGS_PATH = settings_path

    workspace = FIX_ROOT / "registry"
    registry = ProjectRegistry(workspace=workspace)
    store = ProjectStore(db_path=tmp_dir / "store.db")
    service = services_mod.GatewayService(
        project_registry=registry,
        project_store=store,
        llm_config_service=LLMConfigService(
            db_path=tmp_dir / "llm.db", env_path=tmp_dir / ".env"
        ),
        auto_execute=False,
    )
    services_mod._default_service = service
    client = Client()

    try:
        proj_a = service.create_project(name="PsA", path=str(root_a))
        proj_b = service.create_project(name="PsB", path=str(root_b))
        pid_a, pid_b = proj_a["id"], proj_b["id"]

        # --- [2] Migrasi aman dari lokasi LAMA -----------------------------
        legacy = root_a / ".aether" / "permissions.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy_matrix = {
            action: {"inside": "deny", "outside": "deny"}
            for action in ("read_files", "modify_files", "delete_files", "move_files",
                           "terminal_read", "terminal_mutating")
        }
        legacy.write_text(json.dumps(legacy_matrix), encoding="utf-8")
        # Hapus file BARU (dibuat otomatis saat create_project) agar migrasi jalan.
        new_path = root_a / ".aether" / "settings" / "permissions.json"
        if new_path.is_file():
            new_path.unlink()
        got = client.get(f"/api/projects/{pid_a}/policy").json()
        assert got["matrix"] == legacy_matrix, got
        assert new_path.is_file(), "file policy baru belum dibuat oleh migrasi"
        assert _read_json(new_path) == legacy_matrix
        # File lama TIDAK dihapus (aman rollback).
        assert legacy.is_file()
        print("[2] migrasi file lama -> .aether/settings/permissions.json OK")

        # --- [2b] Jika file baru & lama sama-sama ada -> file baru menang ---
        new_custom = {
            action: {"inside": "allow", "outside": "allow"}
            for action in legacy_matrix
        }
        new_path.write_text(json.dumps(new_custom), encoding="utf-8")
        got = client.get(f"/api/projects/{pid_a}/policy").json()
        assert got["matrix"] == new_custom, got
        # File lama tidak berubah (bukan ditimpa).
        assert _read_json(legacy) == legacy_matrix
        print("[2b] file baru menang (tidak ditimpa oleh file lama) OK")

        # --- [3] System Prompt per-project tersimpan & ter-isolasi ----------
        agent_prompt = "PROMPT AGENT PROJECT A"
        consultant_prompt = "PROMPT CONSULTANT PROJECT A"
        resp = client.post(
            f"/api/projects/{pid_a}/agents",
            data=json.dumps(
                {
                    "agent": {"system_prompt": agent_prompt},
                    "consultant": {"system_prompt": consultant_prompt},
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200, (resp.status_code, resp.content)
        assert _read_json(_solution_dir(root_a) / "agent.json")["system_prompt"] == agent_prompt
        assert (
            _read_json(_solution_dir(root_a) / "consultant.json")["system_prompt"]
            == consultant_prompt
        )
        # Project B tidak terpengaruh (isolasi).
        assert not (_solution_dir(root_b) / "agent.json").exists()
        ga = client.get(f"/api/projects/{pid_a}/agents").json()
        gb = client.get(f"/api/projects/{pid_b}/agents").json()
        assert ga["agent"]["system_prompt"] == agent_prompt, ga
        assert ga["consultant"]["system_prompt"] == consultant_prompt, ga
        assert gb["agent"]["system_prompt"] is None, gb
        assert gb["consultant"]["system_prompt"] is None, gb
        print("[3] System Prompt per-project tersimpan + isolasi OK")

        # --- [4] Runtime memakai override project, fallback default ---------
        from agent_ai.config.settings import agent_system_prompt
        from agent_ai.consultant.prompt import build_consultant_system_prompt
        from agent_ai.projects.project_settings import (
            project_agent_system_prompt,
            project_consultant_system_prompt,
        )

        assert project_agent_system_prompt(str(root_a)) == agent_prompt
        assert project_consultant_system_prompt(str(root_a)) == consultant_prompt
        # Project tanpa override -> None (runtime jatuh ke default existing).
        assert project_agent_system_prompt(str(root_b)) is None
        assert project_consultant_system_prompt(str(root_b)) is None
        # Default existing tidak berubah.
        assert agent_system_prompt()
        assert build_consultant_system_prompt()
        print("[4] override project + fallback default existing OK")

        # --- [5] Endpoint Agents MENOLAK key global (provider/model/api key) -
        for bad in (
            {"provider_instance_id": "x"},
            {"model_id": "y"},
            {"api_key": "secret"},
            {"api_url": "http://x"},
            {"port": 9000},
        ):
            resp = client.post(
                f"/api/projects/{pid_a}/agents",
                data=json.dumps(bad),
                content_type="application/json",
            )
            assert resp.status_code == 400, (bad, resp.status_code, resp.content)
        # Prompt kosong = hapus override (default berlaku kembali).
        resp = client.post(
            f"/api/projects/{pid_a}/agents",
            data=json.dumps({"agent": {"system_prompt": ""}}),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content
        assert project_agent_system_prompt(str(root_a)) is None
        assert project_consultant_system_prompt(str(root_a)) == consultant_prompt
        print("[5] endpoint Agents menolak key global + hapus override OK")
    finally:
        settings_mod.SETTINGS_PATH = previous_path
        services_mod._default_service = None

    # --- [6] Boundary frontend --------------------------------------------
    panel = (FRONTEND_DIR / "components" / "ProjectPolicyPanel.vue").read_text(
        encoding="utf-8"
    )
    assert "getProjectAgentSettings" in panel and "saveProjectAgentSettings" in panel
    assert "getProjectPolicy" in panel and "saveProjectPolicy" in panel
    # Dua tab: Security + Agents.
    assert "Security" in panel and "Agents" in panel, "Project Settings harus punya tab"
    assert "activeTab" in panel and "pp-tab" in panel, "tab UI harus ada"
    for bad in ("AgentRuntime", "AgentLoop", "orchestrator", "Replanner"):
        assert bad not in panel, f"ProjectPolicyPanel tidak boleh memuat '{bad}'"

    api_js = (FRONTEND_DIR / "api.js").read_text(encoding="utf-8")
    for fn in ("getProjectAgentSettings", "saveProjectAgentSettings"):
        assert fn in api_js, f"api.js harus mengekspor {fn}"

    # Settings -> Agent (global) TIDAK lagi memuat System Prompt.
    agent_panel = (FRONTEND_DIR / "components" / "AgentSettingsPanel.vue").read_text(
        encoding="utf-8"
    )
    assert "system_prompt" not in agent_panel, (
        "System Prompt TIDAK lagi global (pindah ke Project Settings -> Agents)"
    )
    assert "default_mode" in agent_panel, "panel global harus memuat default_mode"
    print("[6] boundary frontend OK -> Security + Agents tab, prompt pindah per project")

    print()
    print(
        "[OK] Project Settings AETHER bekerja: permissions + Agent/Consultant prompt "
        "per-project di `.aether/settings/` (migrasi aman, tanpa kebocoran)."
    )
    return 0


def main() -> int:
    print("=== Verifikasi Project Settings AETHER (permissions + Agents) ===")
    try:
        return _run()
    finally:
        shutil.rmtree(FIX_ROOT, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
