"""Regression tests: file-change events must reach the Changes list.

Bug yang dikunci di sini (regression "file diedit tidak muncul di Changes"):
`change_detected` sebelum ini HANYA di-emit ke SessionStore in-memory (untuk SSE
live) dan TIDAK pernah ditulis ke Task Log project-local
(`.aether/log/<task_id>.log`). Karena Activity API (sumber rekonstruksi daftar
Changes) hanya membaca Task Log, daftar Changes HILANG setelah task selesai /
dibuka kembali.

Test ini memverifikasi (jalur nyata TaskExecutor, tanpa network):
  1. edit/write sukses -> `change_detected` TERSIMPAN ke Task Log (per task_id).
  2. operasi GAGAL -> TIDAK ada event perubahan (baik SessionStore maupun log).
  3. dedup: satu operasi sukses = satu event (live vs consistency-check akhir
     tidak menduplikasi file yang sama).
  4. rekonstruksi ala frontend (TaskLogReader.get_activity) menemukan file yang
     diedit -> daftar Changes dapat dibangun ulang setelah task selesai.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
import sys

for _p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

from agent_ai.core.response import ActionType, FinishReason, LLMAction, LLMResponse
from agent_ai.permission import PermissionConfig, PermissionManager, PermissionPolicy
from agent_ai.permission.models import PolicyMode
from agent_ai.projects.aether_store import TaskLogReader
from agent_ai.providers.base import BaseProvider, GenerateResult
from agent_ai.session.store import InMemorySessionStore
from agent_ai.task.models import PreparedTask

from api.execution import TaskExecutor


def _make_provider(tool_name: str, tool_args: Dict[str, Any], fail_first: bool = False):
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


def _run_task(workspace: Path, tool_name: str, tool_args: Dict[str, Any], task_id: str, fail_first: bool = False):
    store = InMemorySessionStore()
    session = store.create_session()
    session_id = session.session_id
    pm = PermissionManager(policy=PermissionPolicy(PermissionConfig(workspace_write=PolicyMode.ALLOW)))
    executor = TaskExecutor(
        store,
        provider_factory=lambda: _make_provider(tool_name, tool_args, fail_first),
        permission_manager=pm,
    )
    prepared = PreparedTask(task="task perubahan file", task_id=task_id)
    result = executor.run(prepared, session_id=session_id, task_id=task_id, workspace_root=str(workspace))
    events = store.get_events(task_id=task_id)
    session_changes = [e for e in events if e.event_type.value == "change_detected"]
    log_reader = TaskLogReader(workspace, task_id=task_id)
    log_changes = [e for e in log_reader.get_activity() if e.get("event") == "change_detected"]
    return result, session_changes, log_changes


def test_successful_edit_is_persisted_to_task_log(tmp_path: Path) -> None:
    """edit_file sukses -> change_detected tersimpan di Task Log (bukan hanya SSE)."""
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")

    result, session_changes, log_changes = _run_task(
        workspace, "edit_file", {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"}, "task-edit"
    )

    assert result["status"] == "completed", result
    assert (workspace / "app.py").read_text(encoding="utf-8") == "value = 2\n"

    # LIVE (SSE): tetap ada.
    assert any(e.payload.get("path") == "app.py" for e in session_changes), session_changes

    # PERSISTEN (Activity API): harus ada -> daftar Changes bisa direkonstruksi.
    paths = [e.get("data", {}).get("path") for e in log_changes]
    assert "app.py" in paths, f"change_detected harus tersimpan di Task Log: {log_changes}"


def test_write_then_edit_both_files_persisted(tmp_path: Path) -> None:
    """Dua file berbeda (write + edit) -> keduanya muncul di Task Log."""
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "existing.txt").write_text("old\n", encoding="utf-8")

    _result, _session, log_changes = _run_task(
        workspace, "write_file", {"path": "new.txt", "content": "hi\n"}, "task-write"
    )
    paths = [e.get("data", {}).get("path") for e in log_changes]
    assert "new.txt" in paths, log_changes

    _result2, _session2, log_changes2 = _run_task(
        workspace, "edit_file", {"path": "existing.txt", "old_text": "old", "new_text": "new"}, "task-edit2"
    )
    paths2 = [e.get("data", {}).get("path") for e in log_changes2]
    assert "existing.txt" in paths2, log_changes2


def test_failed_edit_reports_no_change(tmp_path: Path) -> None:
    """edit_file GAGAL -> TIDAK ada event perubahan (SessionStore atau Task Log)."""
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")

    _result, session_changes, log_changes = _run_task(
        workspace,
        "edit_file",
        {"path": "app.py", "old_text": "TIDAK_ADA", "new_text": "x"},
        "task-fail",
    )
    assert session_changes == [], f"operasi gagal tidak boleh emit change: {session_changes}"
    assert log_changes == [], f"operasi gagal tidak boleh tercatat sebagai perubahan: {log_changes}"


def test_same_file_not_duplicated_in_task_log(tmp_path: Path) -> None:
    """Dedup: satu file yang diedit live TIDAK diduplikasi oleh consistency-check."""
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")

    _result, _session, log_changes = _run_task(
        workspace, "edit_file", {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"}, "task-dedup"
    )
    same = [e for e in log_changes if e.get("data", {}).get("path") == "app.py"]
    assert len(same) == 1, f"file yang sama harus satu event (dedup): {log_changes}"


def test_activity_api_reconstruction_finds_edited_file(tmp_path: Path) -> None:
    """Rekonstruksi ala frontend dari Activity API menemukan file yang diedit."""
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")

    _run_task(
        workspace, "edit_file", {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"}, "task-reopen"
    )

    activity = TaskLogReader(workspace, task_id="task-reopen").get_activity()
    # Meniru loadChangesFromEvents(): baca kunci `event`/`data` (bentuk log).
    reconstructed = [
        (ev.get("data") or {}).get("path")
        for ev in activity
        if (ev.get("event_type") or ev.get("event")) == "change_detected"
        and not str((ev.get("payload") or ev.get("data") or {}).get("path", "")).startswith(".aether")
    ]
    assert "app.py" in reconstructed, f"daftar Changes harus dapat direkonstruksi: {activity}"
