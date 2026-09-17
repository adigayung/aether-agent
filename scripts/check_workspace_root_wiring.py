"""Verifikasi wiring workspace root: task menulis ke active project root.

Menguji titik putus yang diperbaiki: native tool call (write_file) dari
provider harus menulis RELATIF terhadap active project root, bukan root
AETHER (J:\\Agent_Ai).

Deterministik, TANPA model/API cloud nyata. Memakai provider fake (in-process)
yang memanggil write_file, lalu memverifikasi file benar-benar dibuat di
folder project aktif.

Fixture project dibuat di J:\\Agent_Ai\\dummy_test\\workspace_root_fixture dan
dibersihkan setelah test.

Menguji:
    1. build_registry(root=...) mengarahkan workspace tools ke root project.
    2. Task via GatewayService menulis file ke active project root.
    3. File TIDAK ditulis ke root AETHER.
    4. Observation hasil write dikembalikan (task completed + result).
    5. Task tidak dianggap COMPLETED hanya karena finish_reason=stop tanpa
       tool call (tanpa write -> tidak ada file).

Jalankan:
    python scripts/check_workspace_root_wiring.py
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "workspace_root_fixture"


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def _make_fake_provider(tool_name: str, tool_args: dict):
    """Provider fake: iterasi 1 -> tool_call, iterasi 2+ -> final."""
    from agent_ai.core.response import ActionType, FinishReason, LLMAction, LLMResponse
    from agent_ai.providers.base import BaseProvider, GenerateResult

    class FakeProvider(BaseProvider):
        name = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
            self.calls += 1
            return GenerateResult(text="", provider="fake", model="fake-1")

        def is_available(self) -> bool:
            return True

        def normalize_response(self, result):
            if self.calls == 1:
                return LLMResponse(
                    text="",
                    actions=[LLMAction(name=tool_name, arguments=tool_args, type=ActionType.TOOL_CALL)],
                    finish_reason=FinishReason.TOOL_CALLS,
                    provider="fake",
                    model="fake-1",
                )
            return LLMResponse(
                text="selesai",
                actions=[],
                finish_reason=FinishReason.STOP,
                provider="fake",
                model="fake-1",
            )

    return FakeProvider()


def _make_stop_only_provider():
    """Provider fake yang langsung finish_reason=stop tanpa tool call."""
    from agent_ai.core.response import FinishReason, LLMResponse
    from agent_ai.providers.base import BaseProvider, GenerateResult

    class StopProvider(BaseProvider):
        name = "fake-stop"

        def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
            return GenerateResult(text="", provider="fake-stop", model="fake-1")

        def is_available(self) -> bool:
            return True

        def normalize_response(self, result):
            return LLMResponse(
                text="tidak ada aksi",
                actions=[],
                finish_reason=FinishReason.STOP,
                provider="fake-stop",
                model="fake-1",
            )

    return StopProvider()


def _wait_for_terminal(service, task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = service.get_task(task_id)
        if rec["status"] in ("completed", "failed"):
            return rec
        time.sleep(0.02)
    return service.get_task(task_id)


def _run() -> int:
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
    import django

    django.setup()

    from django.test import Client

    import api.services as services_mod
    from api.execution import TaskExecutor
    from api.project_store import ProjectStore

    # 1) build_registry(root=...) mengarahkan workspace tools ke root project.
    from agent_ai.tools.registry import build_registry

    reg = build_registry(root=str(FIXTURE))
    assert str(reg.get("write_file").root) == str(FIXTURE), reg.get("write_file").root
    assert str(reg.get("edit_file").root) == str(FIXTURE)
    assert str(reg.get("run_command").root) == str(FIXTURE)
    print(f"[1] build_registry(root) mengarahkan workspace tools OK -> {FIXTURE}")

    # Siapkan service dengan provider fake + project store terisolasi.
    import tempfile

    tmp = tempfile.mkdtemp(prefix="ws_root_store_")
    store = ProjectStore(db_path=Path(tmp) / "t.db")
    service = services_mod.GatewayService(
        project_store=store,
        auto_execute=True,
    )
    services_mod._default_service = service

    # Daftarkan fixture sebagai project + jadikan aktif.
    project = service.create_project(name="WSRootFixture", path=str(FIXTURE))
    project_id = project["id"]
    assert service.get_active_project()["id"] == project_id
    print(f"[1b] project aktif terdaftar OK -> {project_id[:8]}...")

    # 2) Task menulis file ke active project root.
    target_rel = "src/hello.html"
    executor_bridge = TaskExecutor(
        service.sessions,
        provider_factory=lambda: _make_fake_provider(
            "write_file", {"path": target_rel, "content": "<h1>hi</h1>\n"}
        ),
    )
    service._task_executor = executor_bridge

    client = Client()
    resp = client.post(
        "/api/tasks",
        data=json.dumps({"task": "buat HTML sederhana", "project_id": project_id}),
        content_type="application/json",
    )
    assert resp.status_code == 201, (resp.status_code, resp.content)
    task_id = resp.json()["task_id"]
    final = _wait_for_terminal(service, task_id)
    assert final["status"] == "completed", final

    written = FIXTURE / "src" / "hello.html"
    assert written.exists(), f"file harus ditulis ke project root: {written}"
    assert written.read_text(encoding="utf-8") == "<h1>hi</h1>\n"
    print(f"[2] task menulis file ke active project root OK -> {written}")

    # 3) File TIDAK ditulis ke root AETHER.
    leaked = PROJECT_ROOT / "src" / "hello.html"
    assert not leaked.exists(), f"file TIDAK boleh ditulis ke root AETHER: {leaked}"
    print("[3] file TIDAK ditulis ke root AETHER OK")

    # 4) Observation hasil write dikembalikan (task completed + result).
    assert final["result"], "task completed harus punya result"
    events = service.sessions.get_events(task_id=task_id)
    types = [e.event_type.value for e in events]
    assert "tool_called" in types, types
    assert "tool_completed" in types, types
    assert "observation_received" in types, types
    print(f"[4] observation hasil write dikembalikan OK -> {types}")

    # 5) finish_reason=stop tanpa tool call -> tidak ada file baru.
    executor_stop = TaskExecutor(
        service.sessions,
        provider_factory=lambda: _make_stop_only_provider(),
    )
    service._task_executor = executor_stop
    resp = client.post(
        "/api/tasks",
        data=json.dumps({"task": "tidak melakukan apa-apa", "project_id": project_id}),
        content_type="application/json",
    )
    stop_task_id = resp.json()["task_id"]
    stop_final = _wait_for_terminal(service, stop_task_id)
    # Tidak ada file baru yang dibuat (folder tetap hanya berisi hello.html).
    files = sorted(p.name for p in (FIXTURE / "src").iterdir())
    assert files == ["hello.html"], files
    print(f"[5] finish_reason=stop tanpa tool call -> tidak ada file baru OK -> {files}")

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("[OK] Workspace root wiring bekerja (write relatif ke active project root).")
    return 0


def main() -> int:
    print("=== Verifikasi Workspace Root Wiring (DeepSeek write -> project root) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


if __name__ == "__main__":
    raise SystemExit(main())
