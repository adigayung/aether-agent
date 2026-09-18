"""Persistensi konfigurasi LLM pada database GLOBAL AETHER (`data/aether.db`).

Modul ini TIDAK membuat database baru: ia memakai database SQLite global yang
sudah ada (`data/aether.db`, sama dengan launcher gateway). Ia hanya menambah
tabel khusus konfigurasi LLM:

    - llm_provider_instances  -> instance provider (OpenRouter/DeepSeek/...)
    - llm_models              -> model milik sebuah provider instance

Prinsip:
    - API key TIDAK disimpan di sini. Kolom `api_key_env` hanya menyimpan NAMA
      variabel `.env`. Nilai secret tetap di `.env`.
    - Foreign key + ON DELETE CASCADE: menghapus provider instance otomatis
      menghapus model-modelnya (PRAGMA foreign_keys=ON per koneksi).
    - Thread-safe via `threading.Lock` (pola sama seperti store gateway).
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from typing import Any, Dict, List, Optional, Union

from agent_ai.llm_config.models import ModelConfig, ProviderInstance, _now_iso

PathLike = Union[str, "Path"]


def default_db_path() -> Path:
    """Path default database global AETHER (`<repo>/data/aether.db`)."""
    # Import lokal: hindari siklus import saat settings dimuat.
    from agent_ai.config.settings import PROJECT_ROOT

    return Path(PROJECT_ROOT) / "data" / "aether.db"


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS llm_provider_instances (
        id            TEXT PRIMARY KEY,
        name          TEXT NOT NULL UNIQUE,
        provider_type TEXT NOT NULL,
        api_url       TEXT NOT NULL DEFAULT '',
        api_key_env   TEXT NOT NULL DEFAULT '',
        enabled       INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_models (
        id          TEXT PRIMARY KEY,
        provider_id TEXT NOT NULL,
        model_name  TEXT NOT NULL,
        enabled     INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        FOREIGN KEY (provider_id)
            REFERENCES llm_provider_instances (id) ON DELETE CASCADE,
        UNIQUE (provider_id, model_name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_llm_models_provider ON llm_models (provider_id)",
)


class LLMConfigStore:
    """Repositori SQLite untuk konfigurasi LLM (provider instance + model).

    Args:
        db_path: path database SQLite. Default: database global AETHER
            (`data/aether.db`). Dapat diarahkan ke path sementara saat test.
    """

    def __init__(self, db_path: Optional[PathLike] = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------ #
    # Koneksi & skema
    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        """Buka koneksi dengan row factory + foreign key aktif."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Koneksi dengan commit otomatis + SELALU ditutup.

        (Context manager bawaan sqlite3 hanya commit/rollback, tidak menutup
        koneksi; di Windows handle yang terbuka menghalangi penghapusan file DB.)
        """
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        """Buat tabel/index bila belum ada (idempotent)."""
        with self._lock, self._connection() as conn:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)

    # ------------------------------------------------------------------ #
    # Provider instance
    # ------------------------------------------------------------------ #
    def create_provider_instance(
        self,
        *,
        name: str,
        provider_type: str,
        api_url: str = "",
        api_key_env: str = "",
        enabled: bool = True,
    ) -> ProviderInstance:
        """Simpan provider instance baru dan kembalikan datanya."""
        instance = ProviderInstance(
            name=name,
            provider_type=provider_type,
            api_url=api_url,
            api_key_env=api_key_env,
            enabled=enabled,
        )
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO llm_provider_instances
                    (id, name, provider_type, api_url, api_key_env, enabled,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instance.id,
                    instance.name,
                    instance.provider_type,
                    instance.api_url,
                    instance.api_key_env,
                    1 if instance.enabled else 0,
                    instance.created_at,
                    instance.updated_at,
                ),
            )
        return instance

    def get_provider_instance(self, instance_id: str) -> Optional[ProviderInstance]:
        """Ambil provider instance berdasarkan id (None bila tidak ada)."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM llm_provider_instances WHERE id = ?", (instance_id,)
            ).fetchone()
        return ProviderInstance.from_row(row) if row is not None else None

    def find_provider_instance_by_name(self, name: str) -> Optional[ProviderInstance]:
        """Ambil provider instance berdasarkan nama (case-insensitive)."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM llm_provider_instances WHERE name = ? COLLATE NOCASE",
                (name,),
            ).fetchone()
        return ProviderInstance.from_row(row) if row is not None else None

    def list_provider_instances(self) -> List[ProviderInstance]:
        """Daftar semua provider instance (urut nama)."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM llm_provider_instances ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [ProviderInstance.from_row(row) for row in rows]

    def update_provider_instance(
        self, instance_id: str, fields: Dict[str, Any]
    ) -> Optional[ProviderInstance]:
        """Update kolom provider instance yang diberikan (None bila tak ada)."""
        current = self.get_provider_instance(instance_id)
        if current is None:
            return None
        fields = dict(fields)
        fields["updated_at"] = _now_iso()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [
            int(value) if (key == "enabled" and isinstance(value, bool)) else value
            for key, value in fields.items()
        ]
        with self._lock, self._connection() as conn:
            conn.execute(
                f"UPDATE llm_provider_instances SET {assignments} WHERE id = ?",
                (*values, instance_id),
            )
        return self.get_provider_instance(instance_id)

    def delete_provider_instance(self, instance_id: str) -> bool:
        """Hapus provider instance (model ikut terhapus via cascade)."""
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM llm_provider_instances WHERE id = ?", (instance_id,)
            )
        return cursor.rowcount > 0

    # ------------------------------------------------------------------ #
    # Model
    # ------------------------------------------------------------------ #
    def create_model(
        self,
        *,
        provider_id: str,
        model_name: str,
        enabled: bool = True,
    ) -> ModelConfig:
        """Simpan model baru untuk sebuah provider instance."""
        model = ModelConfig(
            provider_id=provider_id,
            model_name=model_name,
            enabled=enabled,
        )
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO llm_models
                    (id, provider_id, model_name, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    model.id,
                    model.provider_id,
                    model.model_name,
                    1 if model.enabled else 0,
                    model.created_at,
                    model.updated_at,
                ),
            )
        return model

    def get_model(self, model_id: str) -> Optional[ModelConfig]:
        """Ambil model berdasarkan id."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM llm_models WHERE id = ?", (model_id,)
            ).fetchone()
        return ModelConfig.from_row(row) if row is not None else None

    def find_model_by_name(
        self, provider_id: str, model_name: str
    ) -> Optional[ModelConfig]:
        """Ambil model berdasarkan (provider_id, model_name)."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM llm_models WHERE provider_id = ? AND model_name = ?",
                (provider_id, model_name),
            ).fetchone()
        return ModelConfig.from_row(row) if row is not None else None

    def list_models(self, provider_id: Optional[str] = None) -> List[ModelConfig]:
        """Daftar model (semua, atau milik satu provider)."""
        with self._connection() as conn:
            if provider_id is None:
                rows = conn.execute(
                    "SELECT * FROM llm_models ORDER BY model_name COLLATE NOCASE"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM llm_models WHERE provider_id = ? "
                    "ORDER BY model_name COLLATE NOCASE",
                    (provider_id,),
                ).fetchall()
        return [ModelConfig.from_row(row) for row in rows]

    def update_model(
        self, model_id: str, fields: Dict[str, Any]
    ) -> Optional[ModelConfig]:
        """Update kolom model yang diberikan (None bila tak ada)."""
        current = self.get_model(model_id)
        if current is None:
            return None
        fields = dict(fields)
        fields["updated_at"] = _now_iso()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [
            int(value) if (key == "enabled" and isinstance(value, bool)) else value
            for key, value in fields.items()
        ]
        with self._lock, self._connection() as conn:
            conn.execute(
                f"UPDATE llm_models SET {assignments} WHERE id = ?",
                (*values, model_id),
            )
        return self.get_model(model_id)

    def delete_model(self, model_id: str) -> bool:
        """Hapus satu model."""
        with self._lock, self._connection() as conn:
            cursor = conn.execute("DELETE FROM llm_models WHERE id = ?", (model_id,))
        return cursor.rowcount > 0

    def count_models(self, provider_id: str) -> int:
        """Jumlah model milik sebuah provider instance."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM llm_models WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
        return int(row["total"])
