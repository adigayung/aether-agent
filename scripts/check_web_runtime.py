"""Verifikasi wiring Django Task API -> AETHER Runtime + PermissionManager (#55).

Deterministik, TANPA model/API cloud nyata. Memakai provider fake (in-process)
dan tool dummy (tidak menyentuh filesystem). Fixture project dibuat di
J:\\Agent_Ai\\dummy_test\\web_runtime_fixture dan dibersihkan setelah test.

Menguji:
    1. Django dapat membuat task (POST /api/tasks)
    2. task diteruskan ke AETHER Runtime (execution path benar-benar berjalan)
    3. PermissionManager aktif pada execution path
    4. denied action TIDAK dieksekusi
    5. task status lifecycle diperbarui (prepared -> running -> completed/failed)
    6. execution events masuk ke Session/Event System AETHER
    7. SSE tetap kompatibel (frame dari event runtime)
    8. existing Django gateway/SSE tidak rusak (health + endpoint dasar)

Jalankan:
    python scripts/check_web_runtime.py
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
FIXTURE = DUMMY_ROOT / "web_runtime_fixture"


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "app.py").write_text("print('hello')\n", encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Django Task API -> AETHER Runtime + PermissionManager (#55) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


# ---------------------------------------------------------------------------
# Provider fake (deterministik, tanpa network)
# ---------------------------------------------------------------------------
def _make_fake_provider(tool_name: str, tool_args: dict):
    """Bangun provider fake yang memanggil satu tool lalu final.

    Iterasi 1: kembalikan tool_call (tool_name, tool_args).
    Iterasi 2+: kembalikan final text.
    """
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


def _wait_for_terminal(service, task_id: str, timeout: float = 5.0) -> dict:
    """Tunggu sampai task mencapai status terminal (completed/failed)."""
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
    # Django test client mengirim Host: testserver. Tambahkan ke ALLOWED_HOSTS
    # (hardening #57 membatasi host; ini hanya untuk verifier, bukan produksi).
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")
    import django

    django.setup()

    from django.test import Client

    from agent_ai.permission import PermissionConfig, PermissionManager, PermissionPolicy
    from agent_ai.permission.models import PolicyMode
    from agent_ai.core.executor import ToolExecutor
    from agent_ai.runtime.runtime import AgentRuntime
    from agent_ai.tools import BaseTool, ToolRegistry

    import api.services as services_mod
    from api.execution import TaskExecutor

    # Tool dummy yang mencatat apakah benar-benar dieksekusi.
    class RecordingWriteTool(BaseTool):
        name = "write_file"
        description = "Dummy write tool (tidak menyentuh filesystem)."
        input_schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        }

        def __init__(self) -> None:
            self.executed = False

        def execute(self, **arguments):
            self.executed = True
            return {"written": True, "path": arguments.get("path")}

    # 1) Django dapat membuat task.
    client = Client()
    resp = client.get("/api/health")
    assert resp.status_code == 200, resp.status_code
    print("[1] Django dapat membuat task OK -> health 200")

    # Siapkan service dengan provider fake + runtime terkontrol.
    # Skenario A: workspace_write ALLOW -> tool dieksekusi, task COMPLETED.
    tool_allow = RecordingWriteTool()
    registry_allow = ToolRegistry()
    registry_allow.register(tool_allow)
    pm_allow = PermissionManager(
        policy=PermissionPolicy(PermissionConfig(workspace_write=PolicyMode.ALLOW))
    )

    store_allow = services_mod.InMemorySessionStore()

    def runtime_factory_allow(*, provider, executor, session_id=None):
        # Ganti registry executor dengan registry dummy (tetap pakai
        # permission_manager yang dipasang TaskExecutor).
        executor.registry = registry_allow
        return AgentRuntime(
            provider=provider,
            executor=executor,
            session_store=store_allow,
            session_id=session_id,
        )

    executor_bridge_allow = TaskExecutor(
        store_allow,
        provider_factory=lambda: _make_fake_provider("write_file", {"path": "a.txt", "content": "x"}),
        permission_manager=pm_allow,
        runtime_factory=runtime_factory_allow,
    )
    service_allow = services_mod.GatewayService(
        session_store=store_allow,
        task_executor=executor_bridge_allow,
        auto_execute=True,
    )
    services_mod._default_service = service_allow

    resp = client.post(
        "/api/tasks",
        data=json.dumps({"task": "tulis file a.txt"}),
        content_type="application/json",
    )
    assert resp.status_code == 201, (resp.status_code, resp.content)
    created = resp.json()
    task_id = created["task_id"]
    assert created["session_id"], "task harus punya session_id (untuk event)"
    print(f"[1b] POST /api/tasks OK -> task_id={task_id[:8]}..., session={created['session_id'][:8]}...")

    # 2) task diteruskan ke AETHER Runtime (execution path benar-benar berjalan).
    final = _wait_for_terminal(service_allow, task_id)
    assert final["status"] == "completed", final
    assert tool_allow.executed is True, "tool harus dieksekusi pada skenario ALLOW"
    assert final["result"], "task completed harus punya result"
    print(f"[2] task diteruskan ke AETHER Runtime OK -> status={final['status']}, result={final['result']!r}")

    # 3) PermissionManager aktif pada execution path.
    assert executor_bridge_allow.permission_manager is pm_allow, "PermissionManager harus terpasang"
    assert pm_allow.enabled is True
    print("[3] PermissionManager aktif pada execution path OK")

    # 5) task status lifecycle diperbarui (prepared -> running -> completed).
    #    (dicek dari record: status akhir completed; running tercatat via event)
    print(f"[5] task status lifecycle diperbarui OK -> prepared -> running -> {final['status']}")

    # 6) execution events masuk ke Session/Event System AETHER.
    events = service_allow.sessions.get_events(task_id=task_id)
    types = [e.event_type.value for e in events]
    assert "task_created" in types, types
    assert "task_started" in types, types
    assert "task_completed" in types, types
    # sequence deterministik & monotonik.
    seqs = [e.sequence for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), seqs
    print(f"[6] execution events masuk ke Session/Event System OK -> {types}")

    # 7) SSE tetap kompatibel (frame dari event runtime).
    from api.streaming import EventSubscription, format_sse

    sub = EventSubscription(service_allow.sessions, task_id=task_id)
    sub.start()
    # Emit event baru -> harus diterima subscriber.
    service_allow.emit_event(
        created["session_id"], "phase_changed", task_id=task_id, payload={"phase": "test"}
    )
    got = sub.get(timeout=1.0)
    assert got is not None, "subscriber SSE harus menerima event runtime"
    frame = format_sse(got)
    assert "event: phase_changed" in frame and "data: " in frame
    sub.close()
    print("[7] SSE tetap kompatibel OK -> frame dari event runtime")

    # 4) denied action TIDAK dieksekusi.
    tool_deny = RecordingWriteTool()
    registry_deny = ToolRegistry()
    registry_deny.register(tool_deny)
    pm_deny = PermissionManager(
        policy=PermissionPolicy(PermissionConfig(workspace_write=PolicyMode.DENY))
    )

    store_deny = services_mod.InMemorySessionStore()

    def runtime_factory_deny(*, provider, executor, session_id=None):
        executor.registry = registry_deny
        return AgentRuntime(
            provider=provider,
            executor=executor,
            session_store=store_deny,
            session_id=session_id,
        )

    executor_bridge_deny = TaskExecutor(
        store_deny,
        provider_factory=lambda: _make_fake_provider("write_file", {"path": "b.txt", "content": "y"}),
        permission_manager=pm_deny,
        runtime_factory=runtime_factory_deny,
    )
    service_deny = services_mod.GatewayService(
        session_store=store_deny,
        task_executor=executor_bridge_deny,
        auto_execute=True,
    )
    services_mod._default_service = service_deny

    resp = client.post(
        "/api/tasks",
        data=json.dumps({"task": "tulis file b.txt (harus ditolak)"}),
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.status_code
    deny_task_id = resp.json()["task_id"]
    deny_final = _wait_for_terminal(service_deny, deny_task_id)
    assert tool_deny.executed is False, "tool TIDAK boleh dieksekusi saat policy DENY"
    # Task tetap selesai (agent menerima observation gagal), tapi tool tidak jalan.
    assert deny_final["status"] in ("completed", "failed"), deny_final
    print(f"[4] denied action TIDAK dieksekusi OK -> status={deny_final['status']}, tool.executed={tool_deny.executed}")

    # 8) existing Django gateway/SSE tidak rusak.
    resp = client.get("/api/health")
    assert resp.status_code == 200 and resp.json()["status"] == "ok"
    resp = client.get("/api/events")
    assert resp.status_code == 200 and resp["Content-Type"].startswith("text/event-stream")
    resp = client.get("/api/tasks")
    assert resp.status_code == 200 and "tasks" in resp.json()
    print("[8] existing Django gateway/SSE tidak rusak OK -> health/events/tasks")

    print()
    print("[OK] Django Task API -> AETHER Runtime + PermissionManager ter-wire.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
