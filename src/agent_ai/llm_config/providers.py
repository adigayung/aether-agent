"""Katalog Provider Type untuk konfigurasi LLM.

Provider type adalah katalog STATIS (bukan koneksi ke provider). Setiap type
menentukan:
    - prefix env untuk API key (mis. OPENROUTER -> OPENROUTER_API_KEY),
    - default API URL,
    - apakah API key wajib (provider lokal seperti Ollama tidak perlu API key).

Digunakan untuk memvalidasi relasi API Key (.env) -> Provider Instance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Pola nama variabel API key di .env
# ---------------------------------------------------------------------------
# HANYA variabel dengan pola baku ini yang dianggap sebagai API key provider:
#     <PREFIX>_API_KEY
#     <PREFIX>_API_KEY_<SUFFIX>
# Contoh valid  : OPENROUTER_API_KEY, OPENROUTER_API_KEY_AKUN_TEMAN
# Contoh invalid: OPENROUTER_MODEL, PATH, OLLAMA_HOST, ANOTHER_SECRET
#
# <PREFIX> = huruf besar diawali huruf, diikuti huruf/angka.
# <SUFFIX> = satu atau lebih segmen `_XXX` (huruf besar/angka/underscore).
API_KEY_ENV_PATTERN = re.compile(
    r"^(?P<prefix>[A-Z][A-Z0-9]*)_API_KEY(?P<suffix>_[A-Z0-9_]+)?$"
)


@dataclass(frozen=True)
class ProviderTypeSpec:
    """Spesifikasi sebuah provider type.

    Attributes:
        key: kunci unik provider type (mis. "openrouter").
        label: label tampilan (mis. "OpenRouter").
        env_prefix: prefix env untuk API key (mis. "OPENROUTER").
        default_api_url: API URL default (base URL) provider.
        requires_api_key: True bila API key wajib (cloud). False untuk lokal.
    """

    key: str
    label: str
    env_prefix: str
    default_api_url: str
    requires_api_key: bool

    def to_dict(self) -> Dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "env_prefix": self.env_prefix,
            "default_api_url": self.default_api_url,
            "requires_api_key": self.requires_api_key,
        }


# ---------------------------------------------------------------------------
# Katalog provider type (sumber tunggal).
# ---------------------------------------------------------------------------
_PROVIDER_TYPES: Dict[str, ProviderTypeSpec] = {
    spec.key: spec
    for spec in (
        ProviderTypeSpec(
            key="openrouter",
            label="OpenRouter",
            env_prefix="OPENROUTER",
            default_api_url="https://openrouter.ai/api/v1",
            requires_api_key=True,
        ),
        ProviderTypeSpec(
            key="deepseek",
            label="DeepSeek",
            env_prefix="DEEPSEEK",
            default_api_url="https://api.deepseek.com/v1",
            requires_api_key=True,
        ),
        ProviderTypeSpec(
            key="openai",
            label="OpenAI",
            env_prefix="OPENAI",
            default_api_url="https://api.openai.com/v1",
            requires_api_key=True,
        ),
        ProviderTypeSpec(
            key="ollama",
            label="Ollama (lokal)",
            env_prefix="OLLAMA",
            default_api_url="http://localhost:11434",
            requires_api_key=False,
        ),
    )
}


# ---------------------------------------------------------------------------
# Akses katalog
# ---------------------------------------------------------------------------
def list_provider_types() -> List[ProviderTypeSpec]:
    """Daftar semua provider type yang didukung."""
    return list(_PROVIDER_TYPES.values())


def provider_type_keys() -> List[str]:
    """Daftar kunci provider type (terurut)."""
    return sorted(_PROVIDER_TYPES)


def get_provider_type(key: str) -> Optional[ProviderTypeSpec]:
    """Ambil spec provider type berdasarkan key (None bila tidak ada)."""
    return _PROVIDER_TYPES.get((key or "").strip().lower())


def require_provider_type(key: str) -> ProviderTypeSpec:
    """Ambil spec provider type; raise bila tidak dikenal."""
    spec = get_provider_type(key)
    if spec is None:
        available = ", ".join(provider_type_keys())
        raise ValueError(f"Provider type '{key}' tidak dikenal. Tersedia: {available}.")
    return spec


def provider_type_for_env_prefix(prefix: str) -> Optional[ProviderTypeSpec]:
    """Cari provider type dari prefix env API key (mis. "OPENROUTER")."""
    upper = (prefix or "").strip().upper()
    for spec in _PROVIDER_TYPES.values():
        if spec.env_prefix == upper:
            return spec
    return None


# ---------------------------------------------------------------------------
# Helper nama variabel API key
# ---------------------------------------------------------------------------
def is_api_key_env_name(name: str) -> bool:
    """True bila `name` mengikuti pola provider + `_API_KEY`."""
    return bool(API_KEY_ENV_PATTERN.match(name or ""))


def parse_api_key_env_name(name: str) -> Optional[Tuple[str, str]]:
    """Pecah nama env menjadi (prefix, suffix).

    Returns:
        (prefix, suffix) bila cocok (suffix bisa string kosong), else None.
        Contoh: "OPENROUTER_API_KEY_AKUN_TEMAN" -> ("OPENROUTER", "_AKUN_TEMAN").
    """
    match = API_KEY_ENV_PATTERN.match(name or "")
    if not match:
        return None
    return match.group("prefix"), (match.group("suffix") or "")


def suffix_label(suffix: str) -> str:
    """Label ramah untuk suffix env (mis. "_AKUN_TEMAN" -> "akun teman")."""
    cleaned = (suffix or "").strip()
    if not cleaned or cleaned == "_":
        return "default"
    return cleaned.lstrip("_").replace("_", " ").lower()
