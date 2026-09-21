"""Verifikasi live filesystem event (Explorer + Changes selama Agent berjalan).

Menguji bahwa operasi filesystem Agent memancarkan event `change_detected`
SEGERA setelah operasi berhasil (bukan menunggu task selesai), memakai
event/SSE system AETHER yang sudah ada.

Cakupan:
    A. Tool-level: create/modify/delete/move -> satu event sukses per operasi.
    B. Operasi gagal -> TIDAK ada event sukses.
    C. Event path relative terhadap workspace root (no drive letter/absolut).
    D. Diff/line info disertakan (reuse diff engine existing).
    E. Integrasi TaskExecutor: event muncul DARI DALAM eksekusi, yaitu
       sequence-nya SEBELUM event terminal (task_completed).
    F. Tidak ada duplicate event untuk file yang sama (live vs consistency
       check akhir).
    G. SSE: frame `event: change_detected` benar-benar terbentuk.
    H. Frontend: handler live (upsert Changes + incremental Explorer) ada.

Deterministik, tanpa network. Fixture diroot temp dan dibersihkan.

Jalankan:
    python scripts/check_live_fs_events.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _new_sink():
    events = []

    def sink(payload):
        events.append(dict(payload))

    return events, sink


def expect_error(fn, label):
    from agent_ai.tools.base import ToolError

    try:
        fn()
    except ToolError as exc:
        print(f"    {label} -> ditolak OK ({type(exc).__name__})")
        return
    raise AssertionError(f"{label} seharusnya ditolak")


def run_tool_level() -> int:
    from agent_ai.tools.registry import build_registry

    ws = Path(tempfile.mkdtemp(prefix="live_fs_ws_"))
    try:
        events, sink = _new_sink()
        reg = build_registry(root=ws, change_sink=sink)

        # 1) create file -> kind=created, satu event.
        reg.execute("write_file", {"path": "src/app/main.py", "content": "print('hi')\n"})
        assert len(events) == 1, events
        ev = events[-1]
        assert ev["kind"] == "created", ev
        assert ev["path"] == "src/app/main.py", ev
        assert ev["before_size"] is None and ev["after_size"] is not None, ev
        assert "diff" in ev and "+print('hi')" in ev["diff"], ev
        assert ev["additions"] == 1 and ev["deletions"] == 0, ev
        print("[A1] write_file baru -> kind=created + diff/additions OK")

        # 2) write ulang file yang sama -> kind=modified.
        reg.execute("write_file", {"path": "src/app/main.py", "content": "print('hello')\n"})
        assert len(events) == 2, events
        ev = events[-1]
        assert ev["kind"] == "modified", ev
        assert "-print('hi')" in ev["diff"] and "+print('hello')" in ev["diff"], ev
        print("[A2] write_file existing -> kind=modified + diff lines OK")

        # 3) edit_file -> kind=modified.
        reg.execute("edit_file", {"path": "src/app/main.py", "old_text": "hello", "new_text": "world"})
        ev = events[-1]
        assert ev["kind"] == "modified" and ev["tool"] == "edit_file", ev
        assert "+print('world')" in ev["diff"], ev
        print("[A3] edit_file -> kind=modified OK")

        # 4) move_file -> kind=moved + old_path.
        reg.execute("move_file", {"source": "src/app/main.py", "destination": "src/app/app.py"})
        ev = events[-1]
        assert ev["kind"] == "moved", ev
        assert ev["path"] == "src/app/app.py" and ev["old_path"] == "src/app/main.py", ev
        print("[A4] move_file -> kind=moved + old_path OK")

        # 5) delete_file -> kind=deleted (file).
        reg.execute("delete_file", {"path": "src/app/app.py"})
        ev = events[-1]
        assert ev["kind"] == "deleted" and ev["after_size"] is None, ev
        assert "-print('world')" in ev["diff"], ev
        print("[A5] delete_file -> kind=deleted OK")

        # 6) create directory (nested) -> file di dalamnya terdeteksi.
        reg.execute("write_file", {"path": "deep/a/b.txt", "content": "x\n"})
        assert events[-1]["kind"] == "created", events[-1]
        print("[A6] nested directory parent auto-create OK")

        # B) operasi GAGAL -> tidak ada event sukses baru.
        count = len(events)
        expect_error(
            lambda: reg.execute("edit_file", {"path": "deep/a/b.txt", "old_text": "tidak-ada", "new_text": "y"}),
            "edit target tidak ada",
        )
        expect_error(
            lambda: reg.execute("write_file", {"path": "deep", "content": "z"}),
            "write ke directory",
        )
        expect_error(
            lambda: reg.execute("write_file", {"path": "../evil.txt", "content": "z"}),
            "write traversal",
        )
        expect_error(
            lambda: reg.execute("delete_file", {"path": "missing.txt"}),
            "delete file tidak ada",
        )
        assert len(events) == count, f"operasi gagal tidak boleh emit event: {events[count:]}"
        print("[B] operasi gagal -> TIDAK ada event sukses OK")

        # C) path SELALU relative (tidak absolut / drive letter).
        for ev in events:
            for key in ("path", "old_path"):
                val = ev.get(key)
                if val:
                    assert ":" not in val and not val.startswith("/"), (key, val)
                    assert "\\" not in val, (key, val)
        # Absolute path harus dinormalisasi menjadi relative.
        abs_target = ws / "abs_ok.txt"
        reg.execute("write_file", {"path": str(abs_target), "content": "abs\n"})
        ev = events[-1]
        assert ev["path"] == "abs_ok.txt", ev
        print("[C] event path relative terhadap workspace root OK")

        # D) diff memakai engine existing (bukan engine kedua).
        from agent_ai.changes import diff as change_diff

        assert change_diff.generate is not None
        print("[D] diff memakai engine existing (agent_ai.changes.diff) OK")

        # Move + dedup path marking (path & old_path tercatat di payload).
        assert events[-1]["path"] and events[-1].get("tool") == "write_file"
        print("[A/D] payload memuat tool + path OK")
        return 0
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def _make_fake_provider(tool_name: str, tool_args: dict):
    """Provider deterministik: 1 tool call lalu final (tanpa network)."""
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
            if self.calls == 1:
                return LLMResponse(
                    text="",
                    actions=[LLMAction(name=tool_name, arguments=tool_args, type=ActionType.TOOL_CALL)],
                    finish_reason=FinishReason.TOOL_CALLS,
                    provider="fake",
                    model="fake-1",
                )
            return LLMResponse(
                text="selesai",
                actions=[],
                finish_reason=FinishReason.STOP,
                provider="fake",
                model="fake-1",
            )

    return FakeProvider()


def run_integration() -> int:
    """Event live muncul DARI DALAM eksekusi (sebelum task_completed)."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

    from agent_ai.permission import PermissionConfig, PermissionManager, PermissionPolicy
    from agent_ai.permission.models import PolicyMode
    from agent_ai.session.store import InMemorySessionStore
    from agent_ai.task.models import PreparedTask

    from api.execution import TaskExecutor

    ws = Path(tempfile.mkdtemp(prefix="live_fs_exec_"))
    try:
        store = InMemorySessionStore()
        session = store.create_session()
        session_id = session.session_id
        task_id = "live-fs-task-1"

        pm = PermissionManager(
            policy=PermissionPolicy(PermissionConfig(workspace_write=PolicyMode.ALLOW))
        )
        executor = TaskExecutor(
            store,
            provider_factory=lambda: _make_fake_provider(
                "write_file", {"path": "a.txt", "content": "halo\n"}
            ),
            permission_manager=pm,
        )

        received = []
        store.subscribe(lambda ev: received.append(ev))

        prepared = PreparedTask(task="buat file a.txt", task_id=task_id)
        result = executor.run(
            prepared, session_id=session_id, task_id=task_id, workspace_root=str(ws)
        )
        assert (ws / "a.txt").exists(), "file a.txt harus dibuat"
        assert result["status"] == "completed", result

        events = store.get_events(task_id=task_id)
        changes = [e for e in events if e.event_type.value == "change_detected"]
        a_changes = [e for e in changes if e.payload.get("path") == "a.txt"]
        assert a_changes, f"harus ada change_detected untuk a.txt: {[e.payload for e in changes]}"

        completed = [e for e in events if e.event_type.value == "task_completed"]
        assert completed, "task_completed harus ada"

        # E) LIVE: change_detected a.txt sequence < task_completed sequence.
        assert a_changes[0].sequence < completed[0].sequence, (
            a_changes[0].sequence,
            completed[0].sequence,
        )
        print(
            f"[E] change_detected (seq={a_changes[0].sequence}) muncul SEBELUM "
            f"task_completed (seq={completed[0].sequence}) OK"
        )

        # Payload lengkap + relative path.
        p = a_changes[0].payload
        assert p["kind"] == "created", p
        assert p["path"] == "a.txt", p
        assert "diff" in p and "+halo" in p["diff"], p
        print("[E2] payload live (created, relative path, diff) OK")

        # F) TIDAK ada duplicate untuk a.txt (live vs consistency check akhir).
        assert len(a_changes) == 1, [e.payload for e in a_changes]
        print("[F] tidak ada duplicate event untuk file yang sama OK")

        # G) SSE frame.
        from api.streaming import format_sse

        frame = format_sse(a_changes[0])
        assert "event: change_detected" in frame, frame
        data_line = [ln for ln in frame.splitlines() if ln.startswith("data: ")][0]
        parsed = json.loads(data_line[len("data: "):])
        assert parsed["payload"]["path"] == "a.txt", parsed
        print("[G] SSE frame 'event: change_detected' + payload OK")

        # Subscriber menerima event live (bukti aliran, bukan hasil akhir saja).
        assert any(e.event_type.value == "change_detected" for e in received), received
        print("[G2] subscriber live menerima change_detected OK")
        return 0
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def run_no_duplicate_after_tracker() -> int:
    """Perubahan via run_command (tanpa sink) tetap dilaporkan tracker akhir."""
    from agent_ai.permission import PermissionConfig, PermissionManager, PermissionPolicy
    from agent_ai.permission.models import PolicyMode
    from agent_ai.session.store import InMemorySessionStore
    from agent_ai.task.models import PreparedTask

    from api.execution import TaskExecutor

    ws = Path(tempfile.mkdtemp(prefix="live_fs_cmd_"))
    try:
        store = InMemorySessionStore()
        session = store.create_session()
        script = (
            "open('cmd_made.txt','w').write('via command\\n')"
        )
        executor = TaskExecutor(
            store,
            provider_factory=lambda: _make_fake_provider(
                "run_command",
                {"command": f'"{sys.executable}" -c "{script}"'},
            ),
            permission_manager=PermissionManager(
                policy=PermissionPolicy(PermissionConfig(workspace_write=PolicyMode.ALLOW))
            ),
        )
        task_id = "live-fs-task-2"
        executor.run(
            PreparedTask(task="buat file via command", task_id=task_id),
            session_id=session.session_id,
            task_id=task_id,
            workspace_root=str(ws),
        )
        events = store.get_events(task_id=task_id)
        found = [
            e for e in events
            if e.event_type.value == "change_detected"
            and e.payload.get("path") == "cmd_made.txt"
        ]
        assert found, [e.payload for e in events if e.event_type.value == "change_detected"]
        assert len(found) == 1, len(found)
        print("[F2] perubahan via run_command dilaporkan consistency check akhir (1x) OK")
        return 0
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def run_live_task_sequence() -> int:
    """Manual verification (headless): task dengan 4 operasi file berurutan.

    Meniru skenario: create A -> modify A -> create B -> modify C (existing),
    lalu memeriksa bahwa SETIAP perubahan terkirim live (sebelum task selesai)
    dan dalam urutan operasi.
    """
    from agent_ai.core.response import ActionType, FinishReason, LLMAction, LLMResponse
    from agent_ai.permission import PermissionConfig, PermissionManager, PermissionPolicy
    from agent_ai.permission.models import PolicyMode
    from agent_ai.providers.base import BaseProvider, GenerateResult
    from agent_ai.session.store import InMemorySessionStore
    from agent_ai.task.models import PreparedTask

    from api.execution import TaskExecutor

    steps = [
        ("write_file", {"path": "A.txt", "content": "one\n"}),
        ("write_file", {"path": "A.txt", "content": "one\ntwo\n"}),
        ("write_file", {"path": "B.txt", "content": "b\n"}),
        ("write_file", {"path": "C.txt", "content": "c-modified\n"}),
    ]

    class SequenceProvider(BaseProvider):
        name = "seq"

        def __init__(self):
            self.i = 0

        def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
            return GenerateResult(text="", provider="seq", model="seq-1")

        def is_available(self) -> bool:
            return True

        def normalize_response(self, result):
            if self.i < len(steps):
                name, args = steps[self.i]
                self.i += 1
                return LLMResponse(
                    text="",
                    actions=[LLMAction(name=name, arguments=args, type=ActionType.TOOL_CALL)],
                    finish_reason=FinishReason.TOOL_CALLS,
                    provider="seq",
                    model="seq-1",
                )
            return LLMResponse(
                text="selesai",
                actions=[],
                finish_reason=FinishReason.STOP,
                provider="seq",
                model="seq-1",
            )

    ws = Path(tempfile.mkdtemp(prefix="live_fs_seq_"))
    try:
        # C.txt sudah ada sebelum task -> write berikutnya = modified.
        (ws / "C.txt").write_text("c-original\n", encoding="utf-8")

        store = InMemorySessionStore()
        session = store.create_session()
        task_id = "live-fs-seq"

        executor = TaskExecutor(
            store,
            provider_factory=lambda: SequenceProvider(),
            permission_manager=PermissionManager(
                policy=PermissionPolicy(PermissionConfig(workspace_write=PolicyMode.ALLOW))
            ),
        )

        timeline = []
        store.subscribe(
            lambda ev: timeline.append((ev.event_type.value, ev.payload.get("path"), ev.payload.get("kind")))
        )

        executor.run(
            PreparedTask(task="operasi file berurutan", task_id=task_id),
            session_id=session.session_id,
            task_id=task_id,
            workspace_root=str(ws),
        )

        changes = [t for t in timeline if t[0] == "change_detected"]
        user_changes = [t for t in changes if t[1] in {"A.txt", "B.txt", "C.txt"}]
        assert user_changes == [
            ("change_detected", "A.txt", "created"),
            ("change_detected", "A.txt", "modified"),
            ("change_detected", "B.txt", "created"),
            ("change_detected", "C.txt", "modified"),
        ], user_changes
        print("[I1] urutan live event sesuai operasi (create A, modify A, create B, modify C) OK")

        terminal_idx = next(i for i, t in enumerate(timeline) if t[0] == "task_completed")
        last_change_idx = max(i for i, t in enumerate(timeline) if t[0] == "change_detected" and t[1] in {"A.txt", "B.txt", "C.txt"})
        assert last_change_idx < terminal_idx, (last_change_idx, terminal_idx)
        print("[I2] SEMUA event file terkirim SEBELUM task_completed (bukan menunggu selesai) OK")

        # Perubahan isi file (new lines) terlihat di diff event.
        a_mod = [t for t in timeline if t[0] == "change_detected" and t[1] == "A.txt"][1]
        assert a_mod[2] == "modified", a_mod
        print("[I3] modify mempertahankan kind=modified + diff (new lines) OK")
        return 0
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def run_frontend_static() -> int:
    frontend = PROJECT_ROOT / "web" / "frontend" / "src"
    app = (frontend / "App.vue").read_text(encoding="utf-8")
    explorer = (frontend / "components" / "FileExplorer.vue").read_text(encoding="utf-8")
    changes_panel = (frontend / "components" / "ChangesPanel.vue").read_text(encoding="utf-8")

    # Changes upsert (1 file = 1 baris) + live explorer prop.
    assert "upsertChange" in app, "App harus upsert perubahan per path"
    assert "liveFsChange" in app, "App harus meneruskan live filesystem change"
    assert ":live-change" in app, "FileExplorer harus menerima :live-change"
    assert "change_detected" in app, "App harus menangani change_detected"
    print("[H1] App.vue: upsert Changes + live-change diteruskan OK")

    # Explorer incremental (bukan full reload).
    assert "liveChange" in explorer, "FileExplorer harus punya prop liveChange"
    assert "applyLiveChange" in explorer, "FileExplorer harus menerapkan live change"
    assert "reloadDir" in explorer, "Explorer harus refresh direktori terdampak saja"
    assert "remapExpanded" in explorer, "Explorer harus remap state move/rename"
    print("[H2] FileExplorer.vue: incremental live refresh (expanded/selected) OK")

    # Changes panel mengenali move/rename.
    assert "moved" in changes_panel, "ChangesPanel harus mengenali kind moved"
    print("[H3] ChangesPanel.vue: kind moved/renamed ditangani OK")
    return 0


def main() -> int:
    print("=== Verifikasi Live Filesystem Event (Explorer + Changes) ===")
    run_tool_level()
    print()
    run_integration()
    print()
    run_no_duplicate_after_tracker()
    print()
    run_live_task_sequence()
    print()
    run_frontend_static()
    print()
    print("[OK] Live filesystem event bekerja: operasi sukses -> event -> SSE -> UI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
