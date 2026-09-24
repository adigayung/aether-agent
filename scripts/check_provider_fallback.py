"""Verifikasi Provider Fallback (#45).

Deterministik, tanpa API cloud. Fixture kecil di
J:\\Agent_Ai\\dummy_test\\provider_fallback_fixture dan dibersihkan setelah test.

Menguji:
    1. provider failure -> fallback candidate dipilih
    2. candidate berbeda dari current provider/model
    3. capability requirement tetap dipenuhi
    4. context requirement tetap dipenuhi
    5. unavailable candidate dilewati
    6. bounded fallback attempts
    7. deterministic
    8. transient/retry decision tidak menduplikasi Reliability
    9. tool/command/validation failures tidak memicu provider fallback
   10. task state tetap dipertahankan
   11. provider abstraction tetap utuh
   12. routing tetap terpisah dari fallback
   13. tidak ada duplicate executor/runtime
   14. architecture boundary bersih

Jalankan:
    python scripts/check_provider_fallback.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.capabilities import (  # noqa: E402
    ModelCapabilities,
    ModelCapability,
    ModelCapabilityRegistry,
)
from agent_ai.core import (  # noqa: E402
    ActionType,
    AgentStatus,
    FinishReason,
    LLMAction,
    LLMResponse,
)
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.fallback import (  # noqa: E402
    FallbackAction,
    FallbackConfig,
    FallbackManager,
    FallbackReason,
    FallbackRequest,
)
from agent_ai.planning import TaskPlanner  # noqa: E402
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402
from agent_ai.reliability import Detector, ReliabilityManager, RetryPolicy  # noqa: E402
from agent_ai.routing import RoutingRegistry  # noqa: E402
from agent_ai.runtime import AgentRuntime, RuntimeStatus  # noqa: E402
from agent_ai.task import PreparedTask  # noqa: E402
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "provider_fallback_fixture"


# ---------------------------------------------------------------------------
# Provider & tool palsu (deterministik, tanpa API cloud)
# ---------------------------------------------------------------------------
class ScriptedProvider(BaseProvider):
    """Provider palsu: respons terprogram per pemanggilan."""

    def __init__(self, name, responses=None, errors=None):
        self.name = name
        self._responses = list(responses or [])
        self._errors = list(errors or [])
        self.calls = 0

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        idx = self.calls
        if idx < len(self._errors) and self._errors[idx] is not None:
            self.calls += 1
            raise self._errors[idx]
        return GenerateResult(text="", model="fake", provider=self.name, raw={})

    def normalize_response(self, result):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


class FailingProvider(BaseProvider):
    """Provider yang selalu error (simulasi provider failure)."""

    def __init__(self, name="provider_a", error=None):
        self.name = name
        self._error = error or RuntimeError("connection refused")

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        raise self._error

    def normalize_response(self, result):  # pragma: no cover
        return LLMResponse(text="", finish_reason=FinishReason.STOP)


class EchoTool(BaseTool):
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


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "note.txt").write_text("fallback fixture\n", encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def _build_cap_registry() -> ModelCapabilityRegistry:
    reg = ModelCapabilityRegistry()
    reg.register(ModelCapabilities(
        provider="provider_a",
        model="model-a",
        capabilities=frozenset({ModelCapability.TOOL_CALLING}),
        context_window=32768,
    ))
    reg.register(ModelCapabilities(
        provider="provider_b",
        model="model-b",
        capabilities=frozenset({ModelCapability.TOOL_CALLING, ModelCapability.REASONING}),
        context_window=128000,
    ))
    reg.register(ModelCapabilities(
        provider="provider_c",
        model="model-c",
        capabilities=frozenset({ModelCapability.TOOL_CALLING, ModelCapability.VISION}),
        context_window=200000,
    ))
    return reg


def main() -> int:
    print("=== Verifikasi Provider Fallback (#45) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    cap_reg = _build_cap_registry()
    routing_reg = RoutingRegistry(capability_registry=cap_reg)

    # 1) provider failure -> fallback candidate dipilih.
    fm = FallbackManager(routing_reg, config=FallbackConfig(max_attempts=2))
    req = FallbackRequest(
        current_provider="provider_a",
        current_model="model-a",
        reason=FallbackReason.CONNECTION_FAILURE,
    )
    dec = fm.fallback(req)
    assert dec.action == FallbackAction.FALLBACK, dec.action
    assert dec.selected is not None, "harus ada kandidat terpilih"
    print(f"[1] provider failure -> fallback candidate OK -> {dec.provider}:{dec.model}")

    # 2) candidate berbeda dari current provider/model.
    assert dec.selected.key != "provider_a:model-a", dec.selected.key
    assert dec.selected.provider != "provider_a", dec.selected.provider
    print(f"[2] candidate berbeda dari current OK -> {dec.selected.key}")

    # 3) capability requirement tetap dipenuhi.
    fm3 = FallbackManager(routing_reg, config=FallbackConfig(max_attempts=2))
    req3 = FallbackRequest(
        current_provider="provider_a",
        current_model="model-a",
        reason=FallbackReason.PROVIDER_API_ERROR,
        required_capabilities=frozenset({ModelCapability.VISION}),
    )
    dec3 = fm3.fallback(req3)
    assert dec3.action == FallbackAction.FALLBACK, dec3.action
    assert dec3.selected.provider == "provider_c", dec3.selected.provider
    assert ModelCapability.VISION in dec3.selected.capabilities
    # provider_b (tanpa vision) harus dieliminasi.
    elim = [c for c in dec3.candidates if not c.eligible]
    assert any(c.provider == "provider_b" for c in elim), "provider_b harus dieliminasi (tanpa vision)"
    print(f"[3] capability requirement dipenuhi OK -> {dec3.selected.key}")

    # 4) context requirement tetap dipenuhi.
    fm4 = FallbackManager(routing_reg, config=FallbackConfig(max_attempts=2))
    req4 = FallbackRequest(
        current_provider="provider_a",
        current_model="model-a",
        reason=FallbackReason.TIMEOUT,
        context_tokens=100000,
    )
    dec4 = fm4.fallback(req4)
    assert dec4.action == FallbackAction.FALLBACK, dec4.action
    assert dec4.selected.context_window >= 100000, dec4.selected.context_window
    assert dec4.selected.provider in ("provider_b", "provider_c"), dec4.selected.provider
    print(f"[4] context requirement dipenuhi OK -> {dec4.selected.key}")

    # 5) unavailable candidate dilewati.
    def availability(provider: str) -> bool:
        return provider.lower() != "provider_b"  # provider_b tidak tersedia

    routing_reg_unavail = RoutingRegistry(capability_registry=cap_reg, availability=availability)
    fm5 = FallbackManager(routing_reg_unavail, config=FallbackConfig(max_attempts=2))
    req5 = FallbackRequest(
        current_provider="provider_a",
        current_model="model-a",
        reason=FallbackReason.PROVIDER_UNAVAILABLE,
    )
    dec5 = fm5.fallback(req5)
    assert dec5.action == FallbackAction.FALLBACK, dec5.action
    assert dec5.selected.provider != "provider_b", dec5.selected.provider
    unavail = [c for c in dec5.candidates if c.provider == "provider_b"]
    assert unavail and unavail[0].eligible is False, "provider_b harus dieliminasi (unavailable)"
    print(f"[5] unavailable candidate dilewati OK -> {dec5.selected.key}")

    # 6) bounded fallback attempts.
    fm6 = FallbackManager(routing_reg, config=FallbackConfig(max_attempts=2))
    req6 = FallbackRequest(
        current_provider="provider_a",
        current_model="model-a",
        reason=FallbackReason.CONNECTION_FAILURE,
    )
    d1 = fm6.fallback(req6)
    assert d1.action == FallbackAction.FALLBACK and fm6.attempts == 1
    d2 = fm6.fallback(req6)
    assert d2.action == FallbackAction.FALLBACK and fm6.attempts == 2
    # Percobaan ke-3 (attempts=2 >= max_attempts=2) -> STOP bounded.
    d3 = fm6.fallback(req6)
    assert d3.action == FallbackAction.STOP and d3.bounded, d3.action
    print(f"[6] bounded fallback attempts OK -> attempts={fm6.attempts}, action={d3.action.value}")

    # 7) deterministic.
    fm7a = FallbackManager(routing_reg, config=FallbackConfig(max_attempts=2))
    fm7b = FallbackManager(routing_reg, config=FallbackConfig(max_attempts=2))
    req7 = FallbackRequest(
        current_provider="provider_a",
        current_model="model-a",
        reason=FallbackReason.CONNECTION_FAILURE,
    )
    da = fm7a.fallback(req7)
    db = fm7b.fallback(req7)
    assert da.selected.key == db.selected.key, (da.selected.key, db.selected.key)
    assert da.rationale == db.rationale
    print(f"[7] deterministic OK -> {da.selected.key}")

    # 8) transient/retry decision tidak menduplikasi Reliability.
    #    Retry current hanya bila Reliability mengizinkan (retry_allowed).
    rel = ReliabilityManager(
        detector=Detector(iteration_limit=10),
        retry_policy=RetryPolicy(max_retries=3, base_delay=0.0),
    )
    fm8 = FallbackManager(routing_reg, config=FallbackConfig(max_attempts=2), reliability=rel)
    req8 = FallbackRequest(
        current_provider="provider_a",
        current_model="model-a",
        reason=FallbackReason.TIMEOUT,
        transient=True,
        # retry_allowed tidak diisi -> diambil dari Reliability (should_retry).
    )
    dec8 = fm8.decide(req8)
    assert dec8.action == FallbackAction.RETRY_CURRENT, dec8.action
    # Reliability tetap sumber tunggal keputusan retry (tidak ada retry controller baru).
    assert rel.retry.attempts == 0, "fallback tidak boleh menaikkan attempts Reliability sendiri"
    # Bila Reliability tidak mengizinkan retry -> fallback (bukan retry).
    rel_no = ReliabilityManager(
        detector=Detector(iteration_limit=10),
        retry_policy=RetryPolicy(max_retries=0, base_delay=0.0),
    )
    fm8b = FallbackManager(routing_reg, config=FallbackConfig(max_attempts=2), reliability=rel_no)
    dec8b = fm8b.decide(FallbackRequest(
        current_provider="provider_a", current_model="model-a",
        reason=FallbackReason.TIMEOUT, transient=True,
    ))
    assert dec8b.action == FallbackAction.FALLBACK, dec8b.action
    print(f"[8] transient/retry tidak menduplikasi Reliability OK -> {dec8.action.value}/{dec8b.action.value}")

    # 9) tool/command/validation failures tidak memicu provider fallback.
    for reason in (FallbackReason.NOT_PROVIDER_FAILURE,):
        fm9 = FallbackManager(routing_reg, config=FallbackConfig(max_attempts=2))
        dec9 = fm9.decide(FallbackRequest(
            current_provider="provider_a", current_model="model-a", reason=reason,
        ))
        assert dec9.action == FallbackAction.STOP, dec9.action
        assert dec9.selected is None
    # Runtime: tool failure (bukan provider error) TIDAK memicu fallback.
    tool = EchoTool(fail=True)
    provider9 = ScriptedProvider("provider_a", responses=[
        tool_call("echo", {"value": "x"}),
        final("selesai; tool gagal tapi provider tidak di-fallback"),
    ])
    fallback_calls = {"n": 0}

    def factory(name):
        fallback_calls["n"] += 1
        return ScriptedProvider(name, responses=[final("pulih")])

    runtime9 = AgentRuntime(
        provider=provider9,
        executor=ToolExecutor(registry=_registry(tool)),
        max_iterations=3,
        fallback_manager=FallbackManager(routing_reg, config=FallbackConfig(max_attempts=2)),
        provider_factory=factory,
        # Provider fallback adalah subsistem jalur LEGACY.
        use_continuous_loop=False,
    )
    result9 = runtime9.run(PreparedTask(task="tool gagal", plan=TaskPlanner().create_plan("Perbaiki bug")))
    assert result9.status == RuntimeStatus.COMPLETED, result9.status
    assert fallback_calls["n"] == 0, "tool failure tidak boleh memicu provider fallback"
    print("[9] tool/command/validation failures tidak memicu fallback OK")

    # 10) task state tetap dipertahankan (plan/messages/lifecycle tidak direset).
    #     Provider A gagal (provider error), provider B sukses -> COMPLETED.
    provider_a = FailingProvider("provider_a", error=RuntimeError("connection refused"))
    provider_b = ScriptedProvider("provider_b", responses=[final("selesai via B")])
    plan10 = TaskPlanner().create_plan("Perbaiki bug login")
    prepared10 = PreparedTask(task="Perbaiki bug login", plan=plan10)
    runtime10 = AgentRuntime(
        provider=provider_a,
        max_iterations=3,
        fallback_manager=FallbackManager(routing_reg, config=FallbackConfig(max_attempts=2)),
        provider_factory=lambda name: provider_b if name == "provider_b" else provider_a,
        # Provider fallback adalah subsistem jalur LEGACY.
        use_continuous_loop=False,
    )
    result10 = runtime10.run(prepared10)
    assert result10.status == RuntimeStatus.COMPLETED, result10.status
    # Plan tetap ada & step tercatat (tidak direset).
    assert prepared10.plan is plan10, "plan tidak boleh diganti"
    assert len(prepared10.plan.steps) > 0, "plan steps harus tetap ada"
    assert result10.progress.completed_steps, "completed work harus tercatat"
    print(f"[10] task state dipertahankan OK -> status={result10.status.value}, steps={len(prepared10.plan.steps)}")

    # 11) provider abstraction tetap utuh.
    from agent_ai.providers.base import BaseProvider as BP
    assert hasattr(BP, "generate") and hasattr(BP, "is_available")
    fb_dir = SRC_DIR / "agent_ai" / "fallback"
    for p in fb_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        for name in ("openrouter", "deepseek", "ollama", "openai_compatible"):
            assert f"import {name}" not in text, f"{p.name} tidak boleh impor provider konkret"
        assert ".generate(" not in text, f"{p.name} tidak boleh memanggil provider.generate"
    print("[11] provider abstraction tetap utuh OK")

    # 12) routing tetap terpisah dari fallback.
    #     - fallback memakai RoutingRegistry (bukan membuat registry sendiri).
    mgr_src = (fb_dir / "manager.py").read_text(encoding="utf-8")
    assert "RoutingRegistry" in mgr_src, "fallback harus memakai RoutingRegistry"
    assert "class ModelCapabilityRegistry" not in mgr_src, "fallback tidak boleh membuat capability registry"
    assert "class ProviderRegistry" not in mgr_src, "fallback tidak boleh membuat provider registry"
    #     - routing tidak boleh impor fallback (routing tetap murni).
    routing_dir = SRC_DIR / "agent_ai" / "routing"
    for p in routing_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "agent_ai.fallback" not in text, f"routing/{p.name} tidak boleh impor fallback"
    print("[12] routing tetap terpisah dari fallback OK")

    # 13) tidak ada duplicate executor/runtime.
    for p in fb_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "import subprocess" not in text, f"{p.name} tidak boleh menjalankan command"
        assert "os.system(" not in text, f"{p.name} tidak boleh menjalankan command"
        assert "class AgentRuntime" not in text, f"{p.name} tidak boleh membuat runtime kedua"
        assert "class ToolExecutor" not in text, f"{p.name} tidak boleh membuat executor kedua"
    print("[13] tidak ada duplicate executor/runtime OK")

    # 14) architecture boundary bersih.
    #     - core TIDAK boleh impor fallback (Agent Core tetap provider-agnostic).
    core_dir = SRC_DIR / "agent_ai" / "core"
    for p in core_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "agent_ai.fallback" not in text, f"core/{p.name} tidak boleh impor fallback"
    #     - fallback tidak boleh impor core/runtime (boundary).
    for p in fb_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "agent_ai.core" not in text, f"{p.name} tidak boleh impor core"
        assert "agent_ai.runtime" not in text, f"{p.name} tidak boleh impor runtime"
        for bad in ("import requests", "import urllib", "import socket"):
            assert bad not in text, f"{p.name} tidak boleh melakukan network: {bad}"
    print("[14] architecture boundary bersih OK")

    print()
    print("[OK] Provider Fallback bekerja (bounded, capability-aware, no duplicate subsystem).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
