"""Extension Lifecycle persistence (Task 06).

Enabled state persisted in data/aether.db existing database.
No ExtensionSettings.json / extensions.json per extension.

State:
  enabled = true/false  (defaults true for extensions without record)
Status abstraction:
  installed / loaded / enabled / disabled / failed

Activity/logging uses observability existing (sanitized), no secret leakage.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agent_ai.core.observability import sanitize_payload

try:
    from agent_ai.config.settings import PROJECT_ROOT as _PROJECT_ROOT
except Exception:
    _PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _default_db_path() -> Path:
    try:
        from agent_ai.config.settings import PROJECT_ROOT

        return Path(PROJECT_ROOT) / "data" / "aether.db"
    except Exception:
        return Path(__file__).resolve().parents[3] / "data" / "aether.db"


_LIFECYCLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS extension_lifecycle (
    extension_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'installed',
    installed_version TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
)
"""

_ACTIVITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS extension_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extension_id TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
)
"""


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class ExtensionLifecycleStore:
    """Persistent store for extension enabled state + status + activity."""

    _instances: Dict[str, "ExtensionLifecycleStore"] = {}
    _lock = threading.Lock()

    def __new__(cls, db_path: Optional[Union[str, Path]] = None):
        key = str(Path(db_path).resolve()) if db_path is not None else str(_default_db_path().resolve())
        with cls._lock:
            if key in cls._instances:
                return cls._instances[key]
            inst = super().__new__(cls)
            inst._initialized = False
            cls._instances[key] = inst
            return inst

    def __init__(self, db_path: Optional[Union[str, Path]] = None):
        if getattr(self, "_initialized", False):
            return
        self.db_path = Path(db_path) if db_path is not None else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local_lock = threading.Lock()
        self._init_schema()
        self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._local_lock, self._connection() as conn:
            conn.execute(_LIFECYCLE_SCHEMA)
            conn.execute(_ACTIVITY_SCHEMA)
            # Ensure migration for existing table: add columns if missing (backward compat)
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(extension_lifecycle)").fetchall()}
                if "status" not in cols:
                    conn.execute("ALTER TABLE extension_lifecycle ADD COLUMN status TEXT NOT NULL DEFAULT 'installed'")
                if "installed_version" not in cols:
                    conn.execute("ALTER TABLE extension_lifecycle ADD COLUMN installed_version TEXT NOT NULL DEFAULT ''")
                if "error" not in cols:
                    conn.execute("ALTER TABLE extension_lifecycle ADD COLUMN error TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass

    # -- enabled -------------------------------------------------------

    def is_enabled(self, extension_id: str) -> bool:
        """Return enabled flag. If no record, default True (backward compat)."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT enabled FROM extension_lifecycle WHERE extension_id=?",
                (extension_id,),
            ).fetchone()
        if row is None:
            return True
        try:
            return bool(row["enabled"])
        except Exception:
            return True

    def set_enabled(self, extension_id: str, enabled: bool) -> None:
        with self._local_lock, self._connection() as conn:
            # Upsert: preserve other fields if existing
            existing = conn.execute(
                "SELECT status, installed_version, error FROM extension_lifecycle WHERE extension_id=?",
                (extension_id,),
            ).fetchone()
            status = existing["status"] if existing is not None and "status" in existing.keys() else ("enabled" if enabled else "disabled")
            # If we toggle enabled, reflect in status unless failed status should stay failed? Keep simple: enabled->enabled, disabled->disabled
            status = "enabled" if enabled else "disabled"
            version = existing["installed_version"] if existing is not None and "installed_version" in existing.keys() else ""
            err = "" if enabled else (existing["error"] if existing is not None and "error" in existing.keys() else "")
            # If enabling failed extension, clear error and mark enabled
            if enabled and existing is not None and (existing["status"] == "failed"):
                err = ""
            updated = _now_iso()
            conn.execute(
                "INSERT INTO extension_lifecycle (extension_id, enabled, status, installed_version, error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(extension_id) DO UPDATE SET enabled=excluded.enabled, status=excluded.status, error=excluded.error, updated_at=excluded.updated_at",
                (extension_id, 1 if enabled else 0, status, version or "", err or "", updated),
            )

    def get_status_row(self, extension_id: str) -> Optional[Dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM extension_lifecycle WHERE extension_id=?",
                (extension_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def set_status(self, extension_id: str, status: str, *, error: str = "", version: str = "", enabled: Optional[bool] = None) -> None:
        """Set status field (and optionally enabled + error + version)."""
        with self._local_lock, self._connection() as conn:
            existing = conn.execute("SELECT enabled, installed_version, error FROM extension_lifecycle WHERE extension_id=?", (extension_id,)).fetchone()
            cur_enabled = bool(existing["enabled"]) if existing is not None else True
            cur_version = existing["installed_version"] if existing is not None else ""
            if enabled is not None:
                cur_enabled = bool(enabled)
            ver = version if version else cur_version
            # If status failed, keep.enabled as False? But spec says failed capability not active -> enabled false.
            if status == "failed" and enabled is None:
                cur_enabled = False
            updated = _now_iso()
            conn.execute(
                "INSERT INTO extension_lifecycle (extension_id, enabled, status, installed_version, error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(extension_id) DO UPDATE SET enabled=excluded.enabled, status=excluded.status, installed_version=excluded.installed_version, error=excluded.error, updated_at=excluded.updated_at",
                (extension_id, 1 if cur_enabled else 0, status, ver or "", error or "", updated),
            )

    def ensure_installed(self, extension_id: str, version: str = "") -> None:
        """Ensure a lifecycle record exists for an installed extension (default enabled)."""
        with self._local_lock, self._connection() as conn:
            existing = conn.execute("SELECT extension_id FROM extension_lifecycle WHERE extension_id=?", (extension_id,)).fetchone()
            if existing is not None:
                # Update version if provided
                if version:
                    conn.execute(
                        "UPDATE extension_lifecycle SET installed_version=?, updated_at=? WHERE extension_id=?",
                        (version, _now_iso(), extension_id),
                    )
                return
            conn.execute(
                "INSERT INTO extension_lifecycle (extension_id, enabled, status, installed_version, error, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (extension_id, 1, "enabled", version or "", "", _now_iso()),
            )

    def remove(self, extension_id: str) -> bool:
        with self._local_lock, self._connection() as conn:
            cur = conn.execute(
                "DELETE FROM extension_lifecycle WHERE extension_id=?",
                (extension_id,),
            )
            return cur.rowcount > 0

    def all_rows(self) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM extension_lifecycle ORDER BY extension_id").fetchall()
        return [dict(r) for r in rows]

    def is_installed_known(self, extension_id: str) -> bool:
        with self._connection() as conn:
            row = conn.execute("SELECT 1 FROM extension_lifecycle WHERE extension_id=?", (extension_id,)).fetchone()
        return row is not None

    # -- activity ------------------------------------------------------

    def log_activity(self, extension_id: str, event: str, detail: str = "") -> None:
        # Use sanitizer to avoid secret leakage (existing sanitizer)
        try:
            safe_detail = sanitize_payload(detail) if isinstance(detail, str) else sanitize_payload(str(detail))
            # also sanitize extension_id? extension_id is not secret
            event_s = str(event)
            ext_s = str(extension_id)
        except Exception:
            event_s = str(event)
            ext_s = str(extension_id)
            safe_detail = ""
        with self._local_lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO extension_activity (extension_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
                (ext_s, event_s, safe_detail if isinstance(safe_detail, str) else str(safe_detail), _now_iso()),
            )

    def list_activity(self, extension_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            if extension_id is not None:
                rows = conn.execute(
                    "SELECT * FROM extension_activity WHERE extension_id=? ORDER BY id DESC LIMIT ?",
                    (extension_id, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM extension_activity ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def clear_all(self) -> None:
        """For tests: clear all rows."""
        with self._local_lock, self._connection() as conn:
            conn.execute("DELETE FROM extension_lifecycle")
            conn.execute("DELETE FROM extension_activity")


_global_lifecycle: Optional[ExtensionLifecycleStore] = None


def get_lifecycle_store(db_path: Optional[Union[str, Path]] = None) -> ExtensionLifecycleStore:
    global _global_lifecycle
    if db_path is not None:
        return ExtensionLifecycleStore(db_path)
    if _global_lifecycle is None:
        _global_lifecycle = ExtensionLifecycleStore()
    return _global_lifecycle
