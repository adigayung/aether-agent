"""Persistent storage for Consultant sessions (JSON file, stdlib only).

This module provides a write-through JSON store for Consultant sessions,
ensuring sessions survive server restarts. It is deliberately separate from
agent_ai.session.SessionStore (which is task-oriented and has no chat
transcripts). This store ONLY handles ConsultantSession data.

Storage layout (PROJECT-LOCAL, per active project):

    <project_path>/.aether/consultant/sessions.json

Every project owns its sessions under its own root, so sessions NEVER leak
across projects. The file path is injectable for tests.

LEGACY: a single GLOBAL file ``<repo>/data/consultant_sessions.json`` was used
before project isolation. That path is now only relevant to migration
(``agent_ai.consultant.migration``); it is never the default for new writes.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent_ai.consultant.models import ConsultantTurn

#: Relative location of the per-project sessions file under a project root.
PROJECT_SESSIONS_RELPATH = (".aether", "consultant", "sessions.json")

#: Legacy GLOBAL sessions file (pre-isolation). Read-only for migration.
LEGACY_SESSIONS_RELPATH = ("data", "consultant_sessions.json")


def legacy_sessions_path() -> str:
    """Absolute path to the legacy GLOBAL sessions file (pre-isolation)."""
    repo_root = Path(__file__).resolve().parents[3]
    return str(repo_root.joinpath(*LEGACY_SESSIONS_RELPATH))


def project_sessions_path(project_root: str) -> str:
    """Absolute path to a project's OWN sessions file.

    Args:
        project_root: authoritative root path of the project.

    Returns:
        ``<project_root>/.aether/consultant/sessions.json``
    """
    return str(Path(project_root).joinpath(*PROJECT_SESSIONS_RELPATH))


class ConsultantSessionStore:
    """JSON-backed persistent store for Consultant sessions.

    Thread-safe: all public methods use a lock.
    Write-through: every mutating operation persists immediately.
    Load-on-init: existing sessions are loaded at construction time.

    The store keeps ALL turns (full retention) so UI can resume full
    transcripts. Context window bounding (for LLM) is handled by
    ConsultantSession.build_task(), not by truncating stored data.

    One store instance = ONE storage file. Project isolation is enforced by
    giving each project its own file (see ``project_sessions_path``).
    """

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        legacy_path: Optional[str] = None,
        ephemeral: bool = False,
    ) -> None:
        #: Legacy GLOBAL path is kept ONLY as a read source for migration.
        self._legacy_path = Path(legacy_path) if legacy_path is not None else None
        #: Ephemeral = in-memory only (no disk). Used when ConsultantService runs
        #: WITHOUT any project context (no scope, no explicit path): a session is
        #: a pure per-call context and must never be written to a shared file.
        self._ephemeral = bool(ephemeral)
        if path is None and not self._ephemeral:
            # No implicit GLOBAL default: callers must pass an explicit path
            # (per-project) or a legacy path for migration. This prevents new
            # sessions from silently landing in the shared global file again.
            raise ValueError(
                "ConsultantSessionStore butuh path eksplisit "
                "(project-local sessions.json) atau ephemeral=True."
            )
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        # In-memory index: (project_id, session_id) -> session dict
        self._sessions: Dict[Tuple[str, str], Dict[str, Any]] = {}
        if not self._ephemeral:
            self._load()

    def _load(self) -> None:
        """Load sessions from JSON file (if exists)."""
        if self._path is None or not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable file -> start fresh
            return
        if not isinstance(data, dict):
            return
        sessions = data.get("sessions")
        if not isinstance(sessions, list):
            return
        for s in sessions:
            if not isinstance(s, dict):
                continue
            sid = s.get("session_id")
            pid = s.get("project_id") or ""
            if not sid:
                continue
            self._sessions[(pid, sid)] = s

    def _save(self) -> None:
        """Persist all sessions to JSON file (atomic write)."""
        if self._ephemeral or self._path is None:
            # In-memory only: no disk write.
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to temp then rename
        tmp_path = self._path.with_suffix(".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(
                    {"sessions": list(self._sessions.values())},
                    f,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            tmp_path.replace(self._path)
        except OSError:
            # Best effort; if rename fails, cleanup temp
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # Public API (thread-safe)
    # ------------------------------------------------------------------ #
    def list_sessions(self, project_id: str) -> List[Dict[str, Any]]:
        """Return session metadata list for ONE project, newest first.

        ``project_id`` identifies the section WITHIN this (already project-local)
        store file. The store never lists "all projects": a store instance maps
        to exactly one project's file.
        """
        project_key = project_id or ""
        with self._lock:
            sessions = [
                s for s in self._sessions.values()
                if s.get("project_id") == project_key
            ]
            sessions.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
            return [self._session_meta(s) for s in sessions]

    def get_session(self, session_id: str, project_id: str) -> Optional[Dict[str, Any]]:
        """Return full session dict (including turns) or None.

        ``project_id`` identifies the section WITHIN this store file. There is
        NO fallback that could restore a session from ANOTHER project section.
        """
        project_key = project_id or ""
        with self._lock:
            return self._sessions.get((project_key, session_id))

    def save_session(self, session_data: Dict[str, Any]) -> None:
        """Save or overwrite a complete session dict and persist it."""
        sid = session_data.get("session_id")
        pid = session_data.get("project_id") or ""
        if not sid:
            return
        with self._lock:
            self._sessions[(pid, sid)] = dict(session_data)
            self._save()

    def create_session(
        self,
        project_id: Optional[str] = None,
        title: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new session and persist it."""
        import uuid
        now = time.time()
        sid = session_id or uuid.uuid4().hex
        project_key = project_id or ""
        session = {
            "session_id": sid,
            "project_id": project_key,
            "title": title or "New Chat",
            "created_at": now,
            "updated_at": now,
            "turns": [],
        }
        with self._lock:
            self._sessions[(project_key, sid)] = session
            self._save()
        return session

    def add_turn(
        self,
        session_id: str,
        role: str,
        text: str,
        project_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Append a turn to the session (full retention, no truncation)."""
        with self._lock:
            project_key = project_id or ""
            session = self._sessions.get((project_key, session_id))
            if session is None:
                return None
            turn = {"role": role, "text": text or ""}
            session["turns"].append(turn)
            session["updated_at"] = time.time()
            # Auto-title from first user message
            if session["title"] == "New Chat" and role == "user" and text and text.strip():
                session["title"] = self._auto_title(text)
            self._save()
            return session

    def rename_session(
        self, session_id: str, title: str, project_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Rename session title."""
        with self._lock:
            project_key = project_id or ""
            session = self._sessions.get((project_key, session_id))
            if session is None:
                return None
            session["title"] = str(title or "New Chat").strip() or "New Chat"
            session["updated_at"] = time.time()
            self._save()
            return session

    def delete_session(self, session_id: str, project_id: str) -> bool:
        """Delete a session (within its own project section only)."""
        project_key = project_id or ""
        with self._lock:
            popped = self._sessions.pop((project_key, session_id), None)
            if popped is not None:
                self._save()
                return True
            return False

    def reset_session(self, session_id: str, project_id: str) -> bool:
        """Clear session turns (keep metadata) within its own project section."""
        project_key = project_id or ""
        with self._lock:
            session = self._sessions.get((project_key, session_id))
            if session is None:
                return False
            session["turns"] = []
            session["updated_at"] = time.time()
            self._save()
            return True

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _session_meta(self, session: Dict[str, Any]) -> Dict[str, Any]:
        """Return lightweight metadata (without turns) for listing."""
        return {
            "session_id": session.get("session_id"),
            "project_id": session.get("project_id"),
            "title": session.get("title"),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
            "turn_count": len(session.get("turns", [])),
        }

    @staticmethod
    def _auto_title(text: str) -> str:
        if not text:
            return "New Chat"
        cleaned = " ".join(str(text).split())
        if not cleaned:
            return "New Chat"
        return cleaned[:40] if len(cleaned) <= 40 else cleaned[:37] + "..."


def read_legacy_sessions(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read sessions from a LEGACY global file (READ-ONLY, for migration).

    This never writes to or moves the legacy file. It is used by the migration
    audit to map verifiable sessions into their project-local stores while
    leaving unowned sessions as orphans.

    Args:
        path: legacy file path (default: ``<repo>/data/consultant_sessions.json``).

    Returns:
        List of raw session dicts (as stored). Empty list when the file is
        missing / unreadable / malformed.
    """
    legacy = Path(path) if path is not None else Path(legacy_sessions_path())
    if not legacy.exists():
        return []
    try:
        with legacy.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        return []
    return [s for s in sessions if isinstance(s, dict) and s.get("session_id")]