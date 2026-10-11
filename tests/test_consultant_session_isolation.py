"""Tests: Consultant session ISOLATION per active project (P0).

Membuktikan kebocoran sesi Consultant lintas-project DIPERBAIKI:

    1. Sesi disimpan PROJECT-LOCAL di
       ``<project_path>/.aether/consultant/sessions.json`` (satu file per project).
    2. Semua operasi (list/get/create/rename/delete/reset/resume) WAJIB ter-scope
       satu project; tanpa project yang jelas -> error, BUKAN daftar global.
    3. Tidak ada fallback lintas-project (``_get_session``/``_find_session``):
       sesi project lain TIDAK bisa dipulihkan.
    4. Project switching dalam SATU proses memakai storage yang benar.
    5. Persistensi setelah restart (instance service baru).
    6. Migrasi legacy GLOBAL idempoten: hanya sesi dengan project_id yang
       TERVERIFIKASI lewat registry dipetakan; sisanya menjadi orphan yang TIDAK
       dihapus/ditebak.

Deterministik: memakai temporary directories (tmp_path), tanpa data produksi.
`api.services.GatewayService` diuji untuk scoping gateway (tanpa LLM nyata).

Jalankan:
    set PYTHONPATH=src;web\\django_app && python -m pytest tests/test_consultant_session_isolation.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
WEB_DIR = PROJECT_ROOT / "web" / "django_app"
for _p in (str(SRC_DIR), str(WEB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_ai.consultant.service import (  # noqa: E402
    ConsultantProjectScope,
    ConsultantScopeError,
    ConsultantService,
)
from agent_ai.consultant.store import (  # noqa: E402
    ConsultantSessionStore,
    project_sessions_path,
    read_legacy_sessions,
)
from agent_ai.consultant.migration import audit_legacy_migration  # noqa: E402


class FakeProjectScope(ConsultantProjectScope):
    """Scope in-memory: peta project_id -> root (mensimulasikan registry)."""

    def __init__(self, projects, active=None):
        self._projects = dict(projects)
        self._active = active

    def active_project_id(self):
        return self._active

    def project_root(self, project_id):
        return self._projects.get(project_id)

    def registered_project_ids(self):
        return list(self._projects.keys())


@pytest.fixture
def two_projects(tmp_path: Path):
    """Dua project dengan root terpisah + scope yang mengenali keduanya."""
    root_a = tmp_path / "proj_a"
    root_b = tmp_path / "proj_b"
    root_a.mkdir()
    root_b.mkdir()
    scope = FakeProjectScope({"pa": str(root_a), "pb": str(root_b)}, active="pa")
    return {"pa": str(root_a), "pb": str(root_b), "scope": scope}


# --------------------------------------------------------------------------- #
# 1) Storage per project + list/get scoped
# --------------------------------------------------------------------------- #
def test_sessions_are_stored_per_project(two_projects):
    scope = two_projects["scope"]
    svc = ConsultantService(project_scope=scope)

    sa = svc.create_session(project_id="pa", title="Chat A")
    sb = svc.create_session(project_id="pb", title="Chat B")

    # Dua file terpisah, bukan satu file global.
    path_a = project_sessions_path(two_projects["pa"])
    path_b = project_sessions_path(two_projects["pb"])
    assert Path(path_a).exists()
    assert Path(path_b).exists()
    assert path_a != path_b

    # List per-project HANYA mengembalikan sesi project itu.
    list_a = svc.list_sessions(project_id="pa")
    list_b = svc.list_sessions(project_id="pb")
    assert [s["session_id"] for s in list_a] == [sa["session_id"]]
    assert [s["session_id"] for s in list_b] == [sb["session_id"]]

    # GET lintas-project TIDAK mungkin: id A pada project B -> None.
    assert svc.get_session(sa["session_id"], project_id="pb") is None
    assert svc.get_session(sb["session_id"], project_id="pa") is None

    # File A tidak memuat sesi B (dan sebaliknya).
    raw_a = json.loads(Path(path_a).read_text(encoding="utf-8"))
    assert [s["session_id"] for s in raw_a["sessions"]] == [sa["session_id"]]


def test_list_without_project_raises_error(two_projects):
    """project_id kosong TANPA active project -> error jelas (bukan global)."""
    scope_no_active = FakeProjectScope({"pa": two_projects["pa"]}, active=None)
    svc = ConsultantService(project_scope=scope_no_active)
    svc.create_session(project_id="pa")

    with pytest.raises(ConsultantScopeError):
        svc.list_sessions()
    with pytest.raises(ConsultantScopeError):
        svc.get_session("whatever")
    with pytest.raises(ConsultantScopeError):
        svc.create_session()
    with pytest.raises(ConsultantScopeError):
        svc.delete_session("whatever")


def test_active_project_fallback_is_safe(two_projects):
    """project_id kosong -> fallback ke active project (bila tersedia)."""
    scope = two_projects["scope"]  # active = "pa"
    svc = ConsultantService(project_scope=scope)

    created = svc.create_session(title="Via active")  # tanpa project_id
    assert created["project_id"] == "pa"
    assert svc.list_sessions()[0]["session_id"] == created["session_id"]


def test_unknown_project_raises_scope_error(two_projects):
    """project_id yang tidak terdaftar -> error (tidak menebak root)."""
    svc = ConsultantService(project_scope=two_projects["scope"])
    with pytest.raises(ConsultantScopeError):
        svc.create_session(project_id="ghost")
    with pytest.raises(ConsultantScopeError):
        svc.list_sessions(project_id="ghost")


# --------------------------------------------------------------------------- #
# 2) Resume penuh + delete/reset scoped
# --------------------------------------------------------------------------- #
def test_resume_full_transcript_and_scoped_delete(two_projects):
    scope = two_projects["scope"]
    svc = ConsultantService(project_scope=scope)

    s = svc.create_session(project_id="pa")
    sid = s["session_id"]
    sess = svc._get_session(sid, project_id="pa")
    sess.add("user", "halo")
    sess.add("consultant", "hai")

    resumed = svc.get_session(sid, project_id="pa")
    assert [t["text"] for t in resumed["turns"]] == ["halo", "hai"]

    # delete pada project SALAH tidak menghapus apa pun.
    assert svc.delete_session(sid, project_id="pb") is False
    assert svc.get_session(sid, project_id="pa") is not None

    # delete pada project BENAR menghapus.
    assert svc.delete_session(sid, project_id="pa") is True
    assert svc.get_session(sid, project_id="pa") is None


def test_no_cross_project_session_restore(two_projects):
    """ID sesi yang sama pada dua project: TIDAK berbagi konteks/state."""
    scope = two_projects["scope"]
    svc = ConsultantService(project_scope=scope)

    svc.create_session(project_id="pa")
    # Buat sesi dengan ID eksplisit yang sama di kedua project.
    fixed_id = "shared-id"
    a = svc._get_session(fixed_id, project_id="pa")
    a.add("user", "pesan A")
    b = svc._get_session(fixed_id, project_id="pb")

    assert [t.text for t in a.turns] == ["pesan A"]
    assert b.turns == []  # TIDAK mewarisi konteks project A

    # Sesi anonim yang "dipindah" project TIDAK terjadi lagi: reset hanya
    # menyentuh project yang diminta.
    assert svc.reset_session(fixed_id, project_id="pb") is True
    assert svc.get_session(fixed_id, project_id="pa") is not None


# --------------------------------------------------------------------------- #
# 3) Project switching dalam satu proses + persistensi restart
# --------------------------------------------------------------------------- #
def test_project_switching_same_process(two_projects):
    scope = two_projects["scope"]
    svc = ConsultantService(project_scope=scope)

    a = svc.create_session(project_id="pa", title="A1")
    b = svc.create_session(project_id="pb", title="B1")

    # Pindah bolak-balik: tiap project melihat HANYA miliknya.
    assert [s["title"] for s in svc.list_sessions(project_id="pa")] == ["A1"]
    assert [s["title"] for s in svc.list_sessions(project_id="pb")] == ["B1"]

    svc.rename_session(a["session_id"], "A1-renamed", project_id="pa")
    assert svc.get_session(b["session_id"], project_id="pb")["title"] == "B1"
    assert svc.get_session(a["session_id"], project_id="pa")["title"] == "A1-renamed"

    # Rename pada project salah -> None (tidak menyentuh sesi project lain).
    assert svc.rename_session(b["session_id"], "X", project_id="pa") is None


def test_persistence_across_restart_per_project(two_projects):
    scope = two_projects["scope"]
    svc1 = ConsultantService(project_scope=scope)
    a = svc1.create_session(project_id="pa", title="Persist A")
    svc1._get_session(a["session_id"], project_id="pa").add("user", "tanya A")

    svc2 = ConsultantService(project_scope=scope)
    loaded = svc2.get_session(a["session_id"], project_id="pa")
    assert loaded is not None
    assert loaded["title"] == "Persist A"
    assert [t["text"] for t in loaded["turns"]] == ["tanya A"]

    # Project B tetap kosong di instance baru.
    assert svc2.list_sessions(project_id="pb") == []


# --------------------------------------------------------------------------- #
# 4) Migrasi legacy idempoten + orphan
# --------------------------------------------------------------------------- #
def _write_legacy(path: Path, sessions):
    path.write_text(json.dumps({"sessions": sessions}), encoding="utf-8")


def test_explicit_store_mode_still_isolates_projects(tmp_path):
    """Mode store_path (satu file) tetap mengisolasi sesi antar project_id.

    Cache internal harus di-key (project_id, session_id), bukan session_id saja,
    agar sesi dengan ID sama pada dua project TIDAK saling menimpa.
    """
    svc = ConsultantService(store_path=str(tmp_path / "sessions.json"))
    a = svc.create_session(project_id="p1", title="P1")
    # ID eksplisit yang sama pada project lain.
    a2 = svc._get_session(a["session_id"], project_id="p2")
    assert a2.project_id == "p2"
    a2.add("user", "halo p2")

    # Project p1 TIDAK terpengaruh konteks p2 (dan sebaliknya).
    assert svc.get_session(a["session_id"], project_id="p1")["turns"] == []
    assert [t["text"] for t in svc.get_session(a["session_id"], project_id="p2")["turns"]] == [
        "halo p2"
    ]
    assert len(svc.list_sessions(project_id="p1")) == 1
    assert len(svc.list_sessions(project_id="p2")) == 1


def test_bare_mode_is_ephemeral_no_global_file(two_projects):
    """Tanpa scope & tanpa store_path -> sesi ephemeral (TIDAK menulis file global).

    Ini jalur pemakaian core (verifier/script) dengan `ConsultantService()` polos:
    perilaku API lama tetap bekerja, tetapi TIDAK ada sesi yang ditulis ke file
    global bersama (mencegah kebocoran lintas-project).
    """
    svc = ConsultantService()
    created = svc._get_session("bare-1").session_id  # noqa: F841
    assert svc.get_session("bare-1") is not None
    # Tidak ada ctx yang menunjuk ke file global legacy.
    for ctx in svc._contexts.values():
        assert ctx.store._path is None  # ephemeral => tanpa path file


def test_migration_maps_only_verifiable_and_keeps_orphans(two_projects, tmp_path):
    scope = two_projects["scope"]
    legacy = tmp_path / "consultant_sessions.json"
    _write_legacy(
        legacy,
        [
            {
                "session_id": "s-a",
                "project_id": "pa",
                "title": "milik A",
                "created_at": 1.0,
                "updated_at": 2.0,
                "turns": [{"role": "user", "text": "hi"}],
            },
            {
                "session_id": "s-b",
                "project_id": "pb",
                "title": "milik B",
                "created_at": 1.0,
                "updated_at": 2.0,
                "turns": [],
            },
            {
                "session_id": "orphan-1",
                "project_id": None,
                "title": "tanpa pemilik",
                "created_at": 1.0,
                "updated_at": 2.0,
                "turns": [],
            },
            {
                "session_id": "orphan-2",
                "project_id": "ghost",
                "title": "project tidak terdaftar",
                "created_at": 1.0,
                "updated_at": 2.0,
                "turns": [],
            },
        ],
    )
    legacy_before = legacy.read_text(encoding="utf-8")

    report = audit_legacy_migration(scope, legacy_path=str(legacy))
    assert sorted(report.migrated) == ["s-a", "s-b"]
    assert sorted(report.orphans) == ["orphan-1", "orphan-2"]

    # Sesi A/B kini ada di project store masing-masing.
    svc = ConsultantService(project_scope=scope)
    assert svc.get_session("s-a", project_id="pa")["title"] == "milik A"
    assert svc.get_session("s-b", project_id="pb")["title"] == "milik B"
    # Orphan TIDAK ditulis ke project mana pun.
    assert svc.get_session("orphan-1", project_id="pa") is None
    assert svc.get_session("orphan-2", project_id="pb") is None

    # Legacy file TIDAK berubah (tidak dihapus/ditimpa/dipindah).
    assert legacy.read_text(encoding="utf-8") == legacy_before


def test_migration_is_idempotent(two_projects, tmp_path):
    scope = two_projects["scope"]
    legacy = tmp_path / "consultant_sessions.json"
    _write_legacy(
        legacy,
        [
            {
                "session_id": "s-a",
                "project_id": "pa",
                "title": "milik A",
                "created_at": 1.0,
                "updated_at": 2.0,
                "turns": [],
            }
        ],
    )

    first = audit_legacy_migration(scope, legacy_path=str(legacy))
    assert first.migrated == ["s-a"]

    # Jalankan ulang -> tidak menulis duplikat; terdeteksi "sudah ada".
    second = audit_legacy_migration(scope, legacy_path=str(legacy))
    assert second.migrated == []
    assert second.skipped_existing == ["s-a"]

    # Store project A tetap berisi tepat satu sesi.
    store = ConsultantSessionStore(project_sessions_path(two_projects["pa"]))
    assert len(store.list_sessions("pa")) == 1


def test_migration_without_scope_is_all_orphans(tmp_path):
    """Tanpa registry (scope None) -> semua sesi jadi orphan (tidak ditebak)."""
    legacy = tmp_path / "consultant_sessions.json"
    _write_legacy(
        legacy,
        [
            {"session_id": "x", "project_id": "pa", "title": "t", "turns": []},
            {"session_id": "y", "project_id": None, "title": "t", "turns": []},
        ],
    )
    report = audit_legacy_migration(None, legacy_path=str(legacy))
    assert report.migrated == []
    assert sorted(report.orphans) == ["x", "y"]
    assert read_legacy_sessions(str(legacy))  # legacy tetap terbaca & utuh


def test_migration_dry_run_writes_nothing(two_projects, tmp_path):
    scope = two_projects["scope"]
    legacy = tmp_path / "consultant_sessions.json"
    _write_legacy(
        legacy,
        [{"session_id": "s-a", "project_id": "pa", "title": "t", "turns": []}],
    )
    report = audit_legacy_migration(scope, legacy_path=str(legacy), dry_run=True)
    assert report.migrated == ["s-a"]
    assert not Path(project_sessions_path(two_projects["pa"])).exists()


# --------------------------------------------------------------------------- #
# 5) Gateway scoping (thin pass-through) — tanpa LLM nyata
# --------------------------------------------------------------------------- #
class DummyProjectStore:
    def __init__(self, active=None, projects=None):
        self._active = active
        self._projects = projects or {}

    def get_active_project_id(self):
        return self._active

    def get_project(self, project_id):
        path = self._projects.get(project_id)
        return {"id": project_id, "path": path} if path else None

    def list_projects(self):
        return [{"id": pid, "path": p} for pid, p in self._projects.items()]


class DummyRegistry:
    def __init__(self, projects=None):
        self._projects = projects or {}

    def get(self, project_id):
        path = self._projects.get(project_id)
        if not path:
            raise KeyError(project_id)
        return type("Cfg", (), {"id": project_id, "root": path})()

    def list(self):
        return [type("Cfg", (), {"id": pid, "root": p})() for pid, p in self._projects.items()]


def _make_gateway(tmp_path, active):
    from api.services import GatewayService

    roots = {"pa": str(tmp_path / "gpa"), "pb": str(tmp_path / "gpb")}
    for r in roots.values():
        Path(r).mkdir(parents=True, exist_ok=True)
    gw = GatewayService(
        project_registry=DummyRegistry(roots),
        project_store=DummyProjectStore(active=active, projects=roots),
    )
    return gw


def test_gateway_scopes_sessions_per_project(tmp_path):
    import os
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    gw = _make_gateway(tmp_path, active="pa")

    a = gw.create_consultant_session(project_id="pa", title="A")
    b = gw.create_consultant_session(project_id="pb", title="B")
    assert [s["session_id"] for s in gw.list_consultant_sessions(project_id="pa")] == [
        a["session_id"]
    ]
    assert [s["session_id"] for s in gw.list_consultant_sessions(project_id="pb")] == [
        b["session_id"]
    ]
    assert gw.get_consultant_session(b["session_id"], project_id="pa") is None

    # Gateway tanpa project_id memakai active project.
    assert gw.list_consultant_sessions()[0]["session_id"] == a["session_id"]


def test_gateway_requires_project_when_no_active(tmp_path):
    import os
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    from api.services import ValidationError

    gw = _make_gateway(tmp_path, active=None)
    with pytest.raises(ValidationError):
        gw.list_consultant_sessions()
    with pytest.raises(ValidationError):
        gw.create_consultant_session()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
