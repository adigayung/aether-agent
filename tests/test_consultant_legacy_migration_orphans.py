"""Tests: SAFE recovery of LEGACY Consultant sessions (GLOBAL -> per project).

Fokus: memperbaiki migrasi ``data/consultant_sessions.json`` yang menyisakan
sesi orphan saat project registry KOSONG.

Yang dibuktikan:

    1. Registry kosong (``projects/`` kosong): tanpa sumber identitas lain,
       SEMUA sesi menjadi orphan (TIDAK ditebak dari title/isi/urutan/active).
    2. Sumber identitas alternatif yang OTORITATIF (mis. launcher SQLite
       id->root) memulihkan sesi yang project_id-nya dapat dibuktikan.
    3. Sesi tanpa identitas tetap orphan; pemulihan HANYA lewat pemetaan
       eksplisit yang dikonfirmasi pengguna (``orphan_mapping``), dan pemetaan
       ke project yang tidak terverifikasi tetap orphan.
    4. Idempoten: menjalankan ulang tidak menulis duplikat.
    5. Konflik session ID: sesi tujuan yang BERBEDA dengan id sama TIDAK
       ditimpa diam-diam (dilaporkan sebagai conflict).
    6. File legacy TIDAK PERNAH diubah (byte-identical sebelum/sesudah).
    7. Timestamp, session ID, isi pesan, dan metadata dipertahankan.
    8. Persistensi setelah restart (instance service baru memuat hasil).

Deterministik: fixture + temporary directories (tmp_path); TIDAK menyentuh
data produksi.
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
    ConsultantService,
)
from agent_ai.consultant.store import (  # noqa: E402
    ConsultantSessionStore,
    project_sessions_path,
)
from agent_ai.consultant.migration import (  # noqa: E402
    STATUS_CONFLICT,
    STATUS_MIGRATED,
    STATUS_ORPHAN,
    STATUS_SKIPPED,
    StaticIdentitySource,
    audit_legacy_migration,
)


class FakeProjectScope(ConsultantProjectScope):
    """Scope registry in-memory: peta project_id -> root."""

    def __init__(self, projects=None, active=None):
        self._projects = dict(projects or {})
        self._active = active

    def active_project_id(self):
        return self._active

    def project_root(self, project_id):
        return self._projects.get(project_id)

    def registered_project_ids(self):
        return list(self._projects.keys())


@pytest.fixture
def roots(tmp_path: Path):
    """Dua root project kosong + scope yang mengenali keduanya."""
    root_a = tmp_path / "proj_a"
    root_b = tmp_path / "proj_b"
    root_a.mkdir()
    root_b.mkdir()
    return {"pa": str(root_a), "pb": str(root_b)}


def _write_legacy(path: Path, sessions) -> str:
    payload = {"sessions": sessions}
    text = json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return text


def _session(sid, pid=None, title="t", turns=None, created=1.0, updated=2.0):
    return {
        "session_id": sid,
        "project_id": pid,
        "title": title,
        "created_at": created,
        "updated_at": updated,
        "turns": turns if turns is not None else [],
    }


# --------------------------------------------------------------------------- #
# 1) Registry KOSONG -> semua orphan (tanpa parsing heuristik)
# --------------------------------------------------------------------------- #
def test_empty_registry_leaves_all_sessions_orphan(tmp_path):
    legacy = tmp_path / "consultant_sessions.json"
    before = _write_legacy(
        legacy,
        [
            _session("cons", pid=None, title="pertanyaan"),
            _session("s-a", pid="pa", title="milik A"),
        ],
    )
    # Registry KOSONG (proyek tidak terdaftar) + TANPA sumber lain.
    empty_scope = FakeProjectScope({}, active="pa")
    report = audit_legacy_migration(empty_scope, legacy_path=str(legacy))

    assert report.migrated == []
    assert sorted(report.orphans) == ["cons", "s-a"]
    # Tidak ada yang ditulis ke mana pun.
    for root in (tmp_path / "proj_a", tmp_path / "proj_b"):
        assert not Path(project_sessions_path(str(root))).exists()
    # Active project TIDAK dipakai untuk menebak pemilik.
    assert report.target_paths == {}
    assert legacy.read_text(encoding="utf-8") == before


def test_no_identity_source_reason_recorded(tmp_path):
    """Alasan orphan dibedakan: tanpa sumber identitas vs tanpa project_id."""
    legacy = tmp_path / "consultant_sessions.json"
    _write_legacy(legacy, [_session("x", pid=None)])
    report = audit_legacy_migration(
        FakeProjectScope({}, active="pa"), legacy_path=str(legacy)
    )
    assert report.orphans == ["x"]
    assert report.entries[0].status == STATUS_ORPHAN
    assert report.entries[0].reason == "no_identity_source"


# --------------------------------------------------------------------------- #
# 2) Sumber identitas alternatif OTORITATIF (registry kosong)
# --------------------------------------------------------------------------- #
def test_alternative_identity_source_recovers_sessions(tmp_path, roots):
    legacy = tmp_path / "consultant_sessions.json"
    _write_legacy(
        legacy,
        [
            _session("s-a", pid="pa", title="milik A",
                     turns=[{"role": "user", "text": "halo A"}], created=10.0, updated=11.0),
            _session("s-b", pid="pb", title="milik B"),
        ],
    )
    before = legacy.read_text(encoding="utf-8")

    # Registry KOSONG, tetapi launcher DB (sumber otoritatif lain) tahu id->root.
    launcher = StaticIdentitySource(roots, label="launcher-db")
    report = audit_legacy_migration(
        FakeProjectScope({}, active=None),
        legacy_path=str(legacy),
        identity_sources=[launcher],
    )

    assert sorted(report.migrated) == ["s-a", "s-b"]
    assert report.orphans == []
    assert "launcher-db" in report.identity_sources

    # Terverifikasi tersimpan per project + timestamp/isi dipertahankan.
    svc = ConsultantService(project_scope=FakeProjectScope(roots))
    a = svc.get_session("s-a", project_id="pa")
    assert a["title"] == "milik A"
    assert a["created_at"] == 10.0
    assert a["updated_at"] == 11.0
    assert [t["text"] for t in a["turns"]] == ["halo A"]
    assert svc.get_session("s-b", project_id="pb")["title"] == "milik B"

    # File legacy tetap utuh.
    assert legacy.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------- #
# 3) Sesi tanpa identitas + pemulihan EKSPLISIT yang dikonfirmasi pengguna
# --------------------------------------------------------------------------- #
def test_no_identity_stays_orphan_without_mapping(tmp_path, roots):
    legacy = tmp_path / "consultant_sessions.json"
    _write_legacy(legacy, [_session("orphan-x", pid=None, title="tanpa pemilik")])
    report = audit_legacy_migration(
        FakeProjectScope(roots, active="pa"), legacy_path=str(legacy)
    )
    assert report.migrated == []
    assert report.orphans == ["orphan-x"]


def test_explicit_user_mapping_recovers_orphan(tmp_path, roots):
    """Pemetaan eksplisit {session_id: project_id} memulihkan sesi orphan."""
    legacy = tmp_path / "consultant_sessions.json"
    _write_legacy(
        legacy,
        [
            _session("orphan-1", pid=None, title="pindah ke A",
                     turns=[{"role": "user", "text": "pesan"}], created=5.0, updated=6.0),
        ],
    )
    scope = FakeProjectScope(roots, active="pa")
    report = audit_legacy_migration(
        scope, legacy_path=str(legacy), orphan_mapping={"orphan-1": "pa"}
    )

    assert report.migrated == ["orphan-1"]
    assert report.orphans == []
    assert report.entries[0].reason == "user_mapped"

    svc = ConsultantService(project_scope=scope)
    recovered = svc.get_session("orphan-1", project_id="pa")
    assert recovered["title"] == "pindah ke A"
    assert recovered["created_at"] == 5.0
    assert [t["text"] for t in recovered["turns"]] == ["pesan"]
    # TIDAK muncul pada project lain (tidak lintas-project).
    assert svc.get_session("orphan-1", project_id="pb") is None


def test_explicit_mapping_to_unverifiable_project_stays_orphan(tmp_path, roots):
    """Pemetaan ke project TIDAK terverifikasi -> tetap orphan (tidak dikarang)."""
    legacy = tmp_path / "consultant_sessions.json"
    _write_legacy(legacy, [_session("orphan-2", pid=None)])
    report = audit_legacy_migration(
        FakeProjectScope(roots, active="pa"),
        legacy_path=str(legacy),
        orphan_mapping={"orphan-2": "ghost"},
    )
    assert report.migrated == []
    assert report.orphans == ["orphan-2"]
    assert report.entries[0].reason == "unverifiable_project"


# --------------------------------------------------------------------------- #
# 4) Idempoten + konflik session ID
# --------------------------------------------------------------------------- #
def test_migration_is_idempotent_and_preserves_ids(tmp_path, roots):
    legacy = tmp_path / "consultant_sessions.json"
    _write_legacy(legacy, [_session("s-a", pid="pa", title="A")])
    scope = FakeProjectScope(roots, active="pa")

    first = audit_legacy_migration(scope, legacy_path=str(legacy))
    assert first.migrated == ["s-a"]

    second = audit_legacy_migration(scope, legacy_path=str(legacy))
    assert second.migrated == []
    assert second.skipped_existing == ["s-a"]
    assert second.entries[0].status == STATUS_SKIPPED

    # Tepat SATU sesi pada store project A (tidak ada duplikat).
    store = ConsultantSessionStore(project_sessions_path(roots["pa"]))
    sessions = store.list_sessions("pa")
    assert [s["session_id"] for s in sessions] == ["s-a"]


def test_session_id_conflict_is_not_overwritten(tmp_path, roots):
    """Sesi tujuan yang BERBEDA dengan id sama TIDAK ditimpa (dilaporkan konflik)."""
    scope = FakeProjectScope(roots, active="pa")

    # Target project A sudah punya sesi "dup" dengan isi BERBEDA.
    svc = ConsultantService(project_scope=scope)
    existing = svc._get_session("dup", project_id="pa")
    existing.add("user", "isi LAMA milik project")
    original = svc.get_session("dup", project_id="pa")
    assert [t["text"] for t in original["turns"]] == ["isi LAMA milik project"]

    legacy = tmp_path / "consultant_sessions.json"
    _write_legacy(
        legacy,
        [_session("dup", pid="pa", title="isi legacy BERBEDA",
                  turns=[{"role": "user", "text": "isi BARU dari legacy"}])],
    )
    report = audit_legacy_migration(scope, legacy_path=str(legacy))

    assert report.migrated == []
    assert report.conflicts == ["dup"]
    assert report.skipped_existing == []
    assert report.entries[0].status == STATUS_CONFLICT

    # Isi target TIDAK berubah (tidak ada silent overwrite).
    svc2 = ConsultantService(project_scope=scope)
    after = svc2.get_session("dup", project_id="pa")
    assert after["title"] != "isi legacy BERBEDA"
    assert [t["text"] for t in after["turns"]] == ["isi LAMA milik project"]


def test_identical_existing_session_is_skipped(tmp_path, roots):
    """Sesi tujuan yang IDENTIK dengan legacy -> skipped (idempoten)."""
    scope = FakeProjectScope(roots, active="pa")
    legacy = tmp_path / "consultant_sessions.json"
    payload = _session("same", pid="pa", title="sama", created=3.0, updated=4.0,
                       turns=[{"role": "user", "text": "hai"}])
    _write_legacy(legacy, [payload])

    first = audit_legacy_migration(scope, legacy_path=str(legacy))
    assert first.migrated == ["same"]
    second = audit_legacy_migration(scope, legacy_path=str(legacy))
    assert second.skipped_existing == ["same"]
    assert second.entries[0].reason == "already_present"


def test_dry_run_writes_nothing(tmp_path, roots):
    legacy = tmp_path / "consultant_sessions.json"
    _write_legacy(legacy, [_session("s-a", pid="pa")])
    report = audit_legacy_migration(
        FakeProjectScope(roots, active="pa"), legacy_path=str(legacy), dry_run=True
    )
    assert report.migrated == ["s-a"]
    assert not Path(project_sessions_path(roots["pa"])).exists()


# --------------------------------------------------------------------------- #
# 5) Persistensi setelah restart (instance service baru)
# --------------------------------------------------------------------------- #
def test_recovered_sessions_persist_after_restart(tmp_path, roots):
    legacy = tmp_path / "consultant_sessions.json"
    _write_legacy(
        legacy,
        [_session("persist-a", pid="pa", title="Persist",
                  turns=[{"role": "user", "text": "tanya"}, {"role": "consultant", "text": "jawab"}])],
    )
    scope = FakeProjectScope(roots, active="pa")
    report = audit_legacy_migration(scope, legacy_path=str(legacy))
    assert report.migrated == ["persist-a"]

    # "Restart": instance service BARU memuat dari disk.
    svc = ConsultantService(project_scope=FakeProjectScope(roots, active="pa"))
    loaded = svc.get_session("persist-a", project_id="pa")
    assert loaded is not None
    assert loaded["title"] == "Persist"
    assert [t["text"] for t in loaded["turns"]] == ["tanya", "jawab"]
    # Project B tetap kosong.
    assert svc.list_sessions(project_id="pb") == []


# --------------------------------------------------------------------------- #
# 6) Migrasi berulang tetap tidak menyentuh legacy file
# --------------------------------------------------------------------------- #
def test_legacy_file_is_never_modified_across_runs(tmp_path, roots):
    legacy = tmp_path / "consultant_sessions.json"
    before = _write_legacy(
        legacy,
        [_session("s-a", pid="pa"), _session("orphan", pid=None)],
    )
    scope = FakeProjectScope(roots, active="pa")
    for _ in range(3):
        audit_legacy_migration(scope, legacy_path=str(legacy))
    assert legacy.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------- #
# 7) Gateway: registry kosong + launcher SQLite sebagai sumber alternatif
# --------------------------------------------------------------------------- #
class _DummyProjectStore:
    def __init__(self, projects=None, active=None):
        self._projects = dict(projects or {})
        self._active = active

    def get_active_project_id(self):
        return self._active

    def get_project(self, project_id):
        path = self._projects.get(project_id)
        return {"id": project_id, "path": path} if path else None

    def list_projects(self):
        return [{"id": pid, "path": p} for pid, p in self._projects.items()]


class _EmptyRegistry:
    """Registry AETHER yang KOSONG (tidak ada project.json)."""

    def get(self, project_id):
        raise KeyError(project_id)

    def list(self):
        return []


def test_gateway_recovers_via_launcher_db_when_registry_empty(tmp_path, roots):
    """Gateway: registry kosong, launcher DB jadi sumber identitas otoritatif."""
    import os
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    from api.services import GatewayService

    gw = GatewayService(
        project_registry=_EmptyRegistry(),
        project_store=_DummyProjectStore(projects=roots, active="pa"),
    )

    legacy = tmp_path / "consultant_sessions.json"
    _write_legacy(
        legacy,
        [
            _session("g-a", pid="pa", title="via launcher"),
            _session("g-orphan", pid=None),
        ],
    )
    result = gw.audit_consultant_legacy_migration(legacy_path=str(legacy))

    assert result["migrated"] == ["g-a"]
    assert result["orphans"] == ["g-orphan"]
    assert "launcher-db" in result["identity_sources"]

    # Sesi terpulihkan bisa dibaca melalui gateway (scoped per project).
    loaded = gw.get_consultant_session("g-a", project_id="pa")
    assert loaded is not None and loaded["title"] == "via launcher"

    # Pemulihan eksplisit untuk orphan lewat gateway.
    result2 = gw.audit_consultant_legacy_migration(
        legacy_path=str(legacy), orphan_mapping={"g-orphan": "pb"}
    )
    assert result2["migrated"] == ["g-orphan"]
    assert gw.get_consultant_session("g-orphan", project_id="pb") is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

