"""Regression tests: Provider Fallback pada JALUR AKTIF (continuous loop).

Menguji integrasi Provider Fallback ke jalur produksi
`AgentRuntime -> AgentOrchestrator -> run_continuous_loop` (Native Tool Calling,
satu percakapan kontinu). Semua test DETERMINISTIK, tanpa network.

Kontrak yang diuji:
    1. provider utama berhasil            -> tidak ada fallback, DONE
    2. provider utama gagal lalu fallback -> berpindah provider, task lanjut, DONE
    3. semua provider gagal               -> FAILED (dilaporkan benar)
    4. cancellation saat provider gagal    -> CANCELLED (fallback tidak dipicu)
    5. tool-calling pada pergantian provider -> riwayat percakapan + hasil tool
       tetap utuh (tool TIDAK diulang karena perpindahan provider)
    6. fallback tidak terjadi untuk tool failure (bukan provider failure)

Jalankan:
    python -m pytest tests/test_provider_fallback_continuous.py
atau:
    python tests/test_provider_fallback_continuous.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.capabilities import (  # noqa: E402
    ModelCapabilities,
    ModelCapability,
    ModelCapabilityRegistry,
)
from agent_ai.core.cancel import CancellationToken  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.fallback import FallbackConfig, FallbackManager  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
)
from agent_ai.routing import RoutingRegistry  # noqa: E402
from agent_ai.runtime import AgentRuntime, RuntimeStatus  # noqa: E402
from agent_ai.task import PreparedTask  # noqa: E402
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


class ScriptedProvider(OpenAICompatibleProvider):
    """Provider palsu: tiap call mengembalikan turn skrip ATAU melempar error.

    Item skrip: dict (turn provider) | BaseException | class Exception.
    Bila skrip habis, item TERAKHIR diulang (mis. satu error -> gagal terus).
    """

    def __init__(self, name: str, script: List[Any]) -> None:
        self.name = name
        self.config = SimpleNamespace(model=f"{name}-model")
        self.script = list(script)
        self.calls = 0
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
            item: Any = _final_turn("(default)")
        elif idx < len(self.script):
            item = self.script[idx]
        else:
            item = self.script[-1]
        if isinstance(item, BaseException) or (
            isinstance(item, type) and issubclass(item, BaseException)
        ):
            if isinstance(item, BaseException):
                raise item
            raise item("provider error")
        return GenerateResult(text="", model=f"{self.name}-model", provider=self.name, raw=item)


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


# --------------------------------------------------------------------------- #
# Routing/Fallback fixture (kandidat deterministik)
# --------------------------------------------------------------------------- #
def _routing_registry() -> RoutingRegistry:
    reg = ModelCapabilityRegistry()
    reg.register(
        ModelCapabilities(
            provider="provider_a",
            model="provider_a-model",
            capabilities=frozenset({ModelCapability.TOOL_CALLING}),
        )
    )
    reg.register(
        ModelCapabilities(
            provider="provider_b",
            model="provider_b-model",
            capabilities=frozenset({ModelCapability.TOOL_CALLING}),
        )
    )
    return RoutingRegistry(capability_registry=reg)


def _fallback_manager(max_attempts: int = 2) -> FallbackManager:
    return FallbackManager(
        _routing_registry(), config=FallbackConfig(max_attempts=max_attempts)
    )


@contextmanager
def _deterministic_retry_config(
    failed_count: int = 3, failed_sleep: float = 0.0
) -> Iterator[None]:
    """Arahkan loader retry (`data/settings.json` -> api_retry) ke config tetap.

    Menjaga test DETERMINISTIK (tidak bergantung settings.json mesin pengembang)
    dan tidak menunggu delay nyata. `SETTINGS_PATH` dipulihkan setelah selesai.
    """
    from agent_ai.config import settings as settings_mod

    previous = settings_mod.SETTINGS_PATH
    directory = Path(tempfile.mkdtemp(prefix="aether-fallback-test-"))
    path = directory / "settings.json"
    path.write_text(
        json.dumps(
            {"api_retry": {"failed_count": failed_count, "failed_sleep": failed_sleep}}
        ),
        encoding="utf-8",
    )
    settings_mod.SETTINGS_PATH = path
    try:
        yield
    finally:
        settings_mod.SETTINGS_PATH = previous


def _make_runtime(
    provider: Any,
    *,
    tool: Optional[CountingEchoTool] = None,
    provider_factory: Optional[Any] = None,
    fallback_manager: Optional[FallbackManager] = None,
    cancel_token: Optional[CancellationToken] = None,
    events: Optional[List[Dict[str, Any]]] = None,
) -> AgentRuntime:
    registry = ToolRegistry()
    if tool is not None:
        registry.register(tool)
    store = None
    if events is not None:
        store = _InMemorySessionStore(
            sink=lambda et, payload: events.append({"type": et, **payload})
        )
    model_name = getattr(getattr(provider, "config", None), "model", "") or "test-model"
    return AgentRuntime(
        provider=provider,
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model=model_name),
        system_prompt=SYSTEM_PROMPT,
        use_continuous_loop=True,
        cancel_token=cancel_token,
        fallback_manager=fallback_manager,
        provider_factory=provider_factory,
        session_store=store,
        session_id="test-session" if store is not None else None,
    )


class _InMemorySessionStore:
    """Minimal session store: hanya menampung event (tanpa SSE)."""

    def __init__(self, sink: Optional[Any] = None) -> None:
        self.events: List[Any] = []
        self.sink = sink

    def append_event(self, event: Any) -> None:
        self.events.append(event)
        if self.sink is not None:
            payload = getattr(event, "payload", None)
            etype = getattr(getattr(event, "event_type", None), "value", None)
            self.sink(etype, dict(payload or {}))


def _prepared(task: str = "task") -> PreparedTask:
    return PreparedTask(task=task)


# --------------------------------------------------------------------------- #
# 1. Provider utama berhasil -> tidak ada fallback
# --------------------------------------------------------------------------- #
def test_primary_provider_success_no_fallback() -> None:
    provider_a = ScriptedProvider("provider_a", [_final_turn("selesai di A")])
    fallback_calls = {"n": 0}

    def factory(name: str) -> Any:
        fallback_calls["n"] += 1
        return ScriptedProvider(name, [_final_turn("B")])

    runtime = _make_runtime(
        provider_a,
        provider_factory=factory,
        fallback_manager=_fallback_manager(),
    )
    with _deterministic_retry_config():
        result = runtime.run(_prepared())

    assert result.status == RuntimeStatus.COMPLETED, result.error
    assert result.result == "selesai di A"
    assert provider_a.calls == 1, provider_a.calls
    assert fallback_calls["n"] == 0, "provider sukses tidak boleh memicu fallback"


# --------------------------------------------------------------------------- #
# 2. Provider utama gagal lalu fallback berhasil -> task lanjut, DONE
# --------------------------------------------------------------------------- #
def test_primary_fails_then_fallback_succeeds() -> None:
    # Provider A SELALU gagal (connection refused) untuk semua attempt.
    provider_a = ScriptedProvider(
        "provider_a", [RuntimeError("connection refused provider_a")]
    )
    provider_b = ScriptedProvider("provider_b", [_final_turn("selesai di B")])

    def factory(name: str) -> Any:
        return provider_b if name == "provider_b" else provider_a

    events: List[Dict[str, Any]] = []
    runtime = _make_runtime(
        provider_a,
        provider_factory=factory,
        fallback_manager=_fallback_manager(),
        events=events,
    )
    with _deterministic_retry_config():
        result = runtime.run(_prepared())

    assert result.status == RuntimeStatus.COMPLETED, result.error
    assert result.result == "selesai di B"
    # Provider A dicoba hingga budget habis (1 awal + 3 retry), lalu fallback.
    assert provider_a.calls == 4, provider_a.calls
    assert provider_b.calls == 1, provider_b.calls
    # Event fallback dipancarkan (phase_changed -> phase 'provider_fallback').
    fb_events = [
        e
        for e in events
        if e.get("phase") == "provider_fallback"
    ]
    assert fb_events, events
    # Provider alternatif di-persist ke runtime (digunakan request berikutnya).
    assert getattr(runtime.provider, "name", "") == "provider_b"


# --------------------------------------------------------------------------- #
# 3. Semua provider gagal -> FAILED (dilaporkan benar)
# --------------------------------------------------------------------------- #
def test_all_providers_fail_reports_failed() -> None:
    provider_a = ScriptedProvider("provider_a", [RuntimeError("a down")])
    provider_b = ScriptedProvider("provider_b", [RuntimeError("b down")])

    def factory(name: str) -> Any:
        return provider_b if name == "provider_b" else provider_a

    runtime = _make_runtime(
        provider_a,
        provider_factory=factory,
        fallback_manager=_fallback_manager(max_attempts=1),
    )
    with _deterministic_retry_config():
        result = runtime.run(_prepared())

    assert result.status == RuntimeStatus.FAILED, result.status
    # Pesan error jelas dan menyebut kegagalan provider.
    assert result.error, "harus ada pesan error"
    assert "gagal" in result.error.lower(), result.error
    # Provider B juga dicoba (setelah fallback), lalu seluruhnya gagal.
    assert provider_a.calls >= 1
    assert provider_b.calls >= 1, "fallback provider harus dicoba"


# --------------------------------------------------------------------------- #
# 4. Cancellation saat provider gagal -> CANCELLED (fallback tidak dipicu)
# --------------------------------------------------------------------------- #
def test_cancellation_during_provider_failure() -> None:
    token = CancellationToken()

    # Provider A gagal dan langsung meminta pembatalan pada error pertama.
    provider_a = ScriptedProvider(
        "provider_a", [RuntimeError("boom")], 
    )
    # Pasang hook pembatalan setelah generate dipanggil: gunakan subclass.
    original_generate = provider_a.generate

    def generate_with_cancel(*args: Any, **kwargs: Any) -> Any:
        token.request("user stop")
        return original_generate(*args, **kwargs)

    provider_a.generate = generate_with_cancel  # type: ignore[assignment]

    fallback_calls = {"n": 0}

    def factory(name: str) -> Any:
        fallback_calls["n"] += 1
        return ScriptedProvider(name, [_final_turn("B")])

    runtime = _make_runtime(
        provider_a,
        provider_factory=factory,
        fallback_manager=_fallback_manager(),
        cancel_token=token,
    )
    with _deterministic_retry_config():
        result = runtime.run(_prepared())

    assert result.status == RuntimeStatus.CANCELLED, result.status
    # Cancellation menghentikan retry segera: tidak ada fallback.
    assert provider_a.calls == 1, provider_a.calls
    assert fallback_calls["n"] == 0, "cancellation tidak boleh memicu fallback"


# --------------------------------------------------------------------------- #
# 5. Tool-calling pada pergantian provider: riwayat & hasil tool tetap utuh
# --------------------------------------------------------------------------- #
def test_tool_calling_preserved_across_provider_switch() -> None:
    tool = CountingEchoTool("echo")

    # Provider A: 1 turn tool call sukses, lalu SELALU gagal (provider error).
    provider_a = ScriptedProvider(
        "provider_a",
        [
            _tool_turn("panggil echo", [_tool_call("c1", "echo", {"value": "x"})]),
            RuntimeError("a down setelah tool"),
        ],
    )
    provider_b = ScriptedProvider("provider_b", [_final_turn("selesai via B")])

    def factory(name: str) -> Any:
        return provider_b if name == "provider_b" else provider_a

    events: List[Dict[str, Any]] = []
    runtime = _make_runtime(
        provider_a,
        tool=tool,
        provider_factory=factory,
        fallback_manager=_fallback_manager(),
        events=events,
    )
    with _deterministic_retry_config():
        result = runtime.run(_prepared())

    assert result.status == RuntimeStatus.COMPLETED, result.error
    assert result.result == "selesai via B"
    # Tool dieksekusi TEPAT SEKALI (perpindahan provider tidak mengulang tool).
    assert tool.calls == 1, tool.calls
    # Provider B menerima riwayat percakapan LENGKAP (system + user + assistant
    # tool_calls + tool result) -> native tool calling tetap utuh.
    assert provider_b.requests, "provider B harus menerima request"
    b_req = provider_b.requests[0]
    roles = [m.get("role") for m in b_req]
    assert "tool" in roles, roles
    tool_msgs = [m for m in b_req if m.get("role") == "tool"]
    assert len(tool_msgs) == 1, tool_msgs
    # Event fallback ada.
    assert any(e.get("phase") == "provider_fallback" for e in events)


# --------------------------------------------------------------------------- #
# 6. Tool failure BUKAN provider failure -> tidak memicu fallback
# --------------------------------------------------------------------------- #
class FailingTool(BaseTool):
    name = "boom"
    description = "selalu gagal"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        self.calls += 1
        raise RuntimeError("tool gagal, bukan provider")


def test_tool_failure_does_not_trigger_provider_fallback() -> None:
    tool = FailingTool()
    # Provider sukses tapi tool gagal; provider TIDAK boleh di-fallback.
    provider_a = ScriptedProvider(
        "provider_a",
        [
            _tool_turn("panggil boom", [_tool_call("c1", "boom", {})]),
            _final_turn("selesai tanpa fallback"),
        ],
    )
    fallback_calls = {"n": 0}

    def factory(name: str) -> Any:
        fallback_calls["n"] += 1
        return ScriptedProvider(name, [_final_turn("B")])

    registry = ToolRegistry()
    registry.register(tool)
    runtime = AgentRuntime(
        provider=provider_a,
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="test-model"),
        system_prompt=SYSTEM_PROMPT,
        use_continuous_loop=True,
        fallback_manager=_fallback_manager(),
        provider_factory=factory,
    )
    with _deterministic_retry_config():
        result = runtime.run(_prepared())

    assert result.status == RuntimeStatus.COMPLETED, result.error
    assert tool.calls == 1, tool.calls
    assert fallback_calls["n"] == 0, "tool failure tidak boleh memicu fallback"
    assert getattr(runtime.provider, "name", "") == "provider_a"


# --------------------------------------------------------------------------- #
# 7. Wiring produksi: build_runtime_fallback dari konfigurasi LLM tersimpan
# --------------------------------------------------------------------------- #
class _FakeInstance:
    def __init__(self, id_: str, provider_type: str, enabled: bool = True) -> None:
        self.id = id_
        self.provider_type = provider_type
        self.enabled = enabled


class _FakeModel:
    def __init__(self, model_name: str, enabled: bool = True) -> None:
        self.model_name = model_name
        self.enabled = enabled


class _FakeLLMConfigService:
    """Konfigurasi LLM minimal untuk menguji wiring produksi (deterministik)."""

    def __init__(self) -> None:
        self._instances = [
            _FakeInstance("i-a", "deepseek"),
            _FakeInstance("i-b", "openrouter"),
        ]
        self._models = {
            "i-a": [_FakeModel("deepseek-chat")],
            "i-b": [_FakeModel("openrouter/auto")],
        }
        self.resolve_calls: List[str] = []

    def list_provider_instances(self) -> List[Any]:
        return list(self._instances)

    def list_models(self, provider_id: str) -> List[Any]:
        return list(self._models.get(provider_id, []))

    def resolve_runtime_config(self, instance_id: str, **kwargs: Any) -> Dict[str, Any]:
        self.resolve_calls.append(instance_id)
        for inst in self._instances:
            if inst.id == instance_id:
                return {
                    "instance_id": inst.id,
                    "instance_name": inst.id,
                    "provider_type": inst.provider_type,
                    "api_url": "http://localhost:0/v1",
                    "api_key": "dummy",
                    "model": self._models[inst.id][0].model_name,
                }
        raise KeyError(instance_id)


class _FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    def is_available(self) -> bool:
        return True


def test_build_runtime_fallback_wiring_candidates_and_factory() -> None:
    from agent_ai.runtime.provider_fallback import (
        build_capability_registry,
        build_provider_factory,
        build_runtime_fallback,
    )

    service = _FakeLLMConfigService()

    # Capability registry: satu entri per (provider instance, model enabled).
    cap_reg = build_capability_registry(service)
    providers = set(cap_reg.providers())
    assert providers == {"deepseek", "openrouter"}, providers

    # Wiring lengkap (fallback_manager + provider_factory).
    wiring = build_runtime_fallback(service)
    assert wiring is not None
    assert wiring["fallback_manager"] is not None
    assert callable(wiring["provider_factory"])

    # Factory membangun provider dari instance yang cocok (provider type).
    factory = build_provider_factory(service)
    # Monkeypatch provider builder agar tidak memanggil konstruktor nyata.
    import agent_ai.providers.factory as provider_factory_mod

    original = provider_factory_mod.build_provider_from_config
    try:
        provider_factory_mod.build_provider_from_config = (  # type: ignore[assignment]
            lambda cfg: _FakeProvider(cfg["provider_type"])
        )
        provider = factory("openrouter")
        assert getattr(provider, "name", "") == "openrouter"
    finally:
        provider_factory_mod.build_provider_from_config = original  # type: ignore[assignment]

    # FallbackManager memutuskan FALLBACK dari kandidat konfigurasi tersimpan.
    from agent_ai.fallback import FallbackAction, FallbackReason, FallbackRequest

    fm = wiring["fallback_manager"]
    decision = fm.fallback(
        FallbackRequest(
            current_provider="deepseek",
            current_model="deepseek-chat",
            reason=FallbackReason.CONNECTION_FAILURE,
        )
    )
    assert decision.action == FallbackAction.FALLBACK, decision.action
    assert decision.selected is not None
    assert decision.selected.provider == "openrouter", decision.selected.provider


def test_build_runtime_fallback_none_without_config() -> None:
    from agent_ai.runtime.provider_fallback import build_runtime_fallback

    assert build_runtime_fallback(None) is None

    class _Empty:
        def list_provider_instances(self) -> List[Any]:
            return []

    assert build_runtime_fallback(_Empty()) is None


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
    print(f"[OK] {len(checks)} test lulus (provider fallback continuous loop)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_standalone())
