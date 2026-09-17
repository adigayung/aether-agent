"""Verifikasi penanganan tool-call yang TERPOTONG (finish_reason=length).

Bug yang diverifikasi (ditemukan saat live test landing page):
    DeepSeek menghasilkan beberapa write_file besar dalam SATU response sampai
    completion_tokens mencapai batas -> finish_reason="length" -> argumen
    tool-call terpotong -> JSON invalid -> ProviderResponseError -> task FAILED
    sebelum file dibuat.

Membuktikan:
    1. write_file normal kecil tetap berhasil.
    2. beberapa write_file normal (valid) tetap berhasil (tidak ada yang hilang).
    3. payload write_file BESAR tetapi valid -> isi tidak hilang.
    4. finish_reason=length + tool-call JSON terpotong -> tool-call tak lengkap
       DIBUANG (tidak ada file parsial/corrupt), tidak menebak/merapikan JSON.
    5. kondisi terpotong bersifat recoverable/observable: agent MELANJUTKAN
       (bukan exception parsing yang mematikan task, bukan DONE kosong).
    6. JSON invalid yang BUKAN truncation tetap ProviderResponseError.
    7. tool lain (read_file/list_files) tetap bekerja.
    8. write_file atomic: kegagalan di tengah tidak meninggalkan file parsial.

Fixture hanya di J:\\Agent_Ai\\dummy_test dan dibersihkan setelah selesai.

Jalankan:
    python scripts/check_truncated_toolcall.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.core.response import FinishReason  # noqa: E402
from agent_ai.providers.base import (  # noqa: E402
    BaseProvider,
    GenerateOptions,
    GenerateResult,
    Message,
    ProviderResponseError,
    ToolChoice,
    ToolDefinition,
)
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.tools.base import ToolError  # noqa: E402
from agent_ai.tools.filesystem import ListFilesTool, ReadFileTool  # noqa: E402
from agent_ai.tools.registry import ToolRegistry  # noqa: E402
from agent_ai.tools.workspace import WriteFileTool  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "truncated_toolcall_fixture"

_TRUNCATED = '{"path": "styles.css", "content": "body { color: red'  # JSON terpotong


# ---------------------------------------------------------------------------
# Provider double (jalur normalisasi OpenAI-compatible yang SAMA dengan nyata).
# ---------------------------------------------------------------------------
class ScriptedProvider(BaseProvider):
    """Mengembalikan sequence raw response OpenAI-compatible yang ditentukan."""

    name = "scripted"

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self._responses = list(responses)
        self._index = 0
        self.calls = 0

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[List[Message]] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[List[ToolDefinition]] = None,
        tool_choice: Optional[ToolChoice] = None,
    ) -> GenerateResult:
        self.calls += 1
        if self._index >= len(self._responses):
            return GenerateResult(
                text="(habis)", provider=self.name, model="scripted",
                raw={"choices": [{"message": {"content": "(habis)"}, "finish_reason": "stop"}]},
            )
        raw = self._responses[self._index]
        self._index += 1
        return GenerateResult(text="", provider=self.name, model="scripted", raw=raw)

    def normalize_response(self, result: GenerateResult):
        return OpenAICompatibleProvider.normalize_response(self, result)


def _tool_call(name: str, arguments: str, call_id: str = "c1") -> Dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _response(tool_calls: List[Dict[str, Any]], finish_reason: str = "tool_calls") -> Dict[str, Any]:
    return {
        "choices": [
            {"message": {"content": "", "tool_calls": tool_calls}, "finish_reason": finish_reason}
        ]
    }


def _final(text: str) -> Dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


def _collect_sink(events: List[Dict[str, Any]]):
    def sink(event_type: str, payload: Dict[str, Any]) -> None:
        events.append({"type": event_type, "payload": payload})
    return sink


def _new_executor() -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(WriteFileTool(root=FIXTURE))
    registry.register(ReadFileTool(root=FIXTURE))
    registry.register(ListFilesTool(root=FIXTURE))
    return ToolExecutor(registry=registry)


def _prove(condition: bool, label: str) -> None:
    assert condition, f"GAGAL: {label}"
    print(f"  OK  {label}")


def main() -> int:
    print("=== Verifikasi Tool-Call Terpotong (finish_reason=length) ===")
    FIXTURE.mkdir(parents=True, exist_ok=True)
    try:
        return _run()
    finally:
        shutil.rmtree(FIXTURE, ignore_errors=True)


def _run() -> int:
    provider_obj = OpenAICompatibleProvider()

    # --- 1) write_file normal kecil tetap berhasil -------------------------
    print("\n[1] write_file normal kecil")
    write = WriteFileTool(root=FIXTURE)
    res = write.execute(path="small.txt", content="HALO")
    _prove(res.get("written") is True, "write_file kecil 'written' True")
    _prove((FIXTURE / "small.txt").read_text(encoding="utf-8") == "HALO", "isi kecil persis")

    # --- 2) beberapa write_file normal (valid) di satu response ------------
    print("\n[2] beberapa write_file normal dalam satu response")
    raw_multi = _response([
        _tool_call("write_file", '{"path": "a.html", "content": "<h1>A</h1>"}', "c1"),
        _tool_call("write_file", '{"path": "b.css", "content": "body{}"}', "c2"),
    ])
    resp_multi = provider_obj.normalize_response(
        GenerateResult(text="", model="m", raw=raw_multi)
    )
    _prove(len(resp_multi.tool_calls()) == 2, "dua tool call lengkap ter-normalisasi")
    _prove(resp_multi.truncated is False, "response normal tidak ditandai truncated")

    # --- 3) payload write_file BESAR tetapi valid -> isi tidak hilang ------
    print("\n[3] payload write_file besar tetapi valid")
    big = "A" * 200000 + "\nakhir\n"
    raw_big = _response([
        _tool_call("write_file", json.dumps({"path": "big.txt", "content": big}), "c1"),
    ])
    resp_big = provider_obj.normalize_response(
        GenerateResult(text="", model="m", raw=raw_big)
    )
    _prove(resp_big.tool_calls()[0].arguments["content"] == big, "isi besar utuh (200k char)")
    _prove(resp_big.truncated is False, "payload besar valid tidak ditandai truncated")
    big_tool = WriteFileTool(root=FIXTURE)
    big_tool.execute(**resp_big.tool_calls()[0].arguments)
    _prove((FIXTURE / "big.txt").read_text(encoding="utf-8") == big, "isi besar tersimpan persis")

    # --- 4) finish_reason=length + JSON terpotong -> dibuang, bukan crash --
    print("\n[4] response terpotong (length) + JSON argumen terpotong")
    raw_trunc = _response(
        [
            _tool_call("write_file", '{"path": "index.html", "content": "<h1>OK</h1>"}', "c1"),
            _tool_call("write_file", _TRUNCATED, "c2"),
        ],
        finish_reason="length",
    )
    resp_trunc = provider_obj.normalize_response(
        GenerateResult(text="", model="m", raw=raw_trunc)
    )
    _prove(resp_trunc.truncated is True, "ditandai truncated")
    _prove(resp_trunc.incomplete_tool_calls == 1, "1 tool call tak lengkap dibuang")
    _prove([a.name for a in resp_trunc.tool_calls()] == ["write_file"], "tool call lengkap dipertahankan")
    _prove(
        resp_trunc.tool_calls()[0].arguments["path"] == "index.html",
        "tool call lengkap tidak rusak",
    )

    # --- 5) agent MELANJUTKAN (recoverable), bukan FAILED/parse-exception --
    print("\n[5] orchestrator: response terpotong -> agent melanjutkan")
    events: List[Dict[str, Any]] = []
    provider = ScriptedProvider([
        # Turn 1: index.html lengkap + styles.css TERPOTONG (finish_reason=length).
        raw_trunc,
        # Turn 2: model menulis file sisanya secara lengkap (satu per respons).
        _response([_tool_call("write_file", '{"path": "styles.css", "content": "body { color: red }"}', "c3")]),
        _final("Selesai: index.html dan styles.css dibuat."),
    ])
    orch = AgentOrchestrator(
        provider=provider,
        executor=_new_executor(),
        max_iterations=6,
        event_sink=_collect_sink(events),
    )
    result = orch.run("Buat file index.html dan styles.css untuk halaman.")

    _prove(result.success, "loop berakhir sukses (bukan parse-exception/FAILED)")
    _prove(result.error is None, "tidak ada error provider")
    _prove((FIXTURE / "index.html").read_text(encoding="utf-8") == "<h1>OK</h1>",
           "tool call LENGKAP tetap dieksekusi (index.html ditulis)")
    _prove((FIXTURE / "styles.css").exists(), "file lanjutan (styles.css) dibuat pada turn berikutnya")
    _prove((FIXTURE / "styles.css").read_text(encoding="utf-8") == "body { color: red }",
           "styles.css berisi konten LENGKAP (bukan versi terpotong)")
    _prove(any(e["type"] == "provider_response_truncated" for e in events),
           "event provider_response_truncated diemit (observable)")
    truncated_events = [e for e in events if e["type"] == "provider_response_truncated"]
    _prove(truncated_events[0]["payload"].get("incomplete_tool_calls") == 1,
           "event mencatat jumlah tool call tak lengkap")

    # Invariant: TIDAK pernah ada file parsial/corrupt & TIDAK ada temp tertinggal.
    _prove(
        (FIXTURE / "styles.css").read_text(encoding="utf-8") != "body { color: red",
        "styles.css bukan versi terpotong (invariant)",
    )
    leftovers = [p.name for p in FIXTURE.rglob("*") if p.name.startswith(".aether_tmp_")]
    _prove(not leftovers, "tidak ada temp file tertinggal")

    # --- 6) truncated tanpa tool call lengkap -> tetap lanjut, bukan DONE kosong
    print("\n[6] terpotong tanpa tool call lengkap -> lanjut (bukan DONE kosong)")
    shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir(parents=True, exist_ok=True)
    provider2 = ScriptedProvider([
        _response([_tool_call("write_file", '{"path": "only.txt", "content": "isi terpoto', "c1")], "length"),
        _response([_tool_call("write_file", '{"path": "only.txt", "content": "isi lengkap"}', "c2")]),
        _final("Selesai."),
    ])
    orch2 = AgentOrchestrator(provider=provider2, executor=_new_executor(), max_iterations=6)
    result2 = orch2.run("Buat file only.txt")
    _prove(result2.success, "loop tetap sukses setelah truncation tanpa tool lengkap")
    _prove(provider2.calls >= 2, "agent memanggil LLM lagi (melanjutkan, bukan berhenti DONE kosong)")
    _prove((FIXTURE / "only.txt").read_text(encoding="utf-8") == "isi lengkap",
           "file dibuat lengkap di turn berikutnya")

    # --- 7) JSON invalid BUKAN truncation -> tetap ProviderResponseError ---
    print("\n[7] JSON invalid non-truncation tetap error jelas")
    raw_bad = _response([_tool_call("write_file", "{not json", "c1")], finish_reason="tool_calls")
    try:
        provider_obj.normalize_response(GenerateResult(text="", model="m", raw=raw_bad))
        _prove(False, "seharusnya ProviderResponseError")
    except ProviderResponseError as exc:
        print(f"  OK  ProviderResponseError tetap dipertahankan -> {exc}")

    # --- 8) tool lain tetap bekerja ---------------------------------------
    print("\n[8] tool lain (read_file/list_files) tetap bekerja")
    listing = _new_executor()
    obs_read = listing.execute_action(_action("read_file", {"path": "only.txt"}))
    _prove(obs_read.success, "read_file tetap bekerja")
    obs_list = listing.execute_action(_action("list_files", {"path": "."}))
    _prove(obs_list.success, "list_files tetap bekerja")

    # --- 9) write_file atomic: gagal di tengah tidak meninggalkan parsial --
    print("\n[9] write_file atomic (tidak meninggalkan file parsial)")
    import agent_ai.tools.workspace as ws_mod

    (FIXTURE / "atomic.txt").write_text("OLD", encoding="utf-8")
    real_replace = ws_mod.os.replace

    def _boom(*args, **kwargs):
        raise OSError("simulasi kegagalan replace")

    ws_mod.os.replace = _boom  # type: ignore[assignment]
    try:
        try:
            WriteFileTool(root=FIXTURE).execute(path="atomic.txt", content="NEW-CONTENT")
            _prove(False, "seharusnya ToolError saat replace gagal")
        except ToolError:
            print("  OK  kegagalan di tengah -> ToolError (bukan file parsial)")
    finally:
        ws_mod.os.replace = real_replace  # type: ignore[assignment]
    _prove((FIXTURE / "atomic.txt").read_text(encoding="utf-8") == "OLD",
           "file lama utuh (tidak setengah isi)")
    _prove(
        not [p.name for p in FIXTURE.rglob("*") if p.name.startswith(".aether_tmp_")],
        "tidak ada temp file tersisa setelah kegagalan",
    )

    print("\n[OK] Penanganan tool-call terpotong bekerja (recoverable, tanpa file parsial).")
    return 0


def _action(name: str, arguments: Dict[str, Any]):
    """Bangun LLMAction sederhana untuk uji tool lain."""
    from agent_ai.core.response import LLMAction

    return LLMAction(name=name, arguments=arguments)


if __name__ == "__main__":
    raise SystemExit(main())
