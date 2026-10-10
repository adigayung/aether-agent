"""Wiring Provider Fallback untuk jalur eksekusi AKTIF (continuous loop).

Modul ini MENJEMBATANI konfigurasi LLM tersimpan (Provider Instance + Model,
SQLite) dengan subsistem Provider Fallback yang sudah ada
(`agent_ai.fallback`) agar `AgentRuntime` (jalur produksi
`AgentRuntime -> AgentOrchestrator -> run_continuous_loop`) dapat berpindah ke
provider alternatif saat provider utama gagal.

Prinsip (tanpa subpoin baru):
    - TIDAK membuat provider registry kedua: kandidat dibangun dari
      `ModelCapabilityRegistry` yang sudah ada (source of truth capability)
      lewat `RoutingRegistry` yang sudah ada.
    - TIDAK membuat capability registry kedua: entri capability dibangun dari
      konfigurasi LLM tersimpan (provider instance + model).
    - TIDAK memakai `ModelRouter`: routing TIDAK diubah. Fallback memakai
      `FallbackManager` existing langsung (kandidat + policy bawaan), bukan
      routing per-task.
    - Provider alternatif dirakit lewat `providers.factory.build_provider_from_config`
      dari provider instance yang cocok (api_url/api_key/model tersimpan).

Boundary: modul ini TIDAK mengimpor core/orchestrator; ia hanya merakit
komponen fallback existing dan menyerahkannya ke AgentRuntime.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from agent_ai.capabilities.models import ModelCapabilities, ModelCapability
from agent_ai.capabilities.registry import ModelCapabilityRegistry


def _safe_str(value: Any) -> str:
    """Normalisasi nilai menjadi string bersih (None -> '')."""
    if value is None:
        return ""
    return str(value).strip()


def build_capability_registry(llm_config_service: Any) -> ModelCapabilityRegistry:
    """Bangun ModelCapabilityRegistry dari konfigurasi LLM tersimpan.

    Setiap (provider instance, model enabled) menjadi satu entri capability.
    Capability TIDAK disimpan pada konfigurasi LLM, sehingga entri diberi
    capability MINIMAL yang aman untuk chat/tool calling
    (`TOOL_CALLING`, `STRUCTURED_OUTPUT`). Ini TIDAK digunakan sebagai klaim
    kemampuan vision/reasoning; hanya agar kandidat tidak dieliminasi keliru
    saat syarat capability benar-benar diminta (lihat `_evaluate` FallbackManager).

    Returns:
        ModelCapabilityRegistry (bisa kosong bila tidak ada konfigurasi).
    """
    registry = ModelCapabilityRegistry()
    if llm_config_service is None:
        return registry
    try:
        instances = llm_config_service.list_provider_instances()
    except Exception:  # noqa: BLE001 - config error -> registry kosong (tanpa crash)
        return registry

    for instance in instances:
        if not getattr(instance, "enabled", True):
            continue
        provider_type = _safe_str(getattr(instance, "provider_type", ""))
        if not provider_type:
            continue
        try:
            models = llm_config_service.list_models(instance.id)
        except Exception:  # noqa: BLE001 - instance buruk -> lewati
            continue
        for model in models:
            if not getattr(model, "enabled", True):
                continue
            model_name = _safe_str(getattr(model, "model_name", ""))
            registry.register(
                ModelCapabilities(
                    provider=provider_type,
                    model=model_name,
                    capabilities=frozenset(
                        {ModelCapability.TOOL_CALLING, ModelCapability.STRUCTURED_OUTPUT}
                    ),
                )
            )
    return registry


def build_provider_factory(
    llm_config_service: Any,
) -> Callable[[str], Any]:
    """Bangun `provider_factory(provider_name) -> BaseProvider` dari konfigurasi.

    `provider_name` adalah provider TYPE (mis. "deepseek", "ollama") yang
    dipakai sebagai identity kandidat fallback (lihat capability registry).
    Factory mencari provider instance ENABLED pertama dengan provider type
    tersebut, lalu merakit provider lewat `build_provider_from_config`
    (model enabled pertama instance). Raise bila tidak ada supaya
    `_provider_fallback_resolver` menangani sebagai "tidak fallback".
    """

    def factory(provider_name: str) -> Any:
        from agent_ai.providers.factory import build_provider_from_config

        target = _safe_str(provider_name).lower()
        if not target:
            raise ValueError("provider_name kosong untuk fallback.")
        instances = llm_config_service.list_provider_instances()
        for instance in instances:
            if not getattr(instance, "enabled", True):
                continue
            if _safe_str(getattr(instance, "provider_type", "")).lower() != target:
                continue
            resolved = llm_config_service.resolve_runtime_config(
                instance.id, include_api_key=True
            )
            return build_provider_from_config(resolved)
        raise LookupError(
            f"Tidak ada provider instance enabled bertipe '{provider_name}' untuk fallback."
        )

    return factory


def build_fallback_manager(
    llm_config_service: Any,
    *,
    config: Any = None,
) -> Any:
    """Rakit FallbackManager dari konfigurasi LLM tersimpan.

    Returns:
        FallbackManager dengan kandidat dari konfigurasi tersimpan. Bila tidak
        ada instance/model enabled, FallbackManager tetap valid tetapi tanpa
        kandidat (fallback tidak akan dipilih -> perilaku lama dipertahankan).
    """
    from agent_ai.fallback.manager import FallbackManager
    from agent_ai.routing.registry import RoutingRegistry

    capability_registry = build_capability_registry(llm_config_service)
    routing_registry = RoutingRegistry(capability_registry=capability_registry)
    return FallbackManager(routing_registry, config=config)


def build_runtime_fallback(
    llm_config_service: Any,
    *,
    config: Any = None,
) -> Optional[Dict[str, Any]]:
    """Rakit (fallback_manager, provider_factory) siap disuntikkan ke AgentRuntime.

    Returns:
        dict `{"fallback_manager": ..., "provider_factory": ...}` bila
        konfigurasi LLM tersedia dan punya minimal satu instance enabled dengan
        model; None bila tidak (agar AgentRuntime berperilaku seperti sebelumnya
        tanpa fallback).
    """
    if llm_config_service is None:
        return None
    try:
        instances = llm_config_service.list_provider_instances()
    except Exception:  # noqa: BLE001 - config tidak tersedia -> tanpa fallback
        return None
    has_candidate = False
    for instance in instances:
        if not getattr(instance, "enabled", True):
            continue
        try:
            models: List[Any] = llm_config_service.list_models(instance.id)
        except Exception:  # noqa: BLE001
            continue
        if any(getattr(m, "enabled", True) for m in models):
            has_candidate = True
            break
    if not has_candidate:
        return None
    return {
        "fallback_manager": build_fallback_manager(llm_config_service, config=config),
        "provider_factory": build_provider_factory(llm_config_service),
    }


__all__ = [
    "build_capability_registry",
    "build_provider_factory",
    "build_fallback_manager",
    "build_runtime_fallback",
]
