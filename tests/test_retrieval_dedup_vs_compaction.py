"""Regression tests: dedup retrieval TIDAK lagi ter-reset oleh context compaction.

Root cause yang dijaga (dari audit runtime + reproduksi deterministik):

    `AgentOrchestrator._sync_retrieval_cache()` menyelaraskan cache dedup
    (read_file/search_code/list_files) dengan pesan yang BENAR-BENAR dikirim ke
    LLM. Sebelumnya, sebuah hasil tool baru dianggap "tidak terlihat" bila
    isinya DIPADATKAN oleh `compile_compacted_messages` — padahal ia MASIH
    dikirim (lokator + preview). Akibatnya dedicated dedup DIMATIKAN setiap
    round ketika anggaran sempit (jendela kerja dipadatkan tiap round):

        read/search -> hasil dipadatkan -> dedup dilepas -> Agent mengulang
        permintaan identik -> physical read/scan ulang + isi dikirim ulang
        -> langsung dipadatkan lagi -> BERULANG (oscillation).

Properti yang dikunci di sini:

    A. Compaction memadatkan hasil read tetapi pesannya MASIH dikirim ->
       dedup TETAP berlaku: read identik kedua = stub (TANPA physical read,
       TANPA kirim ulang isi). Ini menghilangkan oscillation.
    B. Sama untuk `search_code` (klaim pencarian tidak dilepas saat hasilnya
       hanya dipadatkan).
    C. Sama untuk `list_files` (list directory identik = stub).
    D. Eviction SESUNGGUHNYA (pesan benar-benar dibuang dari konteks) TETAP
       melepas dedup: isi/listing/pencarian dapat diambil ulang (tidak ada stub
       palsu yang membuat Agent tidak pernah menerima data).
    E. Mutasi file membatalkan klaim listing (listing akurat kembali).
    F. Backward compatible: tanpa cache (read_cache=None) tidak ada dedup.

Semua deterministik, tanpa network/LLM asli.

Jalankan:
    python -m pytest tests/test_retrieval_dedup_vs_compaction.py
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

from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.history import ConversationHistory  # noqa: E402
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.core.types import ChatMessage  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
)
from agent_ai.tools.filesystem import (  # noqa: E402
    ListFilesTool,
    ReadFileTool,
    SearchCodeTool,
)
from agent_ai.tools.read_cache import ToolReadCache  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402
from agent_ai.tools.workspace import WriteFileTool  # noqa: E402


# --------------------------------------------------------------------------- #
# Harness (fake provider, tanpa network)
# --------------------------------------------------------------------------- #
def _tool_call(call_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _tool_turn(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"content": "", "tool_calls": calls},
                "finish_reason": "tool_calls",
            }
        ]
    }


def _final_turn(text: str = "selesai") -> Dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class ScriptedProvider(OpenAICompatibleProvider):
    """Provider palsu: mengembalikan respons skrip berurutan."""

    name = "scripted"

    def __init__(self, script: List[Dict[str, Any]]) -> None:
        self.config = SimpleNamespace(model="scripted-model")
        self.script = list(script)
        self.calls = 0

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        idx = self.calls
        self.calls += 1
        raw = self.script[idx] if idx < len(self.script) else _final_turn()
        return GenerateResult(text="", model="scripted-model", provider=self.name, raw=raw)


class RecordingReadFile(ReadFileTool):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.results: List[Dict[str, Any]] = []

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        result = super().execute(**arguments)
        self.results.append(result)
        return result


class RecordingSearch(SearchCodeTool):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.results: List[Dict[str, Any]] = []

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        result = super().execute(**arguments)
        self.results.append(result)
        return result


class RecordingList(ListFilesTool):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.results: List[Dict[str, Any]] = []

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        result = super().execute(**arguments)
        self.results.append(result)
        return result


def _write_big(path: Path, lines: int = 400) -> None:
    path.write_text(
        "\n".join(f"needle line {i} " + ("X" * 90) for i in range(1, lines + 1)),
        encoding="utf-8",
    )


def _enable_compression(monkeypatch: Any) -> None:
    import agent_ai.config.settings as settings

    monkeypatch.setattr(settings, "compression_enabled", lambda: True)


def _is_stub(result: Any) -> bool:
    return bool(
        isinstance(result, dict)
        and (
            result.get("already_available")
            or result.get("already_read")
            or result.get("already_searched")
            or result.get("already_listed")
        )
    )


def _run(tmp_path: Path, script: List[Dict[str, Any]], registry: ToolRegistry, budget: int):
    orchestrator = AgentOrchestrator(
        provider=ScriptedProvider(script),
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="scripted-model"),
        system_prompt="agent",  # besar -> memaksa jendela kerja sempit
        use_continuous_loop=True,
        context_budget_tokens=budget,
    )
    return orchestrator.run("inspeksi")


# --------------------------------------------------------------------------- #
# A. read_file: hasil dipadatkan (masih dikirim) -> dedup TETAP berlaku.
# --------------------------------------------------------------------------- #
def test_repeated_read_deduped_under_compaction(tmp_path: Path, monkeypatch) -> None:
    _enable_compression(monkeypatch)
    _write_big(tmp_path / "big.py", lines=400)

    cache = ToolReadCache()
    registry = ToolRegistry()
    registry.read_cache = cache
    reader = RecordingReadFile(root=tmp_path, read_cache=cache)
    registry.register(reader)

    # LLM meminta read IDENTIK 8 kali -> harus 1 physical read + 7 stub.
    script = [
        _tool_turn([_tool_call(f"r{i}", "read_file", {"path": "big.py"})])
        for i in range(8)
    ] + [_final_turn()]

    result = _run(tmp_path, script, registry, budget=4000)
    assert result.status == AgentStatus.DONE
    assert len(reader.results) == 8, reader.results
    full = [r for r in reader.results if "content" in r]
    stubs = [r for r in reader.results if _is_stub(r)]
    # Oscillation hilang: hanya SATU read fisik berisi, sisanya stub.
    assert len(full) == 1, f"read fisik berulang: {len(full)}"
    assert len(stubs) == 7, f"stub dedup hilang: {len(stubs)}"


# --------------------------------------------------------------------------- #
# B. search_code: hasil dipadatkan (masih dikirim) -> dedup TETAP berlaku.
# --------------------------------------------------------------------------- #
def test_repeated_search_deduped_under_compaction(tmp_path: Path, monkeypatch) -> None:
    _enable_compression(monkeypatch)
    _write_big(tmp_path / "big.py", lines=400)

    cache = ToolReadCache()
    registry = ToolRegistry()
    registry.read_cache = cache
    searcher = RecordingSearch(root=tmp_path, read_cache=cache)
    registry.register(searcher)

    script = [
        _tool_turn([_tool_call(f"s{i}", "search_code", {"query": "needle", "path": "big.py"})])
        for i in range(8)
    ] + [_final_turn()]

    result = _run(tmp_path, script, registry, budget=4000)
    assert result.status == AgentStatus.DONE
    assert len(searcher.results) == 8
    real = [r for r in searcher.results if "matches" in r]
    stubs = [r for r in searcher.results if _is_stub(r)]
    assert len(real) == 1, f"pencarian fisik berulang: {len(real)}"
    assert len(stubs) == 7, f"stub dedup search hilang: {len(stubs)}"


# --------------------------------------------------------------------------- #
# C. list_files: listing identik -> stub (dedup baru).
# --------------------------------------------------------------------------- #
def test_repeated_listing_deduped(tmp_path: Path, monkeypatch) -> None:
    _enable_compression(monkeypatch)
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b = 2\n", encoding="utf-8")

    cache = ToolReadCache()
    registry = ToolRegistry()
    registry.read_cache = cache
    lister = RecordingList(root=tmp_path, read_cache=cache)
    registry.register(lister)

    script = [
        _tool_turn([_tool_call(f"l{i}", "list_files", {"path": "."})])
        for i in range(6)
    ] + [_final_turn()]

    result = _run(tmp_path, script, registry, budget=4000)
    assert result.status == AgentStatus.DONE
    assert len(lister.results) == 6
    real = [r for r in lister.results if "entries" in r]
    stubs = [r for r in lister.results if r.get("already_listed")]
    assert len(real) == 1, f"listing fisik berulang: {len(real)}"
    assert len(stubs) == 5, f"stub dedup listing hilang: {len(stubs)}"


# --------------------------------------------------------------------------- #
# D. Eviction SESUNGGUHNYA tetap melepas dedup (isi dapat diambil ulang).
# --------------------------------------------------------------------------- #
def test_evicted_search_released_for_retry(tmp_path: Path) -> None:
    cache = ToolReadCache()
    registry = ToolRegistry()
    registry.read_cache = cache
    registry.register(SearchCodeTool(root=tmp_path, read_cache=cache))
    orchestrator = AgentOrchestrator(
        provider=ScriptedProvider([]),
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="scripted-model"),
        system_prompt="agent",
        use_continuous_loop=True,
    )
    content = json.dumps(
        {"query": "q", "path": ".", "count": 1, "truncated": False,
         "matches": [{"file": "a.py", "line": 1, "text": "q"}]}
    )
    history = ConversationHistory()
    history.append_system_message("s")
    history.append_user_message("u")
    history.append_tool_result("s1", "search_code", content)
    cache.record_search("q", ".", 0)

    # Dipadatkan tetapi MASIH dikirim -> klaim tetap (dedup tetap berlaku).
    compiled_kept = list(history.messages) + [
        ChatMessage(role="tool", content="[padat]", name="search_code", tool_call_id="s1")
    ]
    orchestrator._sync_retrieval_cache(history, compiled_kept)  # noqa: SLF001
    assert cache.claim_search("q", ".", 0) is True

    # Benar-benar DIBUANG -> klaim dilepas (dapat dicari ulang).
    compiled_evicted = [history.messages[0], history.messages[1]]
    orchestrator._sync_retrieval_cache(history, compiled_evicted)  # noqa: SLF001
    assert cache.claim_search("q", ".", 0) is False


def test_evicted_listing_released_for_retry(tmp_path: Path) -> None:
    cache = ToolReadCache()
    registry = ToolRegistry()
    registry.read_cache = cache
    registry.register(ListFilesTool(root=tmp_path, read_cache=cache))
    orchestrator = AgentOrchestrator(
        provider=ScriptedProvider([]),
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="scripted-model"),
        system_prompt="agent",
        use_continuous_loop=True,
    )
    content = json.dumps(
        {"path": ".", "max_entries": 200, "count": 1, "truncated": False,
         "entries": [{"name": "a.py", "type": "file", "size": 1}]}
    )
    history = ConversationHistory()
    history.append_system_message("s")
    history.append_user_message("u")
    history.append_tool_result("l1", "list_files", content)
    cache.claim_listing(".", 200)

    # Dipadatkan tetapi MASIH dikirim -> klaim tetap.
    compiled_kept = list(history.messages) + [
        ChatMessage(role="tool", content="[padat]", name="list_files", tool_call_id="l1")
    ]
    orchestrator._sync_retrieval_cache(history, compiled_kept)  # noqa: SLF001
    assert cache.claim_listing(".", 200) is True

    # Benar-benar DIBUANG -> klaim dilepas.
    compiled_evicted = [history.messages[0], history.messages[1]]
    orchestrator._sync_retrieval_cache(history, compiled_evicted)  # noqa: SLF001
    assert cache.claim_listing(".", 200) is False


# --------------------------------------------------------------------------- #
# E. Mutasi file membatalkan klaim listing (listing akurat kembali).
# --------------------------------------------------------------------------- #
def test_file_mutation_releases_listing_claim(tmp_path: Path) -> None:
    cache = ToolReadCache()
    lister = ListFilesTool(root=tmp_path, read_cache=cache)
    writer = WriteFileTool(root=tmp_path, read_cache=cache)
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")

    first = lister.execute(path=".")
    assert "entries" in first
    assert lister.execute(path=".").get("already_listed") is True

    # Mutasi file -> listing berikutnya HARUS dijalankan ulang (isi berubah).
    writer.execute(path="b.py", content="b = 2\n")
    refreshed = lister.execute(path=".")
    assert "entries" in refreshed
    names = {e["name"] for e in refreshed["entries"]}
    assert "b.py" in names


# --------------------------------------------------------------------------- #
# F. Backward compatible: tanpa cache (read_cache=None) tidak ada dedup.
# --------------------------------------------------------------------------- #
def test_no_cache_no_listing_dedup(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    lister = ListFilesTool(root=tmp_path)  # read_cache=None
    assert "entries" in lister.execute(path=".")
    assert "already_listed" not in lister.execute(path=".")
