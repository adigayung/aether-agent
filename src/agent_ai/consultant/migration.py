"""Migration audit for Consultant sessions: GLOBAL legacy -> per-project.

Context
-------
Before project isolation, Consultant sessions were stored in ONE global file
(``<repo>/data/consultant_sessions.json``) keyed by ``(project_id, session_id)``
with ``project_id`` frequently ``None``/``""``. That layout leaked sessions
across projects.

This module provides a SAFE, IDEMPOTENT, READ-ONLY migration audit:

    * The legacy file is NEVER deleted, overwritten, moved or truncated.
    * Only sessions whose owner can be VERIFIED against an AUTHORITATIVE
      project identity source are copied into that project's own store
      (``<project_path>/.aether/consultant/sessions.json``).
    * Sessions without a verifiable owner remain ORPHANS: they stay exactly
      where they are, reported for later handling. Nothing is guessed -- not
      from the session title, not from the conversation content, not from the
      order in the file, and not from the currently active project.
    * When the primary registry is EMPTY, an additional authoritative identity
      source may be supplied (e.g. the launcher SQLite ``projects`` table). A
      session is recoverable whenever ANY authoritative source can prove its
      ``project_id`` -> ``root`` mapping.
    * For sessions whose identity cannot be proven automatically, the caller
      may supply an EXPLICIT, USER-CONFIRMED mapping ``{session_id: project_id}``.
      A mapping entry is honoured ONLY when the mapped ``project_id`` is
      itself verifiable through an authoritative source; otherwise the session
      stays an orphan (recorded with a reason), so a bad mapping can never
      invent a cross-project home.

Running the audit twice yields the same result (no duplicate writes): a session
already present in the target project store with the same ``session_id`` is
skipped (if identical) or reported as a CONFLICT (if the target already holds a
DIFFERENT session with that id -- it is never overwritten silently).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from agent_ai.consultant.store import (
    ConsultantSessionStore,
    legacy_sessions_path,
    read_legacy_sessions,
    project_sessions_path,
)
from pathlib import Path

# --------------------------------------------------------------------------- #
# Status + reason vocabulary (stable strings for reporting/tests)
# --------------------------------------------------------------------------- #
STATUS_MIGRATED = "migrated"
STATUS_SKIPPED = "skipped_existing"
STATUS_ORPHAN = "orphan"
STATUS_CONFLICT = "conflict"
STATUS_FAILED = "failed"

# Reasons attached to a status (stable strings).
REASON_NO_PROJECT_ID = "no_project_id"
REASON_UNVERIFIABLE_PROJECT = "unverifiable_project"
REASON_NO_IDENTITY_SOURCE = "no_identity_source"
REASON_USER_MAPPED = "user_mapped"
REASON_ALREADY_PRESENT = "already_present"
REASON_ID_CONFLICT = "session_id_conflict"
REASON_WRITE_FAILED = "write_failed"


@dataclass
class MigrationEntry:
    """Per-session outcome entry (keeps a concrete reason for the report)."""

    session_id: str
    status: str
    reason: str
    project_id: str = ""
    target_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "reason": self.reason,
            "project_id": self.project_id,
            "target_path": self.target_path,
        }


@dataclass
class MigrationReport:
    """Result of a legacy migration audit.

    Attributes:
        migrated: session_ids copied into a verifiable project store.
        skipped_existing: session_ids already present (idempotent re-run).
        orphans: session ids whose project_id could not be verified (retained).
        conflicts: session ids where the target already holds a DIFFERENT
            session with the same id (never overwritten).
        failed: session ids that could not be written (I/O error etc).
        entries: full per-session outcome (status + reason).
        legacy_path: the legacy file that was read (never modified).
        target_paths: project-local stores that received sessions.
        identity_sources: human labels of the authoritative identity sources
            that were consulted.
    """

    migrated: List[str] = field(default_factory=list)
    skipped_existing: List[str] = field(default_factory=list)
    orphans: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    entries: List[MigrationEntry] = field(default_factory=list)
    legacy_path: str = ""
    target_paths: Dict[str, str] = field(default_factory=dict)
    identity_sources: List[str] = field(default_factory=list)

    @property
    def orphan_session_ids(self) -> List[str]:
        """Backwards-compatible alias of :attr:`orphans`."""
        return list(self.orphans)

    @property
    def total(self) -> int:
        return (
            len(self.migrated)
            + len(self.skipped_existing)
            + len(self.orphans)
            + len(self.conflicts)
            + len(self.failed)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "migrated": list(self.migrated),
            "skipped_existing": list(self.skipped_existing),
            "orphans": list(self.orphans),
            "orphan_session_ids": list(self.orphans),
            "conflicts": list(self.conflicts),
            "failed": list(self.failed),
            "entries": [e.to_dict() for e in self.entries],
            "legacy_path": self.legacy_path,
            "target_paths": dict(self.target_paths),
            "identity_sources": list(self.identity_sources),
            "total": self.total,
        }


# --------------------------------------------------------------------------- #
# Authoritative identity sources
# --------------------------------------------------------------------------- #
class ProjectIdentitySource:
    """Authoritative source mapping ``project_id`` -> authoritative ``root``.

    Implementations must only return roots the project ACTUALLY owns; a root
    that cannot be resolved must be omitted (never guessed). This is a plain
    duck-typed contract so the core stays free of any web/DB dependency.
    """

    #: Human label used in the report (e.g. "registry", "launcher-db").
    label: str = "source"

    def identities(self) -> Mapping[str, str]:  # pragma: no cover - contract
        raise NotImplementedError


class StaticIdentitySource(ProjectIdentitySource):
    """In-memory identity source (also handy for tests).

    Args:
        projects: mapping ``project_id -> root``.
        label: label reported in :attr:`MigrationReport.identity_sources`.
    """

    def __init__(self, projects: Mapping[str, str], label: str = "static") -> None:
        self._projects = {str(k): str(v) for k, v in dict(projects or {}).items() if k and v}
        self.label = label

    def identities(self) -> Mapping[str, str]:
        return dict(self._projects)


def _verifiable_project_ids(
    project_scope: Any,
    extra_sources: Optional[List[ProjectIdentitySource]] = None,
) -> Dict[str, str]:
    """Return {project_id: root} for projects VERIFIABLE via any source.

    A project_id is verifiable only when SOME authoritative source can resolve
    its root. The primary ``project_scope`` is consulted first (registry), then
    every extra source in order; earlier sources win on conflict (the registry
    is the authoritative structure source). Only roots that are non-empty are
    accepted -- anything else is NOT guessed.

    Args:
        project_scope: object exposing ``registered_project_ids()`` and
            ``project_root(project_id)``. May be ``None`` (registry empty /
            unavailable) -- extra sources are still consulted.
        extra_sources: additional authoritative identity sources (e.g. a
            launcher SQLite ``projects`` table) used when the registry cannot
            prove ownership.
    """
    verified: Dict[str, str] = {}

    # 1) Primary registry-based scope.
    if project_scope is not None:
        try:
            ids = project_scope.registered_project_ids()
        except Exception:  # noqa: BLE001 - audit must not crash on registry errors
            ids = None
        for pid in ids or []:
            if not pid:
                continue
            try:
                root = project_scope.project_root(pid)
            except Exception:  # noqa: BLE001
                root = None
            if root:
                verified.setdefault(str(pid), str(root))

    # 2) Additional authoritative sources (used when the registry is empty).
    for source in extra_sources or []:
        try:
            identities = source.identities()
        except Exception:  # noqa: BLE001 - a bad source must not crash the audit
            continue
        for pid, root in dict(identities or {}).items():
            if not pid or not root:
                continue
            verified.setdefault(str(pid), str(root))

    return verified


def _normalize_session(session: Dict[str, Any], project_id: str) -> Dict[str, Any]:
    """Return a defensive copy of a legacy session with a VERIFIED project_id.

    All original fields (session id, title, turns, timestamps, and any extra
    metadata) are preserved verbatim; only ``project_id`` is overwritten with
    the authoritative value (a blank owner is never trusted).
    """
    data = json.loads(json.dumps(session))  # deep copy, JSON-safe
    data["project_id"] = project_id
    return data


def _same_session(a: Optional[Dict[str, Any]], b: Dict[str, Any]) -> bool:
    """Compare two sessions for MIGRATION identity (ignore project_id section).

    Two sessions are considered identical when their id, title, timestamps and
    full turn list match. ``project_id`` is intentionally excluded because the
    migration rewrites it to the verified value.
    """
    if not isinstance(a, dict):
        return False

    def _key(d: Dict[str, Any]) -> Any:
        return (
            d.get("session_id"),
            d.get("title"),
            d.get("created_at"),
            d.get("updated_at"),
            json.dumps(d.get("turns", []), sort_keys=True, ensure_ascii=False),
        )

    return _key(a) == _key(b)


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #
def audit_legacy_migration(
    project_scope: Any,
    *,
    legacy_path: Optional[str] = None,
    dry_run: bool = False,
    identity_sources: Optional[List[ProjectIdentitySource]] = None,
    orphan_mapping: Optional[Mapping[str, str]] = None,
) -> MigrationReport:
    """Audit + (optionally) copy verifiable legacy sessions per project.

    Args:
        project_scope: object exposing ``registered_project_ids()`` and
            ``project_root(project_id)`` (see ConsultantProjectScope). ``None``
            means the registry is empty/unavailable -> ownership must then be
            proven by ``identity_sources`` or ``orphan_mapping`` (otherwise
            everything stays an orphan).
        legacy_path: explicit legacy file (default:
            ``<repo>/data/consultant_sessions.json``).
        dry_run: when True, compute the report WITHOUT writing anything.
        identity_sources: additional authoritative identity sources consulted
            when the registry is empty (e.g. a launcher SQLite source). Each
            source supplies ``project_id -> root``; unverifiable ids are
            dropped, never guessed.
        orphan_mapping: EXPLICIT, USER-CONFIRMED map ``{session_id: project_id}``
            for sessions whose owner cannot be proven automatically. A mapping
            is honoured ONLY if the target ``project_id`` is itself verifiable
            through an authoritative source; otherwise the session remains an
            orphan (reason ``unverifiable_project``). This never fabricates a
            mapping to remove orphan status.

    Returns:
        :class:`MigrationReport`. The legacy file is never written to.

    Guarantees:
        * Idempotent: re-running does not duplicate sessions.
        * Non-destructive: orphan sessions are retained untouched.
        * No silent overwrite: a target that already holds a DIFFERENT session
          with the same id yields a CONFLICT (recorded, not overwritten).
    """
    report = MigrationReport()
    legacy_file = Path(legacy_path) if legacy_path is not None else Path(legacy_sessions_path())
    report.legacy_path = str(legacy_file)

    legacy_sessions = read_legacy_sessions(str(legacy_file))
    verified = _verifiable_project_ids(project_scope, identity_sources)

    # Which authoritative sources were actually consulted (for the report).
    if project_scope is not None:
        report.identity_sources.append("registry")
    for source in identity_sources or []:
        report.identity_sources.append(getattr(source, "label", "source"))

    mapping = {str(k): str(v) for k, v in dict(orphan_mapping or {}).items() if k}

    def _record(entry: MigrationEntry, bucket: List[str]) -> None:
        bucket.append(entry.session_id)
        report.entries.append(entry)

    for session in legacy_sessions:
        sid = session.get("session_id")
        if not sid:
            continue
        sid = str(sid)

        raw_pid = session.get("project_id") or ""
        raw_pid = str(raw_pid)

        # Determine the effective owner:
        #   * a legacy project_id that is verifiable, OR
        #   * an explicit user-confirmed mapping (only if its target verifies).
        pid = ""
        reason = ""
        used_mapping = False
        if raw_pid and raw_pid in verified:
            pid = raw_pid
        elif sid in mapping:
            mapped_pid = mapping[sid]
            used_mapping = True
            if mapped_pid and mapped_pid in verified:
                pid = mapped_pid
            else:
                # User mapping points at an unverifiable project -> still orphan.
                reason = REASON_UNVERIFIABLE_PROJECT
        elif raw_pid:
            # Owner claimed but not provable by any authoritative source.
            reason = REASON_UNVERIFIABLE_PROJECT
        else:
            # No owner at all.
            reason = REASON_NO_IDENTITY_SOURCE if not verified else REASON_NO_PROJECT_ID

        if not pid:
            _record(
                MigrationEntry(
                    session_id=sid,
                    status=STATUS_ORPHAN,
                    reason=reason or REASON_NO_PROJECT_ID,
                ),
                report.orphans,
            )
            continue

        root = verified[pid]
        target = project_sessions_path(root)
        report.target_paths[pid] = target
        entry_reason = REASON_USER_MAPPED if used_mapping else "verified_project_id"

        if dry_run:
            _record(
                MigrationEntry(
                    session_id=sid,
                    status=STATUS_MIGRATED,
                    reason=entry_reason,
                    project_id=pid,
                    target_path=target,
                ),
                report.migrated,
            )
            continue

        data = _normalize_session(session, pid)

        try:
            store = ConsultantSessionStore(target)
            existing = store.get_session(sid, project_id=pid)
            if existing is not None:
                if _same_session(existing, data):
                    _record(
                        MigrationEntry(
                            session_id=sid,
                            status=STATUS_SKIPPED,
                            reason=REASON_ALREADY_PRESENT,
                            project_id=pid,
                            target_path=target,
                        ),
                        report.skipped_existing,
                    )
                else:
                    # Target already holds a DIFFERENT session with this id:
                    # never overwrite silently.
                    _record(
                        MigrationEntry(
                            session_id=sid,
                            status=STATUS_CONFLICT,
                            reason=REASON_ID_CONFLICT,
                            project_id=pid,
                            target_path=target,
                        ),
                        report.conflicts,
                    )
                continue

            store.save_session(data)
            _record(
                MigrationEntry(
                    session_id=sid,
                    status=STATUS_MIGRATED,
                    reason=entry_reason,
                    project_id=pid,
                    target_path=target,
                ),
                report.migrated,
            )
        except Exception:  # noqa: BLE001 - one bad session must not abort the audit
            _record(
                MigrationEntry(
                    session_id=sid,
                    status=STATUS_FAILED,
                    reason=REASON_WRITE_FAILED,
                    project_id=pid,
                    target_path=target,
                ),
                report.failed,
            )

    return report
