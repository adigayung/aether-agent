"""Verifikasi Cooperative Cancellation (Stop task) end-to-end.

Membuktikan bahwa menekan Stop benar-benar MENGHENTIKAN eksekusi Agent pada
safe boundary — bukan sekadar mengubah status UI. Deterministik, tanpa API
cloud (memakai provider + tool palsu).

Yang diverifikasi:
    1. CancellationToken sebagai primitif sinyal (idempotent, satu arah).
    2. Agent berhenti SEBELUM iteration/LLM call berikutnya setelah cancel.
    3. Agent tidak melakukan tool call baru setelah cancel terdeteksi.
    4. Cancellation tercatat di `.aether/log` (event task_cancelled) + lifecycle.
    5. Gateway: RUNNING -> Stop -> CANCELLED (end-to-end lewat background thread).
    6. Task COMPLETED tidak berubah menjadi CANCELLED (cancel = no-op).
    7. Task normal (tanpa cancel) tetap COMPLETED (backward compatible).
    8. Task yang tidak ada -> NotFoundError.

Fixture berada di `dummy_test/cancellation_fixture` dan dibersihkan setelah test.

Jalankan:
    python scripts/check_cancellation.py
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
for _p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_ai.core import (  # noqa: E402
    ActionType,
    AgentOrchestrator,
    AgentStatus,
    FinishReason,
    LLMAction,
    LLMResponse,
)
from agent_ai.core.cancel import CancellationToken  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402
from agent_ai.runtime import AgentRuntime, RuntimeStatus  # noqa: E402
from agent_ai.session.store import InMemorySessionStore  # noqa: E402
from agent_ai.task import PreparedTask  # noqa: E402
from agent_ai.tasks import TaskLifecycle, TaskStatus  # noqa: E402
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402

from api.execution import TaskExecutor  # noqa: E402
from api.project_store import ProjectStore  # noqa: E402
from api.services import GatewayService, NotFoundError  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "cancellation_fixture"


# --------------------------------------------------------------------------- #
# Palsu: provider + tool (deterministik, tanpa network)
# --------------------------------------------------------------------------- #
def tool_response(*names: str) -> LLMResponse:
    """Response LLM yang meminta satu/lebih tool call."""
    return LLMResponse(
        actions=[
            LLMAction(name=n, arguments={}, type=ActionType.TOOL_CALL) for n in names
        ],
        finish_reason=FinishReason.TOOL_CALLS,
    )


def final_response(text: str) -> LLMResponse:
    """Response final LLM (tanpa tool call)."""
    return LLMResponse(text=text, finish_reason=FinishReason.STOP)


class ScriptedProvider(BaseProvider):
    """Provider palsu: mengembalikan response skrip per pemanggilan generate."""

    name = "cancel-scripted"

    def __init__(
        self,
        responses: List[LLMResponse],
        *,
        on_generate: Optional[Any] = None,
        delay: float = 0.0,
        max_calls: Optional[int] = None,
    ) -> None:
        self._responses = list(responses)
        self._on_generate = on_generate
        self._delay = delay
        self.max_calls = max_calls
        self.calls = 0
        self._lock = threading.Lock()

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        with self._lock:
            self.calls += 1
            n = self.calls
        if self._on_generate is not None:
            self._on_generate(n)
        if self._delay:
            time.sleep(self._delay)
        return GenerateResult(text="", model=self.name, provider=self.name, raw={})

    def normalize_response(self, result: GenerateResult) -> LLMResponse:
        with self._lock:
            n = self.calls
            idx = min(n - 1, len(self._responses) - 1) if self._responses else 0
        if not self._responses:
            return final_response("(kosong)")
        if self.max_calls is not None and n >= self.max_calls:
            return final_response("(batas iterasi uji tercapai)")
        return self._responses[idx]


class CountTool(BaseTool):
    """Tool palsu: menambah counter; callback opsional saat dieksekusi."""

    name = "count_tool"
    description = "Tool palsu untuk verifikasi cancellation."
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, name: str = "count_tool", on_execute: Optional[Any] = None) -> None:
        self.name = name
        self._on_execute = on_execute
        self.calls = 0
        self._lock = threading.Lock()

    def execute(self, **arguments: Any) -> Any:
        with self._lock:
            self.calls += 1
            n = self.calls
        if self._on_execute is not None:
            self._on_execute(n)
        return {"count": n}


def _registry(*tools: BaseTool) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in tools:
        reg.register(tool)
    return reg


def _orchestrator(provider: BaseProvider, executor: ToolExecutor, token: CancellationToken) -> AgentOrchestrator:
    return AgentOrchestrator(
        provider=provider,
        executor=executor,
        use_continuous_loop=True,
        cancel_token=token,
    )


def _wait_for_terminal(service: GatewayService, task_id: str, timeout: float = 6.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = service.get_task(task_id)
        if rec["status"] in ("completed", "failed", "cancelled"):
            return rec
        time.sleep(0.02)
    return service.get_task(task_id)


# --------------------------------------------------------------------------- #
# Skenario
# --------------------------------------------------------------------------- #
def scenario_1_token() -> None:
    token = CancellationToken()
    assert not token.is_cancelled() and not token.cancelled
    token.request("alasan-pertama")
    assert token.is_cancelled() and token.cancelled
    assert token.reason == "alasan-pertama"
    token.request("alasan-kedua")  # idempotent: reason pertama dipertahankan
    assert token.reason == "alasan-pertama"
    token.reset()
    assert not token.is_cancelled() and token.reason is None
    print("[1] CancellationToken (idempotent, satu arah, reset) OK")


def scenario_2_cancel_during_tool() -> None:
    """Cancel dipicu saat tool pertama selesai -> tidak ada iteration/tool baru."""
    token = CancellationToken()
    tool = CountTool(on_execute=lambda n: token.request("user_requested"))
    provider = ScriptedProvider([tool_response("count_tool")])
    orch = _orchestrator(provider, ToolExecutor(registry=_registry(tool)), token)
    result = orch.run("task panjang")
    assert result.status == AgentStatus.CANCELLED, result.status
    assert provider.calls == 1, f"iteration baru setelah cancel: {provider.calls}"
    assert tool.calls == 1, f"tool call baru setelah cancel: {tool.calls}"
    print("[2] cancel saat tool -> berhenti sebelum iteration/tool baru OK")


def scenario_3_cancel_before_start() -> None:
    """Cancel sebelum eksekusi mulai -> LLM/tool tidak pernah dipanggil."""
    token = CancellationToken()
    token.request("pre")
    tool = CountTool()
    provider = ScriptedProvider([tool_response("count_tool")])
    orch = _orchestrator(provider, ToolExecutor(registry=_registry(tool)), token)
    result = orch.run("task")
    assert result.status == AgentStatus.CANCELLED, result.status
    assert provider.calls == 0 and tool.calls == 0
    print("[3] cancel sebelum mulai -> 0 LLM call, 0 tool call OK")


def scenario_4_multi_tool_cancel() -> None:
    """Satu turn dengan 2 tool: cancel saat tool pertama -> tool kedua batal."""
    token = CancellationToken()
    tool_a = CountTool(name="tool_a", on_execute=lambda n: token.request("user"))
    tool_b = CountTool(name="tool_b")
    provider = ScriptedProvider([tool_response("tool_a", "tool_b")])
    orch = _orchestrator(
        provider, ToolExecutor(registry=_registry(tool_a, tool_b)), token
    )
    result = orch.run("task")
    assert result.status == AgentStatus.CANCELLED, result.status
    assert provider.calls == 1, provider.calls
    assert tool_a.calls == 1 and tool_b.calls == 0, (tool_a.calls, tool_b.calls)
    print("[4] cancel antar tool dalam satu turn -> tool berikutnya tidak jalan OK")


def scenario_5_runtime_log_and_lifecycle() -> None:
    """Runtime: status CANCELLED, lifecycle CANCELLED, tercatat di `.aether/log`."""
    token = CancellationToken()
    tool = CountTool(on_execute=lambda n: token.request("user_requested"))
    provider = ScriptedProvider([tool_response("count_tool")])
    prepared = PreparedTask(task="task yang dibatalkan", task_id="cancel-rt-1")
    lifecycle = TaskLifecycle("task yang dibatalkan", task_id=prepared.task_id)
    runtime = AgentRuntime(
        provider=provider,
        executor=ToolExecutor(registry=_registry(tool)),
        project_root=str(FIXTURE),
        project_brain=False,
        cancel_token=token,
    )
    result = runtime.run(prepared, lifecycle=lifecycle)
    assert result.status == RuntimeStatus.CANCELLED, result.status
    assert lifecycle.status == TaskStatus.CANCELLED, lifecycle.status
    assert provider.calls == 1 and tool.calls == 1

    log_file = FIXTURE / ".aether" / "log" / f"{prepared.task_id}.log"
    assert log_file.exists(), "log task tidak ditulis"
    events = [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    names = [e.get("event") for e in events]
    assert "task_cancelled" in names, names
    assert "task_completed" not in names, names
    finished = [e for e in events if e.get("event") == "task_finished"]
    assert finished and finished[-1]["data"].get("status") == "cancelled", finished[-1:]

    from agent_ai.projects.aether_store import AetherProjectStore, TaskLogReader

    info = TaskLogReader(
        AetherProjectStore(FIXTURE), task_id=prepared.task_id
    ).get_task_info()
    assert info and info.get("status") == "cancelled", info
    print("[5] runtime CANCELLED + lifecycle + `.aether/log` task_cancelled OK")


def scenario_6_gateway_end_to_end() -> None:
    """Gateway: task RUNNING -> Stop -> CANCELLED dan eksekusi benar-benar berhenti."""
    sessions = InMemorySessionStore()
    provider = ScriptedProvider(
        [tool_response("count_tool")], delay=0.02, max_calls=1000
    )
    tool = CountTool()
    bridge = TaskExecutor(
        sessions,
        provider_factory=lambda: provider,
    )
    store = ProjectStore(db_path=FIXTURE / "gateway_cancel.db")
    service = GatewayService(
        session_store=sessions,
        task_executor=bridge,
        auto_execute=True,
        project_store=store,
    )

    record = service.create_task("task gateway yang berjalan lama")
    task_id = record["task_id"]
    assert record["status"] in ("prepared", "running"), record

    # Tunggu sampai eksekusi benar-benar mulai memanggil LLM.
    deadline = time.time() + 6.0
    while provider.calls < 1 and time.time() < deadline:
        time.sleep(0.01)
    assert provider.calls >= 1, "provider tidak pernah dipanggil"

    stopped = service.cancel_task(task_id)
    assert stopped["status"] == "cancelled", stopped

    final = _wait_for_terminal(service, task_id)
    assert final["status"] == "cancelled", final

    # Bukti eksekusi benar-benar berhenti (bukan hanya status UI).
    time.sleep(0.4)
    calls_at_rest = provider.calls
    time.sleep(0.4)
    assert provider.calls == calls_at_rest, (
        f"eksekusi masih berjalan setelah cancel: {provider.calls} > {calls_at_rest}"
    )

    # Event cancellation tercatat di Session/Event store AETHER existing.
    types = [e.event_type.value for e in sessions.get_events(task_id=task_id)]
    assert "task_cancelled" in types, types
    assert "task_completed" not in types, types
    print(
        f"[6] gateway RUNNING->Stop->CANCELLED OK "
        f"(iterasi berhenti di {calls_at_rest}, session event task_cancelled ada)"
    )


def scenario_7_completed_not_cancelled() -> None:
    """Task COMPLETED tetap COMPLETED (cancel = no-op); task tidak ada -> 404."""
    sessions = InMemorySessionStore()
    provider = ScriptedProvider([final_response("selesai")])
    bridge = TaskExecutor(sessions, provider_factory=lambda: provider)
    store = ProjectStore(db_path=FIXTURE / "gateway_done.db")
    service = GatewayService(
        session_store=sessions,
        task_executor=bridge,
        auto_execute=True,
        project_store=store,
    )

    record = service.create_task("task singkat")
    task_id = record["task_id"]
    final = _wait_for_terminal(service, task_id)
    assert final["status"] == "completed", final

    after = service.cancel_task(task_id)
    assert after["status"] == "completed", after

    try:
        service.cancel_task("tidak-ada")
        raise AssertionError("cancel task tidak ada seharusnya NotFoundError")
    except NotFoundError:
        pass
    print("[7] task COMPLETED tidak berubah + task tidak ada -> 404 OK")


def scenario_8_normal_unaffected() -> None:
    """Token hadir tapi tidak diminta -> task tetap COMPLETED (tanpa regresi)."""
    token = CancellationToken()
    tool = CountTool()
    provider = ScriptedProvider([final_response("jawaban final")])
    orch = _orchestrator(provider, ToolExecutor(registry=_registry(tool)), token)
    result = orch.run("task biasa")
    assert result.status == AgentStatus.DONE, result.status
    assert result.result == "jawaban final", result.result
    assert not token.is_cancelled()
    print("[8] task normal tanpa cancel tetap COMPLETED (backward compatible) OK")


# --------------------------------------------------------------------------- #
# Fixture + main
# --------------------------------------------------------------------------- #
def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)


def teardown_fixture() -> None:
    import gc

    for _ in range(10):
        gc.collect()
        shutil.rmtree(FIXTURE, ignore_errors=True)
        if not FIXTURE.exists():
            break
        time.sleep(0.1)
    try:
        if DUMMY_ROOT.exists() and not any(DUMMY_ROOT.iterdir()):
            DUMMY_ROOT.rmdir()
    except OSError:
        pass


def main() -> int:
    print("=== Verifikasi Cooperative Cancellation (Stop Task) ===")
    setup_fixture()
    try:
        scenario_1_token()
        scenario_2_cancel_during_tool()
        scenario_3_cancel_before_start()
        scenario_4_multi_tool_cancel()
        scenario_5_runtime_log_and_lifecycle()
        scenario_6_gateway_end_to_end()
        scenario_7_completed_not_cancelled()
        scenario_8_normal_unaffected()
    finally:
        teardown_fixture()
    print()
    print("[OK] Cooperative cancellation bekerja end-to-end (Stop benar-benar menghentikan).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
