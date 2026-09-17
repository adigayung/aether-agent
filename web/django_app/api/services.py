"""Service/facade tipis untuk AETHER Gateway (#50).

Gateway HANYA memanggil komponen AETHER yang sudah ada. TIDAK menduplikasi
logic Agent/Runtime/Orchestrator/Planning/Tool/Validation/Recovery/Routing/
Fallback/Project Intelligence/Session.

Yang dipakai dari AETHER:
    - ProjectRegistry  (src/agent_ai/projects)  -> daftar project
    - TaskPreparation  (src/agent_ai/task)      -> siapkan task (read-only)
    - TaskExecutor     (api/execution.py)       -> bridge ke AgentRuntime (#55)

Task execution (#55): setelah task disiapkan, eksekusi nyata dijalankan lewat
AETHER Runtime di background thread daemon (non-blocking HTTP). TIDAK ada
background queue / worker framework / database. State task disimpan di memori
proses. Event eksekusi memakai SessionStore AETHER (event system existing).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent_ai.projects.registry import (
    ProjectNotFoundError,
    ProjectRegistry,
    ProjectRootNotFoundError,
)
from agent_ai.session.store import InMemorySessionStore, SessionStore
from agent_ai.task.preparation import TaskPreparation
from agent_ai.tasks.models import new_task_id

from api.project_store import ProjectStore


class GatewayError(Exception):
    """Base error gateway (dipetakan ke HTTP oleh views)."""

    status_code = 500
    code = "gateway_error"

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code

    def to_dict(self) -> Dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message}}


class ValidationError(GatewayError):
    """Input request tidak valid."""

    status_code = 400
    code = "validation_error"


class NotFoundError(GatewayError):
    """Resource tidak ditemukan."""

    status_code = 404
    code = "not_found"


@dataclass
class TaskRecord:
    """State task di gateway (in-memory, tanpa database).

    Attributes:
        task_id: id task.
        task: deskripsi task.
        project_id: project terkait (opsional).
        status: status task (lifecycle: prepared/running/completed/failed).
        prepared: ringkasan PreparedTask (task + plan metadata).
        metadata: info tambahan bebas.
        session_id: session AETHER untuk event streaming (#51).
        result: hasil akhir runtime (bila completed).
        error: pesan error runtime (bila failed).
        runtime: ringkasan hasil runtime (iterations, dll).
    """

    task_id: str
    task: str
    project_id: Optional[str] = None
    status: str = "prepared"
    prepared: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    session_id: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    runtime: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task": self.task,
            "project_id": self.project_id,
            "status": self.status,
            "prepared": self.prepared,
            "metadata": self.metadata,
            "session_id": self.session_id,
            "result": self.result,
            "error": self.error,
            "runtime": self.runtime,
        }


class GatewayService:
    """Facade tipis menuju komponen AETHER yang sudah ada.

    Args:
        project_registry: ProjectRegistry opsional (default: registry AETHER).
        task_preparation: TaskPreparation opsional (default: TaskPreparation()).
    """

    def __init__(
        self,
        project_registry: Optional[ProjectRegistry] = None,
        task_preparation: Optional[TaskPreparation] = None,
        session_store: Optional[SessionStore] = None,
        task_executor: Optional[Any] = None,
        auto_execute: bool = True,
        project_store: Optional[ProjectStore] = None,
    ) -> None:
        self.projects = project_registry or ProjectRegistry()
        self.preparation = task_preparation or TaskPreparation()
        # Persistence launcher + active project (SQLite, layer gateway).
        # Bukan Project Registry kedua: registry AETHER tetap sumber kebenaran
        # struktur project; store ini hanya metadata launcher + active state.
        self.project_store = project_store or ProjectStore()
        # SessionStore AETHER (event system existing). Dipakai untuk event
        # streaming (#51). Tidak ada event model kedua.
        self.sessions = session_store or InMemorySessionStore()
        # Execution bridge ke AETHER Runtime (#55). Dibuat lazy agar import
        # runtime tidak membebani jalur read-only (health/projects).
        self._task_executor = task_executor
        self.auto_execute = auto_execute
        self._tasks: Dict[str, TaskRecord] = {}
        # PreparedTask asli (bukan ringkasan) untuk diteruskan ke runtime.
        self._prepared: Dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def task_executor(self) -> Any:
        """TaskExecutor (execution bridge) lazy — dibuat saat pertama dipakai."""
        if self._task_executor is None:
            from api.execution import TaskExecutor

            self._task_executor = TaskExecutor(self.sessions)
        return self._task_executor

    # ------------------------------------------------------------------ #
    # Health
    # ------------------------------------------------------------------ #
    def health(self) -> Dict[str, Any]:
        """Status gateway sederhana (tanpa memanggil model/API)."""
        return {"status": "ok", "service": "aether-gateway"}

    # ------------------------------------------------------------------ #
    # Config (dibaca dari AETHER settings; TIDAK hardcode di frontend)
    # ------------------------------------------------------------------ #
    def get_config(self) -> Dict[str, Any]:
        """Konfigurasi provider/model/mode dari AETHER settings.

        Frontend TIDAK meng-hardcode nama model/provider: semua dibaca dari
        konfigurasi AETHER yang sudah ada (provider-agnostic).

        `providers` = daftar nama provider terdaftar (ProviderRegistry).
        `models`    = daftar {provider, model} yang benar-benar tersedia dari
                      konfigurasi AETHER (.env). Dipakai model selector UI agar
                      pilihan provider+model dapat diteruskan ke runtime.
        """
        from agent_ai.config.settings import settings
        from agent_ai.providers.registry import registry

        provider = settings.default_provider
        # Model default per provider (dari settings AETHER, bukan hardcode UI).
        model = ""
        if provider == "ollama":
            model = settings.ollama.model
        elif provider == "deepseek":
            model = settings.deepseek.model
        elif provider == "openai":
            model = settings.openai.model
        elif provider == "openrouter":
            model = settings.openrouter.model

        # Daftar provider+model yang benar-benar tersedia dari konfigurasi
        # AETHER. Sumber tunggal: settings provider (bukan mapping baru).
        models: List[Dict[str, str]] = []
        for name in registry.list_providers():
            cfg = getattr(settings, name, None)
            cfg_model = getattr(cfg, "model", "") if cfg is not None else ""
            if cfg_model:
                models.append({"provider": name, "model": cfg_model})

        # Mode = retrieval profile AETHER (#40): minimal | balanced | deep.
        # Dipetakan ke label user (Fast/Balanced/Deep) di frontend, bukan
        # routing baru.
        return {
            "provider": provider,
            "model": model,
            "providers": registry.list_providers(),
            "models": models,
            "mode": settings.context.retrieval_profile,
            "modes": ["minimal", "balanced", "deep"],
        }

    # ------------------------------------------------------------------ #
    # Projects (memakai ProjectRegistry AETHER)
    # ------------------------------------------------------------------ #
    def list_projects(self) -> List[Dict[str, Any]]:
        """Daftar project terdaftar (dari ProjectRegistry AETHER)."""
        return [p.to_dict() for p in self.projects.list()]

    def get_project(self, id_or_name: str) -> Dict[str, Any]:
        """Ambil project berdasarkan id/name.

        Raises:
            NotFoundError: bila project tidak ditemukan.
        """
        try:
            data = self.projects.get(id_or_name).to_dict()
        except ProjectNotFoundError as exc:
            raise NotFoundError(str(exc)) from exc
        # Alias `path` = `root` agar frontend konsisten.
        data["path"] = data.get("root")
        return data

    # ------------------------------------------------------------------ #
    # Project Launcher + Active Project (SQLite, layer gateway)
    # ------------------------------------------------------------------ #
    def list_launcher_projects(self) -> List[Dict[str, Any]]:
        """Daftar project untuk Project Launcher (dari SQLite store).

        Sumber daftar launcher = record SQLite (persistence launcher state).
        Ini konsisten dengan delete: menghapus RECORD SQLite langsung
        menghilangkan project dari launcher, tanpa bergantung pada folder
        registry. `path` di-alias dari kolom `path` store agar frontend
        konsisten (launcher menampilkan `path`).
        """
        projects = self.project_store.list_projects()
        for p in projects:
            # Alias `path` (store) tetap `path`; sediakan `root` agar konsisten
            # dengan kontrak project AETHER (registry memakai `root`).
            p["root"] = p.get("path")
        return projects

    def create_project(self, name: str, path: str) -> Dict[str, Any]:
        """Buat project baru: validasi path, daftarkan ke AETHER, jadikan aktif.

        Mengintegrasikan ProjectRegistry AETHER (Core) untuk struktur project,
        lalu menyimpan metadata launcher (last_opened_at) di SQLite. Project
        baru langsung dijadikan active project.

        Raises:
            ValidationError: bila name/path kosong atau path tidak valid.
        """
        if not name or not isinstance(name, str) or not name.strip():
            raise ValidationError("Field 'name' wajib diisi dan tidak boleh kosong.")
        if not path or not isinstance(path, str) or not path.strip():
            raise ValidationError("Field 'path' wajib diisi dan tidak boleh kosong.")

        # Validasi path (tanpa mengubah filesystem).
        from pathlib import Path as _Path

        root = _Path(path.strip())
        if not root.exists() or not root.is_dir():
            raise ValidationError(f"Project path tidak ditemukan: {path}")

        # Daftarkan ke ProjectRegistry AETHER (Core) -> struktur project.
        try:
            config = self.projects.register(name=name.strip(), root=str(root))
        except ProjectRootNotFoundError as exc:
            raise ValidationError(str(exc)) from exc

        # Simpan metadata launcher di SQLite (id sama dengan registry AETHER).
        self.project_store.add_project_with_id(config.id, config.name, config.root)
        # Jadikan active project.
        self.project_store.set_active_project(config.id)
        self.project_store.touch_opened(config.id)
        meta = self.project_store.get_project(config.id)
        meta["root"] = meta.get("path")
        return meta

    def delete_project(self, project_id: str) -> Dict[str, Any]:
        """Hapus RECORD project dari database SQLite AETHER.

        HANYA menghapus record di SQLite (launcher state). TIDAK menghapus,
        memindahkan, atau mengubah folder/filesystem project (baik folder
        project target maupun metadata registry AETHER). Bila project yang
        dihapus sedang aktif, active project ikut dibersihkan (dilakukan di
        ProjectStore.delete_project).

        Raises:
            NotFoundError: bila record project tidak ditemukan di SQLite.
        """
        deleted = self.project_store.delete_project(project_id)
        if not deleted:
            raise NotFoundError(f"Project '{project_id}' tidak ditemukan.")
        return {"deleted": True, "id": project_id}

    def set_active_project(self, project_id: str) -> Dict[str, Any]:
        """Jadikan project sebagai active project (persistent).

        Validasi terhadap record SQLite (konsisten dengan daftar launcher).

        Raises:
            NotFoundError: bila project tidak ditemukan.
        """
        meta = self.project_store.get_project(project_id)
        if meta is None:
            raise NotFoundError(f"Project '{project_id}' tidak ditemukan.")
        self.project_store.set_active_project(project_id)
        self.project_store.touch_opened(project_id)
        meta = self.project_store.get_project(project_id)
        meta["root"] = meta.get("path")
        return meta

    def get_active_project(self) -> Optional[Dict[str, Any]]:
        """Ambil active project (None bila tidak ada).

        Dibaca dari record SQLite (konsisten dengan daftar launcher), bukan
        dari registry, agar active project tetap valid walau folder registry
        tidak ada.
        """
        project_id = self.project_store.get_active_project_id()
        if not project_id:
            return None
        meta = self.project_store.get_project(project_id)
        if meta is None:
            # Record sudah tidak ada -> bersihkan active state.
            self.project_store.clear_active_project()
            return None
        meta["root"] = meta.get("path")
        return meta

    def clear_active_project(self) -> Dict[str, Any]:
        """Close Project: hapus active project state (project tetap tersimpan)."""
        self.project_store.clear_active_project()
        return {"active_project": None}

    def open_active_project_in_explorer(self) -> Dict[str, Any]:
        """Buka Windows Explorer pada path ACTIVE PROJECT (bukan arbitrary path).

        Path TIDAK diterima dari frontend: selalu diambil dari active project
        yang tersimpan di backend. Ini mencegah frontend membuka path sembarang.

        Raises:
            NotFoundError: bila tidak ada active project.
            ValidationError: bila path project tidak valid / bukan directory.
        """
        active = self.get_active_project()
        if active is None:
            raise NotFoundError("Tidak ada active project.")

        root = active.get("root") or active.get("path")
        if not root:
            raise ValidationError("Active project tidak memiliki path.")

        from pathlib import Path as _Path

        target = _Path(root)
        if not target.exists() or not target.is_dir():
            raise ValidationError(f"Path project tidak ditemukan: {root}")

        import os
        import subprocess
        import sys

        try:
            if sys.platform.startswith("win"):
                # Buka Explorer pada folder (bukan shell command dari user).
                os.startfile(str(target))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except Exception as exc:  # noqa: BLE001 - gagal buka explorer -> error jelas
            raise ValidationError(f"Gagal membuka Explorer: {exc}") from exc

        return {"opened": True, "path": str(target)}

    def list_project_files(self, path: str = ".") -> Dict[str, Any]:
        """Daftar file project aktif (read-only) via ListFilesTool AETHER.

        Memakai tool filesystem AETHER yang sudah ada (bukan abstraksi baru).
        Root dibatasi ke path project aktif.

        Raises:
            NotFoundError: bila tidak ada active project.
            ValidationError: bila path di luar project root.
        """
        active = self.get_active_project()
        if active is None:
            raise NotFoundError("Tidak ada active project.")

        from pathlib import Path as _Path

        from agent_ai.tools.base import ToolError
        from agent_ai.tools.filesystem import ListFilesTool

        root = active.get("root") or active.get("path")
        tool = ListFilesTool(root=_Path(root))
        try:
            return tool.execute(path=path or ".")
        except ToolError as exc:
            raise ValidationError(str(exc)) from exc

    # ------------------------------------------------------------------ #
    # Tasks
    # ------------------------------------------------------------------ #
    def create_task(
        self,
        task: str,
        project_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Buat task: validasi + siapkan via TaskPreparation, lalu eksekusi.

        Menyiapkan (context + plan) memakai komponen AETHER yang sudah ada,
        menyimpan state task di memori, lalu (bila auto_execute) menjalankan
        eksekusi nyata lewat AETHER Runtime di background thread.

        Args:
            task: deskripsi task (wajib, non-kosong).
            project_id: project terkait (opsional; divalidasi bila diisi).
            metadata: metadata tambahan (opsional).

        Returns:
            TaskRecord sebagai dict.

        Raises:
            ValidationError: bila task kosong.
            NotFoundError: bila project_id diisi tapi tidak ditemukan.
        """
        if not task or not isinstance(task, str) or not task.strip():
            raise ValidationError("Field 'task' wajib diisi dan tidak boleh kosong.")

        # Validasi project bila diberikan (terhadap record SQLite launcher).
        if project_id:
            if self.project_store.get_project(project_id) is None:
                raise NotFoundError(f"Project '{project_id}' tidak ditemukan.")

        task_id = new_task_id()
        prepared = self.preparation.prepare(task.strip(), task_id=task_id)

        # Session AETHER untuk event streaming (#51). Satu session per task.
        session = self.sessions.create_session(
            project_id=project_id,
            metadata={"task_id": task_id},
        )
        self.sessions.create_task_reference(session.session_id, task_id)

        record = TaskRecord(
            task_id=task_id,
            task=prepared.task,
            project_id=project_id,
            status="prepared",
            prepared={
                "has_context": prepared.metadata.get("has_context", False),
                "has_plan": prepared.metadata.get("has_plan", False),
                "plan_kind": prepared.metadata.get("plan_kind"),
                "step_count": prepared.metadata.get("step_count", 0),
            },
            metadata=dict(metadata or {}),
            session_id=session.session_id,
        )
        with self._lock:
            self._tasks[task_id] = record
            self._prepared[task_id] = prepared

        # Event TASK_CREATED (memakai event AETHER existing).
        self._emit(
            session.session_id,
            "task_created",
            task_id=task_id,
            payload={"task": record.task, "project_id": project_id},
        )

        # Jalankan eksekusi nyata di background (non-blocking HTTP).
        if self.auto_execute:
            self._start_execution(task_id)

        return record.to_dict()

    # ------------------------------------------------------------------ #
    # Execution bridge (#55)
    # ------------------------------------------------------------------ #
    def _start_execution(self, task_id: str) -> None:
        """Mulai eksekusi task di background thread (minimal, tanpa queue)."""
        from api.execution import run_in_background

        run_in_background(lambda: self._execute_task(task_id))

    def _execute_task(self, task_id: str) -> None:
        """Jalankan task lewat AETHER Runtime (dipanggil di background thread).

        Error apa pun ditangkap dan dicatat sebagai status FAILED agar thread
        tidak crash dan task tidak menggantung di status 'running'.
        """
        with self._lock:
            record = self._tasks.get(task_id)
            prepared = self._prepared.get(task_id)
        if record is None or prepared is None:
            return

        def on_status(status: str, result: Optional[str], error: Optional[str]) -> None:
            self._update_task_status(task_id, status, result=result, error=error)

        # Workspace root = active project root (bila ada). Ini mengarahkan
        # tool filesystem/workspace ke folder project aktif sehingga write/edit
        # relatif terhadap project, bukan root AETHER.
        workspace_root = self._resolve_workspace_root(record.project_id)

        # Pilihan provider/model eksplisit dari UI (metadata task). Diteruskan
        # ke runtime agar task benar-benar memakai provider/model yang dipilih,
        # bukan selalu default provider. Bila kosong -> perilaku default.
        meta = record.metadata or {}
        provider_name = meta.get("provider") or None
        model_name = meta.get("model") or None

        try:
            summary = self.task_executor.run(
                prepared,
                session_id=record.session_id,
                task_id=task_id,
                on_status=on_status,
                workspace_root=workspace_root,
                provider_name=provider_name,
                model_name=model_name,
            )
        except Exception as exc:  # noqa: BLE001 - jangan biarkan thread crash
            self._update_task_status(
                task_id,
                "failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return

        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is not None:
                rec.runtime = {
                    "iterations": summary.get("iterations", 0),
                }

    def _resolve_workspace_root(self, project_id: Optional[str]) -> Optional[str]:
        """Tentukan workspace root untuk eksekusi task.

        Prioritas: project task -> active project -> None (root AETHER default).
        Mengembalikan path root project (string) atau None bila tidak ada.
        Path diambil dari record SQLite (konsisten dengan launcher).
        """
        candidate = project_id
        if not candidate:
            candidate = self.project_store.get_active_project_id()
        if not candidate:
            return None
        meta = self.project_store.get_project(candidate)
        if meta is None:
            return None
        return meta.get("path")

    def _update_task_status(
        self,
        task_id: str,
        status: str,
        *,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Update status/result/error sebuah TaskRecord (thread-safe)."""
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            record.status = status
            if result is not None:
                record.result = result
            if error is not None:
                record.error = error

    def _emit(
        self,
        session_id: str,
        event_type: Any,
        *,
        task_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit event ke SessionStore AETHER (tanpa event bus baru)."""
        try:
            self.emit_event(session_id, event_type, task_id=task_id, payload=payload)
        except Exception:  # noqa: BLE001 - event emission tidak boleh crash
            return

    def get_task(self, task_id: str) -> Dict[str, Any]:
        """Ambil task berdasarkan id.

        Raises:
            NotFoundError: bila task tidak ditemukan.
        """
        with self._lock:
            record = self._tasks.get(task_id)
        if record is None:
            raise NotFoundError(f"Task '{task_id}' tidak ditemukan.")
        return record.to_dict()

    def list_tasks(self) -> List[Dict[str, Any]]:
        """Daftar task yang dibuat (in-memory)."""
        with self._lock:
            return [r.to_dict() for r in self._tasks.values()]

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        """Minta penghentian task: tandai CANCELLED + emit event AETHER.

        Catatan: AETHER Runtime existing TIDAK menyediakan cooperative
        cancellation. Method ini menandai status task di gateway dan mengemit
        event `task_cancelled` (event AETHER existing) agar UI/observability
        konsisten. Ini BUKAN execution/stop engine kedua.

        Raises:
            NotFoundError: bila task tidak ditemukan.
        """
        with self._lock:
            record = self._tasks.get(task_id)
        if record is None:
            raise NotFoundError(f"Task '{task_id}' tidak ditemukan.")

        # Hanya task yang belum terminal yang bisa dibatalkan.
        if record.status in ("completed", "failed", "cancelled"):
            return record.to_dict()

        self._update_task_status(task_id, "cancelled")
        if record.session_id:
            self._emit(
                record.session_id,
                "task_cancelled",
                task_id=task_id,
                payload={"reason": "user_requested"},
            )
        return self.get_task(task_id)

    # ------------------------------------------------------------------ #
    # Events (memakai SessionStore AETHER; tanpa event model kedua)
    # ------------------------------------------------------------------ #
    def emit_event(
        self,
        session_id: str,
        event_type: Any,
        *,
        task_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Append event ke SessionStore AETHER (append-only).

        Hanya meneruskan ke store existing; tidak membuat model event baru.
        """
        from agent_ai.session.events import EventType, make_event

        if not isinstance(event_type, EventType):
            event_type = EventType(event_type)
        event = make_event(
            session_id=session_id,
            event_type=event_type,
            task_id=task_id,
            payload=payload,
        )
        stored = self.sessions.append_event(event)
        return stored.to_dict()


# ---------------------------------------------------------------------------
# Instance default (dipakai views). Dibuat lazy agar mudah di-override test.
# ---------------------------------------------------------------------------
_default_service: Optional[GatewayService] = None


def get_service() -> GatewayService:
    """Ambil instance GatewayService default (lazy singleton)."""
    global _default_service
    if _default_service is None:
        _default_service = GatewayService()
    return _default_service
