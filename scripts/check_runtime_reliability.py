"""Verifikasi integrasi Reliability Layer ke Agent Runtime/Orchestrator.

Deterministik (tanpa API cloud). Memakai provider & tool palsu terprogram.

Menguji:
    1. normal execution tetap berjalan
    2. ReliabilityManager benar-benar dipanggil
    3. successful tool call tidak memicu recovery
    4. retryable error -> retry
    5. retry menghormati max_retries
    6. exponential backoff policy dihormati
    7. repeated identical action + unchanged observation -> recovery
    8. repeated legitimate action + changed observation -> tetap lanjut
    9. repeated failed action -> recover (sinyal ke LLM, bukan FAIL task)
   10. timeout -> reliability decision
   11. provider error -> reliability decision
   12. malformed tool response -> reliability decision
   13. iteration limit TIDAK lagi menghentikan task
   14. recover tidak membuat execution loop kedua
   15. final response tidak menjalankan tool
   16. existing AgentLoop tetap digunakan
   17. existing ToolExecutor tetap digunakan
   18. PreparedTask -> Runtime -> Orchestrator tetap terhubung
   19. provider-agnostic
   20. tidak ada duplicate terminal/provider execution logic

Jalankan:
    python scripts/check_runtime_reliability.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core import (  # noqa: E402
    ActionType,
    AgentOrchestrator,
    AgentStatus,
    FinishReason,
    LLMAction,
    LLMResponse,
)
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.loop import AgentLoop  # noqa: E402
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402
from agent_ai.reliability import (  # noqa: E402
    DecisionAction,
    Detector,
    ReliabilityManager,
    RetryPolicy,
)
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402


# ---------------------------------------------------------------------------
# Provider & tool palsu (deterministik, tanpa API cloud)
# ---------------------------------------------------------------------------
class ScriptedProvider(BaseProvider):
    """Provider palsu: mengembalikan LLMResponse terprogram per pemanggilan.

    Bisa juga disetel untuk melempar error pada pemanggilan tertentu.
    """

    name = "scripted"

    def __init__(self, responses, errors=None):
        self._responses = list(responses)
        self._errors = list(errors or [])
        self.calls = 0
        self.last_messages = []
        self.received_tools = []

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        self.last_messages = messages or []
        self.received_tools.append(tools)
        idx = self.calls
        if idx < len(self._errors) and self._errors[idx] is not None:
            self.calls += 1
            raise self._errors[idx]
        return GenerateResult(text="", model="fake", provider=self.name, raw={})

    def normalize_response(self, result):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


class EchoTool(BaseTool):
    """Tool palsu: mengembalikan echo argumen (deterministik)."""

    name = "echo"
    description = "Echo argumen."
    input_schema = {"type": "object", "properties": {"value": {"type": "string"}}}

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def execute(self, **arguments):
        self.calls += 1
        if self.fail:
            raise RuntimeError("echo gagal")
        return {"echo": arguments.get("value", "")}


def tool_call(name, arguments):
    return LLMResponse(
        actions=[LLMAction(name=name, arguments=arguments, type=ActionType.TOOL_CALL)],
        finish_reason=FinishReason.TOOL_CALLS,
    )


def final(text):
    return LLMResponse(text=text, finish_reason=FinishReason.STOP)


def _registry(tool):
    reg = ToolRegistry()
    reg.register(tool)
    return reg


def main() -> int:
    print("=== Verifikasi Integrasi Reliability -> Runtime/Orchestrator ===")
    return _run()


def _run() -> int:
    # 1) normal execution tetap berjalan (tanpa reliability).
    provider = ScriptedProvider([
        tool_call("echo", {"value": "hi"}),
        final("selesai"),
    ])
    tool = EchoTool()
    orch = AgentOrchestrator(use_continuous_loop=False, provider=provider, executor=ToolExecutor(registry=_registry(tool)), max_iterations=5)
    result = orch.run("echo hi lalu final")
    assert result.status == AgentStatus.DONE and result.result == "selesai"
    assert tool.calls == 1
    print("[1] normal execution tetap berjalan OK")

    # 2) ReliabilityManager benar-benar dipanggil.
    mgr = ReliabilityManager(detector=Detector(iteration_limit=10))
    provider2 = ScriptedProvider([tool_call("echo", {"value": "a"}), final("ok")])
    tool2 = EchoTool()
    orch2 = AgentOrchestrator(use_continuous_loop=False, 
        provider=provider2, executor=ToolExecutor(registry=_registry(tool2)),
        max_iterations=5, reliability=mgr,
    )
    orch2.run("echo a lalu final")
    assert len(mgr.history) >= 1, "reliability harus mencatat progres"
    print(f"[2] ReliabilityManager dipanggil OK -> snapshots={len(mgr.history)}")

    # 3) successful tool call tidak memicu recovery.
    assert not any(e.type.value == "no_progress" for e in mgr.events), "success tidak boleh no-progress"
    print("[3] successful tool call tidak memicu recovery OK")

    # 4) retryable error -> retry.
    #    Pemanggilan ke-0 error (provider_error), lalu sukses.
    mgr4 = ReliabilityManager(
        detector=Detector(iteration_limit=10),
        retry_policy=RetryPolicy(max_retries=3, base_delay=0.0, backoff_factor=2.0),
    )
    provider4 = ScriptedProvider(
        responses=[final("pulih")],
        errors=[RuntimeError("provider down")],
    )
    orch4 = AgentOrchestrator(use_continuous_loop=False, 
        provider=provider4, executor=ToolExecutor(registry=_registry(EchoTool())),
        max_iterations=5, reliability=mgr4,
    )
    result4 = orch4.run("task")
    assert result4.status == AgentStatus.DONE and result4.result == "pulih"
    assert mgr4.retry.attempts == 1, "harus retry sekali"
    print(f"[4] retryable error -> retry OK -> attempts={mgr4.retry.attempts}")

    # 5) retry menghormati max_retries.
    mgr5 = ReliabilityManager(
        detector=Detector(iteration_limit=10),
        retry_policy=RetryPolicy(max_retries=2, base_delay=0.0),
    )
    provider5 = ScriptedProvider(
        responses=[final("tidak tercapai")],
        errors=[RuntimeError("down")] * 5,  # selalu error
    )
    orch5 = AgentOrchestrator(use_continuous_loop=False, 
        provider=provider5, executor=ToolExecutor(registry=_registry(EchoTool())),
        max_iterations=5, reliability=mgr5,
    )
    result5 = orch5.run("task")
    assert result5.status == AgentStatus.FAILED
    assert mgr5.retry.attempts == 2, f"harus berhenti di max_retries=2, dapat {mgr5.retry.attempts}"
    print(f"[5] retry menghormati max_retries OK -> attempts={mgr5.retry.attempts}")

    # 6) exponential backoff policy dihormati.
    policy = RetryPolicy(max_retries=3, base_delay=0.5, backoff_factor=2.0, max_delay=10.0)
    assert [policy.delay_for(i) for i in range(4)] == [0.5, 1.0, 2.0, 4.0]
    print("[6] exponential backoff policy OK")

    # 7) repeated identical action + unchanged observation -> recovery.
    mgr7 = ReliabilityManager(
        detector=Detector(repeat_threshold=3, no_progress_threshold=3, iteration_limit=10),
    )
    provider7 = ScriptedProvider([
        tool_call("echo", {"value": "same"}),
        tool_call("echo", {"value": "same"}),
        tool_call("echo", {"value": "same"}),
        tool_call("echo", {"value": "same"}),
        final("selesai setelah recovery"),
    ])
    tool7 = EchoTool()
    orch7 = AgentOrchestrator(use_continuous_loop=False, 
        provider=provider7, executor=ToolExecutor(registry=_registry(tool7)),
        max_iterations=10, reliability=mgr7,
    )
    result7 = orch7.run("ulang terus")
    # Harus ada recovery message di history.
    joined7 = "\n".join(m.content for m in provider7.last_messages)
    assert "[reliability]" in joined7, "harus ada pesan recovery"
    assert result7.status == AgentStatus.DONE, result7.error
    print(f"[7] repeated identical action -> recovery OK -> status={result7.status.value}")

    # 8) repeated legitimate action + changed observation -> tetap lanjut.
    class CounterTool(BaseTool):
        name = "counter"
        description = "Counter."
        input_schema = {"type": "object", "properties": {"value": {"type": "string"}}}

        def __init__(self):
            self.n = 0

        def execute(self, **arguments):
            self.n += 1
            return {"count": self.n}  # observation berubah tiap kali

    mgr8 = ReliabilityManager(
        detector=Detector(repeat_threshold=3, no_progress_threshold=3, iteration_limit=10),
    )
    provider8 = ScriptedProvider([
        tool_call("counter", {"value": "x"}),
        tool_call("counter", {"value": "x"}),
        tool_call("counter", {"value": "x"}),
        final("selesai"),
    ])
    orch8 = AgentOrchestrator(use_continuous_loop=False, 
        provider=provider8, executor=ToolExecutor(registry=_registry(CounterTool())),
        max_iterations=10, reliability=mgr8,
    )
    result8 = orch8.run("counter")
    assert result8.status == AgentStatus.DONE and result8.result == "selesai"
    assert not any(e.type.value == "repeated_action" for e in mgr8.events), "legit repeat tidak boleh jadi event"
    print("[8] repeated legitimate action + changed observation -> lanjut OK")

    # 9) repeated failed action -> fail/recover sesuai policy.
    mgr9 = ReliabilityManager(
        detector=Detector(repeat_threshold=3, iteration_limit=10),
    )
    provider9 = ScriptedProvider([
        tool_call("echo", {"value": "x"}),
        tool_call("echo", {"value": "x"}),
        tool_call("echo", {"value": "x"}),
        final("menyerah; jawaban final setelah gagal berulang"),
    ])
    orch9 = AgentOrchestrator(use_continuous_loop=False, 
        provider=provider9, executor=ToolExecutor(registry=_registry(EchoTool(fail=True))),
        max_iterations=10, reliability=mgr9,
    )
    result9 = orch9.run("gagal terus")
    assert result9.status == AgentStatus.DONE, "repeated failed action -> recover (bukan FAIL)"
    print(f"[9] repeated failed action -> recover OK -> status={result9.status.value}")

    # 10) timeout -> reliability decision.
    mgr10 = ReliabilityManager(
        detector=Detector(iteration_limit=10),
        retry_policy=RetryPolicy(max_retries=1, base_delay=0.0),
    )
    provider10 = ScriptedProvider(
        responses=[final("pulih")],
        errors=[TimeoutError("request timed out")],
    )
    orch10 = AgentOrchestrator(use_continuous_loop=False, 
        provider=provider10, executor=ToolExecutor(registry=_registry(EchoTool())),
        max_iterations=5, reliability=mgr10,
    )
    result10 = orch10.run("task")
    assert result10.status == AgentStatus.DONE
    assert any(e.type.value == "timeout" for e in mgr10.events)
    print("[10] timeout -> reliability decision OK")

    # 11) provider error -> reliability decision.
    mgr11 = ReliabilityManager(
        detector=Detector(iteration_limit=10),
        retry_policy=RetryPolicy(max_retries=1, base_delay=0.0),
    )
    provider11 = ScriptedProvider(
        responses=[final("pulih")],
        errors=[RuntimeError("boom")],
    )
    orch11 = AgentOrchestrator(use_continuous_loop=False, 
        provider=provider11, executor=ToolExecutor(registry=_registry(EchoTool())),
        max_iterations=5, reliability=mgr11,
    )
    result11 = orch11.run("task")
    assert result11.status == AgentStatus.DONE
    assert any(e.type.value == "provider_error" for e in mgr11.events)
    print("[11] provider error -> reliability decision OK")

    # 12) malformed tool response -> reliability decision.
    mgr12 = ReliabilityManager(
        detector=Detector(iteration_limit=10),
        retry_policy=RetryPolicy(max_retries=1, base_delay=0.0),
    )
    provider12 = ScriptedProvider(
        responses=[final("pulih")],
        errors=[ValueError("malformed tool response json")],
    )
    orch12 = AgentOrchestrator(use_continuous_loop=False, 
        provider=provider12, executor=ToolExecutor(registry=_registry(EchoTool())),
        max_iterations=5, reliability=mgr12,
    )
    result12 = orch12.run("task")
    assert result12.status == AgentStatus.DONE
    assert any(e.type.value == "malformed_tool_response" for e in mgr12.events)
    print("[12] malformed tool response -> reliability decision OK")

    # 13) iteration limit TIDAK lagi menghentikan task.
    mgr13 = ReliabilityManager(detector=Detector(iteration_limit=3, iteration_warn_margin=0))
    provider13 = ScriptedProvider([
        tool_call("echo", {"value": "x"}),
        tool_call("echo", {"value": "x"}),
        tool_call("echo", {"value": "x"}),
        tool_call("echo", {"value": "x"}),
        final("selesai melewati iteration limit lama"),
    ])
    orch13 = AgentOrchestrator(use_continuous_loop=False, 
        provider=provider13, executor=ToolExecutor(registry=_registry(EchoTool())),
        max_iterations=3, reliability=mgr13,
    )
    result13 = orch13.run("loop terus")
    assert result13.status == AgentStatus.DONE, result13.error
    assert result13.iterations > 3, result13.iterations
    print(f"[13] iteration limit tidak lagi menghentikan task OK -> status={result13.status.value} iterations={result13.iterations}")

    # 14) recover tidak membuat execution loop kedua.
    #     Bukti: hanya satu AgentLoop yang dipakai; jumlah pemanggilan LLM
    #     wajar (tidak berlipat). Kita cek provider7 dipanggil <= max_iterations.
    assert provider7.calls <= 10, "recover tidak boleh membuat loop kedua"
    print(f"[14] recover tidak membuat execution loop kedua OK -> llm_calls={provider7.calls}")

    # 15) final response tidak menjalankan tool.
    tool15 = EchoTool()
    provider15 = ScriptedProvider([final("langsung final")])
    orch15 = AgentOrchestrator(use_continuous_loop=False, 
        provider=provider15, executor=ToolExecutor(registry=_registry(tool15)),
        max_iterations=5, reliability=ReliabilityManager(),
    )
    result15 = orch15.run("final saja")
    assert result15.status == AgentStatus.DONE and tool15.calls == 0
    print("[15] final response tidak menjalankan tool OK")

    # 16) existing AgentLoop tetap digunakan.
    import agent_ai.core.orchestrator as orch_mod
    assert orch_mod.AgentLoop is AgentLoop
    print("[16] existing AgentLoop tetap digunakan OK")

    # 17) existing ToolExecutor tetap digunakan.
    assert orch_mod.ToolExecutor is ToolExecutor
    print("[17] existing ToolExecutor tetap digunakan OK")

    # 18) PreparedTask -> Runtime -> Orchestrator tetap terhubung.
    from agent_ai.runtime import AgentRuntime
    from agent_ai.task.models import PreparedTask

    provider18 = ScriptedProvider([final("runtime ok")])
    runtime = AgentRuntime(
        provider=provider18,
        executor=ToolExecutor(registry=_registry(EchoTool())),
        max_iterations=5,
    )
    prepared = PreparedTask(task="task runtime")
    runtime_result = runtime.run(prepared)
    assert runtime_result.status.value == "completed"
    print("[18] PreparedTask -> Runtime -> Orchestrator tetap terhubung OK")

    # 19) provider-agnostic.
    orch_src = (SRC_DIR / "agent_ai" / "core" / "orchestrator.py").read_text(encoding="utf-8").lower()
    for name in ("openrouter", "deepseek", "ollama"):
        assert name not in orch_src, f"orchestrator tidak boleh hardcode '{name}'"
    print("[19] provider-agnostic OK")

    # 20) tidak ada duplicate terminal/provider execution logic.
    #     Orchestrator tidak mengimpor subprocess/requests.
    assert "import subprocess" not in orch_src
    assert "import requests" not in orch_src
    print("[20] tidak ada duplicate terminal/provider execution logic OK")

    print()
    print("[OK] Reliability terintegrasi ke Runtime/Orchestrator (retry/recover/stop/fail).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
