"""Extension Config System — generic facade over existing AETHER persistence.

Task 04: Extension declares configuration via context.config.register(...)
and reads/writes via context.config.get/set/has etc. This module provides
validation, type handling, scope awareness, secrets handling, and persistence
via the existing SQLite DB (data/aether.db) — no new JSON per extension.

This is the runtime value layer; definitions live in CapabilityRegistry
(capabilities.ConfigFacade). This module provides the Value Store and validation
helpers used by ConfigFacade.

Extension layer is a facade/adapter for existing systems:
    Extension -> ExtensionContext -> context.config -> Existing Storage (SQLite)
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agent_ai.tools.base import ToolExecutionError

CONFIG_TYPES = (
    "string",
    "integer",
    "number",
    "boolean",
    "enum",
    "path",
    "url",
    "secret",
    "json",
    "list",
)

SUPPORTED_SCOPES = ("global", "extension", "project", "task")
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """Base config error."""

class UnknownConfigKeyError(ConfigError):
    """Unknown config key."""

class ConfigValidationError(ConfigError):
    """Validation failed."""

class RequiredConfigMissingError(ToolExecutionError):
    """Required config missing at runtime (clear ToolExecutionError)."""
    # Inherit ToolExecutionError so LLM gets clear error via tool call


# ---------------------------------------------------------------------------
# Helpers: type validation
# ---------------------------------------------------------------------------

def _validate_type(cfg_type: str, value: Any, choices: Optional[List[Any]] = None) -> Any:
    """Validate and coerce value according to cfg_type. Raises ConfigValidationError."""
    if cfg_type == "string" or cfg_type == "secret" or cfg_type == "path" or cfg_type == "url":
        if not isinstance(value, str):
            raise ConfigValidationError(f'Config type "{cfg_type}" expects string, got {type(value).__name__}')
        if cfg_type == "url":
            if not _URL_RE.match(value.strip()):
                raise ConfigValidationError(f'Config type "url" expects valid URL, got "{value}"')
        if cfg_type == "path":
            # path: accept any non-empty string; do not check filesystem existence
            if not value.strip():
                raise ConfigValidationError('Config type "path" must be non-empty string')
        return value
    if cfg_type == "integer":
        if isinstance(value, bool):
            raise ConfigValidationError('Config type "integer" expects integer, got boolean')
        if isinstance(value, int):
            return value
        raise ConfigValidationError(f'Config type "integer" expects integer, got {type(value).__name__}')
    if cfg_type == "number":
        if isinstance(value, bool):
            raise ConfigValidationError('Config type "number" expects numeric, got boolean')
        if isinstance(value, (int, float)):
            return value
        raise ConfigValidationError(f'Config type "number" expects numeric, got {type(value).__name__}')
    if cfg_type == "boolean":
        if isinstance(value, bool):
            return value
        raise ConfigValidationError(f'Config type "boolean" expects true/false boolean, got {type(value).__name__}')
    if cfg_type == "enum":
        if not isinstance(choices, (list, tuple)) or not choices:
            raise ConfigValidationError('Config type "enum" requires non-empty "choices"')
        if value not in choices:
            raise ConfigValidationError(f'Config type "enum" expects one of {choices}, got "{value}"')
        return value
    if cfg_type == "json":
        # Accept dict, list, or JSON-parsable string (but store as parsed)
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed
            except Exception as exc:
                raise ConfigValidationError(f'Config type "json" expects valid JSON, got error: {exc}') from exc
        raise ConfigValidationError(f'Config type "json" expects dict/list or JSON string, got {type(value).__name__}')
    if cfg_type == "list":
        if not isinstance(value, list):
            raise ConfigValidationError(f'Config type "list" expects list, got {type(value).__name__}')
        return value
    raise ConfigValidationError(f'Unknown config type "{cfg_type}"')


def _validate_default(cfg_type: str, default: Any, choices: Optional[List[Any]] = None) -> None:
    if default is None:
        return
    # Secret should not contain real secret? We allow but warn; spec says don't make default containing real secret
    # We just validate type
    _validate_type(cfg_type, default, choices)


# ---------------------------------------------------------------------------
# SQLite Value Store (reuses data/aether.db)
# ---------------------------------------------------------------------------

def _default_db_path() -> Path:
    try:
        from agent_ai.config.settings import PROJECT_ROOT
        return Path(PROJECT_ROOT) / "data" / "aether.db"
    except Exception:
        # fallback to repo root data
        return Path(__file__).resolve().parents[3] / "data" / "aether.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS extension_config (
    extension_id TEXT NOT NULL,
    config_key   TEXT NOT NULL,
    scope        TEXT NOT NULL,
    project_id   TEXT NOT NULL DEFAULT '',
    value_json   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (extension_id, config_key, scope, project_id)
)
"""

class ConfigValueStore:
    """SQLite-backed store for extension config values. Singleton per db_path."""

    _instances: Dict[str, "ConfigValueStore"] = {}
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
        # task-scoped values are in-memory only (not persisted)
        self._task_store: Dict[tuple, Any] = {}
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
            conn.execute(_SCHEMA)

    # helpers
    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def _key_tuple(self, extension_id: str, config_key: str, scope: str, project_id: str) -> tuple:
        return (extension_id, config_key, scope, project_id or "")

    def set(self, extension_id: str, config_key: str, value: Any, scope: str = "extension", project_id: Optional[str] = None) -> None:
        scope = scope or "extension"
        if scope == "task":
            # task scope: in-memory only
            proj = project_id or ""
            self._task_store[self._key_tuple(extension_id, config_key, scope, proj)] = value
            return
        proj = project_id or ""
        # delete task variant if any? No.
        value_json = json.dumps(value, ensure_ascii=False, default=str)
        updated_at = self._now_iso()
        with self._local_lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO extension_config (extension_id, config_key, scope, project_id, value_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(extension_id, config_key, scope, project_id) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (extension_id, config_key, scope, proj, value_json, updated_at),
            )

    def get(self, extension_id: str, config_key: str, scope: str = "extension", project_id: Optional[str] = None) -> Optional[Any]:
        scope = scope or "extension"
        if scope == "task":
            proj = project_id or ""
            return self._task_store.get(self._key_tuple(extension_id, config_key, scope, proj))
        proj = project_id or ""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value_json FROM extension_config WHERE extension_id=? AND config_key=? AND scope=? AND project_id=?",
                (extension_id, config_key, scope, proj),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value_json"])
        except Exception:
            return row["value_json"]

    def delete(self, extension_id: str, config_key: str, scope: str = "extension", project_id: Optional[str] = None) -> bool:
        scope = scope or "extension"
        if scope == "task":
            proj = project_id or ""
            k = self._key_tuple(extension_id, config_key, scope, proj)
            if k in self._task_store:
                del self._task_store[k]
                return True
            return False
        proj = project_id or ""
        with self._local_lock, self._connection() as conn:
            cur = conn.execute(
                "DELETE FROM extension_config WHERE extension_id=? AND config_key=? AND scope=? AND project_id=?",
                (extension_id, config_key, scope, proj),
            )
            return cur.rowcount > 0

    def has(self, extension_id: str, config_key: str, scope: str = "extension", project_id: Optional[str] = None) -> bool:
        return self.get(extension_id, config_key, scope, project_id) is not None

    def clear_extension(self, extension_id: str) -> None:
        """For testing: clear all config for extension (not used in production disable)."""
        with self._local_lock, self._connection() as conn:
            conn.execute("DELETE FROM extension_config WHERE extension_id=?", (extension_id,))
        # also clear task
        for k in list(self._task_store.keys()):
            if k[0] == extension_id:
                del self._task_store[k]

    def all_for_extension(self, extension_id: str) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM extension_config WHERE extension_id=?", (extension_id,)).fetchall()
        result = []
        for r in rows:
            try:
                val = json.loads(r["value_json"])
            except Exception:
                val = r["value_json"]
            result.append({
                "extension_id": r["extension_id"],
                "config_key": r["config_key"],
                "scope": r["scope"],
                "project_id": r["project_id"],
                "value": val,
                "updated_at": r["updated_at"],
            })
        return result


# Global accessor
_global_store: Optional[ConfigValueStore] = None

def get_config_store(db_path: Optional[Union[str, Path]] = None) -> ConfigValueStore:
    global _global_store
    if db_path is not None:
        return ConfigValueStore(db_path)
    if _global_store is None:
        _global_store = ConfigValueStore()
    return _global_store
