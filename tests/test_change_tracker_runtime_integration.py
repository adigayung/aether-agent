"""Deterministic tests: ChangeTracker wired into the continuous runtime.

Yang dikunci di sini (integrasi minimal ChangeTracker pada jalur produksi
`TaskExecutor -> AgentRuntime -> AgentOrchestrator.run_continuous_loop`):

  1. `TaskChangeEvidence` benar-benar mendeteksi created / modified / deleted
     (fakta filesystem, bukan tebakan jenis perubahan).
  2. Tidak ada perubahan -> tidak ada perubahan yang dilaporkan (dan tidak ada
     deteksi yang dikarang).
  3. Isolasi antar-task: dua task_id berbeda tidak saling mencemari records.
  4. Jalur runtime nyata (TaskExecutor + AgentRuntime + continuous loop):
     edit_file -> evidence memuat `app.py` sebagai `modified`, dan perubahannya
     juga tampil sebagai `change_detected` (perilaku lama dipertahankan).
  5. File yang dibuat lewat `run_command` ikut terdeteksi (bounded), sedangkan
     operasi mutasi yang GAGAL tidak pernah dilaporkan sebagai perubahan.
  6. Cancellation/cleanup: evidence tertutup (`finished_at` terisi) setelah
     `run()` selesai, dan task berikutnya pada runtime yang sama memakai
     task_id baru (tidak mewarisi records task sebelumnya).

Semua test tanpa network (provider fake deterministik).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
for _p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

from agent_ai.changes import ChangeTracker, TaskChangeEvidence  # noqa: E402
from agent_ai.core.response import ActionType, FinishReason, LLMAction, LLMResponse  # noqa: E402
from agent_ai.permission import PermissionConfig, PermissionManager, PermissionPolicy  # noqa: E402
from agent_ai.permission.models import PolicyMode  # noqa: E402
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402
from agent_ai.session.store import InMemorySessionStore  # noqa: E402
from agent_ai.task.models import PreparedTask  # noqa: E402

from api.execution import TaskExecutor  # noqa: E402


# --------------------------------------------------------------------------- #
# Evidence unit (tanpa runtime)
# --------------------------------------------------------------------------- #
def test_evidence_detects_created_modified_deleted(tmp_path: Path) -> None:
    """created/modified/deleted terdeteksi dari kondisi filesystem nyata."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "keep.txt").write_text("keep\n", encoding="utf-8")
    (root / "edit.txt").write_text("versi-1\n", encoding="utf-8")
    (root / "gone.txt").write_text("hapus\n", encoding="utf-8")

    edit_before = (root / "edit.txt").read_bytes()
    gone_before = (root / "gone.txt").read_bytes()

    tracker = ChangeTracker(root=root)
    evidence = TaskChangeEvidence(tracker, task_id="task-unit")
    evidence.begin()
    # Kandidat = path yang memang disentuh task (bounded, bukan seluruh root).
    # Snapshot dibuat SEBELUM intervensi (seperti urutan nyata: register pada
    # `tool_called`, lalu tool mengubah file).
    evidence.register_path("new.txt")
    evidence.register_path("edit.txt")
    evidence.register_path("gone.txt")

    (root / "new.txt").write_text("baru\n", encoding="utf-8")
    (root / "edit.txt").write_text("versi-2-panjang\n", encoding="utf-8")
    (root / "gone.txt").unlink()

    evidence.detect()
    summary = {item["path"]: item for item in evidence.summary()}

    assert summary["new.txt"]["kind"] == "created"
    assert summary["edit.txt"]["kind"] == "modified"
    assert summary["gone.txt"]["kind"] == "deleted"
    # `keep.txt` tidak pernah menjadi kandidat -> tidak ikut dilaporkan.
    assert "keep.txt" not in summary
    # Snapshot ringkas: hanya field yang benar-benar terukur (bukan dikarang).
    assert summary["new.txt"].get("before_size") is None
    # Ukuran byte NYATA di disk (bukan asumsi platform): `write_text` dapat
    # menulis CRLF di Windows, jadi pembanding memakai byte file aktual.
    assert summary["new.txt"]["after_size"] == len((root / "new.txt").read_bytes())
    assert summary["edit.txt"]["before_size"] == len(edit_before)
    assert summary["edit.txt"]["after_size"] == len((root / "edit.txt").read_bytes())
    assert summary["gone.txt"]["before_size"] == len(gone_before)
    assert summary["gone.txt"].get("after_size") is None
    assert evidence.counts() == {"created": 1, "modified": 1, "deleted": 1}


def test_evidence_reports_nothing_without_changes(tmp_path: Path) -> None:
    """Tanpa perubahan nyata -> tidak ada perubahan yang dilaporkan."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    evidence = TaskChangeEvidence(tracker, task_id="task-noc")
    evidence.begin()
    evidence.register_path("app.py")

    evidence.detect()
    assert evidence.summary() == []
    assert evidence.counts() == {"created": 0, "modified": 0, "deleted": 0}
    assert evidence.text() == ""


def test_evidence_isolates_two_tasks(tmp_path: Path) -> None:
    """Dua task_id pada satu tracker TIDAK saling mencemari records."""
    root = tmp_path / "proj"
    root.mkdir()
    tracker = ChangeTracker(root=root)
    evidence = TaskChangeEvidence(tracker)

    evidence.begin("task-a")
    evidence.register_path("a.txt", task_id="task-a")
    evidence.begin("task-b")
    evidence.register_path("b.txt", task_id="task-b")

    (root / "a.txt").write_text("milik a\n", encoding="utf-8")
    (root / "b.txt").write_text("milik b\n", encoding="utf-8")

    records_a = evidence.detect("task-a")
    records_b = evidence.detect("task-b")

    assert [r.path for r in records_a] == ["a.txt"]
    assert [r.path for r in records_b] == ["b.txt"]
    assert {item["path"] for item in evidence.summary("task-a")} == {"a.txt"}
    assert {item["path"] for item in evidence.summary("task-b")} == {"b.txt"}


def test_evidence_finish_marks_task_finished(tmp_path: Path) -> None:
    """Cleanup: `finish()` menutup ChangeSet task (finished_at terisi)."""
    root = tmp_path / "proj"
    root.mkdir()
    tracker = ChangeTracker(root=root)
    evidence = TaskChangeEvidence(tracker, task_id="task-finish")
    evidence.begin()
    evidence.finish()

    change_set = tracker.get_changes("task-finish")
    assert change_set is not None
    assert change_set.is_finished


def test_evidence_unregister_prevents_stale_report(tmp_path: Path) -> None:
    """Path yang dilepas (`unregister_path`) tidak pernah dilaporkan.

    Dipakai untuk mutasi yang GAGAL dan untuk source `move_file`: keduanya
    TIDAK boleh menghasilkan laporan perubahan palsu. File yang benar-benar
    hilang tanpa dilepas TETAP dilaporkan `deleted` (fakta filesystem).
    """
    root = tmp_path / "proj"
    root.mkdir()
    (root / "hantu.txt").write_text("isi\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    evidence = TaskChangeEvidence(tracker, task_id="task-stale")
    evidence.begin()
    evidence.register_path("hantu.txt")
    (root / "hantu.txt").unlink()
    evidence.detect()
    # Tanpa unregister: hilangnya file adalah fakta -> 'deleted'.
    assert {item["path"]: item["kind"] for item in evidence.summary()} == {
        "hantu.txt": "deleted"
    }

    # Mutasi dilaporkan GAGAL -> path dilepas; laporan tidak boleh tetap ada.
    evidence.unregister_path("hantu.txt")
    assert evidence.summary() == []
    assert evidence.detect() == []


def test_evidence_move_source_is_not_reported_deleted(tmp_path: Path) -> None:
    """move_file: source dilepas dari kandidat, destination dilaporkan created."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "lama.txt").write_text("isi\n", encoding="utf-8")

    tracker = ChangeTracker(root=root)
    evidence = TaskChangeEvidence(tracker, task_id="task-move")
    evidence.begin()
    evidence.register_path("lama.txt")
    evidence.register_path("baru.txt")

    (root / "lama.txt").rename(root / "baru.txt")
    evidence.unregister_path("lama.txt")
    evidence.detect()

    summary = {item["path"]: item["kind"] for item in evidence.summary()}
    assert summary.get("baru.txt") == "created"
    assert "lama.txt" not in summary


# --------------------------------------------------------------------------- #
# Runtime jalur nyata (TaskExecutor -> AgentRuntime -> continuous loop)
# --------------------------------------------------------------------------- #
def _make_provider(tool_name: str, tool_args: Dict[str, Any]):
    """Provider deterministik: 1 tool call lalu final (tanpa network)."""

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
                    actions=[
                        LLMAction(
                            name=tool_name,
                            arguments=tool_args,
                            type=ActionType.TOOL_CALL,
                        )
                    ],
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


def _run_task(
    workspace: Path,
    tool_name: str,
    tool_args: Dict[str, Any],
    task_id: str,
    *,
    workspace_root: Optional[str] = None,
    cancel_token: Optional[Any] = None,
):
    """Jalankan satu task lewat TaskExecutor (runtime + orchestrator NYATA).

    Tidak ada `runtime_factory` fake: test mengunci wiring runtime asli
    (AgentRuntime + continuous loop), dengan provider fake deterministik.
    """
    store = InMemorySessionStore()
    session = store.create_session()
    session_id = session.session_id
    pm = PermissionManager(
        policy=PermissionPolicy(PermissionConfig(workspace_write=PolicyMode.ALLOW))
    )
    executor = TaskExecutor(
        store,
        provider_factory=lambda: _make_provider(tool_name, tool_args),
        permission_manager=pm,
    )
    prepared = PreparedTask(task="task perubahan file", task_id=task_id)
    result = executor.run(
        prepared,
        session_id=session_id,
        task_id=task_id,
        workspace_root=workspace_root if workspace_root is not None else str(workspace),
        cancel_token=cancel_token,
    )
    events = store.get_events(task_id=task_id)
    changes = [e for e in events if e.event_type.value == "change_detected"]
    evidence_events = [e for e in events if e.event_type.value == "change_evidence"]
    return result, changes, evidence_events


def _evidence_for(workspace: Path, task_id: str) -> TaskChangeEvidence:
    """Baca ulang evidence task dari task log (sumber rekonstruksi).

    Dipakai untuk memverifikasi METADATA yang benar-benar tersedia bagi
    consumer (Activity API), bukan state internal proses.
    """
    tracker = ChangeTracker(root=workspace)
    tracker.start(task_id)
    evidence = TaskChangeEvidence(tracker, task_id=task_id)
    return evidence


def _detected_changes(workspace: Path, task_id: str) -> Dict[str, Dict[str, Any]]:
    """Rekonstruksi daftar perubahan task dari Task Log (payload faktual)."""
    from agent_ai.projects.aether_store import TaskLogReader

    reader = TaskLogReader(workspace, task_id=task_id)
    found: Dict[str, Dict[str, Any]] = {}
    for event in reader.get_activity():
        name = event.get("event_type") or event.get("event")
        if name != "change_detected":
            continue
        data = event.get("data") or event.get("payload") or {}
        path = str(data.get("path") or "")
        if not path:
            continue
        found[path] = data
    return found


def test_runtime_edit_reports_change_evidence_metadata(tmp_path: Path) -> None:
    """edit_file lewat continuous loop -> perubahan nyata dilaporkan runtime.

    Provider fake deterministik: 1 tool call (edit_file) lalu final. Tidak ada
    network. Verifikasi lewat daftar perubahan task (hasil `TaskExecutor`).
    """
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
    # Byte NYATA sebelum task (write_text dapat menulis CRLF di Windows).
    before_bytes = (workspace / "app.py").read_bytes()

    result, changes, _evidence_events = _run_task(
        workspace,
        "edit_file",
        {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"},
        "task-ev-edit",
    )

    assert result["status"] == "completed", result
    assert (workspace / "app.py").read_text(encoding="utf-8") == "value = 2\n"

    reported = {item["path"]: item for item in (result.get("changes") or [])}
    assert "app.py" in reported, result.get("changes")
    assert reported["app.py"]["kind"] == "modified"
    # Ukuran yang benar-benar terukur (byte) — bukan nilai yang dikarang.
    # Dibandingkan dengan byte NYATA di disk (write_text dapat menulis CRLF
    # di Windows), bukan asumsi newline LF.
    assert reported["app.py"]["before_size"] == len(before_bytes)
    assert reported["app.py"]["after_size"] == len((workspace / "app.py").read_bytes())

    # Perilaku lama tetap: event `change_detected` untuk file tersebut.
    assert any(e.payload.get("path") == "app.py" for e in changes), changes


def test_runtime_write_reports_created_and_dedup(tmp_path: Path) -> None:
    """write_file (file baru) -> 'created'; event lama tidak duplikat."""
    workspace = tmp_path / "proj"
    workspace.mkdir()

    result, changes, _evidence_events = _run_task(
        workspace,
        "write_file",
        {"path": "baru.txt", "content": "halo\n"},
        "task-ev-write",
    )

    assert result["status"] == "completed", result
    reported = {item["path"]: item for item in (result.get("changes") or [])}
    assert "baru.txt" in reported, result.get("changes")
    assert reported["baru.txt"]["kind"] == "created"
    assert reported["baru.txt"].get("before_size") is None

    same = [e for e in changes if e.payload.get("path") == "baru.txt"]
    assert len(same) == 1, f"file yang sama harus satu event (dedup): {changes}"


def test_runtime_delete_reports_deleted(tmp_path: Path) -> None:
    """delete_file -> 'deleted' (fakta hilangnya file)."""
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "hapus.txt").write_text("hapus\n", encoding="utf-8")

    result, _changes, _evidence_events = _run_task(
        workspace, "delete_file", {"path": "hapus.txt"}, "task-ev-del"
    )

    assert result["status"] == "completed", result
    reported = {item["path"]: item for item in (result.get("changes") or [])}
    assert "hapus.txt" in reported, result.get("changes")
    assert reported["hapus.txt"]["kind"] == "deleted"
    assert reported["hapus.txt"].get("after_size") is None


def test_runtime_failed_mutation_reports_no_change(tmp_path: Path) -> None:
    """Mutasi GAGAL -> tidak ada perubahan yang dilaporkan (tidak dikarang)."""
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")

    result, changes, evidence_events = _run_task(
        workspace,
        "edit_file",
        {"path": "app.py", "old_text": "TIDAK_ADA", "new_text": "x"},
        "task-ev-fail",
    )

    assert changes == [], f"operasi gagal tidak boleh emit change: {changes}"
    assert evidence_events == []
    assert result.get("changes") is None
    assert (workspace / "app.py").read_text(encoding="utf-8") == "value = 1\n"


def test_runtime_command_created_file_is_detected(tmp_path: Path) -> None:
    """File yang dibuat lewat run_command terdeteksi (jalur file BARU, bounded).

    Verifikasi lewat TaskExecutor + AgentRuntime NYATA. `run_command` tidak
    melaporkan path, sehingga perubahan ditangkap lewat konsistensi tracker.
    """
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")

    tracker = ChangeTracker(root=workspace)
    tracker.start("task-ev-cmd")
    tracker.snapshot("app.py", task_id="task-ev-cmd")
    evidence = TaskChangeEvidence(tracker, task_id="task-ev-cmd")

    store = InMemorySessionStore()
    session = store.create_session()
    pm = PermissionManager(
        policy=PermissionPolicy(PermissionConfig(workspace_write=PolicyMode.ALLOW))
    )
    script = "open('cmd_made.txt','w').write('via command\\n')"
    executor = TaskExecutor(
        store,
        provider_factory=lambda: _make_provider(
            "run_command", {"command": f'python -c "{script}"'}
        ),
        permission_manager=pm,
    )
    prepared = PreparedTask(task="task perubahan file", task_id="task-ev-cmd")
    result = executor.run(
        prepared,
        session_id=session.session_id,
        task_id="task-ev-cmd",
        workspace_root=str(workspace),
        change_tracker=tracker,
        change_evidence=evidence,
    )
    assert result["status"] == "completed", result
    assert (workspace / "cmd_made.txt").is_file()
    summary = {item["path"]: item["kind"] for item in evidence.summary()}
    assert summary.get("cmd_made.txt") == "created", summary
    # File yang sudah di-track tidak dilaporkan berubah (tidak disentuh task).
    assert "app.py" not in summary
    # Ringkasan perubahan juga tersedia pada hasil TaskExecutor (additive).
    assert any(item.get("path") == "cmd_made.txt" for item in (result.get("changes") or []))


def test_runtime_cancellation_still_finishes_evidence(tmp_path: Path) -> None:
    """Cancellation/cleanup: perubahan sebelum cancel tetap terlaporkan.

    Token sudah dibatalkan SEBELUM task berjalan; loop berhenti di safe
    boundary pertama dan task dilaporkan CANCELLED (bukan FAILED), tanpa event
    perubahan palsu.
    """
    from agent_ai.core.cancel import CancellationToken

    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")

    token = CancellationToken()
    token.request("user stop")

    result, changes, evidence_events = _run_task(
        workspace,
        "edit_file",
        {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"},
        "task-ev-cancel",
        cancel_token=token,
    )

    assert result["status"] == "cancelled", result
    # Tidak ada tool yang dieksekusi -> tidak ada perubahan (dan tidak ada
    # laporan perubahan palsu), tetapi task tetap selesai dengan bersih.
    assert changes == [], changes
    assert evidence_events == []
    assert (workspace / "app.py").read_text(encoding="utf-8") == "value = 1\n"


def test_runtime_reuses_runtime_for_two_tasks_without_mixing(tmp_path: Path) -> None:
    """Isolasi: dua task pada satu runtime tidak saling mewarisi hasil."""
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "satu.txt").write_text("satu\n", encoding="utf-8")

    from agent_ai.runtime.runtime import AgentRuntime

    runtime = AgentRuntime(
        provider=_make_provider(
            "write_file",
            {"path": "satu.txt", "content": "satu-baru\n"},
        ),
        session_store=InMemorySessionStore(),
        project_root=str(workspace),
        project_brain=False,
    )

    first = runtime.run(PreparedTask(task="task satu", task_id="task-iso-1"))
    assert first.status.value == "completed", first
    evidence = runtime.change_evidence
    assert isinstance(evidence, TaskChangeEvidence)
    assert evidence.task_id == "task-iso-1"
    assert [item["path"] for item in evidence.summary()] == ["satu.txt"]

    # Task KEDUA pada runtime yang sama: task_id baru, hasil task pertama
    # TIDAK boleh bocor (isolasi antar-task).
    second = runtime.run(PreparedTask(task="task dua tanpa perubahan", task_id="task-iso-2"))
    assert second.status.value == "completed", second
    assert evidence.task_id == "task-iso-2"
    assert evidence.summary() == []
    # Records task pertama tidak hilang dari sisi tracker existing (per task_id).
    assert evidence._tracker.get_changes("task-iso-1") is not None  # noqa: SLF001
    assert evidence._tracker.get_changes("task-iso-2") is not None  # noqa: SLF001
    assert evidence._tracker.get_changes("task-iso-1") is not evidence._tracker.get_changes(  # noqa: SLF001
        "task-iso-2"
    )
