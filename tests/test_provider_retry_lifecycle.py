"""Regression tests: lifecycle provider-call retry (Agent & Consultant).

Menguji resilience layer GENERIK pada layer LLM/provider-call yang paling umum
(`AgentOrchestrator._generate_with_retry`, dipakai oleh `run_continuous_loop`
yang menjadi jalur NORMAL Agent (AgentRuntime) maupun Consultant
(ConsultantService)). Semua test DETERMINISTIK, tanpa network.

Kontrak yang diuji (global untuk provider/model apa pun):
    1. sukses pada attempt awal        -> 1 call, DONE
    2. sukses pada retry ke-1          -> 2 call, DONE
    3. sukses pada retry ke-2          -> 3 call, DONE
    4. sukses pada retry ke-3          -> 4 call, DONE
    5. gagal seluruh 4 attempts        -> FAILED + lifecycle ditutup
    6. cancellation saat retry         -> CANCELLED, retry berhenti
    7. tool yang sudah berhasil TIDAK diulang saat retry
    8. budget retry per provider-call (bukan global)
    9. telemetry retry diemit TANPA membocorkan credential/secret
   10. jalur direct `run_continuous_loop` (Consultant) mendapat behavior sama

Jalankan:
    python -m pytest tests/test_provider_retry_lifecycle.py
atau:
    python tests/test_provider_retry_lifecycle.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core.cancel import CancellationToken  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
)
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402

SYSTEM_PROMPT = "Kamu adalah coding agent."


# --------------------------------------------------------------------------- #
# Fake provider + tool (lokal, deterministik, tanpa network)
# --------------------------------------------------------------------------- #
def _tool_call(call_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _tool_turn(text: str, calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"content": text, "tool_calls": calls},
                "finish_reason": "tool_calls",
            }
        ]
    }


def _final_turn(text: str) -> Dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class RetryScriptedProvider(OpenAICompatibleProvider):
    """Provider palsu: tiap call mengembalikan turn skrip atau melempar error.

    Item skrip: dict (turn provider) | BaseException | class Exception.
    Bila skrip habis, item TERAKHIR diulang (mis. satu error -> gagal terus).
    """

    name = "retry-scripted"

    def __init__(self, script: List[Any], on_error: Optional[Any] = None) -> None:
        self.config = SimpleNamespace(model="retry-model")
        self.script = list(script)
        self.calls = 0
        self.on_error = on_error
        self.requests: List[List[Dict[str, Any]]] = []

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Any]] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[List[Any]] = None,
        tool_choice: Optional[Any] = None,
    ) -> GenerateResult:
        self.requests.append(
            [dict(m) if isinstance(m, dict) else m for m in (messages or [])]
        )
        idx = self.calls
        self.calls += 1
        if not self.script:
            item = _final_turn("(default)")
        elif idx < len(self.script):
            item = self.script[idx]
        else:
            # Skrip habis: ulangi item TERAKHIR (mis. error yang terus gagal).
            item = self.script[-1]
        if isinstance(item, BaseException) or (
            isinstance(item, type) and issubclass(item, BaseException)
        ):
            if self.on_error is not None:
                self.on_error()
            if isinstance(item, BaseException):
                raise item
            raise item("provider error")
        return GenerateResult(
            text="", model="retry-model", provider=self.name, raw=item
        )


class CountingEchoTool(BaseTool):
    """Tool palsu yang menghitung berapa kali ia dieksekusi."""

    def __init__(self, name: str = "echo") -> None:
        self.name = name
        self.description = "fake echo"
        self.input_schema = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": [],
        }
        self.calls = 0

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        self.calls += 1
        return {"content": f"echo:{arguments.get('value', '')}"}


def _make_orchestrator(
    provider: RetryScriptedProvider,
    tool: Optional[CountingEchoTool] = None,
    *,
    cancel_token: Optional[CancellationToken] = None,
    events: Optional[List[Dict[str, Any]]] = None,
) -> AgentOrchestrator:
    registry = ToolRegistry()
    if tool is not None:
        registry.register(tool)
    sink = None
    if events is not None:
        sink = lambda et, payload: events.append({"type": et, **payload})  # noqa: E731
    return AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="retry-model"),
        system_prompt=SYSTEM_PROMPT,
        use_continuous_loop=True,
        event_sink=sink,
        cancel_token=cancel_token,
    )


# --------------------------------------------------------------------------- #
# 1-4. Sukses pada attempt awal / retry ke-1 / ke-2 / ke-3
# --------------------------------------------------------------------------- #
def test_success_on_first_attempt() -> None:
    provider = RetryScriptedProvider([_final_turn("selesai")])
    result = _make_orchestrator(provider).run("task")
    assert result.status == AgentStatus.DONE, result.error
    assert result.result == "selesai"
    assert provider.calls == 1
    assert result.provider_error is False


def test_success_on_first_retry() -> None:
    provider = RetryScriptedProvider(
        [RuntimeError("boom-1"), _final_turn("pulih")]
    )
    result = _make_orchestrator(provider).run("task")
    assert result.status == AgentStatus.DONE, result.error
    assert result.result == "pulih"
    assert provider.calls == 2, provider.calls


def test_success_on_second_retry() -> None:
    provider = RetryScriptedProvider(
        [
            RuntimeError("boom-1"),
            RuntimeError("boom-2"),
            _final_turn("pulih"),
        ]
    )
    result = _make_orchestrator(provider).run("task")
    assert result.status == AgentStatus.DONE, result.error
    assert result.result == "pulih"
    assert provider.calls == 3, provider.calls


def test_success_on_third_retry() -> None:
    provider = RetryScriptedProvider(
        [
            RuntimeError("boom-1"),
            RuntimeError("boom-2"),
            RuntimeError("boom-3"),
            _final_turn("pulih"),
        ]
    )
    result = _make_orchestrator(provider).run("task")
    assert result.status == AgentStatus.DONE, result.error
    assert result.result == "pulih"
    assert provider.calls == 4, provider.calls


# --------------------------------------------------------------------------- #
# 5. Gagal seluruh 4 attempts -> FAILED + lifecycle ditutup
# --------------------------------------------------------------------------- #
def test_all_attempts_fail_closes_lifecycle_failed() -> None:
    provider = RetryScriptedProvider([RuntimeError("koneksi gagal")])
    result = _make_orchestrator(provider).run("task")
    assert result.status == AgentStatus.FAILED
    assert result.provider_error is True
    # Tepat 4 attempt (1 awal + 3 retry) — tidak lebih (bounded).
    assert provider.calls == 4, provider.calls
    assert "koneksi gagal" in (result.error or ""), result.error


# --------------------------------------------------------------------------- #
# 6. Cancellation saat retry -> retry berhenti, lifecycle CANCELLED
# --------------------------------------------------------------------------- #
def test_cancellation_during_retry() -> None:
    token = CancellationToken()
    provider = RetryScriptedProvider(
        [RuntimeError("boom"), _final_turn("tidak boleh tercapai")],
        on_error=lambda: token.request("user stop"),
    )
    result = _make_orchestrator(provider, cancel_token=token).run("task")
    assert result.status == AgentStatus.CANCELLED, result.status
    # Retry dihentikan segera setelah pembatalan: tidak ada attempt tambahan.
    assert provider.calls == 1, provider.calls


# --------------------------------------------------------------------------- #
# 7. Tool yang sudah berhasil TIDAK diulang saat retry
# --------------------------------------------------------------------------- #
def test_successful_tool_not_repeated_on_retry() -> None:
    tool = CountingEchoTool("echo")
    script = [
        _tool_turn("panggil echo", [_tool_call("c1", "echo", {"value": "x"})]),
        RuntimeError("boom-1"),
        RuntimeError("boom-2"),
        _final_turn("selesai"),
    ]
    provider = RetryScriptedProvider(script)
    result = _make_orchestrator(provider, tool).run("task")

    assert result.status == AgentStatus.DONE, result.error
    assert result.result == "selesai"
    # Provider: 1 turn tool + 3 attempt (gagal, gagal, sukses) pada iterasi 2.
    assert provider.calls == 4, provider.calls
    # Tool dieksekusi TEPAT SEKALI (tidak diulang karena retry provider).
    assert tool.calls == 1, tool.calls
    # Hasil tool hanya dikirim SEKALI (tidak terduplikasi) ke request terakhir.
    last_request = provider.requests[-1]
    tool_msgs = [m for m in last_request if m.get("role") == "tool"]
    assert len(tool_msgs) == 1, tool_msgs


# --------------------------------------------------------------------------- #
# 8. Budget retry per provider-call (bukan global)
# --------------------------------------------------------------------------- #
def test_retry_budget_is_per_provider_call() -> None:
    tool = CountingEchoTool("echo")
    script = [
        RuntimeError("boom-iter1"),  # iterasi 1 attempt 1 gagal
        _tool_turn("panggil echo", [_tool_call("c1", "echo", {"value": "y"})]),
        RuntimeError("boom-iter2"),  # iterasi 2 attempt 1 gagal
        _final_turn("selesai"),
    ]
    provider = RetryScriptedProvider(script)
    result = _make_orchestrator(provider, tool).run("task")

    assert result.status == AgentStatus.DONE, result.error
    assert result.result == "selesai"
    # Dua iterasi, masing-masing meng-retry sekali -> total 4 provider call.
    assert provider.calls == 4, provider.calls
    assert tool.calls == 1, tool.calls


# --------------------------------------------------------------------------- #
# 9. Telemetry retry diemit tanpa membocorkan credential/secret
# --------------------------------------------------------------------------- #
def test_retry_telemetry_no_credential_leak() -> None:
    events: List[Dict[str, Any]] = []
    secret_error = RuntimeError("Gagal ke provider: Bearer sk-supersecret1234567890")
    provider = RetryScriptedProvider([secret_error, _final_turn("ok")])
    result = _make_orchestrator(provider, events=events).run("task")

    assert result.status == AgentStatus.DONE, result.error

    retry_events = [e for e in events if e["type"] == "provider_retry"]
    assert len(retry_events) == 1, retry_events
    ev = retry_events[0]
    assert ev["attempt"] == 1
    assert ev["next_attempt"] == 2
    assert ev["max_attempts"] == 4
    assert ev["provider"] == "retry-scripted"
    assert ev["model"] == "retry-model"
    assert ev["error_type"] == "RuntimeError"

    # Telemetry sukses retry juga ada.
    assert any(e["type"] == "provider_retry_succeeded" for e in events)

    # TIDAK ada credential/secret yang bocor ke payload event apa pun.
    joined = json.dumps(events)
    assert "sk-supersecret1234567890" not in joined
    assert "[redacted]" in joined


def test_retry_exhausted_telemetry_present() -> None:
    events: List[Dict[str, Any]] = []
    provider = RetryScriptedProvider([RuntimeError("gagal terus")])
    result = _make_orchestrator(provider, events=events).run("task")

    assert result.status == AgentStatus.FAILED
    exhausted = [e for e in events if e["type"] == "provider_retry_exhausted"]
    assert len(exhausted) == 1, exhausted
    assert exhausted[0]["attempts"] == 4
    assert exhausted[0]["error_type"] == "RuntimeError"
    # Retry diemit 3 kali (attempt 1->2, 2->3, 3->4).
    retries = [e for e in events if e["type"] == "provider_retry"]
    assert [e["attempt"] for e in retries] == [1, 2, 3], retries


# --------------------------------------------------------------------------- #
# 10. Jalur direct `run_continuous_loop` (Consultant) mendapat behavior sama
# --------------------------------------------------------------------------- #
def test_run_continuous_loop_direct_has_retry() -> None:
    provider = RetryScriptedProvider([RuntimeError("boom"), _final_turn("ok")])
    orch = _make_orchestrator(provider)
    result = orch.run_continuous_loop("task consultant")
    assert result.status == AgentStatus.DONE, result.error
    assert result.result == "ok"
    assert provider.calls == 2, provider.calls


# --------------------------------------------------------------------------- #
# Standalone runner (tanpa pytest)
# --------------------------------------------------------------------------- #
def _standalone() -> int:
    checks = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = 0
    for name, func in checks:
        try:
            func()
            print(f"[PASS] {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
    print()
    if failures:
        print(f"[FAILED] {failures} test gagal")
        return 1
    print(f"[OK] {len(checks)} test lulus (provider retry lifecycle)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_standalone())
