"""Tests: retry request API LLM (`data/settings.json` -> api_retry).

Membuktikan bahwa:

- Loader `api_retry_config` / `api_retry_failed_count` / `api_retry_failed_sleep`
  membaca objek `api_retry` dari `data/settings.json` (field `failed_count` &
  `failed_sleep`), dengan DEFAULT AMAN (mempertahankan behavior lama) bila file
  / objek / field tidak ada atau nilainya tidak valid.
- `failed_count` mengontrol jumlah pengulangan retry request API LLM pada jalur
  retry yang SUDAH ADA (`AgentOrchestrator._generate_with_retry`): total attempt
  = 1 attempt awal + `failed_count`. TIDAK mengubah reasoning/decision loop LLM.
- `failed_sleep` = jeda (detik) SEBELUM setiap pengulangan (0 = tanpa jeda).
- Setelah request berhasil, counter retry untuk request berikutnya kembali ke 1
  (budget retry adalah per pemanggilan provider, bukan global).
- Logging response API existing tetap mencatat SETIAP attempt ke
  `.aether/log/response/<task_id>.json`.

Semua test deterministik, tanpa network, dan tanpa delay nyata.

Jalankan:
    python -m pytest tests/test_api_retry_config.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import agent_ai.core.orchestrator as orchestrator_mod  # noqa: E402
from agent_ai.config import settings as settings_mod  # noqa: E402
from agent_ai.config.settings import (  # noqa: E402
    ApiRetryConfig,
    api_retry_config,
    api_retry_failed_count,
    api_retry_failed_sleep,
)
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.projects.aether_store import ResponseLog  # noqa: E402
from agent_ai.providers.base import (  # noqa: E402
    GenerateOptions,
    GenerateResult,
)
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes (lokal, deterministik, tanpa network)
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
            {"message": {"content": text, "tool_calls": calls}, "finish_reason": "tool_calls"}
        ]
    }


def _final_turn(text: str) -> Dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class ScriptedProvider(OpenAICompatibleProvider):
    """Provider palsu: tiap call mengembalikan turn skrip atau melempar error.

    Item skrip: dict (turn provider) | BaseException | class Exception. Bila
    skrip habis, item TERAKHIR diulang (mis. satu error -> gagal terus).
    """

    name = "scripted-retry"

    def __init__(self, script: List[Any]) -> None:
        self.config = SimpleNamespace(model="retry-model")
        self.script = list(script)
        self.calls = 0

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Any]] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[List[Any]] = None,
        tool_choice: Optional[Any] = None,
    ) -> GenerateResult:
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
            raise item if isinstance(item, BaseException) else item("provider error")
        return GenerateResult(text="", model="retry-model", provider=self.name, raw=item)


class EchoTool(BaseTool):
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
    provider: ScriptedProvider,
    *,
    registry: Optional[ToolRegistry] = None,
    response_log: Optional[Any] = None,
) -> AgentOrchestrator:
    if registry is None:
        registry = ToolRegistry()
    return AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="retry-model"),
        use_continuous_loop=True,
        response_log=response_log,
    )


@contextmanager
def _retry_settings(failed_count: int, failed_sleep: float) -> Iterator[Path]:
    """Arahkan loader retry ke settings.json SEMENTARA (deterministik)."""
    previous = settings_mod.SETTINGS_PATH
    directory = Path(tempfile.mkdtemp(prefix="aether-api-retry-"))
    path = directory / "settings.json"
    path.write_text(
        json.dumps(
            {"api_retry": {"failed_count": failed_count, "failed_sleep": failed_sleep}}
        ),
        encoding="utf-8",
    )
    settings_mod.SETTINGS_PATH = path
    try:
        yield path
    finally:
        settings_mod.SETTINGS_PATH = previous


# --------------------------------------------------------------------------- #
# Loader (data/settings.json -> api_retry)
# --------------------------------------------------------------------------- #
def test_loader_reads_api_retry_values(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps({"api_retry": {"failed_count": 10, "failed_sleep": 2}}),
        encoding="utf-8",
    )
    cfg = api_retry_config()
    assert cfg.failed_count == 10
    assert cfg.failed_sleep == 2.0
    assert api_retry_failed_count() == 10
    assert api_retry_failed_sleep() == 2.0


def test_loader_default_when_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "no_such.json")
    assert api_retry_config() == ApiRetryConfig(3, 0.0)


def test_loader_default_when_missing_section(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps({"compression": {"enabled": False}, "write_log_response_api": True}),
        encoding="utf-8",
    )
    assert api_retry_config() == ApiRetryConfig(3, 0.0)


def test_loader_default_when_invalid_values(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps({"api_retry": {"failed_count": -5, "failed_sleep": "abc"}}),
        encoding="utf-8",
    )
    assert api_retry_config() == ApiRetryConfig(3, 0.0)


def test_loader_default_when_section_not_object(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps({"api_retry": 5}), encoding="utf-8"
    )
    assert api_retry_config() == ApiRetryConfig(3, 0.0)


def test_loader_partial_field_keeps_other_default(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps({"api_retry": {"failed_count": 7}}), encoding="utf-8"
    )
    cfg = api_retry_config()
    assert cfg.failed_count == 7
    assert cfg.failed_sleep == 0.0


# --------------------------------------------------------------------------- #
# failed_count mengontrol jumlah pengulangan retry
# --------------------------------------------------------------------------- #
def test_failed_count_controls_retry_attempts():
    provider = ScriptedProvider([RuntimeError("koneksi gagal")])
    with _retry_settings(2, 0):
        result = _make_orchestrator(provider).run("task")
    assert result.status == AgentStatus.FAILED
    # 1 attempt awal + 2 pengulangan = 3 attempt.
    assert provider.calls == 3, provider.calls
    assert "gagal setelah 3 attempt" in (result.error or "")


def test_failed_count_zero_means_no_retry():
    provider = ScriptedProvider([RuntimeError("koneksi gagal")])
    with _retry_settings(0, 0):
        result = _make_orchestrator(provider).run("task")
    assert result.status == AgentStatus.FAILED
    assert provider.calls == 1, provider.calls


def test_success_within_configured_retries():
    provider = ScriptedProvider(
        [RuntimeError("boom-1"), RuntimeError("boom-2"), _final_turn("pulih")]
    )
    with _retry_settings(2, 0):
        result = _make_orchestrator(provider).run("task")
    assert result.status == AgentStatus.DONE, result.error
    assert result.result == "pulih"
    assert provider.calls == 3, provider.calls


def test_default_config_preserves_old_behavior(tmp_path, monkeypatch):
    # Tanpa konfigurasi -> default lama: 1 attempt awal + 3 pengulangan = 4.
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "no_such.json")
    provider = ScriptedProvider([RuntimeError("boom")])
    result = _make_orchestrator(provider).run("task")
    assert result.status == AgentStatus.FAILED
    assert provider.calls == 4, provider.calls


# --------------------------------------------------------------------------- #
# failed_sleep = jeda SEBELUM setiap pengulangan
# --------------------------------------------------------------------------- #
@contextmanager
def _record_sleeps():
    recorded: List[float] = []
    original = orchestrator_mod.time
    orchestrator_mod.time = SimpleNamespace(sleep=lambda seconds: recorded.append(seconds))
    try:
        yield recorded
    finally:
        orchestrator_mod.time = original


def test_failed_sleep_used_before_each_retry():
    provider = ScriptedProvider(
        [RuntimeError("boom-1"), RuntimeError("boom-2"), _final_turn("ok")]
    )
    with _record_sleeps() as recorded:
        with _retry_settings(2, 1.5):
            result = _make_orchestrator(provider).run("task")
    assert result.status == AgentStatus.DONE, result.error
    assert recorded == [1.5, 1.5], recorded


def test_zero_failed_sleep_does_not_sleep():
    provider = ScriptedProvider([RuntimeError("boom"), _final_turn("ok")])
    with _record_sleeps() as recorded:
        with _retry_settings(3, 0):
            result = _make_orchestrator(provider).run("task")
    assert result.status == AgentStatus.DONE, result.error
    assert recorded == [], recorded


# --------------------------------------------------------------------------- #
# Counter retry reset untuk request berikutnya (budget per provider-call)
# --------------------------------------------------------------------------- #
def test_retry_counter_resets_for_next_request():
    tool = EchoTool("echo")
    registry = ToolRegistry()
    registry.register(tool)
    script = [
        RuntimeError("iter1-attempt1-gagal"),
        _tool_turn("pakai echo", [_tool_call("c1", "echo", {"value": "x"})]),
        _final_turn("selesai"),
    ]
    provider = ScriptedProvider(script)
    # Budget 1 retry per request; request iterasi 1 memakainya, request iterasi 2
    # TIDAK boleh "keracunan" sisa budget -> mulai lagi dari awal.
    with _retry_settings(1, 0):
        result = _make_orchestrator(provider, registry=registry).run("task")
    assert result.status == AgentStatus.DONE, result.error
    assert provider.calls == 3, provider.calls
    assert tool.calls == 1, tool.calls


# --------------------------------------------------------------------------- #
# Logging response API existing tetap mencatat SETIAP attempt
# --------------------------------------------------------------------------- #
def _response_path(root: Path, task_id: str) -> Path:
    return root / ".aether" / "log" / "response" / f"{task_id}.json"


def test_response_log_records_each_attempt(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    provider = ScriptedProvider(
        [RuntimeError("gagal-1"), RuntimeError("gagal-2"), _final_turn("selesai")]
    )
    response_log = ResponseLog(root, task_id="task-retry")
    with _retry_settings(2, 0):
        result = _make_orchestrator(provider, response_log=response_log).run("task")
    assert result.status == AgentStatus.DONE, result.error

    data = json.loads(_response_path(root, "task-retry").read_text(encoding="utf-8"))
    records = data["responses"]
    # Satu round logis, seluruh attempt tercatat berurutan.
    assert [r["round"] for r in records] == [1, 1, 1]
    assert [r["attempt"] for r in records] == [1, 2, 3]
    assert [r["status"] for r in records] == ["error", "error", "success"]
