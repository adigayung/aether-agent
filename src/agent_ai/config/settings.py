"""Sistem konfigurasi terpusat.

Membaca variabel dari file .env (via python-dotenv) dan menyediakannya
sebagai objek konfigurasi yang mudah dipakai oleh seluruh aplikasi.

Tahap ini hanya menyiapkan konfigurasi provider AI (Ollama + placeholder
provider cloud). Belum ada Agent Core, UI, tools, atau database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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
# Konfigurasi per provider
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OllamaConfig:
    """Konfigurasi provider Ollama (lokal)."""

    host: str = field(default_factory=lambda: _get("OLLAMA_HOST", "http://127.0.0.1:11434"))
    model: str = field(default_factory=lambda: _get("OLLAMA_MODEL", "qwen2.5-coder:7b"))
    timeout: int = field(default_factory=lambda: _get_int("OLLAMA_TIMEOUT", 120))


@dataclass(frozen=True)
class DeepSeekConfig:
    """Placeholder konfigurasi provider DeepSeek (tahap berikutnya)."""

    api_key: str = field(default_factory=lambda: _get("DEEPSEEK_API_KEY"))
    base_url: str = field(default_factory=lambda: _get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"))
    model: str = field(default_factory=lambda: _get("DEEPSEEK_MODEL", "deepseek-coder"))
    timeout: int = field(default_factory=lambda: _get_int("DEEPSEEK_TIMEOUT", 120))

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

    default_provider: str = field(default_factory=lambda: _get("DEFAULT_PROVIDER", "ollama"))
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO"))

    # Parameter generasi default (dipakai provider bila tidak di-override)
    default_temperature: float = field(default_factory=lambda: _get_float("DEFAULT_TEMPERATURE", 0.2))
    default_max_tokens: int = field(default_factory=lambda: _get_int("DEFAULT_MAX_TOKENS", 2048))

    # Sub-konfigurasi provider
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    openrouter: OpenRouterConfig = field(default_factory=OpenRouterConfig)

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
