"""Verifikasi FINAL AGENT REPORT utuh (tanpa truncation AETHER) + tombol Copy.

Deterministik, TANPA model/API cloud nyata (provider fake in-process).
Fixture dibuat di `dummy_test/final_report_fixture` dan dibersihkan setelah test.

Yang diuji:
    1. LLM final response == AgentRuntime result (tidak dipotong)
    2. Payload event `task_completed` (jalur SSE/session store) == LLM response
    3. `.aether/log/<task_id>.log` (task_completed & task_finished) == LLM response
    4. Report API gateway (GET /api/tasks/<id>/report -> .aether/log) == LLM response
    5. Report yang dirender frontend memuat SELURUH isi (sentinel + semua bagian)
    6. Frontend: tombol Copy ada, mengambil SELURUH report (bukan viewport/HTML)
    7. REGRESI: limit activity log / tool output internal TIDAK diubah
       (string panjang biasa tetap dipotong `...[truncated]` seperti sebelumnya)
    8. REGRESI: tombol Copy Consultant tetap ada

Jalankan:
    python scripts/check_final_report.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
FRONTEND_DIR = PROJECT_ROOT / "web" / "frontend" / "src"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "final_report_fixture"

#: Penanda akhir report: harus muncul di semua layer bila report benar-benar utuh.
SENTINEL = "SENTINEL-AKHIR-LAPORAN-YANG-TIDAK-BOLEH-HILANG"
REPORT_SECTIONS = 80
TRUNCATION_MARKER = "...[truncated]"


def build_synthetic_report() -> str:
    """Laporan sintetis sengaja PANJANG (>10.000 karakter) + markdown + sentinel."""
    parts = ["# Laporan Akhir AETHER", ""]
    for i in range(1, REPORT_SECTIONS + 1):
        parts.append(f"## Bagian {i}: Analisis {i}")
        parts.append("")
        parts.append(
            "Penjelasan panjang "
            + ("lorem ipsum dolor sit amet consectetur adipiscing elit " * 3).strip()
            + f" (bagian {i})."
        )
        parts.append("")
        parts.append(f"- poin {i}.1")
        parts.append(f"- poin {i}.2")
        parts.append("")
    parts.append("```python")
    parts.append("print('contoh code block di dalam report')")
    parts.append("```")
    parts.append("")
    parts.append(SENTINEL)
    return "\n".join(parts)


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


# ---------------------------------------------------------------------------
# Provider fake (deterministik, tanpa network)
# ---------------------------------------------------------------------------
def _make_fake_provider(final_text: str, *, tool_call_first: str | None = None):
    """Provider fake: opsional satu tool call dulu, lalu final text."""
    from agent_ai.core.response import ActionType, FinishReason, LLMAction, LLMResponse
    from agent_ai.providers.base import BaseProvider, GenerateResult

    class FakeProvider(BaseProvider):
        name = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
            self.calls += 1
            return GenerateResult(text="", provider="fake", model="fake-1")

        def is_available(self) -> bool:
            return True

        def normalize_response(self, result):
            if tool_call_first and self.calls == 1:
                return LLMResponse(
                    text="",
                    actions=[
                        LLMAction(name=tool_call_first, arguments={}, type=ActionType.TOOL_CALL)
                    ],
                    finish_reason=FinishReason.TOOL_CALLS,
                    provider="fake",
                    model="fake-1",
                )
            return LLMResponse(
                text=final_text,
                actions=[],
                finish_reason=FinishReason.STOP,
                provider="fake",
                model="fake-1",
            )

    return FakeProvider()


def _big_output_tool(output_len: int):
    """Tool dummy yang mengembalikan output sangat panjang (uji limit internal)."""
    from agent_ai.tools import BaseTool, ToolRegistry

    class BigOutputTool(BaseTool):
        name = "big_output"
        description = "Tool dummy: output panjang (tidak menyentuh filesystem)."
        input_schema = {"type": "object", "properties": {}}

        def execute(self, **arguments):
            return "OUT" + ("z" * output_len)

    registry = ToolRegistry()
    registry.register(BigOutputTool())
    return registry


def _run_runtime(report: str, task_id: str, *, tool_call_first: str | None = None):
    """Jalankan satu task lewat AgentRuntime dengan project_root = fixture."""
    from agent_ai.core.executor import ToolExecutor
    from agent_ai.runtime.runtime import AgentRuntime
    from agent_ai.session.store import InMemorySessionStore
    from agent_ai.task.models import PreparedTask

    store = InMemorySessionStore()
    session = store.create_session()

    registry = _big_output_tool(5000)
    provider = _make_fake_provider(report, tool_call_first=tool_call_first)

    runtime = AgentRuntime(
        provider=provider,
        executor=ToolExecutor(registry=registry),
        session_store=store,
        session_id=session.session_id,
        project_root=str(FIXTURE),
        project_brain=False,
    )
    result = runtime.run(PreparedTask(task="buat laporan akhir yang panjang", task_id=task_id))
    return result, store, session.session_id


def _read_log_events(task_id: str) -> list:
    log_path = FIXTURE / ".aether" / "log" / f"{task_id}.log"
    assert log_path.exists(), f"log task harus ada: {log_path}"
    events = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def _check_render(report: str) -> dict:
    """Render report dengan renderer frontend (node) untuk membuktikan UI utuh."""
    node = shutil.which("node")
    if not node:
        return {"skipped": True}

    markdown_js = (FRONTEND_DIR / "markdown.js").resolve()
    probe = DUMMY_ROOT / "_final_report_render_probe.mjs"
    payload = DUMMY_ROOT / "_final_report_payload.json"
    payload.write_text(json.dumps({"report": report}), encoding="utf-8")
    probe.write_text(
        "import { readFileSync } from 'node:fs';\n"
        f"const md = await import({json.dumps(markdown_js.as_uri())});\n"
        "const data = JSON.parse(readFileSync(process.argv[2], 'utf8'));\n"
        "const html = md.renderMarkdown(data.report);\n"
        "process.stdout.write(JSON.stringify({\n"
        "  htmlLen: html.length,\n"
        "  headings: (html.match(/<h2/g) || []).length,\n"
        "  hasSentinel: html.includes(process.argv[3]),\n"
        "  hasCodeBlock: html.includes('contoh code block di dalam report'),\n"
        "}));\n",
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            [node, str(probe), str(payload), SENTINEL],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        probe.unlink(missing_ok=True)
        payload.unlink(missing_ok=True)
    if proc.returncode != 0:
        return {"skipped": True, "error": proc.stderr[:300]}
    return json.loads(proc.stdout.strip())


def main() -> int:
    print("=== Verifikasi Final Agent Report utuh + tombol Copy ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    report = build_synthetic_report()
    assert len(report) > 10000, f"laporan sintetis harus panjang, dapat {len(report)}"
    assert report.endswith(SENTINEL)
    print(f"[0] laporan sintetis siap -> {len(report)} karakter (>10.000) OK")

    task_id = "finalreporttask0001"
    result, store, session_id = _run_runtime(report, task_id)

    # 1) LLM final response == runtime result (tidak dipotong).
    assert result.result == report, (
        f"runtime result harus sama dengan LLM response "
        f"({len(report)} vs {len(result.result or '')})"
    )
    assert TRUNCATION_MARKER not in result.result
    print(f"[1] LLM final response == AgentRuntime result OK -> {len(result.result)} karakter")

    # 2) Payload event task_completed (jalur live/SSE) == LLM response.
    events = store.get_events(task_id=task_id)
    completed = [e for e in events if e.event_type.value == "task_completed"]
    assert completed, "event task_completed harus ada di session store"
    live_report = completed[-1].payload.get("result")
    assert live_report == report, (
        f"payload task_completed (SSE) harus utuh "
        f"({len(report)} vs {len(live_report or '')})"
    )
    # Frame SSE nyata (bukan hanya objek in-memory).
    from api.streaming import format_sse

    frame = format_sse(completed[-1])
    assert SENTINEL in frame, "frame SSE harus memuat akhir report"
    print(f"[2] payload task_completed (SSE live) utuh OK -> {len(live_report)} karakter")

    # 3) Persistent log .aether/log/<task_id>.log == LLM response.
    log_events = _read_log_events(task_id)
    by_type = {}
    for ev in log_events:
        by_type.setdefault(ev.get("event"), []).append(ev.get("data") or {})
    assert by_type.get("task_completed"), "task_completed harus tercatat di log"
    log_report = by_type["task_completed"][-1].get("result")
    assert log_report == report, (
        f"task_completed.data.result di log harus utuh "
        f"({len(report)} vs {len(log_report or '')})"
    )
    finished_report = by_type.get("task_finished", [{}])[-1].get("result")
    assert finished_report == report, "task_finished.data.result (fallback report) harus utuh"
    raw_log = (FIXTURE / ".aether" / "log" / f"{task_id}.log").read_text(encoding="utf-8")
    assert SENTINEL in raw_log, "file log mentah harus memuat akhir report (tidak terpotong)"
    assert TRUNCATION_MARKER not in log_report[-len(TRUNCATION_MARKER) :]
    print(f"[3] .aether/log (task_completed + task_finished) utuh OK -> {len(log_report)} karakter")

    # 4) Report API gateway membaca report utuh dari log.
    from api.services import GatewayService

    class _StubProjectStore:
        """Stub launcher store: arahkan root project ke fixture (tanpa SQLite)."""

        def get_project(self, project_id):
            return {"id": project_id, "path": str(FIXTURE)} if project_id == "fx" else None

        def get_active_project_id(self):
            return "fx"

    service = GatewayService(project_store=_StubProjectStore(), auto_execute=False)
    api_result = service.get_task_report(task_id, "fx")
    assert api_result.get("report") == report, (
        f"Report API harus mengembalikan report utuh "
        f"({len(report)} vs {len(api_result.get('report') or '')})"
    )
    activity = service.get_task_activity(task_id, "fx")
    act_completed = [e for e in activity if e.get("event") == "task_completed"]
    assert act_completed and act_completed[-1]["data"]["result"] == report
    print(f"[4] Report API + Activity API utuh OK -> {len(api_result['report'])} karakter")

    # 5) Report yang DIRENDER frontend memuat seluruh isi.
    render = _check_render(report)
    if render.get("skipped"):
        print(f"[5] render frontend SKIP (node tidak tersedia) -> {render.get('error', '')}")
    else:
        assert render["headings"] == REPORT_SECTIONS, render
        assert render["hasSentinel"] is True, render
        assert render["hasCodeBlock"] is True, render
        print(
            f"[5] render frontend memuat SELURUH report OK -> "
            f"html={render['htmlLen']}B, {render['headings']} heading, sentinel ada"
        )

    # 6) Frontend: tombol Copy final report ada dan menyalin SELURUH report.
    activity_vue = (FRONTEND_DIR / "components" / "AgentActivity.vue").read_text(encoding="utf-8")
    styles_css = (FRONTEND_DIR / "styles.css").read_text(encoding="utf-8")
    assert "reportText" in activity_vue, "teks report asli harus disimpan untuk Copy"
    assert "copyReport" in activity_vue, "fungsi copyReport harus ada"
    assert "navigator.clipboard.writeText(text)" in activity_vue, (
        "Copy harus memakai navigator.clipboard.writeText(text) dengan teks report"
    )
    assert 'class="copy-btn"' in activity_vue, "tombol Copy harus memakai .copy-btn (pola Consultant)"
    assert 'class="cmsg-actions"' in activity_vue, "tombol Copy harus di .cmsg-actions (kiri bawah)"
    assert '"Copied" : "Copy"' in activity_vue, "feedback label Copy -> Copied harus ada"
    assert "item.reportHtml" in activity_vue and "renderMarkdown" in activity_vue
    # Report TIDAK boleh dipotong di frontend (tanpa marker truncation/slicing).
    assert TRUNCATION_MARKER not in activity_vue, "frontend tidak boleh memakai marker truncation"
    assert "reportText.slice" not in activity_vue and "reportText).slice" not in activity_vue, (
        "teks report tidak boleh dipotong (slice) sebelum disalin"
    )
    assert ".act-report-block" in styles_css, "wrapper report + tombol copy harus punya style"
    print("[6] frontend: report utuh + tombol Copy (pola .cmsg-actions/.copy-btn) OK")

    # 7) REGRESI: limit activity log / tool output internal TIDAK diubah.
    from agent_ai.core.observability import sanitize_payload

    long_internal = "x" * 5000
    cleaned = sanitize_payload({"content": long_internal})
    assert cleaned["content"] != long_internal, "string internal panjang harus TETAP dipotong"
    assert cleaned["content"].endswith(TRUNCATION_MARKER), cleaned["content"][-30:]
    assert len(cleaned["content"]) == 2000 + len(TRUNCATION_MARKER), len(cleaned["content"])

    internal_task_id = "finalreporttask0002"
    _, store2, _ = _run_runtime(report, internal_task_id, tool_call_first="big_output")
    internal_log = _read_log_events(internal_task_id)
    observations = [
        ev.get("data", {}) for ev in internal_log if ev.get("event") == "observation_received"
    ]
    assert observations, "observation_received harus tercatat"
    obs_content = observations[-1].get("content")
    assert isinstance(obs_content, str) and obs_content.endswith(TRUNCATION_MARKER), (
        "tool output internal panjang harus tetap dipotong di log"
    )
    print(
        f"[7] REGRESI limit internal TIDAK berubah OK -> sanitize 5000 -> "
        f"{len(cleaned['content'])}, observation log -> {len(obs_content)}"
    )

    # 8) REGRESI: tombol Copy Consultant tetap ada (tidak rusak).
    consultant_vue = (FRONTEND_DIR / "components" / "ConsultantChat.vue").read_text(
        encoding="utf-8"
    )
    assert "copyMessage" in consultant_vue and "navigator.clipboard.writeText" in consultant_vue
    assert 'class="copy-btn"' in consultant_vue
    print("[8] REGRESI tombol Copy Consultant tetap bekerja OK")

    print("\nSEMUA CHECK FINAL REPORT + COPY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
