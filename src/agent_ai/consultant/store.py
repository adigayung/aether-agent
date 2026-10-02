"""Persistent storage for Consultant sessions (JSON file, stdlib only).

This module provides a write-through JSON store for Consultant sessions,
ensuring sessions survive server restarts. It is deliberately separate from
agent_ai.session.SessionStore (which is task-oriented and has no chat
transcripts). This store ONLY handles ConsultantSession data.

Path default: <repo>/data/consultant_sessions.json (global, injectable for tests).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent_ai.consultant.models import ConsultantTurn


class ConsultantSessionStore:
    """JSON-backed persistent store for Consultant sessions.

    Thread-safe: all public methods use a lock.
    Write-through: every mutating operation persists immediately.
    Load-on-init: existing sessions are loaded at construction time.

    The store keeps ALL turns (full retention) so UI can resume full
    transcripts. Context window bounding (for LLM) is handled by
    ConsultantSession.build_task(), not by truncating stored data.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        if path is None:
            # Default to <repo>/data/consultant_sessions.json
            repo_root = Path(__file__).resolve().parents[3]
            path = str(repo_root / "data" / "consultant_sessions.json")
        self._path = Path(path)
        self._lock = threading.Lock()
        # In-memory index: (project_id, session_id) -> session dict
        self._sessions: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        """Load sessions from JSON file (if exists)."""
        if not self._path.exists():
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
    def list_sessions(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return session metadata list, newest first. Filter by project_id if given."""
        with self._lock:
            project_key = project_id or ""
            sessions = [
                s for s in self._sessions.values()
                if project_id is None or s.get("project_id") == project_key
            ]
            sessions.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
            return [self._session_meta(s) for s in sessions]

    def get_session(
        self, session_id: str, project_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Return full session dict (including turns) or None."""
        with self._lock:
            if project_id is not None:
                return self._sessions.get((project_id, session_id))
            for (pid, sid), sess in self._sessions.items():
                if sid == session_id:
                    return sess
            return None

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

    def delete_session(
        self, session_id: str, project_id: Optional[str] = None
    ) -> bool:
        """Delete a session."""
        with self._lock:
            if project_id is not None:
                popped = self._sessions.pop((project_id, session_id), None)
                if popped is not None:
                    self._save()
                    return True
                return False
            for key, sess in list(self._sessions.items()):
                if sess.get("session_id") == session_id:
                    del self._sessions[key]
                    self._save()
                    return True
            return False

    def reset_session(
        self, session_id: str, project_id: Optional[str] = None
    ) -> bool:
        """Clear session turns (keep metadata)."""
        with self._lock:
            if project_id is not None:
                session = self._sessions.get((project_id, session_id))
                if session is None:
                    return False
                session["turns"] = []
                session["updated_at"] = time.time()
                self._save()
                return True
            for sess in self._sessions.values():
                if sess.get("session_id") == session_id:
                    sess["turns"] = []
                    sess["updated_at"] = time.time()
                    self._save()
                    return True
            return False

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