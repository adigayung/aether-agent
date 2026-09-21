"""Execution bridge: Django Gateway -> AETHER Runtime (#55 wiring).

Django HANYA menjadi gateway/orchestration boundary. Modul ini TIDAK
mendefinisikan Runtime/Orchestrator/Loop/Planning/Tool baru: ia hanya
MERAKIT komponen AETHER yang sudah ada dan menjalankannya.

Alur:
    PreparedTask
        -> AgentRuntime (AETHER, existing)
             -> AgentOrchestrator -> ToolExecutor(permission_manager=...)
                  -> PermissionManager (#54) -> ToolRegistry -> Tools
        -> TaskLifecycle (AETHER, existing) -> status lifecycle
        -> SessionStore (AETHER, existing) -> events -> SSE (#51)

Eksekusi dijalankan di background thread daemon (minimal, tanpa dependency
baru). Ini BUKAN Task Queue subsystem / worker framework: hanya satu thread
per task agar request HTTP tidak blocking. Technical debt dicatat di laporan.

Sumber konfigurasi provider (provider-agnostic):
    1. KONFIGURASI TERSIMPAN (SQLite): `provider_instance_id` + `model_id`.
       Provider dirakit dari api_url/api_key/model instance lewat
       `agent_ai.providers.factory`; `model_name` diabaikan karena model
       berasal dari konfigurasi tersimpan. Ini sumber tunggal provider aktif.
    2. KATALOG REGISTRY (fallback eksplisit): `provider_name` + `model_name`
       dari ProviderRegistry AETHER. Dipakai hanya bila `provider_name`
       diberikan eksplisit (mis. verifier); TIDAK ada default dari .env.

Bila provider tidak tersedia (mis. tidak ada API key / server lokal mati),
task ditandai FAILED dengan error jelas (tidak crash).
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional

from agent_ai.core.executor import ToolExecutor
from agent_ai.permission.manager import PermissionManager
from agent_ai.runtime.models import RuntimeStatus
from agent_ai.runtime.runtime import AgentRuntime
from agent_ai.session.events import EventType, make_event
from agent_ai.session.store import SessionStore
from agent_ai.task.models import PreparedTask
from agent_ai.tasks.lifecycle import TaskLifecycle
from agent_ai.tasks.models import TaskStatus


def _normalize_change_path(path: Any) -> str:
    """Normalisasi path perubahan menjadi relative posix (untuk dedup event).

    Hanya menyamakan bentuk path (backslash -> slash, buang './'); TIDAK
    mengubah makna path. Dipakai agar event live (dari tool) dan event
    consistency-check (ChangeTracker) tidak duplikat untuk file yang sama.
    """
    if not path:
        return ""
    text = str(path).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip()


class TaskExecutor:
    """Merakit & menjalankan AETHER Runtime untuk sebuah PreparedTask.

    Args:
        session_store: SessionStore AETHER (event system existing).
        provider_factory: callable `() -> BaseProvider` opsional. Bila None,
            provider diambil dari ProviderRegistry AETHER berdasarkan nama
            eksplisit. Disediakan agar verifier dapat menyuntikkan provider fake.
        permission_manager: PermissionManager opsional (#54). Bila None,
            dibuat default (dari settings) sehingga policy tetap terpasang.
        runtime_factory: callable opsional untuk membangun AgentRuntime
            (disediakan agar verifier dapat menyuntikkan runtime terkontrol).
        llm_config_service: LLMConfigService opsional (konfigurasi LLM tersimpan).
            Bila None, dibuat lazy (database GLOBAL `data/aether.db`). Dipakai
            untuk merakit provider dari provider instance + model yang dipilih
            di UI konfigurasi LLM (SQLite-driven), bukan hanya dari settings.
    """

    def __init__(
        self,
        session_store: SessionStore,
        *,
        provider_factory: Optional[Callable[[], Any]] = None,
        permission_manager: Optional[PermissionManager] = None,
        runtime_factory: Optional[Callable[..., AgentRuntime]] = None,
        llm_config_service: Optional[Any] = None,
    ) -> None:
        self.sessions = session_store
        self._provider_factory = provider_factory
        self.permission_manager = permission_manager or PermissionManager()
        self._runtime_factory = runtime_factory
        self._llm_config_service = llm_config_service

    @property
    def llm_config_service(self) -> Any:
        """LLMConfigService efektif (lazy; database GLOBAL `data/aether.db`)."""
        if self._llm_config_service is None:
            from agent_ai.llm_config import LLMConfigService

            self._llm_config_service = LLMConfigService()
        return self._llm_config_service

    # ------------------------------------------------------------------ #
    # Provider
    # ------------------------------------------------------------------ #
    def _resolve_provider_config(
        self,
        provider_instance_id: Optional[str],
        *,
        model_id: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Baca konfigurasi provider instance + model dari SQLite (bila dipilih).

        Returns:
            dict hasil `resolve_runtime_config(...)` atau None bila tidak ada
            provider instance yang dipilih (jalur registry eksplisit).

        Raises:
            LLMConfigError: instance/model tidak ditemukan atau tidak valid.
        """
        if not provider_instance_id:
            return None
        return self.llm_config_service.resolve_runtime_config(
            provider_instance_id,
            model_id=model_id,
            model_name=model_name,
            include_api_key=True,
        )

    def _build_provider(
        self,
        provider_name: Optional[str] = None,
        *,
        resolved_config: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Bangun provider AETHER (provider-agnostic).

        Args:
            provider_name: nama provider eksplisit (mis. "deepseek", "ollama").
                WAJIB diisi bila `resolved_config` tidak diberikan (tidak ada
                default dari .env).
            resolved_config: konfigurasi provider instance dari SQLite. Bila
                diisi, provider dirakit dari api_url/api_key/model tersimpan,
                lewat `providers.factory`.
        """
        if self._provider_factory is not None:
            return self._provider_factory()
        if resolved_config is not None:
            from agent_ai.providers.factory import build_provider_from_config

            return build_provider_from_config(resolved_config)
        from agent_ai.providers.registry import get_provider

        return get_provider(provider_name)

    # ------------------------------------------------------------------ #
    # Runtime
    # ------------------------------------------------------------------ #
    def _build_runtime(
        self,
        provider: Any,
        session_id: Optional[str] = None,
        workspace_root: Optional[str] = None,
        model_name: Optional[str] = None,
        cancel_token: Optional[Any] = None,
        change_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> AgentRuntime:
        """Rakit AgentRuntime dengan ToolExecutor yang punya PermissionManager.

        PermissionManager (#54) DIPASANG di sini sehingga policy benar-benar
        aktif pada execution path produksi: action yang ditolak tidak sampai
        dieksekusi oleh tool.

        SessionStore + session_id (#55) diteruskan agar event observability
        (tool/provider/validation) tercatat ke Session/Event System existing.

        workspace_root: bila diisi (active project root), tool filesystem/
        workspace diarahkan ke root tersebut sehingga write/edit relatif
        terhadap project aktif (bukan root AETHER). Bila None, memakai
        registry global (backward compatible).

        model_name: nama model eksplisit dari pilihan UI. Bila diisi, dipakai
        sebagai GenerateOptions.model sehingga provider memakai model tersebut
        (bukan model default provider). Bila None, provider memakai default.

        change_sink: callback opsional `(payload) -> None` yang diteruskan ke
        tool mutasi workspace (write/edit/delete/move). Dipakai untuk live
        filesystem event SEGERA setelah operasi file berhasil (Explorer/
        Changes), tanpa menunggu task selesai. Bila None, tidak ada event live
        (backward compatible).
        """
        if workspace_root:
            from agent_ai.tools.registry import build_registry

            registry = build_registry(root=workspace_root, change_sink=change_sink)
            executor = ToolExecutor(
                registry=registry, permission_manager=self.permission_manager
            )
        else:
            executor = ToolExecutor(permission_manager=self.permission_manager)

        options = None
        if model_name:
            from agent_ai.providers.base import GenerateOptions

            options = GenerateOptions(model=model_name)

        if self._runtime_factory is not None:
            # Backward compatible: hanya teruskan `options` bila diisi, agar
            # runtime_factory lama (tanpa parameter options) tetap bekerja.
            if options is not None:
                return self._runtime_factory(
                    provider=provider,
                    executor=executor,
                    session_id=session_id,
                    options=options,
                )
            return self._runtime_factory(
                provider=provider,
                executor=executor,
                session_id=session_id,
            )
        return AgentRuntime(
            provider=provider,
            executor=executor,
            session_store=self.sessions,
            session_id=session_id,
            options=options,
            # Project-local storage (Task 5): root project target -> Task Log
            # (`.aether/log/<task_id>.log`) + AI Project Bible
            # (`.aether/bible`). Bila None, storage project-local dilewati.
            project_root=workspace_root,
            # Cooperative cancellation: token dibagikan gateway -> runtime ->
            # orchestrator agar loop berhenti di safe boundary saat user Stop.
            cancel_token=cancel_token,
        )

    # ------------------------------------------------------------------ #
    # Event helpers (memakai SessionStore AETHER; tanpa event bus baru)
    # ------------------------------------------------------------------ #
    def _emit(
        self,
        session_id: str,
        event_type: EventType,
        *,
        task_id: Optional[str],
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            event = make_event(
                session_id=session_id,
                event_type=event_type,
                task_id=task_id,
                payload=payload or {},
            )
            self.sessions.append_event(event)
        except Exception:  # noqa: BLE001 - event emission tidak boleh crash
            return

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def run(
        self,
        prepared: PreparedTask,
        *,
        session_id: str,
        task_id: str,
        on_status: Optional[Callable[[str, Optional[str], Optional[str]], None]] = None,
        workspace_root: Optional[str] = None,
        provider_name: Optional[str] = None,
        model_name: Optional[str] = None,
        provider_instance_id: Optional[str] = None,
        model_id: Optional[str] = None,
        cancel_token: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Jalankan PreparedTask lewat AETHER Runtime (synchronous).

        Args:
            prepared: PreparedTask (task + context + plan).
            session_id: session AETHER untuk event.
            task_id: id task (untuk lifecycle + event).
            on_status: callback opsional `(status, result, error)` dipanggil
                saat status berubah (dipakai gateway untuk update TaskRecord).
            workspace_root: active project root opsional. Bila diisi, tool
                filesystem/workspace diarahkan ke root tersebut sehingga
                write/edit relatif terhadap project aktif.
            provider_name: nama provider eksplisit dari pilihan UI (mis.
                "deepseek", "ollama"). Bila None, memakai default AETHER.
            model_name: nama model eksplisit dari pilihan UI. Bila None,
                provider memakai model default-nya.
            provider_instance_id: id provider instance dari konfigurasi LLM
                tersimpan (SQLite). Bila diisi, provider + api_url + api_key +
                model diambil dari konfigurasi ini (mengalahkan provider_name).
            model_id: id model spesifik (dari konfigurasi LLM tersimpan) yang
                harus dipakai. Hanya relevan bila provider_instance_id diisi.
            cancel_token: token pembatalan kooperatif (opsional). Bila diisi,
                runtime/orchestrator berhenti di safe boundary saat token
                diminta dan task dilaporkan CANCELLED (bukan FAILED).

        Returns:
            Ringkasan hasil: {"status", "result", "error", "iterations"}.
        """
        lifecycle = TaskLifecycle(task=prepared.task, task_id=task_id)

        # Catatan: event task_started diemit oleh AgentRuntime (sumber tunggal
        # observability) saat eksekusi dimulai. Di sini hanya update status.
        if on_status is not None:
            on_status(TaskStatus.RUNNING.value, None, None)

        # Change Tracker AETHER (existing): snapshot SEBELUM eksekusi, lalu
        # deteksi perubahan SETELAH eksekusi. Read-only terhadap project.
        # Ini menjadi CONSISTENCY CHECK akhir (mis. perubahan lewat
        # run_command), BUKAN lagi satu-satunya sumber update UI: event live
        # dipancarkan SEGERA setelah tiap operasi file berhasil (change_sink).
        change_tracker = None
        if workspace_root:
            try:
                from pathlib import Path as _Path

                from agent_ai.changes.tracker import ChangeTracker

                change_tracker = ChangeTracker(root=_Path(workspace_root))
                change_tracker.start(task_id)
                change_tracker.snapshot(".", task_id=task_id)
            except Exception:  # noqa: BLE001 - tracking tidak boleh crash task
                change_tracker = None

        # Path yang sudah diemit LIVE oleh tool (write/edit/delete/move).
        # Dipakai untuk dedup: consistency-check akhir tidak mengemit ulang
        # file yang sama (satu operasi sukses = satu logical change event).
        live_changed: set = set()

        def _change_sink(payload: Dict[str, Any]) -> None:
            """Emit change_detected SEGERA (sink dari tool filesystem)."""
            try:
                rel = _normalize_change_path(payload.get("path"))
                if rel:
                    live_changed.add(rel)
                old = _normalize_change_path(payload.get("old_path"))
                if old:
                    # Move: tandai source juga agar tracker akhir tidak
                    # mengemit 'deleted' untuk file yang hanya dipindah.
                    live_changed.add(old)
                self._emit(
                    session_id,
                    EventType.CHANGE_DETECTED,
                    task_id=task_id,
                    payload=payload,
                )
            except Exception:  # noqa: BLE001 - event tidak boleh crash task
                pass

        try:
            # Konfigurasi LLM tersimpan (SQLite) mengalahkan jalur registry
            # default: provider + api_url + api_key + model dari instance.
            resolved_config = self._resolve_provider_config(
                provider_instance_id, model_id=model_id, model_name=model_name
            )
            effective_model = (resolved_config or {}).get("model") or model_name
            provider = self._build_provider(
                provider_name, resolved_config=resolved_config
            )
            runtime = self._build_runtime(
                provider,
                session_id=session_id,
                workspace_root=workspace_root,
                model_name=effective_model,
                cancel_token=cancel_token,
                change_sink=_change_sink,
            )
            result = runtime.run(prepared, lifecycle=lifecycle)
        except Exception as exc:  # noqa: BLE001 - provider/runtime error -> FAILED
            error = f"{type(exc).__name__}: {exc}"
            self._emit(
                session_id,
                EventType.TASK_FAILED,
                task_id=task_id,
                payload={"error": error},
            )
            if on_status is not None:
                on_status(TaskStatus.FAILED.value, None, error)
            return {"status": TaskStatus.FAILED.value, "result": None, "error": error, "iterations": 0}

        # Consistency check AKHIR via Change Tracker AETHER (existing) dan emit
        # event change_detected (event system existing) untuk perubahan yang
        # TIDAK tercakup event live (mis. file dibuat lewat run_command).
        # File yang sudah diemit live di-skip (hindari duplicate event).
        if change_tracker is not None:
            try:
                records = change_tracker.detect_changes(task_id, path=".")
                change_tracker.finish(task_id)
                for rec in records:
                    rel = _normalize_change_path(rec.path)
                    if rel in live_changed:
                        continue
                    self._emit(
                        session_id,
                        EventType.CHANGE_DETECTED,
                        task_id=task_id,
                        payload={
                            "path": rel,
                            "kind": rec.change_type.value,
                            "before_size": rec.before_size,
                            "after_size": rec.after_size,
                        },
                    )
            except Exception:  # noqa: BLE001 - deteksi tidak boleh crash task
                pass

        # Sinkronkan status akhir dari runtime ke record gateway.
        # Catatan: event terminal (task_completed/task_failed/task_cancelled)
        # diemit oleh AgentRuntime (sumber tunggal observability). Di sini hanya
        # update status record gateway.
        runtime_status = getattr(result, "status", None)
        if runtime_status == RuntimeStatus.CANCELLED:
            # Dibatalkan secara kooperatif: CANCELLED, bukan FAILED (tanpa retry).
            status = TaskStatus.CANCELLED.value
        elif runtime_status == RuntimeStatus.COMPLETED:
            status = TaskStatus.COMPLETED.value
        elif runtime_status == RuntimeStatus.FAILED:
            status = TaskStatus.FAILED.value
        else:
            # Backward compatible: hasil runtime yang tidak mengekspos `.status`
            # (mis. runtime/fake verifier) memakai flag `.success` seperti dulu.
            status = (
                TaskStatus.COMPLETED.value
                if getattr(result, "success", False)
                else TaskStatus.FAILED.value
            )

        if on_status is not None:
            on_status(status, result.result, result.error)

        return {
            "status": status,
            "result": result.result,
            "error": result.error,
            "iterations": result.iterations,
        }


def run_in_background(target: Callable[[], Any]) -> threading.Thread:
    """Jalankan `target` di background thread daemon (minimal, tanpa queue).

    Ini BUKAN worker framework / Task Queue subsystem: hanya satu thread
    daemon per task agar request HTTP tidak blocking. Technical debt: tidak ada
    retry/persistence/backpressure (lihat laporan).
    """
    thread = threading.Thread(target=target, daemon=True, name="aether-task-exec")
    thread.start()
    return thread
