"""Verifikasi LIVE STATUS Consultant end-to-end (deterministik, tanpa API key).

Menguji rantai event NYATA yang dipakai indikator `.consultant-thinking`:

    ConsultantService.consult(event_emit=_sink)
        -> GatewayService._consult_event_emit -> emit_event
        -> SessionStore (InMemorySessionStore) append_event
        -> EventSubscription(session_id=...) -> sse_stream -> format_sse
        -> payload JSON yang dikonsumsi frontend (ConsultantChat.vue)

Yang diverifikasi:
    1. tool_called & tool_completed BENAR-BENAR dipancarkan ConsultantService
       dengan event_emit, memakai session_id sesi konsultasi.
    2. GatewayService.consult meneruskan keduanya ke SessionStore (tanpa
       store/transport kedua) — event tersimpan dan subscriber menerimanya.
    3. Payload yang lolos SSE berbentuk frame AETHER lengkap dan memuat
       `event_type` + `session_id` + `payload.tool` (kontrak frontend).
    4. `event: <event_type>` pada frame SSE = nama event bernama (tool_called /
       tool_completed) yang didaftarkan api.js KNOW_EVENTS.
    5. Pemetaan frontend: tool_completed -> "Analyzing results…" dan status
       tidak tereset oleh event berikutnya (via node consultantStatus.test.mjs
       bila Node tersedia).
    6. Boundary: GET /api/events tetap hanya transport (EventSubscription +
       sse_stream) dan tidak ada endpoint/console model event kedua.
    7. CSS: `.consultant-thinking` memakai warna cyan neon + text-shadow glow
       dan animasi TIDAK mengganti/memalsukan status.

Jalankan:
    python scripts/check_consultant_live_status.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
FRONTEND_SRC = PROJECT_ROOT / "web" / "frontend" / "src"
for p in (SRC_DIR, PROJECT_ROOT / "web" / "django_app"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# Event runtime Consultant yang bermakna untuk status live.
CONSULTANT_EVENTS = ("tool_called", "tool_completed")


# --------------------------------------------------------------------------- #
# Fixtures (sejajar dengan scripts/check_consultant.py: FakeProvider deterministik)
# --------------------------------------------------------------------------- #
def _import_consultant_bits():
    from agent_ai.providers.base import BaseProvider, GenerateResult
    from agent_ai.core.response import FinishReason, LLMAction, LLMResponse

    return BaseProvider, GenerateResult, FinishReason, LLMAction, LLMResponse


def _final(text, LLMResponse, FinishReason):
    return LLMResponse(text=text, finish_reason=FinishReason.STOP)


def _tool(name, LLMResponse, FinishReason, LLMAction, **args):
    return LLMResponse(
        actions=[LLMAction(name=name, arguments=dict(args))],
        finish_reason=FinishReason.TOOL_CALLS,
    )


def _make_provider():
    BaseProvider, GenerateResult, FinishReason, LLMAction, LLMResponse = (
        _import_consultant_bits()
    )

    class FakeProvider(BaseProvider):
        """Provider deterministik: search_code -> jawaban final."""

        name = "fake"

        def __init__(self, script):
            self.script = list(script)
            self.calls = 0
            self.last_messages = None

        def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
            self.last_messages = messages
            return GenerateResult(text="", model="fake-model", provider="fake")

        def normalize_response(self, result):
            idx = min(self.calls, len(self.script) - 1)
            self.calls += 1
            return self.script[idx]

    return FakeProvider, GenerateResult, FinishReason, LLMAction, LLMResponse


# --------------------------------------------------------------------------- #
# 1-4) Rantai event: ConsultantService -> Gateway -> SessionStore -> SSE
# --------------------------------------------------------------------------- #
def scenario_event_chain(root: Path) -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
    import django

    django.setup()

    import api.services as services_mod
    from agent_ai.consultant import ConsultantService as CoreConsultantService
    from agent_ai.session.events import EventType
    from api.streaming import EventSubscription, format_sse, sse_stream

    FakeProvider, GenerateResult, FinishReason, LLMAction, LLMResponse = _make_provider()

    script = [
        _tool("search_code", LLMResponse, FinishReason, LLMAction, query="def add"),
        _final("Findings: fixture OK.", LLMResponse, FinishReason),
    ]

    class _StubProjectStore:
        """Project store minimal: satu active project deterministik."""

        def __init__(self, root_path: Path):
            self._root = str(root_path)

        def get_active_project_id(self):
            return "proj-live-status"

        def get_project(self, project_id):
            return {"project_id": project_id, "name": "fixture", "root": self._root}

    gw = services_mod.GatewayService(
        auto_execute=False,
        consultant_service=CoreConsultantService(),
        project_store=_StubProjectStore(root),
    )
    provider = FakeProvider(script)
    gw._build_consultant_provider = lambda *a, **k: provider  # type: ignore[assignment]

    result = gw.consult(message="analisa fixture", root=str(root), mode="investigate")
    session_id = result["session_id"]
    assert session_id, result
    assert result["status"] == "done", result["status"]

    # 1) tool_events terisi (tool_called success=None, tool_completed success=True).
    tools = [e["tool"] for e in result["tool_events"]]
    assert "search_code" in tools, tools
    print(f"[1] ConsultantService menjalankan tool + tool_events OK -> {tools}")

    # 2) Event BENAR-BENAR dipancarkan ke SessionStore (dengan session_id).
    stored = gw.sessions.get_events(session_id=session_id)
    by_type: dict[str, list] = {}
    for ev in stored:
        by_type.setdefault(ev.event_type.value, []).append(ev)
    for name in CONSULTANT_EVENTS:
        assert by_type.get(name), (
            f"event '{name}' tidak ada di SessionStore untuk session {session_id}; "
            f"dapat {sorted(by_type)}"
        )
    called = by_type["tool_called"][0]
    completed = by_type["tool_completed"][0]
    assert called.session_id == session_id, called
    assert completed.session_id == session_id, completed
    assert called.payload.get("tool") == "search_code", called.payload
    assert completed.payload.get("tool") == "search_code", completed.payload
    assert completed.payload.get("success") is True, completed.payload
    # TIDAK ada task_id: event observability Consultant bukan event task.
    assert called.task_id is None and completed.task_id is None
    print(
        "[2] GatewayService.consult -> SessionStore: "
        f"tool_called + tool_completed tersimpan (session_id={session_id}) OK"
    )

    # 3) Subscriber SSE menerima event dalam URUTAN dan hanya untuk session ini.
    sub = EventSubscription(gw.sessions, session_id=session_id)
    sub.start()

    # Re-run satu giliran supaya event di-append SETELAH subscription aktif
    # (EventSubscription hanya menerima event baru, bukan replay).
    provider2 = FakeProvider(list(script))
    gw._build_consultant_provider = lambda *a, **k: provider2  # type: ignore[assignment]
    gw.consult(message="analisa ulang", root=str(root), session_id=session_id, mode="investigate")

    frames = []
    while True:
        ev = sub.get(timeout=0.05)
        if ev is None:
            break
        frames.append(ev)
    sub.close()
    assert frames, "subscriber SSE tidak menerima event apa pun"

    names = [e.event_type.value for e in frames]
    for name in CONSULTANT_EVENTS:
        assert name in names, (name, names)
    # Urutan: tool_called mendahului tool_completed untuk tool yang sama.
    i_called = names.index("tool_called")
    i_completed = names.index("tool_completed")
    assert i_called < i_completed, names
    print(f"[3] EventSubscription menerima urutan {names} OK")

    # 4) format_sse -> frame + payload JSON yang dikonsumsi frontend.
    raw = format_sse(completed)
    assert raw.startswith("id: "), raw
    assert f"event: {EventType.TOOL_COMPLETED.value}" in raw, raw
    data_line = [ln for ln in raw.splitlines() if ln.startswith("data: ")][0]
    payload = json.loads(data_line[len("data: "):])
    for key in ("event_id", "session_id", "task_id", "event_type", "payload", "sequence"):
        assert key in payload, (key, payload)
    assert payload["event_type"] == "tool_completed", payload
    assert payload["session_id"] == session_id, payload
    assert payload["payload"]["tool"] == "search_code", payload
    print("[4] format_sse -> payload JSON SSE (event_type + session_id + payload.tool) OK")

    # 4b) Nama event yang dipakai = nama yang didaftarkan api.js KNOW_EVENTS.
    api_js = (FRONTEND_SRC / "api.js").read_text(encoding="utf-8")
    for name in CONSULTANT_EVENTS:
        assert f'"{name}"' in api_js, f"api.js KNOW_EVENTS harus memuat '{name}'"
    print("[4b] api.js KNOW_EVENTS memuat tool_called + tool_completed OK")

    # 4c) HTTP end-to-end: /api/consultant/consult -> 200 (kontrak API utuh)
    #     + GET /api/events tetap transport (EventSubscription + sse_stream).
    services_mod._default_service = gw
    from django.test import Client

    client = Client()
    resp = client.post(
        "/api/consultant/consult",
        data=json.dumps({"message": "halo", "mode": "quick"}),
        content_type="application/json",
    )
    assert resp.status_code == 200, (resp.status_code, resp.content)
    assert resp.json()["session_id"], resp.json()
    print("[4c] POST /api/consultant/consult -> 200 (kontrak API dipertahankan) OK")

    # 4d) sse_stream (generator) dapat mengalirkan frame lalu berhenti saat
    #     client disconnect -> lifecycle pembersihan stream tetap ada.
    sub2 = EventSubscription(gw.sessions, session_id=session_id)
    sub2.start()
    calls = {"n": 0}

    def is_disconnected() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # berhenti pada iterasi kedua

    # Append satu event baru agar generator mengalirkan frame nyata.
    gw.emit_event(session_id, "tool_called", payload={"tool": "search_code"})
    it = sse_stream(sub2, heartbeat=0.01, is_disconnected=is_disconnected)
    first = next(it)
    assert first.startswith("id: "), first
    it.close()  # generator finalizer harus menutup subscription
    assert sub2.closed, "sse_stream harus menutup subscription saat berhenti"
    print("[4d] sse_stream mengalirkan frame + menutup subscription (cleanup) OK")

    # 5) Boundary: view events hanya transport (tanpa model event kedua).
    views_src = (PROJECT_ROOT / "web" / "django_app" / "api" / "views.py").read_text(
        encoding="utf-8"
    )
    evt_fn = views_src[views_src.index("def events(request") :]
    evt_fn = evt_fn[: evt_fn.index("\n@") if "\n@" in evt_fn else len(evt_fn)]
    assert "EventSubscription(" in evt_fn and "sse_stream(" in evt_fn, evt_fn
    assert "Event(" not in evt_fn.split("streaming")[0], "tidak boleh ada model event kedua"
    # Service hanya memakai mekanisme emit_event existing (satu jalur).
    services_src = (PROJECT_ROOT / "web" / "django_app" / "api" / "services.py").read_text(
        encoding="utf-8"
    )
    assert "self.emit_event(emit_session_id, event_type, payload=payload)" in services_src
    print("[5] GET /api/events tetap transport (SessionStore existing) OK")


# --------------------------------------------------------------------------- #
# 6) Frontend: pemetaan status (dijalankan dengan Node bila tersedia)
# --------------------------------------------------------------------------- #
def scenario_frontend_mapping() -> None:
    node = shutil.which("node")
    test_file = FRONTEND_SRC / "consultantStatus.test.mjs"
    assert test_file.is_file(), "consultantStatus.test.mjs harus ada"
    if not node:
        print("[6] SKIP: node tidak tersedia; jalankan manual consultantStatus.test.mjs")
        return
    proc = subprocess.run(
        [node, str(test_file)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(f"consultantStatus.test.mjs GAGAL:\n{proc.stdout}\n{proc.stderr}")
    assert "Analyzing results" in proc.stdout, proc.stdout
    print("[6] Node: tool_completed -> \"Analyzing results…\" + status tidak tereset OK")

    # ConsultantChat.vue memakai helper murni itu (bukan implementasi kedua).
    chat = (FRONTEND_SRC / "components" / "ConsultantChat.vue").read_text(encoding="utf-8")
    assert "../consultantStatus.js" in chat, "ConsultantChat harus memakai consultantStatus.js"
    assert "applyConsultantEvent(liveStatus.value, payload)" in chat, (
        "status harus di-update lewat applyConsultantEvent (anti-tertimpa)"
    )
    assert "eventBelongsToSession(payload, sid)" in chat, "filter session_id harus dipakai"
    # Lifecycle stream dipertahankan.
    assert "function closeLiveStream()" in chat and "liveStream.close()" in chat
    assert "openEventStream({" in chat and "sessionId: sid" in chat
    assert "finally {" in chat, "cleanup stream di finally harus tetap ada"
    # Ringkasan tool (tool_events) dipertahankan.
    assert "function summarizeTools(events)" in chat and "data.tool_events" in chat
    # Tidak ada timer/rotasi teks/simulasi status.
    for banned in ("setInterval(", "setTimeout(() => liveStatus", "rotatingStatus"):
        assert banned not in chat, f"status tidak boleh memakai simulasi: {banned}"
    print("[6b] ConsultantChat.vue: helper murni + cleanup stream + tool_events utuh OK")


# --------------------------------------------------------------------------- #
# 7) CSS: neon cyan + glow + animasi tidak memalsukan status
# --------------------------------------------------------------------------- #
def scenario_css() -> None:
    css = (PROJECT_ROOT / "web" / "frontend" / "src" / "styles.css").read_text(
        encoding="utf-8"
    )
    assert ".consultant-thinking {" in css, "rule .consultant-thinking harus ada"
    block = css[css.index(".consultant-thinking {") :]
    block = block[: block.index("}") + 1]

    # Warna cyan neon (bukan var(--text-faint) redup seperti sebelumnya).
    assert "color: #00e5ff" in block.lower(), block
    assert "text-shadow" in block, "harus ada text-shadow glow"
    assert "rgba(0, 229, 255" in block, block
    # Layout/ukuran font/posisi dipertahankan.
    assert "align-self: flex-start" in block, block
    assert "font-size: 12px" in block, block
    assert "font-style: italic" in block, block
    assert "padding: 4px 2px" in block, block
    print("[7] CSS .consultant-thinking -> cyan neon #00E5FF + glow OK")

    # Animasi (bila ada) hanya memodulasi text-shadow -> tidak ada konten teks
    # yang di-inject lewat CSS (content:), jadi status tidak bisa dipalsukan.
    assert "@keyframes consultant-thinking-glow" in css, "keyframes glow harus ada"
    kf = css[css.index("@keyframes consultant-thinking-glow") :]
    kf = kf[: kf.index("\n@") if "\n@" in kf else len(kf)]
    for step in ("0%", "50%", "100%"):
        assert step in kf, (step, kf)
    assert "text-shadow" in kf, kf
    assert "content:" not in kf, "animasi tidak boleh menyuntik teks status"
    assert "opacity" not in kf or "text-shadow" in kf, kf
    # Preferensi aksesibilitas dihormati (glow tetap menyala tanpa animasi).
    assert "prefers-reduced-motion: reduce" in css, "harus menghormati reduced-motion"
    print("[7b] Animasi glow hanya memodulasi text-shadow (tanpa content/simulasi) OK")

    # Efek glow berlaku untuk SELURUH teks live status: tidak ada selector yang
    # menimpa warna/glow per-status (mis. .consultant-thinking.analyzing).
    assert not re.search(r"\.consultant-thinking\s*\.", css), (
        "tidak boleh ada rule per-status yang menimpa glow"
    )
    print("[7c] Glow berlaku untuk seluruh teks Live Status (tanpa override per-status) OK")


def main() -> int:
    print("=== Verifikasi Consultant Live Status + Neon Cyan ===")
    root = Path(tempfile.mkdtemp(prefix="consultant_live_fixture_"))
    try:
        (root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        scenario_event_chain(root)
        scenario_frontend_mapping()
        scenario_css()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print()
    print("[OK] Live Status Consultant mengikuti event runtime NYATA + neon cyan terpasang.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
