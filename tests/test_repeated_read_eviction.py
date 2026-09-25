"""Regression tests: repeated `read_file` akibat EVICTION isi konteks.

Root cause yang dijaga test ini (dari runtime evidence task nyata: audit Agent
Mode pada `TaskComposer.vue` — file yang sama dibaca 30x, 0 stub dedup):

    head konteks (system + Project Bible + task) + definisi tool menghabiskan
    sebagian besar anggaran pesan -> jendela kerja sangat kecil -> setiap round
    `compile_compacted_messages` memadatkan SELURUH jendela terbaru (Tahap 5/5b)
    -> isi file yang BARU dibaca hilang dari konteks -> LLM membaca ulang file
    yang sama -> riwayat tumbuh tanpa batas -> compaction lagi (loop).

Properti yang dikunci di sini:

    A. Porsi konteks pengetahuan (Bible) dibatasi RELATIF terhadap anggaran
       percakapan, bukan hanya batas absolut, sehingga jendela kerja tidak
       tergerus oleh blok pengetahuan statis.
    B. Isi BARU (hasil tool terbaru) TIDAK dikorbankan selama bagian LAMA masih
       dapat diperkecil -> urutan pengorbanan: lama-dulu, isi-baru-terakhir.
    C. Kontrak dedup context-aware tidak berubah: stub `already_available`
       HANYA sah bila isinya masih terlihat LLM; isi yang benar-benar hilang
       dari konteks HARUS dapat dibaca ulang secara penuh (tanpa stub palsu).
    D. `force=true` tetap memaksa kirim ulang.

Semua deterministik, tanpa network/LLM asli.

Jalankan:
    python -m pytest tests/test_repeated_read_eviction.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.history import ConversationHistory  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.core.types import ToolCall  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
)
from agent_ai.tools.registry import build_registry  # noqa: E402

SYSTEM_PROMPT = "Kamu adalah coding agent."
TASK = "Audit fitur Agent Mode."


# --------------------------------------------------------------------------- #
# Helper
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
    """Provider palsu: respons skrip berurutan (tanpa network)."""

    name = "scripted"

    def __init__(self, script: List[Dict[str, Any]]) -> None:
        from types import SimpleNamespace

        self.config = SimpleNamespace(model="scripted-model", context_window=0)
        self.script = list(script)
        self.calls = 0

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        idx = self.calls
        self.calls += 1
        raw = self.script[idx] if idx < len(self.script) else _final_turn()
        return GenerateResult(text="", model="scripted-model", provider=self.name, raw=raw)


def _history_with_tools(turns: int, size: int, head_chars: int = 0) -> ConversationHistory:
    """Riwayat: system + task + `turns` pasangan assistant/tool hasil read_file."""
    history = ConversationHistory()
    history.append_system_message(SYSTEM_PROMPT)
    history.append_user_message(TASK)
    if head_chars:
        history.append_system_message("BIBLE:" + ("K" * head_chars))
    for index in range(turns):
        call = ToolCall.create("read_file", {"path": f"f{index}.py"}, id=f"call-{index}")
        history.append_assistant_message(content=None, tool_calls=[call])
        history.append_tool_result(call.id, "read_file", "X" * size)
    return history


def _write_source(path: Path, lines: int = 400) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        f"def fungsi_{i}():  # baris sumber yang panjang agar tidak muat anggaran"
        for i in range(lines)
    )
    path.write_text(body + "\n", encoding="utf-8")
    return path


def _run_reads(tmp_path: Path, budget: Optional[int], calls: List[Dict[str, Any]]):
    """Jalankan orchestrator nyata dengan registry + dedup asli; balikan event."""
    registry = build_registry(root=tmp_path)
    script = [_tool_turn([c]) for c in calls] + [_final_turn()]
    events: List[Dict[str, Any]] = []
    orchestrator = AgentOrchestrator(
        provider=ScriptedProvider(script),
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="scripted-model"),
        system_prompt=SYSTEM_PROMPT,
        context_budget_tokens=budget,
        use_continuous_loop=True,
        event_sink=lambda et, payload: events.append({"type": et, **payload}),
    )
    orchestrator.run(TASK)
    return events, orchestrator


def _reads(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        e
        for e in events
        if e["type"] == "observation_received" and e.get("tool") == "read_file"
    ]


def _is_stub(content: Any) -> bool:
    return bool(
        isinstance(content, dict)
        and (content.get("already_available") or content.get("already_read"))
    )


# --------------------------------------------------------------------------- #
# A. Porsi konteks pengetahuan dibatasi relatif terhadap anggaran
# --------------------------------------------------------------------------- #
def test_a_knowledge_context_bounded_by_share() -> None:
    """Bible tidak boleh menghabiskan seluruh anggaran percakapan."""
    from agent_ai.config.settings import settings

    share = float(settings.context.knowledge_share)
    assert share > 0, "porsi konteks pengetahuan harus aktif secara default"

    orchestrator = AgentOrchestrator(
        provider=ScriptedProvider([]),
        options=GenerateOptions(model="scripted-model"),
        use_continuous_loop=True,
    )
    conversation, _ = orchestrator._context_budget_decision()
    knowledge = orchestrator._knowledge_context_budget_tokens()
    assert conversation and knowledge
    assert knowledge <= int(conversation * share), (knowledge, conversation, share)
    assert knowledge <= int(settings.context.knowledge_max_tokens)
    # Jendela kerja tetap ada (bukan nol).
    assert conversation - knowledge > 0

    # share=0 -> kembali ke perilaku lama (hanya batas absolut).
    original = settings.context.knowledge_share
    object.__setattr__(settings.context, "knowledge_share", 0.0)
    try:
        assert orchestrator._knowledge_context_budget_tokens() == int(
            settings.context.knowledge_max_tokens
        )
    finally:
        object.__setattr__(settings.context, "knowledge_share", original)


# --------------------------------------------------------------------------- #
# B. Isi BARU tidak dikorbankan selama bagian LAMA masih bisa diperkecil
# --------------------------------------------------------------------------- #
def test_b_fresh_tool_results_survive_compaction() -> None:
    """Regresi inti: jendela terbaru yang MUAT tidak boleh dipadatkan."""
    # 6 turn lama (besar) + 2 turn terbaru (kecil). Total melebihi anggaran,
    # tetapi dua pesan TERBARU sendirian muat.
    history = _history_with_tools(turns=6, size=6000)
    call = ToolCall.create("read_file", {"path": "fresh1.py"}, id="fresh-1")
    history.append_assistant_message(content=None, tool_calls=[call])
    history.append_tool_result(call.id, "read_file", "F" * 1200)
    call2 = ToolCall.create("read_file", {"path": "fresh2.py"}, id="fresh-2")
    history.append_assistant_message(content=None, tool_calls=[call2])
    history.append_tool_result(call2.id, "read_file", "G" * 1200)

    stats: Dict[str, int] = {}
    compiled = history.compile_compacted_messages(
        4000, overhead_tokens=0, stats=stats
    )
    by_id = {
        m.tool_call_id: (m.content or "")
        for m in compiled
        if getattr(m, "role", None) == "tool"
    }
    # Isi BARU tetap utuh.
    assert "F" * 1200 in by_id["fresh-1"], "hasil tool terbaru dipadatkan"
    assert "G" * 1200 in by_id["fresh-2"], "hasil tool terbaru dipadatkan"
    assert stats["context_recent_compacted"] == 0, stats
    # Bagian LAMA yang dikorbankan (diperkecil), bukan dibuang.
    assert stats["context_old_recompacted"] + stats["context_old_minimal"] >= 1, stats
    assert stats["context_dropped"] == 0, "pesan tidak boleh dibuang di tahap ini"
    # Protokol tetap valid: jumlah pesan tidak berubah.
    assert len(compiled) == len(history.messages)


def test_b2_old_part_shrunk_to_minimal_when_still_over() -> None:
    """Bagian lama boleh menyusut sampai bentuk minimal, pesan tetap ada."""
    history = _history_with_tools(turns=12, size=6000)
    stats: Dict[str, int] = {}
    compiled = history.compile_compacted_messages(3000, overhead_tokens=0, stats=stats)
    assert len(compiled) == len(history.messages)
    # Pesan terakhir (hasil tool terbaru) tidak pernah dibuang.
    assert compiled[-1].tool_call_id == history.messages[-1].tool_call_id
    assert stats["context_old_recompacted"] >= 1, stats


def test_b3_compacted_is_never_dropped_before_shrinking() -> None:
    """Kontrak lama: memadatkan LEBIH DULU, membuang hanya upaya terakhir."""
    history = _history_with_tools(turns=8, size=6000)
    stats: Dict[str, int] = {}
    history.compile_compacted_messages(5000, overhead_tokens=0, stats=stats)
    assert stats["context_dropped"] == 0, stats
    assert stats["context_old_recompacted"] >= 1, stats


# --------------------------------------------------------------------------- #
# C. Dedup context-aware (runtime nyata)
# --------------------------------------------------------------------------- #
def test_c_same_read_deduped_while_content_visible(tmp_path: Path) -> None:
    """Read identik kedua -> stub, karena isinya MASIH terlihat LLM."""
    _write_source(tmp_path / "a.py", lines=20)
    calls = [
        _tool_call("c1", "read_file", {"path": "a.py"}),
        _tool_call("c2", "read_file", {"path": "a.py"}),
    ]
    events, _ = _run_reads(tmp_path, budget=100_000, calls=calls)
    reads = _reads(events)
    assert len(reads) == 2, reads
    assert not _is_stub(reads[0]["content"]), "read pertama harus isi penuh"
    assert _is_stub(reads[1]["content"]), "read identik kedua harus stub dedup"


def test_c2_force_bypasses_dedup(tmp_path: Path) -> None:
    """force=true tetap memaksa kirim ulang isi."""
    _write_source(tmp_path / "a.py", lines=20)
    calls = [
        _tool_call("c1", "read_file", {"path": "a.py"}),
        _tool_call("c2", "read_file", {"path": "a.py", "force": True}),
    ]
    events, _ = _run_reads(tmp_path, budget=100_000, calls=calls)
    reads = _reads(events)
    assert not _is_stub(reads[1]["content"]), "force=true harus mengirim isi ulang"


def test_c3_evicted_content_can_be_read_again(tmp_path: Path) -> None:
    """Isi yang benar-benar hilang dari konteks TIDAK boleh di-stub.

    Ini yang membuat Agent tidak terjebak: bila compaction benar-benar membuang
    isi dari konteks, permintaan berikutnya HARUS mengirim isi penuh (bukan
    stub palsu yang membuat Agent tidak pernah memperoleh datanya).
    """
    _write_source(tmp_path / "big.py", lines=400)
    calls = [
        _tool_call("c1", "read_file", {"path": "big.py"}),
        _tool_call("c2", "read_file", {"path": "big.py"}),
    ]
    # Anggaran kecil (tetapi kepala masih MUAT, sehingga Tahap 5/5b aktif) ->
    # hasil read pertama tidak muat jendela -> dipadatkan -> isinya hilang dari
    # konteks. Jalur `head_oversized` TIDAK dipakai di sini karena jalur itu
    # justru melindungi pesan terakhir (perilaku lama yang sudah benar).
    events, _ = _run_reads(tmp_path, budget=4000, calls=calls)
    reads = _reads(events)
    assert len(reads) == 2, reads
    second = reads[1]["content"]
    assert not _is_stub(second), "isi yang sudah tidak terlihat tidak boleh di-stub"
    assert isinstance(second, dict) and second.get("content"), (
        "isi penuh harus dikirim ulang agar Agent dapat melanjutkan"
    )


def test_c4_eviction_is_observable(tmp_path: Path) -> None:
    """Eviction terlihat di telemetry: span terlihat vs tersembunyi."""
    _write_source(tmp_path / "big.py", lines=400)
    calls = [
        _tool_call("c1", "read_file", {"path": "big.py"}),
        _tool_call("c2", "read_file", {"path": "big.py"}),
    ]
    events, _ = _run_reads(tmp_path, budget=4000, calls=calls)
    requests = [e for e in events if e["type"] == "provider_request"]
    assert requests, "provider_request harus tercatat"
    last = requests[-1]
    for key in (
        "context_head",
        "context_window",
        "context_recent_compacted",
        "context_spans_visible",
        "context_spans_hidden",
    ):
        assert key in last, f"telemetry {key} hilang"
    assert last["context_head"] >= 0
