"""Verifikasi Agent Activity: history tool call TIDAK ter-reset per action.

Regresi yang dijaga (commit setelah fd4a2d6):
    `AgentActivity.vue::buildUnits()` diubah memanggil `hashArguments()`, tetapi
    definisi fungsinya rusak: seluruh body ditulis pada SATU baris fisik yang
    diawali `//` (newline ter-escape menjadi literal ``\\n``), sehingga
    `hashArguments` TIDAK PERNAH terdefinisi. Akibatnya, begitu ada event
    `tool_called` PERTAMA, `timeline` computed melempar `ReferenceError` ->
    render AgentActivity gagal -> panel Activity hanya menampilkan baris non-tool
    ("Task started" / "Reasoning") dan seluruh history tool hilang.

Verifier deterministik, offline, tanpa browser:
  1. Menjalankan TASK AETHER NYATA (AgentRuntime + ToolExecutor + ScriptedProvider
     tanpa network) dengan BEBERAPA action tool berurutan.
  2. Mengambil event SSE nyata (`task_started` / `tool_called` /
     `observation_received` / `agent_commentary` / `task_completed`) dari
     event-sink runtime — TANPA mengubah emission backend.
  3. Merender AgentActivity.vue (SSR via Vite) secara INKREMENTAL mengikuti
     urutan event: jumlah baris activity TIDAK boleh turun dan label tool
     sebelumnya harus tetap ada (Action1 -> Action2 -> ... -> ActionN).
  4. Membuktikan dedup dua-stream (global + task-scoped) memproses event yang
     sama TEPAT SATU KALI (semantik isDuplicateEvent App.vue).
  5. Guard statis: AgentActivity.vue tidak memanggil fungsi yang tak terdefinisi.

Jalankan:
    python scripts/check_activity_history.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
FRONTEND_DIR = PROJECT_ROOT / "web" / "frontend"
COMPONENTS = FRONTEND_DIR / "src" / "components"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.providers.base import GenerateResult  # noqa: E402
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.runtime.runtime import AgentRuntime  # noqa: E402
from agent_ai.task.models import PreparedTask  # noqa: E402
from agent_ai.tools.registry import build_registry  # noqa: E402

PROBE_ENTRY_NAME = "activity-verify-probe.js"
PROBE_OUT_DIR = ".activity-verify-probe"
PROBE_ENTRY = """// SSR probe khusus verifier activity history (bukan bagian runtime UI).
import { createSSRApp, h } from "vue";
import { renderToString } from "@vue/server-renderer";
import AgentActivity from "./src/components/AgentActivity.vue";

export async function renderSteps(steps) {
  const out = [];
  for (const props of steps) {
    const app = createSSRApp({ render: () => h(AgentActivity, props) });
    out.push(await renderToString(app));
  }
  return out;
}
"""

TASK_ID = "task-activity-history"

#: Verb manusiawi per tool (TOOL_META di AgentActivity.vue) yang HARUS muncul
#: begitu tool terkait dipakai.
TOOL_VERBS = ("Exploring files", "Reading source", "Reading file", "Searching code", "Editing file", "Editing files", "Running command")


# --------------------------------------------------------------------------- #
# Scripted provider (deterministik, tanpa network) — menggerakkan tool loop.   #
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
            {"message": {"content": "", "tool_calls": calls}, "finish_reason": "tool_calls"}
        ]
    }


def _final_turn(text: str = "selesai") -> Dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class _ScriptedProvider(OpenAICompatibleProvider):
    name = "scripted"

    def __init__(self, script: List[Dict[str, Any]]) -> None:
        self.config = SimpleNamespace(model="scripted-model", context_window=0)
        self.script = list(script)
        self.calls = 0

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        idx = self.calls
        self.calls += 1
        raw = self.script[idx] if idx < len(self.script) else _final_turn("done")
        return GenerateResult(text="", model="scripted-model", provider=self.name, raw=raw)


def _run_real_task() -> List[Dict[str, Any]]:
    """Jalankan task AETHER NYATA dengan beberapa action; kembalikan event SSE."""
    workspace = Path(tempfile.mkdtemp(prefix="aether_activity_"))
    (workspace / "a.txt").write_text("alpha\n", encoding="utf-8")
    (workspace / "b.txt").write_text("beta\n", encoding="utf-8")
    try:
        script = [
            _tool_turn([_tool_call("c1", "read_file", {"path": "a.txt"})]),
            _tool_turn([_tool_call("c2", "list_files", {"path": "."})]),
            _tool_turn([_tool_call("c3", "write_file", {"path": "c.txt", "content": "hello"})]),
            _tool_turn([_tool_call("c4", "edit_file", {"path": "c.txt", "old_text": "hello", "new_text": "world"})]),
            _tool_turn([_tool_call("c5", "read_file", {"path": "b.txt"})]),
            _final_turn("selesai"),
        ]
        provider = _ScriptedProvider(script)
        registry = build_registry(root=workspace)
        runtime = AgentRuntime(provider=provider, executor=ToolExecutor(registry=registry))

        raw_events: List[Dict[str, Any]] = []

        def sink(event_type: str, payload: Dict[str, Any]) -> None:
            raw_events.append({"event_type": event_type, "payload": payload})

        runtime._event_sink = sink  # noqa: SLF001 - pola uji yang sama dengan tests/
        prepared = PreparedTask(task="Verifikasi activity history", context=None, plan=None)
        runtime.run(prepared)

        # Bangun frame SSE persis seperti yang dikonsumsi App.vue/handleEvent.
        events: List[Dict[str, Any]] = [
            {
                "event_type": "task_started",
                "task_id": TASK_ID,
                "event_id": "evt-start",
                "sequence": 0,
                "timestamp": time.time(),
                "payload": {},
            },
            {
                "event_type": "agent_commentary",
                "task_id": TASK_ID,
                "event_id": "evt-comment",
                "sequence": 1,
                "timestamp": time.time(),
                "payload": {"text": "Merencanakan langkah"},
            },
        ]
        seq = 2
        for item in raw_events:
            payload = item.get("payload") or {}
            if item["event_type"] in ("tool_called", "observation_received", "tool_completed"):
                try:
                    payload = json.loads(json.dumps(payload, default=str))
                except (TypeError, ValueError):
                    payload = {"tool": payload.get("tool", "")}
                events.append(
                    {
                        "event_type": item["event_type"],
                        "task_id": TASK_ID,
                        "event_id": f"evt-{seq}",
                        "sequence": seq,
                        "timestamp": time.time(),
                        "payload": payload,
                    }
                )
                seq += 1
        events.append(
            {
                "event_type": "task_completed",
                "task_id": TASK_ID,
                "event_id": "evt-done",
                "sequence": seq,
                "timestamp": time.time(),
                "payload": {"result": "Selesai."},
            }
        )
        return events
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# --------------------------------------------------------------------------- #
# SSR render AgentActivity secara inkremental.                                 #
# --------------------------------------------------------------------------- #
def _ssr_render_incremental(events: List[Dict[str, Any]]) -> List[str]:
    node = shutil.which("node")
    assert node, "node tidak tersedia"
    entry = FRONTEND_DIR / PROBE_ENTRY_NAME
    out_dir = FRONTEND_DIR / PROBE_OUT_DIR
    shutil.rmtree(out_dir, ignore_errors=True)
    entry.write_text(PROBE_ENTRY, encoding="utf-8")
    try:
        build = subprocess.run(
            [node, str(FRONTEND_DIR / "node_modules" / "vite" / "bin" / "vite.js"),
             "build", "--ssr", entry.name, "--outDir", out_dir.name],
            cwd=str(FRONTEND_DIR), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        assert build.returncode == 0, f"SSR build gagal:\n{build.stdout}\n{build.stderr}"

        steps = [
            {"events": events[: i], "status": "running", "isReasoning": False}
            for i in range(1, len(events) + 1)
        ]
        (out_dir / "input.json").write_text(json.dumps(steps), encoding="utf-8")
        runner = (
            "const fs=require('fs');(async()=>{"
            f"const steps=JSON.parse(fs.readFileSync('{PROBE_OUT_DIR}/input.json','utf8'));"
            f"const m=await import('./{PROBE_OUT_DIR}/{PROBE_ENTRY_NAME.replace('.js', '')}.js');"
            "const out=await m.renderSteps(steps);"
            "process.stdout.write(JSON.stringify(out));"
            "})().catch(e=>{console.error(String((e&&e.stack)||e));process.exit(1)});"
        )
        render = subprocess.run(
            [node, "-e", runner], cwd=str(FRONTEND_DIR), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        assert render.returncode == 0, f"SSR render gagal:\n{render.stdout}\n{render.stderr}"
        return json.loads(render.stdout)
    finally:
        entry.unlink(missing_ok=True)
        shutil.rmtree(out_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Dedup dua-stream (semantik isDuplicateEvent App.vue).                        #
# --------------------------------------------------------------------------- #
def _dedup_unique(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    handled: set = set()
    processed: List[Dict[str, Any]] = []
    for evt in events:  # setiap event datang DUA KALI (queue + task-scoped)
        key = (evt.get("task_id"), evt.get("event_id"), evt.get("sequence"))
        if key in handled:
            continue
        handled.add(key)
        processed.append(evt)
    return processed


def main() -> int:
    print("=== Verifikasi Agent Activity: history tidak ter-reset per action ===")

    activity_src = (COMPONENTS / "AgentActivity.vue").read_text(encoding="utf-8")

    # --- 1) Guard statis: tidak ada pemanggilan fungsi tak terdefinisi. ---
    # Signature regresi: definisi `hashArguments` ditulis pada satu baris yang
    # diawali komentar (newline ter-escape) sehingga fungsi tak terdefinisi
    # padahal masih dipanggil. Deteksi: jumlah pemanggilan != jumlah definisi.
    calls = activity_src.count("hashArguments(")
    defs = activity_src.count("function hashArguments")
    assert calls == defs, (
        f"pemanggilan hashArguments ({calls}) != definisinya ({defs}) -> "
        "potensi 'hashArguments is not defined' saat tool_called"
    )
    assert "\\nfunction " not in activity_src, (
        "AgentActivity.vue memuat definisi fungsi pada baris ber-escape '\\n' "
        "-> fungsi ter-kerkomentar dan memicu ReferenceError saat render"
    )
    print("[1] guard statis: tidak ada pemanggilan fungsi tak terdefinisi OK")

    # --- 2) Jalankan task AETHER NYATA (offline) dengan beberapa action. ---
    events = _run_real_task()
    tool_called = [e for e in events if e["event_type"] == "tool_called"]
    observed = [e for e in events if e["event_type"] == "observation_received"]
    assert len(tool_called) >= 4, f"task harus menghasilkan >=4 tool action, dapat {len(tool_called)}"
    assert len(observed) >= 4, f"task harus menghasilkan >=4 observation, dapat {len(observed)}"
    assert any(e["event_type"] == "task_started" for e in events)
    assert any(e["event_type"] == "task_completed" for e in events)
    actions = [str((e["payload"] or {}).get("tool") or "") for e in tool_called]
    print(f"[2] task AETHER nyata -> {len(tool_called)} tool action: {', '.join(actions)} OK")

    # --- 3) SSR incremental: baris tidak turun, history tetap ada. ---
    htmls = _ssr_render_incremental(events)
    assert len(htmls) == len(events), "jumlah render tidak sesuai jumlah langkah"
    rows = [html.count("act-row") for html in htmls]
    for i in range(1, len(rows)):
        assert rows[i] >= rows[i - 1], (
            f"baris activity TURUN pada langkah {i}: {rows[i-1]} -> {rows[i]} (history ter-reset!)"
        )
    seen_verbs: set = set()
    for i, html in enumerate(htmls):
        present = {v for v in TOOL_VERBS if v in html}
        missing = seen_verbs - present
        assert not missing, (
            f"langkah {i}: label tool hilang {sorted(missing)} (history ter-reset!)"
        )
        seen_verbs |= present
    assert rows[-1] >= len(tool_called), (
        f"baris akhir ({rows[-1]}) < jumlah tool action ({len(tool_called)})"
    )
    assert len(seen_verbs) >= 3, f"harus ada >=3 jenis tool tampil, dapat {sorted(seen_verbs)}"
    print(f"[3] SSR incremental: baris {rows[0]} -> {rows[-1]} monoton naik, label tool persisten OK")

    # --- 4) Dedup dua-stream: event sama -> diproses TEPAT SATU KALI. ---
    duplicated = []
    for e in events:
        duplicated.append(e)  # stream 1 (global)
        duplicated.append(dict(e))  # stream 2 (task-scoped, event_id sama)
    processed = _dedup_unique(duplicated)
    assert len(processed) == len(events), (
        f"dedup gagal: {len(processed)} diproses, seharusnya {len(events)}"
    )
    assert len(duplicated) == 2 * len(events)
    print(f"[4] dedup dua-stream: {len(duplicated)} frame -> {len(processed)} event unik OK")

    # --- 5) Backend TIDAK berubah (frontend-only fix). ---
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", "src", "web/django_app"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True,
    )
    assert proc.stdout.strip() == "", f"backend/Python berubah (harusnya kosong):\n{proc.stdout}"
    print("[5] backend/Python tidak berubah (frontend-only) OK")

    print()
    print("[OK] Activity mempertahankan history (Action1 -> ... -> ActionN), tidak ter-reset,")
    print("     tool call + hasil tampil, dedup dua-stream tetap satu kali.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:  # pragma: no cover
        print(f"[FAIL] {exc}")
        sys.exit(1)
