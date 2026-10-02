"""Backend tests for Consultant Session persistence and Gateway endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_ai.consultant.service import ConsultantService, _MAX_CONTEXT_TURNS
from api.services import GatewayService


@pytest.fixture
def tmp_store(tmp_path: Path):
    """Temporary JSON store for ConsultantService tests."""
    return str(tmp_path / "consultant_sessions.json")


def test_create_and_list_sessions(tmp_store):
    svc = ConsultantService(store_path=tmp_store)

    s1 = svc.create_session(project_id="p1", title="First chat")
    s2 = svc.create_session(project_id="p1")
    s3 = svc.create_session(project_id="p2", title="Other project")

    assert s1["project_id"] == "p1"
    assert s1["title"] == "First chat"

    p1 = svc.list_sessions(project_id="p1")
    assert len(p1) == 2

    all_s = svc.list_sessions()
    assert len(all_s) == 3


def test_full_retention_context_bounded(tmp_store):
    """All turns are retained; only bounded recent window is sent to LLM."""
    svc = ConsultantService(store_path=tmp_store)
    session = svc.create_session(project_id="p1")
    sid = session["session_id"]

    for i in range(_MAX_CONTEXT_TURNS * 2 + 10):
        svc._get_session(sid, project_id="p1").add("user", f"msg {i}")
        svc._get_session(sid, project_id="p1").add("consultant", f"reply {i}")

    session_obj = svc._get_session(sid, project_id="p1")
    assert len(session_obj.turns) == (_MAX_CONTEXT_TURNS * 2 + 10) * 2

    task = session_obj.build_task("next")
    # Only bounded recent window (_MAX_CONTEXT_TURNS pairs = 12 user turns)
    assert task.count("User:") == _MAX_CONTEXT_TURNS
    assert "msg 0" not in task
    assert "next" in task


def test_persistence_across_restart(tmp_store):
    svc1 = ConsultantService(store_path=tmp_store)
    s = svc1.create_session(project_id="p1")
    sid = s["session_id"]

    sess_obj = svc1._get_session(sid, project_id="p1")
    sess_obj.add("user", "hello")
    sess_obj.add("consultant", "hi there")

    # Simulate restart with a fresh service instance
    svc2 = ConsultantService(store_path=tmp_store)

    loaded = svc2.get_session(sid, project_id="p1")
    assert loaded is not None
    assert loaded["title"] == "hello"
    assert len(loaded["turns"]) == 2
    assert loaded["turns"][0]["role"] == "user"
    assert loaded["turns"][0]["text"] == "hello"


def test_delete_session(tmp_store):
    svc = ConsultantService(store_path=tmp_store)
    s = svc.create_session(project_id="p1")
    sid = s["session_id"]
    assert svc.delete_session(sid, project_id="p1")
    assert svc.get_session(sid, project_id="p1") is None
    # Also persisted
    svc2 = ConsultantService(store_path=tmp_store)
    assert svc2.get_session(sid, project_id="p1") is None


def test_rename_session(tmp_store):
    svc = ConsultantService(store_path=tmp_store)
    s = svc.create_session(project_id="p1", title="Old")
    sid = s["session_id"]

    updated = svc.rename_session(sid, "New title", project_id="p1")
    assert updated["title"] == "New title"

    svc2 = ConsultantService(store_path=tmp_store)
    loaded = svc2.get_session(sid, project_id="p1")
    assert loaded["title"] == "New title"


class DummyProjectStore:
    def __init__(self, project_id=None):
        self._active = project_id

    def get_active_project_id(self):
        return self._active

    def list_projects(self):
        return []


class _TestGateway(GatewayService):
    def __init__(self, consultant_service, project_id=None):
        self._consultant_service = consultant_service
        self.project_store = DummyProjectStore(project_id=project_id)
        self._sessions = None
        self._llm_config_service = None
        self._github_backup_service = None


def test_gateway_pass_through(tmp_store):
    consultant = ConsultantService(store_path=tmp_store)
    gateway = _TestGateway(consultant, project_id="p1")

    session = gateway.create_consultant_session(project_id="p1", title="Gateway chat")
    sid = session["session_id"]

    listed = gateway.list_consultant_sessions(project_id="p1")
    assert len(listed) == 1

    loaded = gateway.get_consultant_session(sid, project_id="p1")
    assert loaded["session_id"] == sid

    renamed = gateway.rename_consultant_session(sid, "Renamed", project_id="p1")
    assert renamed["title"] == "Renamed"

    assert gateway.delete_consultant_session(sid, project_id="p1")
    assert gateway.get_consultant_session(sid, project_id="p1") is None


def test_views_list_create_get(tmp_store):
    from unittest.mock import MagicMock
    import os
    import sys
    import django
    from pathlib import Path
    from django.conf import settings
    from django.test import Client
    import api.views as views

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web" / "django_app"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
    if "testserver" not in settings.ALLOWED_HOSTS:
        settings.ALLOWED_HOSTS = ["testserver", "127.0.0.1", "localhost"] + list(settings.ALLOWED_HOSTS)
    django.setup()

    consultant = ConsultantService(store_path=tmp_store)
    mock_project_store = MagicMock()
    mock_project_store.get_active_project_id.return_value = "p1"
    gateway = GatewayService(
        project_store=mock_project_store,
        project_registry=MagicMock(),
        task_preparation=MagicMock(),
        session_store=MagicMock(),
        consultant_service=consultant,
    )

    orig = views.get_service
    views.get_service = lambda: gateway
    client = Client()
    try:
        # Create
        resp = client.post(
            "/api/consultant/sessions",
            data=json.dumps({"project_id": "p1", "title": "Api chat"}),
            content_type="application/json",
        )
        assert resp.status_code == 201
        data = resp.json()
        sid = data["session_id"]
        assert data["title"] == "Api chat"

        # List
        resp = client.get("/api/consultant/sessions?project_id=p1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["sessions"]) == 1
        assert data["sessions"][0]["session_id"] == sid
        assert "turns" not in data["sessions"][0]

        # Get
        resp = client.get(f"/api/consultant/sessions/{sid}?project_id=p1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == sid

        # Rename (PATCH)
        resp = client.patch(
            f"/api/consultant/sessions/{sid}?project_id=p1",
            data=json.dumps({"title": "Renamed chat"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Renamed chat"

        # Delete
        resp = client.delete(f"/api/consultant/sessions/{sid}?project_id=p1")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # Verify not found after delete
        resp = client.get(f"/api/consultant/sessions/{sid}?project_id=p1")
        assert resp.status_code == 404
    finally:
        views.get_service = orig


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
