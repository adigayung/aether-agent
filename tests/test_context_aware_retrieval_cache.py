"""Regression tests: cache retrieval SADAR-KONTEKS + prompt default Agent.

Fokus 1 (CORE FIX): dedup read/search TIDAK boleh mengklaim sumber "sudah
tersedia" bila detailnya baru saja DIBUANG oleh runtime context compaction dari
konteks yang dikirim ke LLM (false positive -> LLM tidak pernah menerima isi
yang dibutuhkannya). Perbaikan minimal memakai satu instance ToolReadCache per
task dan MENYELARASKANnya dengan pesan yang benar-benar dikirim.

    A. build_registry membagi SATU ToolReadCache; Consultant TIDAK (read_cache
       None) -> boundary Consultant tidak berubah.
    B. sync_from_context(): rentang yang hilang dari konteks TIDAK lagi
       "tersedia"; rentang yang masih terlihat tetap "tersedia".
    C. forget_search(): pencarian yang hasilnya hilang dapat dijalankan ulang.
    D. Orchestrator: konteks PENDek -> dedup normal tetap bekerja (stub).
    E. Orchestrator: konteks yang DIPADATKAN -> read_file mengirim ISI LAGI.
    F. Orchestrator: search_code yang dipadatkan -> pencarian diulang (bukan
       already_searched palsu).
    G. Alur normal (baca -> edit -> validasi) TIDAK berubah.

Fokus 2 (prompt): panduan Agent default (retrieval -> implementasi -> validasi +
semantik state) benar-benar ada dan di-inject runtime bila pemanggil tidak
memberi system prompt.

Semua test memakai fake provider/tool lokal (TANPA network).

Jalankan:
    python -m pytest tests/test_context_aware_retrieval_cache.py
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

from agent_ai.consultant.tools import build_consultant_registry  # noqa: E402
from agent_ai.core.agent_prompt import build_agent_system_prompt  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.history import ConversationHistory  # noqa: E402
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.core.types import ChatMessage  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
)
from agent_ai.runtime.runtime import AgentRuntime  # noqa: E402
from agent_ai.tools.filesystem import ReadFileTool, SearchCodeTool  # noqa: E402
from agent_ai.tools.read_cache import ToolReadCache  # noqa: E402
from agent_ai.tools.registry import ToolRegistry, build_registry  # noqa: E402


# --------------------------------------------------------------------------- #
# Fake provider (lokal, deterministik, tanpa network)
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
        raw = self.script[idx] if idx < len(self.script) else _final_turn("(default)")
        return GenerateResult(
            text="", model="scripted-model", provider=self.name, raw=raw
        )


class RecordingReadFile(ReadFileTool):
    """read_file yang merekam HASIL yang benar-benar dikembalikan (stub/isi).

    Dipakai agar test dapat memeriksa apakah read kedua mengirim ISI atau stub
    TANPA bergantung pada bentuk pesan yang sudah dipadatkan di request.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.results: List[Dict[str, Any]] = []

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        result = super().execute(**arguments)
        self.results.append(result)
        return result


def _read_registry(root: Path) -> tuple[ToolRegistry, ToolReadCache, RecordingReadFile]:
    """Registry minimal (hanya read_file) dengan cache bersama (mirror build_registry)."""
    cache = ToolReadCache()
    registry = ToolRegistry()
    registry.read_cache = cache
    reader = RecordingReadFile(root=root, read_cache=cache)
    registry.register(reader)
    return registry, cache, reader


def _min_orchestrator(registry: ToolRegistry, **kwargs: Any) -> AgentOrchestrator:
    return AgentOrchestrator(
        provider=ScriptedProvider([]),
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="scripted-model"),
        system_prompt="agent",
        use_continuous_loop=True,
        **kwargs,
    )


def _write_lines(path: Path, count: int, fill: int = 40) -> None:
    path.write_text(
        "\n".join(f"line {i} " + ("X" * fill) for i in range(1, count + 1)),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# A. Wiring cache: Agent dibagi, Consultant TIDAK.
# --------------------------------------------------------------------------- #
def test_registry_shares_one_read_cache(tmp_path: Path) -> None:
    registry = build_registry(root=tmp_path)
    cache = registry.read_cache
    assert isinstance(cache, ToolReadCache)
    # Tool read/search memakai cache yang SAMA (bukan instance terpisah).
    assert registry.get("read_file")._read_cache is cache  # noqa: SLF001
    assert registry.get("search_code")._read_cache is cache  # noqa: SLF001


def test_consultant_registry_not_context_synced(tmp_path: Path) -> None:
    # Consultant TIDAK memakai cache retrieval -> sinkronisasi konteks no-op
    # (boundary Consultant tidak berubah).
    registry = build_consultant_registry(root=tmp_path, mode="investigate")
    assert registry.read_cache is None


# --------------------------------------------------------------------------- #
# B. sync_from_context: rentang hilang -> tidak tersedia; masih ada -> tersedia.
# --------------------------------------------------------------------------- #
def test_sync_from_context_drops_lost_and_keeps_visible(tmp_path: Path) -> None:
    _write_lines(tmp_path / "a.py", 300)
    cache = ToolReadCache()
    reader = ReadFileTool(root=tmp_path, read_cache=cache)

    assert "content" in reader.execute(path="a.py", start_line=1, end_line=100)
    assert "content" in reader.execute(path="a.py", start_line=150, end_line=200)

    # Konteks hanya MEMUAT rentang 1-100 (150-200 dibuang oleh compaction).
    cache.sync_from_context({"a.py": [(1, 100)]}, {"a.py"})

    # Masih tercakup -> tetap dedup.
    kept = reader.execute(path="a.py", start_line=50, end_line=60)
    assert kept.get("already_available") is True and "content" not in kept

    # Tidak lagi tercakup -> isi DIKIRIM LAGI (bukan stub palsu).
    resent = reader.execute(path="a.py", start_line=160, end_line=180)
    assert "content" in resent and "already_available" not in resent


def test_sync_from_context_all_lost_resends_full(tmp_path: Path) -> None:
    _write_lines(tmp_path / "a.py", 50)
    cache = ToolReadCache()
    reader = ReadFileTool(root=tmp_path, read_cache=cache)

    assert "content" in reader.execute(path="a.py", start_line=1, end_line=20)
    # Semua hasil read hilang dari konteks.
    cache.sync_from_context({}, {"a.py"})
    again = reader.execute(path="a.py", start_line=1, end_line=20)
    assert "content" in again and "already_available" not in again


# --------------------------------------------------------------------------- #
# C. forget_search: hasil hilang -> pencarian dapat dijalankan ulang.
# --------------------------------------------------------------------------- #
def test_forget_search_reallows_same_query(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("needle\nother\nneedle\n", encoding="utf-8")
    cache = ToolReadCache()
    searcher = SearchCodeTool(root=tmp_path, read_cache=cache)

    first = searcher.execute(query="needle")
    assert first["count"] == 2 and "matches" in first
    # Query identik di state sama -> stub, TIDAK dijalankan ulang.
    assert searcher.execute(query="needle").get("already_searched") is True

    # Hasil hilang dari konteks -> lupakan -> pencarian dijalankan ulang.
    cache.forget_search("needle", ".", 0)
    again = searcher.execute(query="needle")
    assert again["count"] == 2 and "matches" in again


# --------------------------------------------------------------------------- #
# D/E. Orchestrator: dedup normal vs pemulihan setelah compaction.
# --------------------------------------------------------------------------- #
def test_dedup_preserved_when_context_intact(tmp_path: Path) -> None:
    _write_lines(tmp_path / "big.py", 200)
    registry, _, reader = _read_registry(tmp_path)
    provider = ScriptedProvider(
        [
            _tool_turn(
                "baca",
                [_tool_call("r1", "read_file", {"path": "big.py", "start_line": 1, "end_line": 200})],
            ),
            _tool_turn(
                "baca lagi",
                [_tool_call("r2", "read_file", {"path": "big.py", "start_line": 1, "end_line": 200})],
            ),
            _final_turn("selesai"),
        ]
    )
    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="scripted-model"),
        system_prompt="agent",
        use_continuous_loop=True,
        context_budget_tokens=200_000,  # cukup besar -> TANPA compaction
    )
    result = orchestrator.run("baca big.py")
    assert result.status == AgentStatus.DONE

    # Dua read dijalankan; yang kedua tetap di-dedup karena konteks utuh.
    assert len(reader.results) == 2
    second = reader.results[1]
    # Konteks masih utuh -> dedup normal bekerja (stub, tanpa isi).
    assert second.get("already_available") is True
    assert "content" not in second


def test_compaction_recovers_read_content(tmp_path: Path) -> None:
    _write_lines(tmp_path / "big.py", 200)
    registry, _, reader = _read_registry(tmp_path)
    provider = ScriptedProvider(
        [
            _tool_turn(
                "baca",
                [_tool_call("r1", "read_file", {"path": "big.py", "start_line": 1, "end_line": 200})],
            ),
            _tool_turn(
                "baca lagi",
                [_tool_call("r2", "read_file", {"path": "big.py", "start_line": 1, "end_line": 200})],
            ),
            _final_turn("selesai"),
        ]
    )
    events: List[Dict[str, Any]] = []
    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="scripted-model"),
        system_prompt="agent",
        use_continuous_loop=True,
        context_budget_tokens=1_200,  # kecil -> compaction aktif
        event_sink=lambda event_type, payload: events.append(
            {"type": event_type, **payload}
        ),
    )
    result = orchestrator.run("baca big.py")
    assert result.status == AgentStatus.DONE
    assert any(
        event.get("context_compacted")
        for event in events
        if event.get("type") == "provider_request"
    )

    second = reader.results[1]
    # Detail read pertama sudah DIBUANG compaction -> isi DIKIRIM LAGI.
    assert "content" in second
    assert second.get("already_available") is not True


def test_sync_forgets_search_when_not_visible(tmp_path: Path) -> None:
    cache = ToolReadCache()
    registry = ToolRegistry()
    registry.read_cache = cache
    registry.register(SearchCodeTool(root=tmp_path, read_cache=cache))
    orchestrator = _min_orchestrator(registry)

    content = json.dumps(
        {
            "query": "needle",
            "path": ".",
            "count": 1,
            "truncated": False,
            "matches": [{"file": "a.py", "line": 1, "text": "needle"}],
        }
    )
    history = ConversationHistory()
    history.append_system_message("s")
    history.append_user_message("u")
    history.append_tool_result("s1", "search_code", content)

    # Pencarian pernah diklaim (simulasi hasil search sebelumnya).
    cache.record_search("needle", ".", 0)
    # Hasil MASIH terlihat -> klaim tetap ada.
    orchestrator._sync_retrieval_cache(history, history.messages)  # noqa: SLF001
    assert cache.claim_search("needle", ".", 0) is True

    # Hasil DIPADATKAN (isi hilang) -> klaim dilupakan -> bisa dicari ulang.
    compiled = [
        history.messages[0],
        history.messages[1],
        ChatMessage(
            role="tool",
            content="[dipadatkan] search_code(query='needle') …",
            name="search_code",
            tool_call_id="s1",
        ),
    ]
    orchestrator._sync_retrieval_cache(history, compiled)  # noqa: SLF001
    assert cache.claim_search("needle", ".", 0) is False


# --------------------------------------------------------------------------- #
# F. Alur normal (baca -> edit) tetap berjalan.
# --------------------------------------------------------------------------- #
def test_normal_read_then_edit_flow_still_works(tmp_path: Path) -> None:
    from agent_ai.tools.workspace import EditFileTool

    (tmp_path / "m.py").write_text("value = 1\nother = 2\n", encoding="utf-8")
    cache = ToolReadCache()
    registry = ToolRegistry()
    registry.read_cache = cache
    registry.register(ReadFileTool(root=tmp_path, read_cache=cache))
    registry.register(EditFileTool(root=tmp_path, read_cache=cache))
    provider = ScriptedProvider(
        [
            _tool_turn(
                "baca",
                [_tool_call("r1", "read_file", {"path": "m.py", "start_line": 1, "end_line": 1})],
            ),
            _tool_turn(
                "edit",
                [_tool_call("e1", "edit_file", {"path": "m.py", "old_text": "value = 1", "new_text": "value = 42"})],
            ),
            _final_turn("selesai"),
        ]
    )
    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="scripted-model"),
        system_prompt="agent",
        use_continuous_loop=True,
        context_budget_tokens=200_000,
    )
    result = orchestrator.run("ubah value menjadi 42")
    assert result.status == AgentStatus.DONE
    assert (tmp_path / "m.py").read_text(encoding="utf-8").startswith("value = 42")


# --------------------------------------------------------------------------- #
# G. Prompt default Agent + wiring runtime.
# --------------------------------------------------------------------------- #
def test_agent_default_prompt_guides_retrieval_and_state() -> None:
    prompt = build_agent_system_prompt()
    # Fase kerja eksplisit.
    assert "RETRIEVAL" in prompt
    assert "BERHENTI RETRIEVAL" in prompt
    assert "IMPLEMENTASI" in prompt and "VALIDASI" in prompt
    # Semantik state tool result.
    assert "State Sumber Informasi" in prompt
    assert "already_available" in prompt
    assert "already_read" in prompt
    assert "already_searched" in prompt
    assert "force=true" in prompt
    # Larangan memakai run_command untuk membaca source.
    assert "JANGAN pakai run_command" in prompt


def test_runtime_injects_default_agent_prompt() -> None:
    runtime = AgentRuntime(provider=ScriptedProvider([]), project_root=None)
    orchestrator = runtime._make_orchestrator(runtime.provider)  # noqa: SLF001
    assert orchestrator.system_prompt == build_agent_system_prompt()


def test_runtime_respects_explicit_system_prompt() -> None:
    runtime = AgentRuntime(
        provider=ScriptedProvider([]), system_prompt="CUSTOM PROMPT", project_root=None
    )
    orchestrator = runtime._make_orchestrator(runtime.provider)  # noqa: SLF001
    assert orchestrator.system_prompt == "CUSTOM PROMPT"
