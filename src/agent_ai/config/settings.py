"""Sistem konfigurasi terpusat.

Membaca variabel dari file .env (via python-dotenv) dan menyediakannya
sebagai objek konfigurasi yang mudah dipakai oleh seluruh aplikasi.

Tahap ini hanya menyiapkan konfigurasi provider AI (Ollama + placeholder
provider cloud). Belum ada Agent Core, UI, tools, atau database.
"""

from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Lokasi file .env (root project = empat level di atas file ini:
# src/agent_ai/config/settings.py -> config -> agent_ai -> src -> root project)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

# Muat .env ke environment. override=False agar variabel environment
# yang sudah ada (mis. dari shell) tidak tertimpa.
load_dotenv(dotenv_path=ENV_PATH, override=False)


def _get(key: str, default: str = "") -> str:
    """Ambil nilai string dari environment dengan default aman."""
    value = os.getenv(key, default)
    return value.strip() if isinstance(value, str) else default


def _get_int(key: str, default: int) -> int:
    """Ambil nilai integer dari environment, fallback ke default bila invalid."""
    raw = _get(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(key: str, default: float) -> float:
    """Ambil nilai float dari environment, fallback ke default bila invalid."""
    raw = _get(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Global conversation compaction switch (`data/settings.json`)
# ---------------------------------------------------------------------------
SETTINGS_PATH = PROJECT_ROOT / "data" / "settings.json"


def compression_enabled() -> bool:
    """Baca `compression.enabled` dari `data/settings.json`.

    Global switch ON/OFF untuk conversation compaction existing (runtime
    context compaction sebelum dikirim ke LLM). Default True (backward
    compatible): bila file/objek/field tidak ada, atau ada error baca, return
    True sehingga behavior existing tidak berubah. Hanya nilai eksplisit
    `false` pada `data/settings.json` -> `compression.enabled` yang menonaktifkan.
    """
    try:
        text = SETTINGS_PATH.read_text(encoding="utf-8")
        return bool(json.loads(text).get("compression", {}).get("enabled", True))
    except Exception:  # noqa: BLE001 - default aman (ON) bila file korup/absent
        return True


def write_log_response_api() -> bool:
    """Baca `write_log_response_api` dari `data/settings.json`.

    Global switch ON/OFF untuk logging response mentah API LLM per task ke
    `<root project target>/.aether/log/response/<task_id>.json`. Default False
    (backward compatible): bila file/field tidak ada, atau terjadi error baca,
    return False sehingga AETHER berjalan PERSIS seperti sekarang (tanpa
    menulis response API). Hanya nilai eksplisit `true` yang mengaktifkan.
    """
    try:
        text = SETTINGS_PATH.read_text(encoding="utf-8")
        return bool(json.loads(text).get("write_log_response_api", False))
    except Exception:  # noqa: BLE001 - default aman (OFF) bila file korup/absent
        return False


# ---------------------------------------------------------------------------
# Retry request API LLM (`data/settings.json` -> api_retry)
# ---------------------------------------------------------------------------
#: Default retry request API LLM bila konfigurasi tidak tersedia. Nilai default
#: ini SENGAJA mempertahankan behavior lama (1 attempt awal + 3 pengulangan,
#: tanpa jeda tambahan di layer pemanggilan provider), sehingga ketika
#: `data/settings.json` tidak ada / tidak memuat `api_retry`, AETHER berjalan
#: PERSIS seperti sebelumnya.
DEFAULT_API_RETRY_FAILED_COUNT = 3
DEFAULT_API_RETRY_FAILED_SLEEP = 0.0


@dataclass(frozen=True)
class ApiRetryConfig:
    """Konfigurasi retry request API LLM (`data/settings.json` -> `api_retry`).

    Attributes:
        failed_count: jumlah pengulangan maksimum SETELAH request API gagal
            (>= 0). Total attempt = 1 attempt awal + `failed_count`.
        failed_sleep: waktu tunggu (detik) SEBELUM setiap pengulangan (>= 0.0).
    """

    failed_count: int = DEFAULT_API_RETRY_FAILED_COUNT
    failed_sleep: float = DEFAULT_API_RETRY_FAILED_SLEEP


def api_retry_config() -> ApiRetryConfig:
    """Baca objek `api_retry` dari `data/settings.json` dengan default AMAN.

    Membaca field `failed_count` (int) dan `failed_sleep` (float). Bila file
    tidak ada, field tidak ada, atau nilainya tidak valid (bukan angka /
    negatif), field tersebut jatuh ke default aman sehingga behavior lama tetap
    berjalan. Fungsi ini TIDAK pernah melempar: selalu mengembalikan
    `ApiRetryConfig` valid, berapa pun isi `data/settings.json`.
    """
    default = ApiRetryConfig()
    try:
        text = SETTINGS_PATH.read_text(encoding="utf-8")
        raw = json.loads(text).get("api_retry", {})
    except Exception:  # noqa: BLE001 - default aman bila file korup/absent
        return default
    if not isinstance(raw, dict):
        return default

    failed_count = default.failed_count
    failed_sleep = default.failed_sleep

    if "failed_count" in raw:
        try:
            value = int(raw["failed_count"])
        except (TypeError, ValueError):
            value = None
        if value is not None and value >= 0:
            failed_count = value

    if "failed_sleep" in raw:
        try:
            value_f = float(raw["failed_sleep"])
        except (TypeError, ValueError):
            value_f = None
        if value_f is not None and value_f >= 0.0:
            failed_sleep = value_f

    return ApiRetryConfig(failed_count=failed_count, failed_sleep=failed_sleep)


def api_retry_failed_count() -> int:
    """Jumlah pengulangan maksimum setelah request API gagal (default aman 3)."""
    return api_retry_config().failed_count


def api_retry_failed_sleep() -> float:
    """Waktu tunggu (detik) sebelum setiap pengulangan (default aman 0.0)."""
    return api_retry_config().failed_sleep


# ---------------------------------------------------------------------------
# Sumber konfigurasi GLOBAL AETHER (`data/settings.json`)
#
# `data/settings.json` adalah SATU-SATUNYA sumber konfigurasi global AETHER.
# Fungsi di bawah HANYA membaca/menulis file yang sama dengan loader di atas
# (tidak ada file konfigurasi kedua, tidak ada skema kedua):
#   - `global_settings()`   -> nilai AKTUAL (efektif) yang dipakai AETHER,
#   - `update_global_settings(...)` -> tulis SEBAGIAN key saja, dengan
#     deep-merge sehingga key/setting lain (termasuk yang belum punya UI)
#     TIDAK hilang.
# Penulisan bersifat atomik (tulis ke file sementara lalu `os.replace`) agar
# file tidak pernah setengah tertulis. Semua batas tipe ada di sini (layer
# konfigurasi), bukan di view/gateway, sehingga UI tetap tipis.
# ---------------------------------------------------------------------------
#: Default port AETHER bila `data/settings.json` tidak memuat `port`. Nilai ini
#: SAMA dengan default launcher (run.bat / scripts/install_aether.py) sehingga
#: tidak ada dua nilai default yang berbeda.
DEFAULT_PORT = 8000

#: Batas maksimum praktis jumlah pengulangan retry API (mencegah UI menulis
#: angka yang membuat task menggantung praktis tanpa batas).
MAX_API_RETRY_FAILED_COUNT = 1000
#: Batas maksimum jeda (detik) antar pengulangan retry API (bounded).
MAX_API_RETRY_FAILED_SLEEP = 3600.0

#: Key user-facing yang boleh diubah lewat UI Settings (Global). Key lain di
#: `data/settings.json` TIDAK boleh dihapus/ditimpa.
_EDITABLE_SETTINGS_KEYS = frozenset(
    {"port", "compression", "write_log_response_api", "api_retry", "agent"}
)

#: Key yang MILIK Project Settings / Policy (project-local, disimpan di
#: `<root>/.aether/permissions.json`) — BUKAN Global Settings AETHER.
#:
#: Ditolak eksplisit di layer konfigurasi global agar policy/permission project
#: TIDAK PERNAH tercampur ke `data/settings.json`. Ini menegakkan pemisahan
#: konfigurasi: Global Settings vs Project Policy memakai sumber masing-masing.
_PROJECT_POLICY_KEYS = frozenset(
    {"mode", "scope", "permission", "permissions", "policy"}
)


class SettingsWriteError(RuntimeError):
    """Gagal membaca/menulis `data/settings.json` (mis. file bukan JSON valid)."""


def _read_settings_document() -> Dict[str, Any]:
    """Baca `data/settings.json` sebagai object ({} bila absent/korup).

    TIDAK melempar: file yang hilang atau korup diperlakukan sebagai object
    kosong agar pemanggil dapat memakai default aman.
    """
    try:
        text = SETTINGS_PATH.read_text(encoding="utf-8")
        data = json.loads(text)
    except Exception:  # noqa: BLE001 - absent/korup -> object kosong
        return {}
    return data if isinstance(data, dict) else {}


def port_setting() -> int:
    """Baca `port` dari `data/settings.json` (default aman `DEFAULT_PORT`).

    Nilai non-angka atau di luar rentang port valid (1..65535) jatuh ke default
    sehingga AETHER tetap berjalan. Fungsi ini TIDAK pernah melempar.
    """
    raw = _read_settings_document().get("port", DEFAULT_PORT)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PORT
    if not (1 <= value <= 65535):
        return DEFAULT_PORT
    return value


# ---------------------------------------------------------------------------
# System Prompt Agent (`data/settings.json` -> `agent.system_prompt`)
#
# System prompt Agent DAPAT DIKELOLA dari Sidebar -> Settings -> Agent. Sumber
# konfigurasinya TETAP `data/settings.json` (satu sumber konfigurasi global yang
# sama) — TIDAK ada file/skema konfigurasi kedua dan TIDAK ada sistem prompt
# kedua. Nilai default (bila user belum mengaturnya) = isi System Prompt Agent
# existing di `agent_ai.core.agent_prompt`, sehingga behavior AETHER tetap sama
# pada pemakaian pertama.
# ---------------------------------------------------------------------------
#: Batas maksimum panjang System Prompt Agent yang boleh disimpan lewat UI
#: (mencegah penulisan nilai tak terbatas ke file konfigurasi).
MAX_AGENT_SYSTEM_PROMPT_CHARS = 200_000


def _default_agent_system_prompt() -> str:
    """System Prompt Agent BAWAAN (isi existing, dipakai bila belum diatur).

    Sumber default tetap modul prompt Agent yang sudah ada
    (`agent_ai.core.agent_prompt`) supaya isi bawaan PERSIS sama dengan
    perilaku sebelumnya. Import SENGAJA lazy dan dibungkus try: layer
    konfigurasi tidak boleh gagal hanya karena modul prompt bermasalah.
    """
    try:
        from agent_ai.core.agent_prompt import build_agent_system_prompt

        return build_agent_system_prompt()
    except Exception:  # noqa: BLE001 - default kosong lebih baik daripada crash
        return ""


def agent_system_prompt() -> str:
    """System Prompt Agent EFEKTIF dari `data/settings.json` -> `agent.system_prompt`.

    Return nilai yang DIKONFIGURASI user bila ada dan tidak kosong (setelah
    `strip`); selain itu default bawaan (`_default_agent_system_prompt()`),
    sehingga AETHER berperilaku sama seperti sebelumnya. Fungsi ini TIDAK pernah
    melempar (file hilang/korup -> default).
    """
    raw = _read_settings_document().get("agent", {})
    if isinstance(raw, dict):
        value = raw.get("system_prompt")
        if isinstance(value, str) and value.strip():
            return value
    return _default_agent_system_prompt()


def global_settings() -> Dict[str, Any]:
    """Nilai AKTUAL konfigurasi global user-facing dari `data/settings.json`.

    Nilai yang dikembalikan = nilai EFEKTIF yang benar-benar dipakai loader
    AETHER (lihat `compression_enabled`, `write_log_response_api`,
    `api_retry_config`, `port_setting`, `agent_system_prompt`), sehingga UI
    TIDAK PERNAH menampilkan nilai yang berbeda dari yang dipakai runtime.

    Returns:
        {
            "port": int,
            "compression": {"enabled": bool},
            "write_log_response_api": bool,
            "api_retry": {"failed_count": int, "failed_sleep": float},
            "agent": {"system_prompt": str, "default_system_prompt": str},
        }

    Catatan: `agent.system_prompt` = nilai EFEKTIF yang dipakai Agent
    (`agent_system_prompt()`), sedangkan `agent.default_system_prompt` = isi
    System Prompt BAWAAN AETHER (konstanta, bukan setting) yang dipakai UI
    untuk tombol "Restore default". Keduanya berasal dari satu sumber
    konfigurasi/prompt yang sama.
    """
    retry = api_retry_config()
    return {
        "port": port_setting(),
        "compression": {"enabled": compression_enabled()},
        "write_log_response_api": write_log_response_api(),
        "api_retry": {
            "failed_count": retry.failed_count,
            "failed_sleep": retry.failed_sleep,
        },
        "agent": {
            "system_prompt": agent_system_prompt(),
            "default_system_prompt": _default_agent_system_prompt(),
        },
    }


def _coerce_bool(name: str, value: Any) -> bool:
    """Validasi & konversi nilai boolean (menerima bool atau 0/1)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise SettingsWriteError(f"'{name}' harus berupa boolean (true/false).")


def _coerce_int(name: str, value: Any, *, minimum: int, maximum: int) -> int:
    """Validasi & konversi nilai integer dalam rentang [minimum, maximum]."""
    if isinstance(value, bool):
        raise SettingsWriteError(f"'{name}' harus berupa angka bulat.")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise SettingsWriteError(f"'{name}' harus berupa angka bulat.") from exc
    if not (minimum <= result <= maximum):
        raise SettingsWriteError(
            f"'{name}' harus berada di antara {minimum} dan {maximum}."
        )
    return result


def _coerce_float(name: str, value: Any, *, minimum: float, maximum: float) -> Any:
    """Validasi & konversi nilai numerik dalam rentang [minimum, maximum].

    Nilai bulat dipertahankan sebagai `int` agar penulisan ulang tidak mengubah
    gaya penulisan asli file (mis. `3` tidak menjadi `3.0`).
    """
    if isinstance(value, bool):
        raise SettingsWriteError(f"'{name}' harus berupa angka.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SettingsWriteError(f"'{name}' harus berupa angka.") from exc
    if result != result:  # NaN
        raise SettingsWriteError(f"'{name}' harus berupa angka valid.")
    if not (minimum <= result <= maximum):
        raise SettingsWriteError(
            f"'{name}' harus berada di antara {minimum} dan {maximum}."
        )
    if result.is_integer():
        return int(result)
    return result


def _coerce_agent_system_prompt(value: Any) -> str:
    """Validasi System Prompt Agent (`agent.system_prompt`) -> string.

    Menerima string non-kosong (setelah `strip`) dengan batas panjang
    `MAX_AGENT_SYSTEM_PROMPT_CHARS`. Nilai kosong / bukan string / terlalu
    panjang ditolak sebagai `SettingsWriteError` sehingga file konfigurasi
    TIDAK pernah menerima nilai yang tidak berarti.
    """
    if not isinstance(value, str):
        raise SettingsWriteError("'agent.system_prompt' harus berupa teks.")
    if not value.strip():
        raise SettingsWriteError(
            "'agent.system_prompt' tidak boleh kosong. "
            "Gunakan tombol Reset untuk kembali ke default."
        )
    if len(value) > MAX_AGENT_SYSTEM_PROMPT_CHARS:
        raise SettingsWriteError(
            "'agent.system_prompt' terlalu panjang "
            f"(maksimum {MAX_AGENT_SYSTEM_PROMPT_CHARS} karakter)."
        )
    return value


def normalize_global_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Validasi payload update global settings -> dict bertipe & ternormalisasi.

    HANYA key user-facing yang dikenal yang diproses (`port`,
    `compression.enabled`, `write_log_response_api`, `api_retry.failed_count`,
    `api_retry.failed_sleep`, `agent.system_prompt`). Payload boleh PARSIAL
    (subset key) — key yang tidak dikirim TIDAK akan disentuh.

    Raises:
        SettingsWriteError: bila tipe/rentang nilai tidak valid atau terdapat
            key yang tidak dikenal (mencegah typo menulis konfigurasi yang
            tidak pernah dipakai AETHER).

    Returns:
        dict siap deep-merge ke `data/settings.json` (mis.
        `{"port": 8000, "compression": {"enabled": True}}`).
    """
    if not isinstance(updates, dict):
        raise SettingsWriteError("Body update harus berupa object JSON.")

    # Pemisahan konfigurasi: policy/permission adalah milik Project Settings
    # (`<root>/.aether/permissions.json`), BUKAN Global Settings AETHER. Tolak
    # dengan pesan yang mengarahkan user ke tempat yang benar (bukan menerima
    # diam-diam lalu menyimpan konfigurasi project ke file global).
    migrated = set(updates) & _PROJECT_POLICY_KEYS
    if migrated:
        raise SettingsWriteError(
            "Setting berikut milik Project Settings / Policy (per project), "
            "bukan Global Settings: "
            f"{', '.join(sorted(migrated))}. "
            "Kelola dari Sidebar -> Projects -> Project Settings / Policy."
        )

    unknown = set(updates) - _EDITABLE_SETTINGS_KEYS
    if unknown:
        raise SettingsWriteError(
            f"Setting tidak dikenal: {', '.join(sorted(unknown))}."
        )

    normalized: Dict[str, Any] = {}

    if "port" in updates:
        normalized["port"] = _coerce_int(
            "port", updates["port"], minimum=1, maximum=65535
        )

    if "compression" in updates:
        compression = updates["compression"]
        if not isinstance(compression, dict):
            raise SettingsWriteError("'compression' harus berupa object.")
        extra = set(compression) - {"enabled"}
        if extra:
            raise SettingsWriteError(
                f"Setting tidak dikenal: {', '.join(sorted(extra))}."
            )
        if "enabled" in compression:
            normalized.setdefault("compression", {})["enabled"] = _coerce_bool(
                "compression.enabled", compression["enabled"]
            )

    if "write_log_response_api" in updates:
        normalized["write_log_response_api"] = _coerce_bool(
            "write_log_response_api", updates["write_log_response_api"]
        )

    if "api_retry" in updates:
        retry = updates["api_retry"]
        if not isinstance(retry, dict):
            raise SettingsWriteError("'api_retry' harus berupa object.")
        extra = set(retry) - {"failed_count", "failed_sleep"}
        if extra:
            raise SettingsWriteError(
                f"Setting tidak dikenal: {', '.join(sorted(extra))}."
            )
        target = normalized.setdefault("api_retry", {})
        if "failed_count" in retry:
            target["failed_count"] = _coerce_int(
                "api_retry.failed_count",
                retry["failed_count"],
                minimum=0,
                maximum=MAX_API_RETRY_FAILED_COUNT,
            )
        if "failed_sleep" in retry:
            target["failed_sleep"] = _coerce_float(
                "api_retry.failed_sleep",
                retry["failed_sleep"],
                minimum=0.0,
                maximum=MAX_API_RETRY_FAILED_SLEEP,
            )

    if "agent" in updates:
        agent = updates["agent"]
        if not isinstance(agent, dict):
            raise SettingsWriteError("'agent' harus berupa object.")
        extra = set(agent) - {"system_prompt"}
        if extra:
            raise SettingsWriteError(
                f"Setting tidak dikenal: {', '.join(sorted(extra))}."
            )
        if "system_prompt" in agent:
            normalized.setdefault("agent", {})["system_prompt"] = (
                _coerce_agent_system_prompt(agent["system_prompt"])
            )

    return normalized


def _deep_merge(target: Dict[str, Any], patch: Dict[str, Any]) -> None:
    """Merge `patch` ke `target` secara rekursif (in-place, tanpa menghapus key).

    Nilai object di-merge; nilai skalar/list menggantikan nilai lama. Key yang
    TIDAK ada di `patch` dibiarkan APA ADANYA — inilah yang menjamin setting
    lain (termasuk yang belum punya kontrol UI) tidak hilang.
    """
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def update_global_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Simpan SEBAGIAN konfigurasi global ke `data/settings.json`.

    Alur: baca dokumen LENGKAP -> validasi payload -> deep-merge -> tulis
    atomik. Key lama yang tidak dikirim TIDAK dihapus dan strukturnya
    dipertahankan (bukan overwrite seluruh file dengan object dari UI).

    Args:
        updates: payload mentah (subset key user-facing) dari UI/API.

    Raises:
        SettingsWriteError: payload tidak valid atau penulisan gagal.

    Returns:
        Nilai global settings AKTUAL setelah penulisan (hasil `global_settings()`).
    """
    patch = normalize_global_settings(updates)
    document = _read_settings_document()
    _deep_merge(document, patch)
    _write_settings_document(document)
    return global_settings()


def _write_settings_document(document: Dict[str, Any]) -> None:
    """Tulis dokumen konfigurasi global secara ATOMIK ke `data/settings.json`."""
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        temporary = SETTINGS_PATH.parent / (SETTINGS_PATH.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, SETTINGS_PATH)
    except OSError as exc:
        raise SettingsWriteError(
            f"Gagal menulis {SETTINGS_PATH.name}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Konfigurasi per provider
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OllamaConfig:
    """Konfigurasi provider Ollama (lokal)."""

    host: str = field(default_factory=lambda: _get("OLLAMA_HOST", "http://127.0.0.1:11434"))
    model: str = field(default_factory=lambda: _get("OLLAMA_MODEL", "qwen2.5-coder:7b"))
    timeout: int = field(default_factory=lambda: _get_int("OLLAMA_TIMEOUT", 120))
    #: Context window (token) yang diminta ke server Ollama lewat option
    #: `num_ctx`. Default server Ollama KECIL (~4096) dan prompt yang melebihi
    #: kapasitas dipotong DARI DEPAN tanpa error: system prompt + awal konteks
    #: (mis. Project Bible pada Consultant) hilang sehingga model menjawab
    #: generik/halusinasi. Menyetel `num_ctx` membuat Ollama benar-benar
    #: menerima konteks yang dikirim — perilakunya setara provider lain yang
    #: memakai context window modelnya sendiri. 0 = tidak mengirim num_ctx
    #: (pakai default server). Override: env `OLLAMA_NUM_CTX`.
    num_ctx: int = field(default_factory=lambda: _get_int("OLLAMA_NUM_CTX", 32768))


@dataclass(frozen=True)
class DeepSeekConfig:
    """Placeholder konfigurasi provider DeepSeek (tahap berikutnya)."""

    api_key: str = field(default_factory=lambda: _get("DEEPSEEK_API_KEY"))
    base_url: str = field(default_factory=lambda: _get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"))
    model: str = field(default_factory=lambda: _get("DEEPSEEK_MODEL", "deepseek-coder"))
    timeout: int = field(default_factory=lambda: _get_int("DEEPSEEK_TIMEOUT", 120))
    #: Context window model (token) yang BENAR-BENAR dapat dipakai provider ini.
    #: 0 = tidak diketahui -> AETHER memakai anggaran config global (perilaku
    #: lama). Diisi eksplisit (mis. 64_000) agar provider dengan context window
    #: besar tidak dipaksa turun ke default global.
    context_window: int = field(
        default_factory=lambda: _get_int("DEEPSEEK_CONTEXT_WINDOW", 0)
    )

    @property
    def is_configured(self) -> bool:
        """True bila API key sudah diisi."""
        return bool(self.api_key)


@dataclass(frozen=True)
class OpenAIConfig:
    """Placeholder konfigurasi provider OpenAI-compatible (tahap berikutnya)."""

    api_key: str = field(default_factory=lambda: _get("OPENAI_API_KEY"))
    base_url: str = field(default_factory=lambda: _get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    model: str = field(default_factory=lambda: _get("OPENAI_MODEL", "gpt-4o-mini"))
    timeout: int = field(default_factory=lambda: _get_int("OPENAI_TIMEOUT", 120))
    #: Context window model (token). 0 = tidak diketahui (perilaku lama).
    context_window: int = field(
        default_factory=lambda: _get_int("OPENAI_CONTEXT_WINDOW", 0)
    )

    @property
    def is_configured(self) -> bool:
        """True bila API key sudah diisi."""
        return bool(self.api_key)


@dataclass(frozen=True)
class OpenRouterConfig:
    """Konfigurasi provider OpenRouter (OpenAI-compatible)."""

    api_key: str = field(default_factory=lambda: _get("OPENROUTER_API_KEY"))
    base_url: str = field(default_factory=lambda: _get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))
    model: str = field(default_factory=lambda: _get("OPENROUTER_MODEL"))
    timeout: int = field(default_factory=lambda: _get_int("OPENROUTER_TIMEOUT", 120))
    #: Context window model (token). 0 = tidak diketahui (perilaku lama).
    #: Perhatikan: satu provider instance dapat memuat banyak model dengan
    #: context window berbeda; isi dengan nilai model yang PALING KECIL yang
    #: dipakai agar request tidak pernah melebihi kemampuan model.
    context_window: int = field(
        default_factory=lambda: _get_int("OPENROUTER_CONTEXT_WINDOW", 0)
    )

    @property
    def is_configured(self) -> bool:
        """True bila API key sudah diisi."""
        return bool(self.api_key)


@dataclass(frozen=True)
class NineRouterConfig:
    """Konfigurasi provider 9Router (OpenAI-compatible, tanpa model)."""

    #: Nama env utama = SEMBILAN_ROUTER_API_KEY (pola <PROVIDER>_API_KEY);
    #: 9ROUTER_API_KEY tetap didukung sebagai nama lama.
    api_key: str = field(
        default_factory=lambda: _get("SEMBILAN_ROUTER_API_KEY") or _get("9ROUTER_API_KEY")
    )
    base_url: str = "http://127.0.0.1:20128/v1"
    model: str = field(default="")
    timeout: int = 120
    context_window: int = 0

    @property
    def is_configured(self) -> bool:
        """True bila API key sudah diisi."""
        return bool(self.api_key)


# ---------------------------------------------------------------------------
# Konfigurasi Advanced Context / Token Budgeting (#40)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ContextConfig:
    """Konfigurasi retrieval profile + context budget.

    Semua nilai dapat di-override lewat environment (.env). Policy retrieval
    TIDAK di-hardcode di Agent Core; dibaca dari sini.
    """

    # Profile default: minimal | balanced | deep
    retrieval_profile: str = field(
        default_factory=lambda: _get("CONTEXT_RETRIEVAL_PROFILE", "balanced")
    )

    # Budget global (dipakai bila profile tidak menimpanya).
    max_files: int = field(default_factory=lambda: _get_int("CONTEXT_MAX_FILES", 8))
    max_bytes: int = field(default_factory=lambda: _get_int("CONTEXT_MAX_BYTES", 60_000))
    max_tokens: int = field(default_factory=lambda: _get_int("CONTEXT_MAX_TOKENS", 16_000))
    #: Batas token untuk KONTEKS PENGETAHUAN (Project Bible / retrieval).
    #: TERPISAH dari `max_tokens` (anggaran PERCAKAPAN): context window provider
    #: adalah anggaran seluruh percakapan, sehingga konteks pengetahuan harus
    #: dibatasi pada porsinya — kalau tidak, Bible dapat menghabiskan hampir
    #: seluruh anggaran dan jendela kerja Agent menjadi kosong (compaction terus
    #: menerus). 0 = tanpa batas tambahan (perilaku lama).
    knowledge_max_tokens: int = field(
        default_factory=lambda: _get_int("CONTEXT_KNOWLEDGE_MAX_TOKENS", 8_000)
    )
    #: Porsi MAKSIMUM anggaran percakapan yang boleh dipakai konteks pengetahuan
    #: (Project Bible). Ini yang mencegah blok pengetahuan STATIS menggerus
    #: jendela kerja: dengan budget 16.000, batas absolut 8.000 token membuat
    #: sisa jendela kerja hanya ~3.500 token sehingga compaction memadatkan
    #: SELURUH jendela terbaru tiap round dan Agent kehilangan isi file yang
    #: baru dibaca (gejala repeated `read_file`). Batas absolut tetap berlaku
    #: sebagai plafon; porsi ini membuatnya menyesuaikan diri terhadap budget
    #: provider (16K maupun 64K). 0 = nonaktif (hanya batas absolut).
    knowledge_share: float = field(
        default_factory=lambda: _get_float("CONTEXT_KNOWLEDGE_SHARE", 0.25)
    )
    max_depth: int = field(default_factory=lambda: _get_int("CONTEXT_MAX_DEPTH", 1))
    max_nodes: int = field(default_factory=lambda: _get_int("CONTEXT_MAX_NODES", 30))
    relevance_threshold: float = field(
        default_factory=lambda: _get_float("CONTEXT_RELEVANCE_THRESHOLD", 1.0)
    )
    partial_read_limit: int = field(
        default_factory=lambda: _get_int("CONTEXT_PARTIAL_READ_LIMIT", 200)
    )

    # Toggle fitur.
    partial_read: bool = field(
        default_factory=lambda: _get("CONTEXT_PARTIAL_READ", "true").lower() in ("1", "true", "yes", "on")
    )
    duplicate_read_prevention: bool = field(
        default_factory=lambda: _get("CONTEXT_DUPLICATE_READ_PREVENTION", "true").lower()
        in ("1", "true", "yes", "on")
    )

    # Override per-profile (opsional; kosong = pakai nilai global di atas).
    minimal_max_files: int = field(default_factory=lambda: _get_int("CONTEXT_MINIMAL_MAX_FILES", 3))
    minimal_max_bytes: int = field(default_factory=lambda: _get_int("CONTEXT_MINIMAL_MAX_BYTES", 12_000))
    minimal_max_tokens: int = field(default_factory=lambda: _get_int("CONTEXT_MINIMAL_MAX_TOKENS", 4_000))
    minimal_max_depth: int = field(default_factory=lambda: _get_int("CONTEXT_MINIMAL_MAX_DEPTH", 0))
    minimal_max_nodes: int = field(default_factory=lambda: _get_int("CONTEXT_MINIMAL_MAX_NODES", 5))
    minimal_relevance_threshold: float = field(
        default_factory=lambda: _get_float("CONTEXT_MINIMAL_RELEVANCE_THRESHOLD", 3.0)
    )

    balanced_max_files: int = field(default_factory=lambda: _get_int("CONTEXT_BALANCED_MAX_FILES", 8))
    balanced_max_bytes: int = field(default_factory=lambda: _get_int("CONTEXT_BALANCED_MAX_BYTES", 60_000))
    balanced_max_tokens: int = field(default_factory=lambda: _get_int("CONTEXT_BALANCED_MAX_TOKENS", 16_000))
    balanced_max_depth: int = field(default_factory=lambda: _get_int("CONTEXT_BALANCED_MAX_DEPTH", 1))
    balanced_max_nodes: int = field(default_factory=lambda: _get_int("CONTEXT_BALANCED_MAX_NODES", 30))
    balanced_relevance_threshold: float = field(
        default_factory=lambda: _get_float("CONTEXT_BALANCED_RELEVANCE_THRESHOLD", 1.0)
    )

    deep_max_files: int = field(default_factory=lambda: _get_int("CONTEXT_DEEP_MAX_FILES", 20))
    deep_max_bytes: int = field(default_factory=lambda: _get_int("CONTEXT_DEEP_MAX_BYTES", 160_000))
    deep_max_tokens: int = field(default_factory=lambda: _get_int("CONTEXT_DEEP_MAX_TOKENS", 40_000))
    deep_max_depth: int = field(default_factory=lambda: _get_int("CONTEXT_DEEP_MAX_DEPTH", 2))
    deep_max_nodes: int = field(default_factory=lambda: _get_int("CONTEXT_DEEP_MAX_NODES", 80))
    deep_relevance_threshold: float = field(
        default_factory=lambda: _get_float("CONTEXT_DEEP_RELEVANCE_THRESHOLD", 0.0)
    )


# ---------------------------------------------------------------------------
# Konfigurasi Planning / Replanning (#41)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PlanningConfig:
    """Konfigurasi batas planning & replanning.

    Policy penting (max steps/replans/depth) TIDAK di-hardcode di planner;
    dibaca dari sini dan dapat di-override lewat environment (.env).
    """

    max_plan_steps: int = field(default_factory=lambda: _get_int("PLANNING_MAX_STEPS", 12))
    max_replans: int = field(default_factory=lambda: _get_int("PLANNING_MAX_REPLANS", 3))
    max_plan_depth: int = field(default_factory=lambda: _get_int("PLANNING_MAX_DEPTH", 3))


# ---------------------------------------------------------------------------
# Konfigurasi Validation <-> Runtime Integration (#42)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ValidationConfig:
    """Konfigurasi integrasi Validation ke execution lifecycle.

    Policy penting (enabled, batas cycle, stop-on-failure, timeout) TIDAK
    di-hardcode di runtime; dibaca dari sini dan dapat di-override lewat
    environment (.env).

    Catatan: VALIDATION_ENABLED default False agar Runtime(prepared) tetap
    berperilaku seperti sebelumnya (backward compatible). Validation hanya
    aktif bila runner/request diberikan secara eksplisit ke runtime.
    """

    enabled: bool = field(
        default_factory=lambda: _get("VALIDATION_ENABLED", "false").lower()
        in ("1", "true", "yes", "on")
    )
    max_replan_cycles: int = field(
        default_factory=lambda: _get_int("VALIDATION_MAX_REPLAN_CYCLES", 2)
    )
    stop_on_failure: bool = field(
        default_factory=lambda: _get("VALIDATION_STOP_ON_FAILURE", "false").lower()
        in ("1", "true", "yes", "on")
    )
    timeout: float = field(
        default_factory=lambda: _get_float("VALIDATION_TIMEOUT", 0.0)
    )


# ---------------------------------------------------------------------------
# Konfigurasi Advanced Recovery (#43)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RecoveryConfig:
    """Konfigurasi recovery policy (bounded).

    Policy penting (enabled, batas attempts/replans/cycles) TIDAK di-hardcode
    di runtime; dibaca dari sini dan dapat di-override lewat environment (.env).
    """

    enabled: bool = field(
        default_factory=lambda: _get("RECOVERY_ENABLED", "true").lower()
        in ("1", "true", "yes", "on")
    )
    max_attempts: int = field(
        default_factory=lambda: _get_int("RECOVERY_MAX_ATTEMPTS", 3)
    )
    max_replans: int = field(
        default_factory=lambda: _get_int("RECOVERY_MAX_REPLANS", 2)
    )
    max_total_cycles: int = field(
        default_factory=lambda: _get_int("RECOVERY_MAX_TOTAL_CYCLES", 5)
    )
    retry_delay: float = field(
        default_factory=lambda: _get_float("RECOVERY_RETRY_DELAY", 0.0)
    )


# ---------------------------------------------------------------------------
# Konfigurasi Model Routing (#44)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RoutingConfig:
    """Konfigurasi model routing (deterministik).

    Hanya policy yang benar-benar diperlukan. Routing TIDAK memakai LLM dan
    TIDAK melakukan fallback (itu #45).
    """

    enabled: bool = field(
        default_factory=lambda: _get("ROUTING_ENABLED", "true").lower()
        in ("1", "true", "yes", "on")
    )
    prefer_default_provider: bool = field(
        default_factory=lambda: _get("ROUTING_PREFER_DEFAULT_PROVIDER", "true").lower()
        in ("1", "true", "yes", "on")
    )


# ---------------------------------------------------------------------------
# Konfigurasi Provider Fallback (#45)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FallbackConfig:
    """Konfigurasi provider fallback (bounded).

    Hanya policy yang benar-benar diperlukan. Fallback TIDAK memakai LLM dan
    TIDAK melakukan retry tanpa batas.
    """

    enabled: bool = field(
        default_factory=lambda: _get("FALLBACK_ENABLED", "true").lower()
        in ("1", "true", "yes", "on")
    )
    max_attempts: int = field(
        default_factory=lambda: _get_int("FALLBACK_MAX_ATTEMPTS", 2)
    )
    allow_retry_current: bool = field(
        default_factory=lambda: _get("FALLBACK_ALLOW_RETRY_CURRENT", "true").lower()
        in ("1", "true", "yes", "on")
    )


# ---------------------------------------------------------------------------
# Konfigurasi Provider Infrastructure Retry (technical only)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProviderRetryConfig:
    """Konfigurasi retry INFRASTRUKTUR di layer provider (bounded).

    HANYA untuk kegagalan TEKNIS yang bersifat sementara: network/timeout/
    connection error dan status HTTP 429/500/529. BUKAN untuk kegagalan logika
    agent (tool error, command exit != 0, validation gagal, prompt salah).

    Policy TIDAK di-hardcode di provider; dibaca dari sini dan dapat
    di-override lewat environment (.env). Default aman: retry terbatas + backoff.

    Attributes:
        enabled: bila False, retry dimatikan (provider langsung melaporkan error).
        max_retries: jumlah retry maksimum setelah percobaan pertama (3-5).
        base_delay: delay awal (detik) sebelum retry pertama.
        max_delay: batas atas delay (detik) antar retry.
        backoff_factor: faktor backoff eksponensial antar retry.
    """

    enabled: bool = field(
        default_factory=lambda: _get("PROVIDER_RETRY_ENABLED", "true").lower()
        in ("1", "true", "yes", "on")
    )
    max_retries: int = field(
        default_factory=lambda: _get_int("PROVIDER_RETRY_MAX_ATTEMPTS", 3)
    )
    base_delay: float = field(
        default_factory=lambda: _get_float("PROVIDER_RETRY_BASE_DELAY", 1.0)
    )
    max_delay: float = field(
        default_factory=lambda: _get_float("PROVIDER_RETRY_MAX_DELAY", 8.0)
    )
    backoff_factor: float = field(
        default_factory=lambda: _get_float("PROVIDER_RETRY_BACKOFF_FACTOR", 2.0)
    )


# ---------------------------------------------------------------------------
# Konfigurasi Vision / Multimodal Input (#46)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VisionConfig:
    """Konfigurasi vision / preprocessing gambar (bounded).

    Hanya policy preprocessing. Vision TIDAK melakukan OCR/enhancement dan
    TIDAK meng-hardcode perilaku provider tertentu.
    """

    enabled: bool = field(
        default_factory=lambda: _get("VISION_ENABLED", "true").lower()
        in ("1", "true", "yes", "on")
    )
    max_dimension: int = field(default_factory=lambda: _get_int("VISION_MAX_DIMENSION", 1568))
    readability_max_dimension: int = field(
        default_factory=lambda: _get_int("VISION_READABILITY_MAX_DIMENSION", 2048)
    )
    jpeg_quality: int = field(default_factory=lambda: _get_int("VISION_JPEG_QUALITY", 85))
    max_bytes: int = field(default_factory=lambda: _get_int("VISION_MAX_BYTES", 4_000_000))
    preserve_alpha: bool = field(
        default_factory=lambda: _get("VISION_PRESERVE_ALPHA", "true").lower()
        in ("1", "true", "yes", "on")
    )
    readability_mode: bool = field(
        default_factory=lambda: _get("VISION_READABILITY_MODE", "true").lower()
        in ("1", "true", "yes", "on")
    )


# ---------------------------------------------------------------------------
# Konfigurasi Permission / Safety Policy Layer (#54)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PermissionConfig:
    """Konfigurasi permission/safety policy (per ActionClass).

    Policy penting (mode per ActionClass) TIDAK di-hardcode di executor/runtime;
    dibaca dari sini dan dapat di-override lewat environment (.env).

    Default aman & backward-compatible: read-only, workspace write, delete/move,
    dan command execution diizinkan (sesuai perilaku workspace existing).
    Action yang tidak terklasifikasi (UNKNOWN) default require_approval (aman).

    Catatan: PERMISSION_ENABLED default True, tetapi enforcement hanya berlaku
    bila PermissionManager diberikan secara eksplisit ke ToolExecutor. Tanpa
    manager, executor berperilaku persis seperti sebelumnya.
    """

    enabled: bool = field(
        default_factory=lambda: _get("PERMISSION_ENABLED", "true").lower()
        in ("1", "true", "yes", "on")
    )
    read_only: str = field(default_factory=lambda: _get("PERMISSION_READ_ONLY", "allow"))
    workspace_write: str = field(
        default_factory=lambda: _get("PERMISSION_WORKSPACE_WRITE", "allow")
    )
    delete_move: str = field(default_factory=lambda: _get("PERMISSION_DELETE_MOVE", "allow"))
    command_execution: str = field(
        default_factory=lambda: _get("PERMISSION_COMMAND_EXECUTION", "allow")
    )
    external_network: str = field(
        default_factory=lambda: _get("PERMISSION_EXTERNAL_NETWORK", "allow")
    )
    unknown: str = field(
        default_factory=lambda: _get("PERMISSION_UNKNOWN", "require_approval")
    )


# ---------------------------------------------------------------------------
# Konfigurasi global
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    """Objek konfigurasi utama aplikasi."""

    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO"))

    # Parameter generasi default (dipakai provider bila tidak di-override)
    default_temperature: float = field(default_factory=lambda: _get_float("DEFAULT_TEMPERATURE", 0.2))
    default_max_tokens: int = field(default_factory=lambda: _get_int("DEFAULT_MAX_TOKENS", 2048))

    # Sub-konfigurasi provider
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    openrouter: OpenRouterConfig = field(default_factory=OpenRouterConfig)
    nine_router: NineRouterConfig = field(default_factory=NineRouterConfig)

    # Advanced Context / Token Budgeting (#40)
    context: ContextConfig = field(default_factory=ContextConfig)

    # Better Planning / Replanning (#41)
    planning: PlanningConfig = field(default_factory=PlanningConfig)

    # Validation <-> Runtime Integration (#42)
    validation: ValidationConfig = field(default_factory=ValidationConfig)

    # Advanced Recovery (#43)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)

    # Model Routing (#44)
    routing: RoutingConfig = field(default_factory=RoutingConfig)

    # Provider Fallback (#45)
    fallback: FallbackConfig = field(default_factory=FallbackConfig)

    # Provider Infrastructure Retry (technical only)
    provider_retry: ProviderRetryConfig = field(default_factory=ProviderRetryConfig)

    # Vision / Multimodal Input (#46)
    vision: VisionConfig = field(default_factory=VisionConfig)

    # Permission / Safety Policy Layer (#54)
    permission: PermissionConfig = field(default_factory=PermissionConfig)


# Instance global yang bisa di-import: `from config.settings import settings`
settings = Settings()
