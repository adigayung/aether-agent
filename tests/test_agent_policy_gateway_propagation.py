"""Tests: Agent Execution Policy propagation Task metadata -> gateway -> runtime.

Membuktikan integrasi praktis (gateway Django) TANPA provider/LLM nyata:

    1. Mode dari metadata task (`agent_mode`/`policy_mode`/`mode`) diteruskan
       ke `TaskExecutor.run` sebagai `requested_mode` (hanya bila executor
       mendukungnya).
    2. Ringkasan policy dari executor disimpan pada TaskRecord
       (requested_mode/effective_mode/reason/escalated).
    3. Task TANPA mode -> `requested_mode` TIDAK dikirim (perilaku lama).
    4. Executor lama yang belum mengenal `requested_mode` tetap dipanggil
       dengan signature lamanya (backward compatible).
    5. Mode tidak valid di metadata -> memoria default `balanced` (tidak error).

Catatan: test ini memakai TaskExecutor palsu (tanpa runtime/LLM nyata) sehingga
deterministik.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PROJECT_ROOT / "src"), str(PROJECT_ROOT / "web" / "django_app")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from api.project_store import ProjectStore  # noqa: E402
from api.services import GatewayService  # noqa: E402


class RecordingExecutor:
    """TaskExecutor palsu: mencatat kwargs run + mengembalikan ringkasan policy."""

    def __init__(self, *, supports_requested_mode: bool = True) -> None:
        self.supports_requested_mode = supports_requested_mode
        self.calls: List[Dict[str, Any]] = []
        self.policy_response: Optional[Dict[str, Any]] = None

    def run(
        self,
        prepared: Any,
        *,
        session_id: str,
        task_id: str,
        on_status: Any,
        requested_mode: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.calls.append({"task_id": task_id, "requested_mode": requested_mode})
        if on_status is not None:
            on_status("running", None, None)
            on_status("completed", "ok", None)
        return {
            "status": "completed",
            "result": "ok",
            "error": None,
            "iterations": 1,
            "policy": self.policy_response,
        }


class LegacyExecutor:
    """TaskExecutor palsu LAMA: signature TANPA `requested_mode` & tanpa **kwargs."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def run(
        self,
        prepared: Any,
        *,
        session_id: str,
        task_id: str,
        on_status: Any,
        workspace_root: Optional[str] = None,
        provider_name: Optional[str] = None,
        model_name: Optional[str] = None,
        provider_instance_id: Optional[str] = None,
        model_id: Optional[str] = None,
        cancel_token: Any = None,
    ) -> Dict[str, Any]:
        self.calls.append({"task_id": task_id})
        if on_status is not None:
            on_status("running", None, None)
            on_status("completed", "ok", None)
        return {"status": "completed", "result": "ok", "error": None, "iterations": 1}


def _wait_terminal(service: GatewayService, task_id: str, timeout: float = 5.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = service.get_task(task_id)
        if rec["status"] in ("completed", "failed", "cancelled"):
            return rec
        time.sleep(0.01)
    return service.get_task(task_id)


def _service(tmp_path: Path, executor: Any) -> GatewayService:
    return GatewayService(
        session_store=__import__("agent_ai.session.store", fromlist=["InMemorySessionStore"]).InMemorySessionStore(),
        task_executor=executor,
        auto_execute=True,
        project_store=ProjectStore(db_path=tmp_path / "gateway.db"),
    )


def test_requested_mode_from_metadata_reaches_executor(tmp_path):
    executor = RecordingExecutor()
    service = _service(tmp_path, executor)
    created = service.create_task("kerjakan sesuatu", metadata={"mode": "fast"})
    record = _wait_terminal(service, created["task_id"])
    assert record["status"] == "completed"
    assert executor.calls and executor.calls[0]["requested_mode"] == "fast"


def test_alias_minimal_normalized_to_fast(tmp_path):
    executor = RecordingExecutor()
    service = _service(tmp_path, executor)
    created = service.create_task("kerjakan", metadata={"mode": "minimal"})
    _wait_terminal(service, created["task_id"])
    assert executor.calls[0]["requested_mode"] == "fast"


def test_no_mode_means_no_requested_mode_kwarg(tmp_path):
    executor = RecordingExecutor()
    service = _service(tmp_path, executor)
    created = service.create_task("kerjakan")
    _wait_terminal(service, created["task_id"])
    assert executor.calls[0]["requested_mode"] is None


def test_invalid_mode_falls_back_to_balanced(tmp_path):
    executor = RecordingExecutor()
    service = _service(tmp_path, executor)
    created = service.create_task("kerjakan", metadata={"mode": "super-mode"})
    _wait_terminal(service, created["task_id"])
    assert executor.calls[0]["requested_mode"] == "balanced"


def test_legacy_executor_without_requested_mode(tmp_path):
    """Executor lama (tanpa parameter requested_mode) tetap bekerja."""
    executor = LegacyExecutor()
    service = _service(tmp_path, executor)
    created = service.create_task("kerjakan", metadata={"mode": "deep"})
    record = _wait_terminal(service, created["task_id"])
    assert record["status"] == "completed"
    assert executor.calls  # dipanggil dengan signature lama (tanpa requested_mode)


def test_policy_summary_stored_on_task_record(tmp_path):
    executor = RecordingExecutor()
    executor.policy_response = {
        "requested_mode": "fast",
        "effective_mode": "deep",
        "reason": "Architecture investigation required",
        "escalated": True,
        "escalations": [
            {"from": "fast", "to": "deep", "reason": "Architecture investigation required", "step": 1}
        ],
    }
    service = _service(tmp_path, executor)
    created = service.create_task("investigasi", metadata={"mode": "fast"})
    record = _wait_terminal(service, created["task_id"])
    assert record["requested_mode"] == "fast"
    assert record["effective_mode"] == "deep"
    assert record["policy_reason"] == "Architecture investigation required"
    assert record["policy_escalated"] is True
    assert record["policy_escalations"][0]["to"] == "deep"
    # Ringkasan ikut terekspos pada GET /api/tasks (list_tasks -> to_dict).
    listed = [t for t in service.list_tasks() if t["task_id"] == created["task_id"]][0]
    assert listed["effective_mode"] == "deep"


def test_task_record_policy_fields_default_none(tmp_path):
    """Task lama (tanpa policy dari executor) -> field policy tetap None."""
    executor = RecordingExecutor()  # policy_response None
    service = _service(tmp_path, executor)
    created = service.create_task("kerjakan")
    record = _wait_terminal(service, created["task_id"])
    assert record["requested_mode"] is None
    assert record["effective_mode"] is None
    assert record["policy_reason"] is None
    assert record["policy_escalated"] is False
    assert record["policy_escalations"] == []
