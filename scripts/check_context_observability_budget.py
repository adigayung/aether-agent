"""Verifier Observability & Context Budget (S0 + S1).

Memverifikasi dua fondasi yang menjadi PRASYARAT perbaikan repeated `read_file`:

S0 — Observability:
    A. Metrik context TIDAK lagi ter-redact (rename key + pengecualian metrik).
    B. Secret nyata TETAP ter-redact (API key, Authorization, token kredensial).
    C. `observation_received` tidak lagi dipotong pada 2.000 karakter, tetapi
       tetap BOUNDED (bukan penghapusan limit global).
    D. Event `retrieval_repeat` muncul saat retrieval resource yang sama
       benar-benar berulang — TANPA mengubah perilaku retrieval/dedup/loop.

S1 — Context Budget:
    E. Provider yang TIDAK melaporkan capability -> anggaran config (perilaku
       lama dipertahankan; tidak ada perubahan diam-diam).
    F. Provider yang MELAPORKAN context window -> anggaran = window - reserve,
       dan anggaran selalu <= kemampuan provider.
    G. Ollama tetap konsisten: anggaran <= `num_ctx` runtime.
    H. Anggaran KONTEKS PENGETAHUAN (Bible) terpisah dari anggaran PERCAKAPAN
       dan tidak dapat menghabiskan seluruh anggaran.

Jalankan:
    python scripts/check_context_observability_budget.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.config.settings import OllamaConfig, settings  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.observability import (  # noqa: E402
    _MAX_STRING_LEN,
    _OBSERVATION_MAX_STRING_LEN,
    sanitize_event_payload,
    sanitize_payload,
)
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.ollama import OllamaProvider  # noqa: E402
from agent_ai.providers.openai_compatible import (  # noqa: E402
    OpenAICompatibleProvider,
)
from agent_ai.tools.base import BaseTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402


# --------------------------------------------------------------------------- #
# Provider & tool palsu (tanpa network / filesystem)
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


def _final_turn(text: str) -> Dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class ScriptedProvider(OpenAICompatibleProvider):
    """Provider palsu: respons skrip berurutan (tanpa network)."""

    name = "scripted"

    def __init__(
        self, script: List[Dict[str, Any]], context_window: int = 0
    ) -> None:
        self.config = SimpleNamespace(
            model="scripted-model", context_window=context_window
        )
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
        return GenerateResult(
            text="", model="scripted-model", provider=self.name, raw=raw
        )


class FakeReadTool(BaseTool):
    """read_file palsu: meniru stub `already_available` pada pembacaan ulang."""

    name = "read_file"
    description = "Baca file (palsu untuk verifier)."
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def __init__(self) -> None:
        self.seen: Dict[tuple, int] = {}

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        key = (
            arguments.get("path"),
            arguments.get("start_line"),
            arguments.get("end_line"),
            bool(arguments.get("force")),
        )
        self.seen[key] = self.seen.get(key, 0) + 1
        if self.seen[key] > 1 and not arguments.get("force"):
            return {
                "path": arguments.get("path"),
                "already_read": True,
                "already_available": True,
                "cache_hit": True,
                "message": "ALREADY_AVAILABLE (verifier)",
            }
        return {
            "path": arguments.get("path"),
            "total_lines": 20,
            "content": "line content\n" * 20,
        }


# --------------------------------------------------------------------------- #
# A + B. Redaksi
# --------------------------------------------------------------------------- #
def check_redaction() -> None:
    metrics = {
        "context_budget": 16000,
        "context_budget_source": "config",
        "context_overhead": 2720,
        "context_before": 23813,
        "context_after": 12885,
        "context_budget_tokens": 16000,
        "knowledge_budget_tokens": 8000,
        "input_tokens": 1200,
        "output_tokens": 400,
        "context_window": 64000,
    }
    secrets = {
        "DEEPSEEK_API_KEY": "sk-not-a-real-key",
        "Authorization": "Bearer not-a-real-token",
        "api_key": "not-a-real-key",
        "refresh_token": "not-a-real-token",
        "password": "not-a-real-password",
        "session_key": "not-a-real-session",
    }
    payload: Dict[str, Any] = {**metrics, **secrets, "nested": {"token": "x"}}
    clean = sanitize_payload(payload)

    for key, value in metrics.items():
        assert clean[key] == value, f"metrik {key} tidak terbaca: {clean[key]!r}"
    for key in secrets:
        assert clean[key] == "[redacted]", f"secret {key} TIDAK ter-redact"
    assert clean["nested"]["token"] == "[redacted]", "token kredensial nested bocor"
    print("[A/B] metrik context terbaca & secret tetap ter-redact OK")

    # Event provider_request nyata harus membawa angka (bukan [redacted]).
    event = sanitize_event_payload(
        "provider_request",
        {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "iteration": 4,
            "context_budget": 16000,
            "context_budget_source": "config",
            "context_overhead": 2720,
            "context_before": 23813,
            "context_after": 12885,
            "context_compacted": True,
        },
    )
    assert event["context_budget"] == 16000, event
    assert event["context_before"] == 23813, event
    assert event["context_compacted"] is True, event
    print("[A] event provider_request membawa angka context OK")


# --------------------------------------------------------------------------- #
# C. Truncation per event
# --------------------------------------------------------------------------- #
def check_observation_truncation() -> None:
    big = "y" * 12000
    obs = sanitize_event_payload(
        "observation_received",
        {"tool": "read_file", "success": True, "content": {"content": big}},
    )
    assert len(obs["content"]["content"]) == 12000, (
        "observation_received masih kehilangan payload pada 12.000 karakter"
    )

    # Event lain tetap BOUNDED seperti sebelumnya (limit tidak dihapus global).
    other = sanitize_event_payload("tool_called", {"arguments": {"path": big}})
    assert "[truncated]" in other["arguments"]["path"], "limit default hilang"

    # Payload sangat besar tetap dibatasi (bounded, bukan tanpa batas).
    huge = sanitize_event_payload("observation_received", {"content": "z" * 60000})
    assert "[truncated]" in huge["content"], "payload raksasa tidak dibatasi"
    assert len(huge["content"]) <= _OBSERVATION_MAX_STRING_LEN + 64, len(huge["content"])
    print(
        f"[C] observation_received utuh s/d {_OBSERVATION_MAX_STRING_LEN} char, "
        f"payload lain tetap dibatasi {_MAX_STRING_LEN} char OK"
    )

    # Task Log nyata (jalur B1) harus menyimpan payload secara utuh.
    from agent_ai.projects.aether_store import TaskLog

    with tempfile.TemporaryDirectory() as tmp:
        log = TaskLog(tmp, task_id="verifier-log-1")
        log.append(
            "observation_received",
            {"tool": "read_file", "content": {"content": big}},
        )
        log.append("tool_called", {"tool": "read_file", "arguments": {"path": big}})
        lines = [
            json.loads(line)
            for line in Path(log.path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    obs_record = next(r for r in lines if r["event"] == "observation_received")
    call_record = next(r for r in lines if r["event"] == "tool_called")
    stored = obs_record["data"]["content"]["content"]
    assert len(stored) == 12000, f"Task Log masih memotong payload: {len(stored)}"
    assert "[truncated]" in call_record["data"]["arguments"]["path"], (
        "Task Log tidak lagi membatasi payload event lain"
    )
    print("[C] Task Log menyimpan observation_received utuh (12.000 char) OK")


# --------------------------------------------------------------------------- #
# D. retrieval_repeat
# --------------------------------------------------------------------------- #
def check_retrieval_repeat() -> None:
    tool = FakeReadTool()
    registry = ToolRegistry()
    registry.register(tool)
    script = [
        _tool_turn(
            [
                _tool_call("c1", "read_file", {"path": "a.py"}),
                _tool_call("c2", "read_file", {"path": "a.py"}),
            ]
        ),
        _tool_turn(
            [
                _tool_call("c3", "read_file", {"path": "b.py", "start_line": 1, "end_line": 10}),
                _tool_call(
                    "c4",
                    "read_file",
                    {"path": "b.py", "start_line": 1, "end_line": 10, "force": True},
                ),
            ]
        ),
        _final_turn("selesai"),
    ]
    events: List[Dict[str, Any]] = []
    orchestrator = AgentOrchestrator(
        provider=ScriptedProvider(script),
        executor=ToolExecutor(registry=registry),
        options=GenerateOptions(model="scripted-model"),
        system_prompt="verifier",
        use_continuous_loop=True,
        event_sink=lambda et, payload: events.append({"type": et, **payload}),
    )
    orchestrator.run("uji retrieval_repeat")

    repeats = [e for e in events if e["type"] == "retrieval_repeat"]
    assert len(repeats) == 1, f"retrieval_repeat tidak sesuai: {repeats}"
    repeat = repeats[0]
    for field in ("path", "range", "mode", "force", "stub", "repeat_count"):
        assert field in repeat, f"field wajib hilang: {field}"
    assert repeat["path"] == "a.py", repeat
    assert repeat["range"] == "full", repeat
    assert repeat["mode"] == "default", repeat
    assert repeat["force"] is False, repeat
    assert repeat["stub"] is True, "stub tidak terdeteksi"
    assert repeat["repeat_count"] == 2, repeat
    assert "a.py" not in repeat["path"] or repeat["tool"] == "read_file", repeat
    print("[D] retrieval_repeat muncul dengan field lengkap (repeat_count=2) OK")

    # force=true punya identitas BERBEDA -> bukan repeat dari pembacaan normal.
    forced = [
        e
        for e in events
        if e["type"] == "retrieval_repeat" and e["path"] == "b.py"
    ]
    assert forced == [], f"force=true disalahartikan sebagai repeat: {forced}"
    print("[D] identitas repeat memisahkan force=true dari pembacaan normal OK")


# --------------------------------------------------------------------------- #
# E + F + G + H. Context budget
# --------------------------------------------------------------------------- #
def _make_orchestrator(provider: Any) -> AgentOrchestrator:
    return AgentOrchestrator(
        provider=provider,
        options=GenerateOptions(model="scripted-model"),
        use_continuous_loop=True,
    )


def check_context_budget_flow() -> None:
    configured = int(settings.context.max_tokens)
    knowledge_cap = int(settings.context.knowledge_max_tokens)

    rows: List[tuple] = []

    # E. Provider tanpa capability -> anggaran config (perilaku lama).
    plain = _make_orchestrator(ScriptedProvider([], context_window=0))
    budget, source = plain._context_budget_decision()
    assert source == "config", source
    assert budget == configured, (budget, configured)
    rows.append(("scripted", "scripted-model", "unknown", configured, budget, source))

    # F. Provider melaporkan context window -> anggaran = window - reserve.
    reporting = _make_orchestrator(ScriptedProvider([], context_window=64000))
    budget2, source2 = reporting._context_budget_decision()
    assert source2 == "provider", source2
    assert budget2 == 64000 - 4096, budget2
    assert budget2 <= 64000, "anggaran melebihi kemampuan provider"
    rows.append(("scripted", "scripted-model", 64000, configured, budget2, source2))

    # Window terlalu kecil untuk reserve -> aman (None, kembali ke config).
    tiny = _make_orchestrator(ScriptedProvider([], context_window=2048))
    budget3, source3 = tiny._context_budget_decision()
    assert budget3 == configured and source3 == "config", (budget3, source3)
    rows.append(("scripted", "scripted-model", 2048, configured, budget3, source3))

    # G. Ollama: anggaran <= num_ctx runtime.
    ollama = OllamaProvider(config=OllamaConfig(host="http://localhost:11434", model="m", num_ctx=32768))
    reported = ollama.knowledge_budget_tokens()
    assert reported is not None and reported <= 32768, reported
    o = _make_orchestrator(ollama)
    ob, osrc = o._context_budget_decision()
    assert osrc == "provider" and ob == reported, (ob, osrc, reported)
    assert ob <= 32768, "anggaran Ollama melebihi num_ctx"
    rows.append(("ollama", "m", 32768, configured, ob, osrc))

    # H. Anggaran konteks pengetahuan (Bible): terpisah, dibatasi batas ABSOLUT
    #    dan PORSI anggaran percakapan. Porsi inilah yang menjamin blok
    #    pengetahuan STATIS tidak menggerus jendela kerja Agent (penyebab
    #    repeated `read_file`: jendela kerja menyusut lalu seluruh isi baru
    #    dipadatkan setiap round).
    share = float(getattr(settings.context, "knowledge_share", 0.0) or 0.0)
    assert share > 0, "porsi konteks pengetahuan harus aktif secara default"

    def expected_knowledge(orchestrator) -> int:
        conversation, _ = orchestrator._context_budget_decision()
        caps = [knowledge_cap, int(int(conversation) * share)]
        return min(caps)

    kb_plain = plain._knowledge_context_budget_tokens()
    assert kb_plain == expected_knowledge(plain), (kb_plain, knowledge_cap, share)
    kb_reporting = reporting._knowledge_context_budget_tokens()
    assert kb_reporting == expected_knowledge(reporting), kb_reporting
    kb_ollama = o._knowledge_context_budget_tokens()
    assert kb_ollama == expected_knowledge(o), (kb_ollama, reported)
    for kb in (kb_plain, kb_reporting, kb_ollama):
        assert kb is not None and kb < 64000, "Bible dapat menghabiskan seluruh anggaran"
    # Jendela kerja SELALU tersisa untuk percakapan.
    for orchestrator in (plain, reporting, o):
        conversation, _ = orchestrator._context_budget_decision()
        knowledge = orchestrator._knowledge_context_budget_tokens()
        assert conversation > knowledge, (conversation, knowledge)
    print(
        "[H] anggaran Bible terpisah + dibatasi porsi anggaran percakapan "
        f"(cap={knowledge_cap} token, share={share}) OK"
    )

    print("\n  provider   model            capability  configured  effective  source")
    for provider, model, capability, conf, eff, src in rows:
        print(
            f"  {provider:<10} {model:<16} {str(capability):<11} "
            f"{conf:<11} {eff:<10} {src}"
        )
    print("[E/F/G] aliran context budget konsisten & <= kemampuan provider OK")


# --------------------------------------------------------------------------- #
def _run() -> int:
    print("=" * 78)
    print("VERIFIER S0 (observability) + S1 (context budget)")
    print("=" * 78)
    check_redaction()
    check_observation_truncation()
    check_retrieval_repeat()
    check_context_budget_flow()
    print("-" * 78)
    print("[OK] Observability & context budget bekerja (redaksi, truncation,")
    print("     retrieval_repeat, anggaran provider-aware).")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run())
