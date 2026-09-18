"""Model data (dataclass) untuk konfigurasi LLM.

Representasi ini murni data (JSON-friendly) dan provider-agnostic. Ia BUKAN
model ORM: persistensi ditangani oleh `agent_ai.llm_config.store`.

Relasi:
    ProviderInstance (1) --< ModelConfig (N)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _now_iso() -> str:
    """Timestamp UTC dalam format ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    """Identifier unik singkat."""
    return uuid.uuid4().hex


@dataclass
class ProviderInstance:
    """Satu instance provider yang dikonfigurasi user.

    Attributes:
        id: identifier unik instance.
        name: nama tampilan (unik), mis. "OpenRouter Utama".
        provider_type: kunci provider type (mis. "openrouter").
        api_url: base URL API (default bila kosong -> dari provider type).
        api_key_env: NAMA variabel .env untuk API key. Kosong bila provider
            tidak butuh API key (mis. Ollama). Nilai secret TIDAK disimpan di
            sini, hanya nama variabelnya.
        enabled: True bila instance aktif dipakai.
        created_at / updated_at: timestamp ISO-8601.
    """

    name: str
    provider_type: str
    id: str = field(default_factory=_new_id)
    api_url: str = ""
    api_key_env: str = ""
    enabled: bool = True
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "provider_type": self.provider_type,
            "api_url": self.api_url,
            "api_key_env": self.api_key_env,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProviderInstance":
        return cls(
            id=data.get("id") or _new_id(),
            name=data.get("name", ""),
            provider_type=data.get("provider_type", ""),
            api_url=data.get("api_url", ""),
            api_key_env=data.get("api_key_env", ""),
            enabled=bool(data.get("enabled", True)),
            created_at=data.get("created_at", "") or _now_iso(),
            updated_at=data.get("updated_at", "") or _now_iso(),
        )

    @classmethod
    def from_row(cls, row: Any) -> "ProviderInstance":
        """Bangun dari baris sqlite3.Row."""
        return cls(
            id=row["id"],
            name=row["name"],
            provider_type=row["provider_type"],
            api_url=row["api_url"] or "",
            api_key_env=row["api_key_env"] or "",
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class ModelConfig:
    """Satu model yang terdaftar pada sebuah provider instance.

    Attributes:
        id: identifier unik model.
        provider_id: id ProviderInstance pemilik (foreign key).
        model_name: nama model (mis. "openai/gpt-4o-mini").
        enabled: True bila model aktif dipakai.
        created_at / updated_at: timestamp ISO-8601.
    """

    provider_id: str
    model_name: str
    id: str = field(default_factory=_new_id)
    enabled: bool = True
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "provider_id": self.provider_id,
            "model_name": self.model_name,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelConfig":
        return cls(
            id=data.get("id") or _new_id(),
            provider_id=data.get("provider_id", ""),
            model_name=data.get("model_name", ""),
            enabled=bool(data.get("enabled", True)),
            created_at=data.get("created_at", "") or _now_iso(),
            updated_at=data.get("updated_at", "") or _now_iso(),
        )

    @classmethod
    def from_row(cls, row: Any) -> "ModelConfig":
        """Bangun dari baris sqlite3.Row."""
        return cls(
            id=row["id"],
            provider_id=row["provider_id"],
            model_name=row["model_name"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class CredentialInfo:
    """Ringkasan satu credential API key di .env (TANPA nilai secret).

    Attributes:
        name: nama variabel env (mis. OPENROUTER_API_KEY).
        provider_type: kunci provider type terkait (None bila prefix tak dikenal).
        provider_label: label provider (atau prefix bila tak dikenal).
        env_prefix: prefix env (mis. "OPENROUTER").
        suffix: suffix variabel (mis. "_AKUN_TEMAN"); kosong untuk default.
        suffix_label: label ramah suffix (mis. "akun teman").
        is_set: True bila nilai variabel ada (di .env atau environment).
        masked: nilai tersamar (mis. "sk-a****wxyz"); aman ditampilkan.
        used_by: daftar nama provider instance yang memakai credential ini.
    """

    name: str
    provider_type: Optional[str]
    provider_label: str
    env_prefix: str
    suffix: str
    suffix_label: str
    is_set: bool
    masked: str
    used_by: list

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "provider_type": self.provider_type,
            "provider_label": self.provider_label,
            "env_prefix": self.env_prefix,
            "suffix": self.suffix,
            "suffix_label": self.suffix_label,
            "is_set": self.is_set,
            "masked": self.masked,
            "used_by": list(self.used_by),
        }
