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
# Contoh valid  : OPENROUTER_API_KEY, OPENROUTER_API_KEY_AKUN_TEMAN,
#                 SEMBILAN_ROUTER_API_KEY, 9ROUTER_API_KEY
# Contoh invalid: OPENROUTER_MODEL, PATH, OLLAMA_HOST, ANOTHER_SECRET
#
# <PREFIX> = satu atau lebih segmen [A-Z0-9] dipisah '_' (mis. "SEMBILAN_ROUTER",
#            "9ROUTER", "OPENROUTER"); TIDAK mengizinkan '_' di awal/akhir atau
#            ganda, sehingga nama env yang malformed tetap ditolak.
# <SUFFIX> = satu atau lebih segmen `_XXX` (huruf besar/angka/underscore).
API_KEY_ENV_PATTERN = re.compile(
    r"^(?P<prefix>[A-Z0-9]+(?:_[A-Z0-9]+)*)_API_KEY(?P<suffix>_[A-Z0-9_]+)?$"
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
        requires_model: True bila USER harus memilih model (model selector di UI
            ditampilkan dan model dianggap wajib). DUA hal ini TERPISAH: provider
            bisa tetap MENGIRIM field `model` (mis. nilai "auto"/"auto-test")
            walau user TIDAK dipaksa memilih model konkret.
        needs_model_field: True bila AETHER menyertakan field `model` pada request
            ke API. False untuk endpoint yang menolak field `model`.
        allow_custom_env: True bila nama variabel env API key boleh memakai prefix
            BEBAS (bukan harus sama dengan `env_prefix`). Dipakai oleh provider
            generik "Custom OpenAI Compatible" agar user bebas memilih nama env
            (mis. GERRY_API_KEY, BARISKA_API_KEY) tanpa hardcode per layanan.
        supports_model_discovery: True bila AETHER boleh MENEMUKAN model lewat
            endpoint OpenAI-compatible `GET {base_url}/models` BILA instance
            tidak punya model eksplisit. Dipakai provider generik (endpoint yang
            menyediakan `/models`, mis. banyak gateway OpenAI-compatible) agar
            model ID yang VALID diambil dari endpoint, bukan dikosongkan
            (sebagian endpoint menolak request tanpa model). Provider dengan
            model wajib (cloud) TIDAK memakainya.
        env_prefix_aliases: prefix env TAMBAHAN (alias) yang juga dianggap milik
            provider type ini (mis. 9Router = "SEMBILAN_ROUTER" dengan alias
            "9ROUTER"). Kosong bila tidak ada alias.
    """

    key: str
    label: str
    env_prefix: str
    default_api_url: str
    requires_api_key: bool
    requires_model: bool = True
    needs_model_field: bool = True
    allow_custom_env: bool = False
    supports_model_discovery: bool = False
    env_prefix_aliases: Tuple[str, ...] = ()

    def matches_env_prefix(self, prefix: str) -> bool:
        """True bila `prefix` env API key milik provider type ini.

        Mencakup `env_prefix` utama MAUPUN seluruh `env_prefix_aliases`
        (case-insensitive).
        """
        upper = (prefix or "").strip().upper()
        if not upper:
            return False
        if upper == self.env_prefix.upper():
            return True
        return upper in {alias.upper() for alias in self.env_prefix_aliases}

    def to_dict(self) -> Dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "env_prefix": self.env_prefix,
            "env_prefix_aliases": list(self.env_prefix_aliases),
            "default_api_url": self.default_api_url,
            "requires_api_key": self.requires_api_key,
            "requires_model": self.requires_model,
            "needs_model_field": self.needs_model_field,
            "allow_custom_env": self.allow_custom_env,
            "supports_model_discovery": self.supports_model_discovery,
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
        ProviderTypeSpec(
            key="9router",
            label="9Router",
            env_prefix="SEMBILAN_ROUTER",
            env_prefix_aliases=("9ROUTER",),
            default_api_url="http://127.0.0.1:20128/v1",
            requires_api_key=True,
            requires_model=False,
        ),
        # Provider GENERIK untuk endpoint apa pun yang kompatibel dengan skema
        # OpenAI (/chat/completions). Ini pengganti provider per-layanan
        # (Gerry/Bariska/9Router/lokal): user bebas menentukan nama instance,
        # Base URL, nama env API key, dan model (termasuk "auto"/alias routing).
        #   - requires_api_key=False: endpoint lokal boleh tanpa API key.
        #   - requires_model=False : user TIDAK dipaksa memilih model konkret;
        #     nilai `model` tetap dikirim bila ada (mis. "auto").
        #   - allow_custom_env=True: nama env API key bebas (mis. GERRY_API_KEY).
        #   - supports_model_discovery=True: bila model kosong, AETHER GET
        #     /models untuk mengambil model ID valid (endpoint OpenAI-compatible
        #     umum menyediakannya).
        ProviderTypeSpec(
            key="custom",
            label="Custom OpenAI Compatible",
            env_prefix="",
            default_api_url="",
            requires_api_key=False,
            requires_model=False,
            allow_custom_env=True,
            # Endpoint OpenAI-compatible umumnya menyediakan `GET /models`.
            # Bila instance TIDAK punya model eksplisit, AETHER boleh menemukan
            # model ID valid dari endpoint (bukan mengosongkan field `model`,
            # yang ditolak sebagian gateway). Tidak mengubah 9Router/cloud.
            supports_model_discovery=True,
        ),
        # Provider untuk OpenCode Zen (https://opencode.ai/docs/zen)
        # Endpoint OpenAI-compatible cloud resmi: https://opencode.ai/zen/v1
        # Autentikasi memakai Bearer token standar (OPENCODE_API_KEY).
        ProviderTypeSpec(
            key="opencode",
            label="OpenCode Zen",
            env_prefix="OPENCODE",
            default_api_url="https://opencode.ai/zen/v1",
            requires_api_key=True,
            requires_model=True,
            needs_model_field=True,
            allow_custom_env=False,
            supports_model_discovery=True,
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
    """Cari provider type dari prefix env API key (mis. "OPENROUTER").

    Mencocokkan `env_prefix` utama maupun alias (mis. "9ROUTER" -> 9Router).
    """
    upper = (prefix or "").strip().upper()
    if not upper:
        return None
    for spec in _PROVIDER_TYPES.values():
        if spec.matches_env_prefix(upper):
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
