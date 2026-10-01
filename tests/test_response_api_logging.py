"""Tests: logging response API LLM (`data/settings.json` -> write_log_response_api).

Membuktikan bahwa:

- Loader `write_log_response_api` membaca `data/settings.json` (default False).
- Bila AKTIF, seluruh response LLM per task disimpan ke SATU file
  `.aether/log/response/<task_id>.json` (satu record per round/attempt),
  memuat metadata: provider, model, round, status, finish_reason, error, dan
  response mentah yang diterima AETHER.
- Bila response GAGAL (mis. connection/API error) -> status error + partial
  response terakhir yang diterima sebelum error (bila tersedia).
- Bila NONAKTIF (default) -> tidak ada file response yang ditulis (AETHER
  berjalan seperti sekarang).
- Directory `.aether/log/response/` dibuat otomatis.
- Integrasi runtime: logger hanya dibuat bila setting aktif.

Semua test memakai fake provider lokal (TANPA network).

Jalankan:
    python -m pytest tests/test_response_api_logging.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.config import settings as settings_mod  # noqa: E402
from agent_ai.config.settings import write_log_response_api  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.projects.aether_store import ResponseLog  # noqa: E402
from agent_ai.providers.base import (  # noqa: E402
    BaseProvider,
    GenerateOptions,
    GenerateResult,
    ProviderAPIError,
)
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.runtime.runtime import AgentRuntime  # noqa: E402
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
            {
                "message": {"content": text, "tool_calls": calls},
                "finish_reason": "tool_calls",
            }
        ]
    }


def _final_turn(text: str) -> Dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class ScriptedProvider(OpenAICompatibleProvider):
    """Provider palsu: mengembalikan respons skrip berurutan (tanpa network)."""

    name = "scripted"

    def __init__(self, script: List[Dict[str, Any]]) -> None:
        self.config = SimpleNamespace(model="scripted-model")
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
        raw = self.script[idx] if idx < len(self.script) else _final_turn("(default)")
        return GenerateResult(text="", model="scripted-model", provider=self.name, raw=raw)


class FailingProvider(BaseProvider):
    """Provider palsu yang SELALU melempar error (mis. API terputus)."""

    name = "failing"

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls = 0

    def generate(self, *args: Any, **kwargs: Any) -> GenerateResult:
        self.calls += 1
        raise self._error


class NoopTool(BaseTool):
    """Tool palsu yang mengembalikan output kecil."""

    def __init__(self, name: str = "read_file") -> None:
        self.name = name
        self.description = "fake noop reader"
        self.input_schema = {"type": "object", "properties": {"path": {"type": "string"}}}

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        return {"content": "ok"}


def _registry_with_noop() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(NoopTool("read_file"))
    return registry


def _response_path(root: Path, task_id: str) -> Path:
    return root / ".aether" / "log" / "response" / f"{task_id}.json"


class _Prepared:
    """PreparedTask minimal untuk menguji runtime (tanpa planner/LLM)."""

    def __init__(self, task: str, task_id: Optional[str] = None, plan: Any = None) -> None:
        self.task = task
        self.task_id = task_id
        self.plan = plan

    def context_text(self) -> str:
        return ""


# --------------------------------------------------------------------------- #
# Loader (data/settings.json -> write_log_response_api)
# --------------------------------------------------------------------------- #
def test_loader_true(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps({"write_log_response_api": True}), encoding="utf-8"
    )
    assert write_log_response_api() is True


def test_loader_false(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps({"compression": {"enabled": False}, "write_log_response_api": False}),
        encoding="utf-8",
    )
    assert write_log_response_api() is False


def test_loader_default_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "no_such_settings.json")
    assert write_log_response_api() is False


def test_loader_default_missing_field(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(json.dumps({"compression": {}}), encoding="utf-8")
    assert write_log_response_api() is False


# --------------------------------------------------------------------------- #
# Orchestrator: seluruh response dari setiap round dalam SATU file
# --------------------------------------------------------------------------- #
def test_orchestrator_writes_all_rounds_single_file(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    script = [
        _tool_turn("baca a", [_tool_call("c1", "read_file", {"path": "a.txt"})]),
        _tool_turn("baca b", [_tool_call("c2", "read_file", {"path": "b.txt"})]),
        _final_turn("Selesai."),
    ]
    provider = ScriptedProvider(script)
    response_log = ResponseLog(root, task_id="task-abc")
    orch = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=_registry_with_noop()),
        response_log=response_log,
    )
    result = orch.run("kerjakan task")
    assert result.status == AgentStatus.DONE, result.error
    assert provider.calls == 3

    path = _response_path(root, "task-abc")
    assert path.exists(), path
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["task_id"] == "task-abc"
    responses = data["responses"]
    # SATU file berisi SELURUH response dari setiap round.
    assert len(responses) == 3
    assert [r["round"] for r in responses] == [1, 2, 3]

    first = responses[0]
    # Metadata relevan tersedia.
    for key in ("provider", "model", "round", "status", "finish_reason", "error", "response"):
        assert key in first, key
    assert first["provider"] == "scripted"
    assert first["model"] == "scripted-model"
    assert first["status"] == "success"
    assert first["finish_reason"] == "tool_calls"
    # Response mentah yang benar-benar diterima AETHER tersimpan.
    assert first["response"] is not None
    assert first["response"]["choices"][0]["message"]["tool_calls"][0]["id"] == "c1"

    last = responses[-1]
    assert last["status"] == "success"
    assert last["finish_reason"] == "stop"
    assert last["text"] == "Selesai."


def test_record_directory_created_automatically(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    response_log = ResponseLog(root, task_id="auto-dir")
    assert response_log.append({"round": 1, "status": "success"}) is True
    assert _response_path(root, "auto-dir").parent.is_dir()
    data = json.loads(_response_path(root, "auto-dir").read_text(encoding="utf-8"))
    assert data["responses"] == [{"round": 1, "status": "success"}]


def test_append_keeps_previous_records(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    response_log = ResponseLog(root, task_id="t-append")
    assert response_log.append({"round": 1})
    assert response_log.append({"round": 2})
    # Instance baru membaca file yang sudah ada -> tidak menghilangkan record lama.
    again = ResponseLog(root, task_id="t-append")
    assert again.append({"round": 3})
    data = json.loads(_response_path(root, "t-append").read_text(encoding="utf-8"))
    assert [r["round"] for r in data["responses"]] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# Error: partial response terakhir tersimpan
# --------------------------------------------------------------------------- #
def test_error_records_partial_response(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    # Retry request API kini dibaca dari `data/settings.json` -> `api_retry`.
    # Arahkan ke konfigurasi tetap (failed_count=3, failed_sleep=0) agar test ini
    # tetap DETERMINISTIK & cepat (menguji logging per-attempt, bukan jumlah
    # retry) dan tidak mewarisi delay nyata dari settings.json mesin pengembang.
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps({"api_retry": {"failed_count": 3, "failed_sleep": 0}}),
        encoding="utf-8",
    )
    error = ProviderAPIError(
        "Provider 'x' mengembalikan HTTP 500",
        status_code=500,
        response_body="{\"partial\": \"terputus di tengah",
    )
    provider = FailingProvider(error)
    response_log = ResponseLog(root, task_id="task-err")
    orch = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=_registry_with_noop()),
        response_log=response_log,
    )
    result = orch.run("kerjakan task")
    assert result.status == AgentStatus.FAILED

    data = json.loads(_response_path(root, "task-err").read_text(encoding="utf-8"))
    responses = data["responses"]
    assert responses, "error attempt harus tercatat"
    # Semua attempt pada round yang sama (1 attempt awal + retry).
    assert {r["round"] for r in responses} == {1}
    assert [r["attempt"] for r in responses] == list(range(1, len(responses) + 1))
    for record in responses:
        assert record["status"] == "error"
        assert record["error"]["type"] == "ProviderAPIError"
        # Partial response terakhir yang diterima sebelum error tersimpan.
        assert record["partial_response"] == "{\"partial\": \"terputus di tengah"


# --------------------------------------------------------------------------- #
# Nonaktif: tanpa logger -> tidak ada response API yang ditulis
# --------------------------------------------------------------------------- #
def test_disabled_writes_nothing(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    provider = ScriptedProvider([_final_turn("selesai")])
    orch = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=_registry_with_noop()),
        # response_log tidak diberikan (default None) = logging nonaktif.
    )
    result = orch.run("kerjakan task")
    assert result.status == AgentStatus.DONE
    assert not (root / ".aether").exists()


# --------------------------------------------------------------------------- #
# Integrasi runtime: logger dibuat HANYA bila setting aktif
# --------------------------------------------------------------------------- #
def test_runtime_gates_response_log_on_setting(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    provider = ScriptedProvider([_final_turn("x")])
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")

    # OFF (default) -> tidak ada logger response.
    (tmp_path / "settings.json").write_text(
        json.dumps({"write_log_response_api": False}), encoding="utf-8"
    )
    off = AgentRuntime(provider=provider, project_root=str(root), project_brain=False)
    off._current_task_id = "t-off"
    off._setup_project_storage(_Prepared(task="x", task_id="t-off"))
    assert off._response_log is None
    assert off._make_orchestrator(provider).response_log is None

    # ON -> logger dibuat dan diteruskan ke orchestrator.
    (tmp_path / "settings.json").write_text(
        json.dumps({"write_log_response_api": True}), encoding="utf-8"
    )
    on = AgentRuntime(provider=provider, project_root=str(root), project_brain=False)
    on._current_task_id = "t-on"
    on._setup_project_storage(_Prepared(task="x", task_id="t-on"))
    assert on._response_log is not None
    assert on._make_orchestrator(provider).response_log is on._response_log


def test_runtime_run_writes_response_file(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps({"write_log_response_api": True}), encoding="utf-8"
    )
    provider = ScriptedProvider([_final_turn("selesai")])
    runtime = AgentRuntime(provider=provider, project_root=str(root), project_brain=False)
    result = runtime.run(_Prepared(task="halo", task_id="taskZ"))
    assert result.status.value == "completed", result.error

    path = _response_path(root, "taskZ")
    assert path.exists(), path
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["responses"][0]["status"] == "success"
    assert data["responses"][0]["provider"] == "scripted"
