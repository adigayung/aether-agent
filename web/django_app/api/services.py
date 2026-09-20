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

Konfigurasi provider (provider-agnostic): provider instance + model dibaca dari
LLMConfigService (SQLite GLOBAL `data/aether.db`, tabel yang sama dengan
ProjectStore) via metadata task (`provider_instance_id`, `model_id`). Sumber
tunggal pemilihan provider aktif = Provider Instance -> Model (SQLite), BUKAN
.env. `get_config()` juga mengekspos daftar provider instance + model ke UI
agar pilihan tidak di-hardcode di frontend.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent_ai.core.cancel import CancellationToken
from agent_ai.projects.registry import (
    ProjectNotFoundError,
    ProjectRegistry,
    ProjectRootNotFoundError,
)
from agent_ai.session.store import InMemorySessionStore, SessionStore
from agent_ai.task.preparation import TaskPreparation
from agent_ai.tasks.models import new_task_id

from api.project_store import ProjectStore


# Batas ukuran gambar Consultant (per gambar, estimasi decoded bytes). Bounded
# agar payload base64 tidak membengkakkan request/response (anti OOM).
_MAX_CONSULT_IMAGE_BYTES = 8_000_000


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


class ConflictError(GatewayError):
    """Konflik resource (mis. nama provider instance sudah dipakai)."""

    status_code = 409
    code = "conflict"


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
    # Status antrian (TAMPILAN/kontrol UI), TERPISAH dari `status` lifecycle.
    # Nilai: "pending" | "running" | "disabled" | "done".
    # SENGAJA bukan bagian dari enum TaskStatus core / TERMINAL_STATUSES, agar
    # TaskState/TaskLifecycle/.aether/log/SSE/task history tidak terpengaruh.
    # Pada tahap ini belum ada scheduler serial: nilai queue_state hanya
    # merepresentasikan niat user (mis. disable = jangan dieksekusi).
    queue_state: str = "pending"
    # Urutan posisi di antrian (FIFO by creation; Move Up/Down mengubah nilai).
    queue_order: int = 0

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
            "queue_state": self.queue_state,
            "queue_order": self.queue_order,
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
        llm_config_service: Optional[Any] = None,
        consultant_service: Optional[Any] = None,
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
        # Konfigurasi LLM tersimpan (SQLite) — provider instance + model.
        # Lazy agar jalur read-only tetap ringan. Database GLOBAL AETHER.
        self._llm_config_service = llm_config_service
        # Consultant (AETHER reasoning layer, read-only terhadap CODE PROJECT).
        # Lazy agar jalur read-only tetap ringan; verifier dapat menyuntikkan.
        self._consultant_service = consultant_service
        self._tasks: Dict[str, TaskRecord] = {}
        # PreparedTask asli (bukan ringkasan) untuk diteruskan ke runtime.
        self._prepared: Dict[str, Any] = {}
        self._lock = threading.Lock()
        # Cooperative cancellation: satu token per task yang sedang dieksekusi.
        # Bukan sistem cancellation kedua — primitif tunggal (agent_ai.core.cancel)
        # yang dibagikan ke runtime/orchestrator agar loop berhenti di safe
        # boundary. Token dihapus saat eksekusi selesai.
        self._cancel_tokens: Dict[str, "CancellationToken"] = {}
        # Monotonic counter untuk urutan antrian (di belakang self._lock).
        self._queue_seq = 0
        # Scheduler serial GLOBAL (1 execution slot). `_pumping` hanya penjaga
        # re-entrancy agar pump tidak rekursif; keputusan "slot bebas" SELALU
        # dibaca ulang dari self._tasks di dalam self._lock (bukan flag ini).
        # Ini BUKAN worker framework/queue subsystem kedua: satu queue, satu
        # scheduler, satu slot — sumber data tetap self._tasks.
        self._pumping = False

    @property
    def task_executor(self) -> Any:
        """TaskExecutor (execution bridge) lazy — dibuat saat pertama dipakai."""
        if self._task_executor is None:
            from api.execution import TaskExecutor

            self._task_executor = TaskExecutor(
                self.sessions, llm_config_service=self.llm_config_service
            )
        return self._task_executor

    @property
    def llm_config_service(self) -> Any:
        """LLMConfigService efektif (lazy; database GLOBAL `data/aether.db`).

        Dibuat lazy agar gateway tetap ringan pada jalur read-only, dan agar
        verifier dapat menyuntikkan service dengan DB fixture sementara.
        """
        if self._llm_config_service is None:
            from agent_ai.llm_config import LLMConfigService

            self._llm_config_service = LLMConfigService()
        return self._llm_config_service

    @property
    def consultant_service(self) -> Any:
        """ConsultantService AETHER (lazy).

        Consultant adalah reasoning layer (bukan Agent eksekutor): memakai loop
        & tool AETHER yang sudah ada dengan boundary read-only terhadap CODE
        PROJECT dan read+update terhadap Project Bible. Dibuat lazy agar jalur
        read-only gateway tetap ringan.
        """
        if self._consultant_service is None:
            from agent_ai.consultant import ConsultantService

            self._consultant_service = ConsultantService()
        return self._consultant_service

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
        """Konfigurasi provider/model/mode dari AETHER.

        Frontend TIDAK meng-hardcode nama model/provider: semua dibaca dari
        konfigurasi AETHER yang sudah ada (provider-agnostic).

        Sumber tunggal pemilihan provider aktif = Provider Instance + Model
        (SQLite). `providers` = daftar nama provider terdaftar (ProviderRegistry,
        hanya katalog), `provider_instances` = instance + nested model tersimpan,
        `provider_instance_id`/`model_id` = default terpilih.
        """
        from agent_ai.config.settings import settings
        from agent_ai.providers.registry import registry

        # Provider instance + model dari konfigurasi LLM tersimpan (SQLite).
        # Sumber tunggal pemilihan provider aktif: Provider Instance -> Model.
        # Frontend membaca dari sini (bukan hardcode) sehingga task dapat
        # menunjuk provider_instance_id + model_id yang benar-benar tersimpan.
        instances: List[Dict[str, Any]] = []
        try:
            instances = self.llm_config_service.get_full_config()
        except Exception:  # noqa: BLE001 - config read tidak boleh mematikan UI
            instances = []

        # Default terpilih: instance enabled pertama yang punya model enabled.
        default_instance_id = ""
        default_model_id = ""
        for inst in instances:
            if not inst.get("enabled"):
                continue
            inst_models = inst.get("models") or []
            enabled_models = [m for m in inst_models if m.get("enabled")] or inst_models
            if enabled_models:
                default_instance_id = inst.get("id", "")
                default_model_id = enabled_models[0].get("id", "")
                break

        return {
            "providers": registry.list_providers(),
            "provider_instances": instances,
            "provider_instance_id": default_instance_id,
            "model_id": default_model_id,
            "mode": settings.context.retrieval_profile,
            "modes": ["minimal", "balanced", "deep"],
        }

    # ------------------------------------------------------------------ #
    # LLM Config (halaman Settings; LLMConfigService AETHER existing)
    #
    # Gateway HANYA memanggil facade CRUD konfigurasi LLM AETHER
    # (`agent_ai.llm_config`). TIDAK ada model konfigurasi kedua. Nilai
    # secret (.env) TIDAK pernah dikembalikan: hanya versi masked.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _llm_error_to_gateway(exc: Exception) -> GatewayError:
        """Petakan error konfigurasi LLM AETHER -> error gateway (HTTP)."""
        from agent_ai.llm_config import (
            LLMConfigConflictError,
            LLMConfigNotFoundError,
            LLMConfigValidationError,
        )

        if isinstance(exc, LLMConfigNotFoundError):
            return NotFoundError(str(exc))
        if isinstance(exc, LLMConfigConflictError):
            return ConflictError(str(exc))
        if isinstance(exc, LLMConfigValidationError):
            return ValidationError(str(exc))
        return GatewayError(str(exc))

    def get_llm_config(self) -> Dict[str, Any]:
        """Konfigurasi LLM lengkap untuk halaman Settings (TANPA secret).

        Mengembalikan:
            credentials: daftar credential .env (masked + relasi pemakai),
            provider_types: katalog provider type (statis), dan
            providers: provider instance tersimpan + nested model.
        """
        from agent_ai.llm_config import list_provider_types

        try:
            credentials = self.llm_config_service.list_credentials()
            providers = self.llm_config_service.get_full_config()
        except Exception as exc:  # noqa: BLE001 - error baca -> error gateway
            raise self._llm_error_to_gateway(exc) from exc

        return {
            "credentials": [c.to_dict() for c in credentials],
            "provider_types": [t.to_dict() for t in list_provider_types()],
            "providers": providers,
        }

    def list_llm_providers(self) -> List[Dict[str, Any]]:
        """Daftar provider instance + nested model (TANPA secret).

        Dipakai alur New Task: dropdown Provider Instance + Model diambil dari
        konfigurasi LLM tersimpan (SQLite) via LLMConfigService AETHER existing,
        BUKAN dari settings/.env. Nilai secret tidak pernah dikembalikan.
        """
        try:
            return self.llm_config_service.get_full_config()
        except Exception as exc:  # noqa: BLE001 - error baca -> error gateway
            raise self._llm_error_to_gateway(exc) from exc

    # ---- Credential (.env API key) ----
    def create_llm_credential(self, name: str, value: str) -> Dict[str, Any]:
        """Simpan/set API key di .env (dikembalikan hanya versi masked)."""
        from agent_ai.llm_config import LLMConfigError

        try:
            info = self.llm_config_service.set_api_key(name, value)
        except LLMConfigError as exc:
            raise self._llm_error_to_gateway(exc) from exc
        return info.to_dict()

    def delete_llm_credential(self, name: str, force: bool = False) -> Dict[str, Any]:
        """Hapus API key dari .env (hanya baris variabel terkait)."""
        from agent_ai.llm_config import LLMConfigError

        try:
            deleted = self.llm_config_service.delete_api_key(name, force=bool(force))
        except LLMConfigError as exc:
            raise self._llm_error_to_gateway(exc) from exc
        if not deleted:
            raise NotFoundError(f"Credential '{name}' tidak ditemukan.")
        return {"deleted": True, "name": name}

    # ---- Provider Instance ----
    def create_llm_provider(
        self,
        name: str,
        provider_type: str,
        api_key_env: str = "",
        api_url: str = "",
        enabled: bool = True,
    ) -> Dict[str, Any]:
        """Buat provider instance baru (relasi ke credential .env)."""
        from agent_ai.llm_config import LLMConfigError

        try:
            instance = self.llm_config_service.create_provider_instance(
                name=name,
                provider_type=provider_type,
                api_key_env=api_key_env,
                api_url=api_url,
                enabled=enabled,
            )
            return self.llm_config_service.get_provider_config(instance.id)
        except LLMConfigError as exc:
            raise self._llm_error_to_gateway(exc) from exc

    def update_llm_provider(
        self,
        provider_id: str,
        name: Optional[str] = None,
        provider_type: Optional[str] = None,
        api_key_env: Optional[str] = None,
        api_url: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Update provider instance (field None = tidak diubah)."""
        from agent_ai.llm_config import LLMConfigError

        try:
            self.llm_config_service.update_provider_instance(
                provider_id,
                name=name,
                provider_type=provider_type,
                api_key_env=api_key_env,
                api_url=api_url,
                enabled=enabled,
            )
            return self.llm_config_service.get_provider_config(provider_id)
        except LLMConfigError as exc:
            raise self._llm_error_to_gateway(exc) from exc

    def delete_llm_provider(self, provider_id: str) -> Dict[str, Any]:
        """Hapus provider instance (beserta model-nya, cascade di store)."""
        deleted = self.llm_config_service.delete_provider_instance(provider_id)
        if not deleted:
            raise NotFoundError(f"Provider instance '{provider_id}' tidak ditemukan.")
        return {"deleted": True, "id": provider_id}

    # ---- Model ----
    def create_llm_model(
        self, provider_id: str, model_name: str, enabled: bool = True
    ) -> Dict[str, Any]:
        """Tambah model pada sebuah provider instance."""
        from agent_ai.llm_config import LLMConfigError

        try:
            model = self.llm_config_service.add_model(
                provider_id, model_name, enabled=enabled
            )
        except LLMConfigError as exc:
            raise self._llm_error_to_gateway(exc) from exc
        return model.to_dict()

    def update_llm_model(
        self,
        model_id: str,
        model_name: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Update model (nama/enabled; None = tidak diubah)."""
        from agent_ai.llm_config import LLMConfigError

        try:
            model = self.llm_config_service.update_model(
                model_id, model_name=model_name, enabled=enabled
            )
        except LLMConfigError as exc:
            raise self._llm_error_to_gateway(exc) from exc
        return model.to_dict()

    def delete_llm_model(self, model_id: str) -> Dict[str, Any]:
        """Hapus satu model."""
        deleted = self.llm_config_service.delete_model(model_id)
        if not deleted:
            raise NotFoundError(f"Model '{model_id}' tidak ditemukan.")
        return {"deleted": True, "id": model_id}

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

    def reveal_file_in_explorer(self, file_path: str) -> Dict[str, Any]:
        """Buka Windows Explorer dan highlight file tertentu.

        Path harus berada di dalam active project root.
        Backend memvalidasi path sebelum membuka Explorer.

        Raises:
            NotFoundError: bila tidak ada active project.
            ValidationError: bila path di luar project root atau tidak ditemukan.
        """
        active = self.get_active_project()
        if active is None:
            raise NotFoundError("Tidak ada active project.")

        root = active.get("root") or active.get("path")
        if not root:
            raise ValidationError("Active project tidak memiliki path.")

        from pathlib import Path as _Path

        root_resolved = _Path(root).resolve()
        target = _Path(file_path)
        if not target.is_absolute():
            target = root_resolved / target
        target = target.resolve()

        # Validasi: path harus berada di dalam project root.
        if target != root_resolved and root_resolved not in target.parents:
            raise ValidationError(f"Path '{file_path}' berada di luar project root.")

        if not target.exists():
            raise ValidationError(f"File tidak ditemukan: {file_path}")

        import os
        import subprocess
        import sys

        try:
            if sys.platform.startswith("win"):
                # Buka Explorer dan highlight file menggunakan /select,
                # yang didukung oleh Windows Explorer.
                subprocess.Popen(
                    ["explorer", "/select,", str(target)],  # type: ignore[attr-defined]
                )
            else:
                # Non-Windows: buka folder induk.
                parent = target.parent
                if sys.platform == "darwin":
                    subprocess.Popen(["open", str(parent)])
                else:
                    subprocess.Popen(["xdg-open", str(parent)])
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"Gagal membuka Explorer: {exc}") from exc

        return {"opened": True, "path": str(target)}

    def delete_project_entry(self, rel_path: str, entry_type: str = "file") -> Dict[str, Any]:
        """Hapus file atau folder dari project active.

        Path harus berada di dalam active project root.
        Untuk folder, isi folder juga dihapus secara rekursif.

        Raises:
            NotFoundError: bila tidak ada active project.
            ValidationError: bila path di luar project root atau tidak ditemukan.
        """
        active = self.get_active_project()
        if active is None:
            raise NotFoundError("Tidak ada active project.")

        root = active.get("root") or active.get("path")
        if not root:
            raise ValidationError("Active project tidak memiliki path.")

        from pathlib import Path as _Path

        root_resolved = _Path(root).resolve()
        target = root_resolved / rel_path
        target = target.resolve()

        # Validasi: path harus berada di dalam project root.
        if target != root_resolved and root_resolved not in target.parents:
            raise ValidationError(f"Path '{rel_path}' berada di luar project root.")

        if not target.exists():
            raise ValidationError(f"Path tidak ditemukan: {rel_path}")

        import shutil

        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"Gagal menghapus '{rel_path}': {exc}") from exc

        return {"deleted": True, "path": str(target)}

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

        # Validasi pilihan provider instance/model (bila diberikan) terhadap
        # konfigurasi LLM tersimpan (SQLite). Ini menjamin relasi
        # Provider Instance -> Model valid SEBELUM task dieksekusi.
        self._validate_provider_selection(metadata or {})

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
            self._queue_seq += 1
            record.queue_order = self._queue_seq
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
        # Scheduler serial GLOBAL yang mengontrol slot: task baru SELALU masuk
        # antrian sebagai queue_state="pending" (sudah default TaskRecord), lalu
        # di-promosikan ke RUNNING oleh pump BILA tidak ada blocker di depannya
        # (FIFO by queue_order, skip disabled). Ini menggantikan pemanggilan
        # _start_execution langsung agar concurrency dibatasi 1 slot.
        if self.auto_execute:
            self._scheduler_pump()

        return record.to_dict()

    # ------------------------------------------------------------------ #
    # Serial scheduler GLOBAL (1 execution slot)
    # ------------------------------------------------------------------ #
    def _scheduler_pump(self) -> None:
        """Pilih SATU task pending eligible berikutnya dan jalankan.

        Satu queue GLOBAL, satu scheduler, satu slot. Dipanggil (idempoten):
            - setelah create_task,
            - setelah status terminal (completed/failed/cancelled),
            - setelah disable/enable/cancel.

        Algoritma (seluruh keputusan di dalam self._lock):
            1. Bila sudah ada task RUNNING -> tidak ada slot -> return.
            2. Ambil task pending paling awal menurut queue_order (FIFO).
               Task disabled/done/terminal otomatis dilewati.
            3. Tandai slot terpakai (queue_state="running") secara atomic agar
               task lain tidak bisa mengambil slot yang sama.
            4. Keluar lock, lalu mulai eksekusi (JANGAN tahan lock saat
               TaskExecutor bekerja).

        Re-entrancy dijaga oleh self._pumping; ini hanya mencegah rekursi
        tak terbatas, bukan sumber kebenaran status slot.
        """
        if self._pumping:
            return

        from api.execution import run_in_background

        with self._lock:
            # [1] Sudah ada eksekusi aktif? -> slot terpakai, tidak lanjut.
            #     Slot dianggap terpakai bila ADA task yang queue_state="running"
            #     (slot yang sudah direservasi) ATAU masih ada cancellation token
            #     aktif (thread eksekutor masih hidup, mis. task baru saja
            #     di-cancel tetapi belum selesai wind-down). Ini menutup race
            #     "Stop task running + scheduler mencari task berikutnya" dan
            #     menjamin tidak pernah ada dua eksekusi paralel.
            if self._cancel_tokens or any(
                r.queue_state == "running" for r in self._tasks.values()
            ):
                return
            # [2] Kandidat: pending, bukan terminal, queue_order paling awal.
            candidates = [
                r
                for r in self._tasks.values()
                if r.queue_state == "pending"
                and r.status not in ("completed", "failed", "cancelled")
            ]
            if not candidates:
                return  # Sistem idle.
            candidate = min(candidates, key=lambda r: (r.queue_order, r.task_id))
            # [3] Ambil slot secara atomic (di dalam lock yang sama).
            candidate.queue_state = "running"
            task_id = candidate.task_id
            # Token cancellation dibuat & didaftarkan SINKRON di sini
            # (sebelum thread jalan) agar Stop selalu menemukan token.
            token = CancellationToken()
            self._cancel_tokens[task_id] = token
            self._pumping = True

        try:
            # [4] Mulai eksekusi di luar lock.
            run_in_background(lambda: self._execute_task(task_id, token))
        except Exception:  # noqa: BLE001 - kegagalan start tidak boleh deadlock
            with self._lock:
                self._pumping = False
                self._cancel_tokens.pop(task_id, None)
                rec = self._tasks.get(task_id)
                if rec is not None and rec.queue_state == "running":
                    rec.queue_state = "pending"
            raise
        else:
            with self._lock:
                self._pumping = False

    # ------------------------------------------------------------------ #
    # Execution bridge (#55)
    # ------------------------------------------------------------------ #
    def _start_execution(self, task_id: str) -> None:
        """Mulai eksekusi task di background thread (slot sudah direservasi).

        Token cancellation DIBUAT & DIDAFTARKAN di sini (thread pemanggil,
        sinkron) SEBELUM thread daemon dijalankan. Ini menutup race: Stop yang
        datang tepat setelah task dibuat tetap menemukan token dan dapat
        menandainya (tidak ada jendela "belum terdaftar").

        Catatan: pemanggil normal adalah `_scheduler_pump` (yang sudah menandai
        slot running). Method ini dipertahankan agar tetap kompatibel dengan
        pemanggil existing/verifier yang memanggilnya secara langsung.
        """
        from api.execution import run_in_background

        with self._lock:
            existing = self._tasks.get(task_id)
            if existing is not None and existing.queue_state == "pending":
                existing.queue_state = "running"
        token = CancellationToken()
        with self._lock:
            self._cancel_tokens[task_id] = token
        run_in_background(lambda: self._execute_task(task_id, token))

    def _execute_task(self, task_id: str, token: CancellationToken) -> None:
        """Jalankan task lewat AETHER Runtime (dipanggil di background thread).

        Error apa pun ditangkap dan dicatat sebagai status FAILED agar thread
        tidak crash dan task tidak menggantung di status 'running'.

        Slot release: blok `finally` terluar SELALU melepas execution slot
        (menghapus token + memastikan queue_state tidak "nyangkut" running bila
        runtime gagal tanpa on_status terminal) lalu memanggil `_scheduler_pump`
        agar task berikutnya (per queue_order) mulai. Ini TIDAK menjadikan
        AgentRuntime sebagai scheduler: runtime tetap tak tahu soal queue.
        """
        try:
            self._run_task_inner(task_id, token)
        finally:
            # --- Release execution slot (COMPLETED/FAILED/CANCELLED/semua path).
            with self._lock:
                self._cancel_tokens.pop(task_id, None)
                rec = self._tasks.get(task_id)
                # Bila runtime crash tanpa pernah mengirim status terminal,
                # jangan biarkan slot "nyangkut" running selamanya.
                if rec is not None and rec.queue_state == "running":
                    if rec.status in ("completed", "failed", "cancelled"):
                        rec.queue_state = "done"
                    else:
                        rec.queue_state = "done"
            # Slot bebas -> scheduler memilih task berikutnya (FIFO).
            self._scheduler_pump()

    def _run_task_inner(self, task_id: str, token: CancellationToken) -> None:
        """Isi eksekusi task (dipisah agar slot release di `_execute_task`)."""
        with self._lock:
            record = self._tasks.get(task_id)
            prepared = self._prepared.get(task_id)
        if record is None or prepared is None:
            return

        def on_status(status: str, result: Optional[str], error: Optional[str]) -> None:
            # Sekali pembatalan diminta, jangan biarkan runtime menimpa status
            # CANCELLED dengan completed/failed (menutup race di akhir eksekusi).
            if token.is_cancelled() and status != "cancelled":
                status = "cancelled"
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

        # Pilihan provider instance/model dari konfigurasi LLM tersimpan
        # (SQLite). Bila diisi, backend merakit provider + api_url + api_key +
        # model dari konfigurasi ini (mengalahkan provider_name registry).
        provider_instance_id = meta.get("provider_instance_id") or None
        model_id = meta.get("model_id") or None

        try:
            summary = self.task_executor.run(
                prepared,
                session_id=record.session_id,
                task_id=task_id,
                on_status=on_status,
                workspace_root=workspace_root,
                provider_name=provider_name,
                model_name=model_name,
                provider_instance_id=provider_instance_id,
                model_id=model_id,
                cancel_token=token,
            )
        except Exception as exc:  # noqa: BLE001 - jangan biarkan thread crash
            if token.is_cancelled():
                # Dibatalkan saat error: pertahankan status CANCELLED.
                self._update_task_status(
                    task_id,
                    "cancelled",
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
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

    def _validate_provider_selection(self, metadata: Dict[str, Any]) -> None:
        """Validasi provider_instance_id / model_id dari metadata task.

        Hanya memvalidasi bila field diisi (backward compatible). Mengubah
        error konfigurasi LLM menjadi ValidationError gateway sehingga request
        invalid ditolak dengan pesan jelas.

        Raises:
            ValidationError: instance/model tidak ada atau model bukan milik
                provider instance yang dipilih.
        """
        instance_id = metadata.get("provider_instance_id")
        model_id = metadata.get("model_id")
        if not instance_id and not model_id:
            return

        from agent_ai.llm_config import LLMConfigError

        try:
            if instance_id:
                self.llm_config_service.get_provider_instance(instance_id)
            if model_id:
                model = self.llm_config_service.get_model(model_id)
                if instance_id and model.provider_id != instance_id:
                    raise ValidationError(
                        f"Model '{model_id}' bukan milik provider instance "
                        f"'{instance_id}'."
                    )
        except LLMConfigError as exc:
            raise ValidationError(str(exc)) from exc

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
            # Sinkronisasi queue_state (TAMPILAN antrian) dengan lifecycle status.
            # Ini HANYA proyeksi UI; tidak mengubah semantics eksekusi Agent.
            if status == "running":
                record.queue_state = "running"
            elif status in ("completed", "failed", "cancelled"):
                record.queue_state = "done"

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

    # ------------------------------------------------------------------ #
    # Task Queue (TAMPILAN/kontrol UI — bukan scheduler eksekusi)
    # ------------------------------------------------------------------ #
    # CATATAN: pada tahap ini BELUM ada scheduler serial. queue_state adalah
    # proyeksi UI dari TaskRecord (pending/running/disabled/done) + niat user
    # (disable = jangan dieksekusi). TIDAK ada TaskManager/queue subsystem
    # kedua: sumber data tetap self._tasks (satu queue GLOBAL AETHER).
    _QUEUE_ACTIVE = ("pending", "running", "disabled")

    def list_queue(self) -> List[Dict[str, Any]]:
        """Daftar antrian task (aktif saja: pending/running/disabled).

        Task terminal (queue_state == "done") TIDAK masuk antrian; riwayatnya
        tetap tersedia lewat Task History API (existing).
        """
        with self._lock:
            records = sorted(self._tasks.values(), key=lambda r: r.queue_order)
        return [r.to_dict() for r in records if r.queue_state in self._QUEUE_ACTIVE]

    def set_queue_state(self, task_id: str, queue_state: str) -> Dict[str, Any]:
        """Set queue_state (disable/enable). TIDAK menyentuh eksekusi Agent.

        Aturan:
            - "disabled"/"pending" hanya boleh untuk task yang BELUM running
              (queue_state running ditolak) — agar disable != cancel dan tidak
              ada dua mekanisme penghentian.
            - Task terminal (done) tidak dapat diubah.

        Raises:
            NotFoundError: bila task tidak ditemukan.
            ValidationError: bila transisi tidak valid.
        """
        from api.services import ValidationError as _ValidationError

        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise NotFoundError(f"Task '{task_id}' tidak ditemukan.")
            if record.queue_state == "done":
                raise _ValidationError(
                    "Task sudah selesai dan tidak dapat diubah di antrian."
                )
            if record.queue_state == "running":
                raise _ValidationError(
                    "Task sedang berjalan; gunakan Stop (cancel) untuk menghentikannya."
                )
            if queue_state not in ("pending", "disabled"):
                raise _ValidationError("queue_state harus 'pending' atau 'disabled'.")
            record.queue_state = queue_state
            result = record.to_dict()
        # Disable/Enable memengaruhi eligibility scheduler -> pump.
        # Enable bisa langsung mempromosikan task ke RUNNING bila tidak ada
        # blocker di depannya (FIFO). Pump dilakukan di luar lock.
        if self.auto_execute:
            self._scheduler_pump()
        return result

    def move_task(self, task_id: str, direction: str) -> List[Dict[str, Any]]:
        """Geser posisi task non-running di antrian (FIFO default).

        Args:
            task_id: id task yang digeser.
            direction: "up" atau "down".

        Raises:
            NotFoundError: bila task tidak ditemukan.
            ValidationError: bila task running/terminal atau arah tidak valid.
        """
        from api.services import ValidationError as _ValidationError

        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise NotFoundError(f"Task '{task_id}' tidak ditemukan.")
            if record.queue_state in ("running", "done"):
                raise _ValidationError(
                    "Hanya task yang belum berjalan yang dapat digeser di antrian."
                )
            ordered = sorted(
                [r for r in self._tasks.values() if r.queue_state in self._QUEUE_ACTIVE],
                key=lambda r: r.queue_order,
            )
            idx = next((i for i, r in enumerate(ordered) if r.task_id == task_id), None)
            if idx is None:
                raise _ValidationError("Task tidak berada di antrian aktif.")
            if direction == "up" and idx > 0:
                swap = ordered[idx - 1]
                record.queue_order, swap.queue_order = swap.queue_order, record.queue_order
            elif direction == "down" and idx < len(ordered) - 1:
                swap = ordered[idx + 1]
                record.queue_order, swap.queue_order = swap.queue_order, record.queue_order
            elif direction not in ("up", "down"):
                raise _ValidationError("direction harus 'up' atau 'down'.")
        return self.list_queue()

    def remove_task(self, task_id: str) -> Dict[str, Any]:
        """Hapus task dari daftar/antrian (HANYA non-running).

        Ini BUKAN cancel: cancel memakai CancellationToken existing. Remove
        hanya membuang task yang belum berjalan dari daftar in-memory.

        Raises:
            NotFoundError: bila task tidak ditemukan.
            ValidationError: bila task sedang berjalan.
        """
        from api.services import ValidationError as _ValidationError

        with self._lock:
            record = self._tasks.pop(task_id, None)
            if record is None:
                raise NotFoundError(f"Task '{task_id}' tidak ditemukan.")
            if record.queue_state == "running":
                # Kembalikan: running tidak boleh dihapus.
                self._tasks[task_id] = record
                raise _ValidationError(
                    "Task sedang berjalan; gunakan Stop (cancel), bukan Remove."
                )
            self._prepared.pop(task_id, None)
            self._cancel_tokens.pop(task_id, None)
        return {"task_id": task_id, "removed": True}

    # ------------------------------------------------------------------ #
    # Task History (from .aether/log/ persistent store)
    # ------------------------------------------------------------------ #
    def list_task_history(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Daftar semua task dari .aether/log/ (persistent source of truth).

        Membaca file log task dan mengembalikan ringkasan terurut
        terbaru -> terlama berdasarkan `last_timestamp` dari isi log
        (BUKAN nama file / UUID / urutan filesystem).

        Pencarian mencakup seluruh candidate root (project_id/active project,
        AETHER workspace, project terdaftar) lalu di-dedupe per task_id.

        Args:
            project_id: project terkait (opsional).

        Returns:
            Daftar task info terurut terbaru ke terlama.
        """
        from agent_ai.projects.aether_store import AetherProjectStore, TaskLogReader

        by_task: Dict[str, Dict[str, Any]] = {}
        for root in self._candidate_log_roots(project_id):
            try:
                store = AetherProjectStore(root)
                log_paths = store.list_task_logs()
            except Exception:  # noqa: BLE001 - satu root rusak tidak mengganggu root lain
                continue
            for log_path in log_paths:
                task_id = log_path.stem  # nama file tanpa .log = identitas task
                try:
                    info = TaskLogReader(store, task_id=task_id).get_task_info()
                except Exception:  # noqa: BLE001 - satu task rusak tidak mengganggu yang lain
                    continue
                if info is None:
                    continue
                existing = by_task.get(task_id)
                if existing is None or (info.get("last_timestamp") or "") > (
                    existing.get("last_timestamp") or ""
                ):
                    by_task[task_id] = info

        tasks = list(by_task.values())
        tasks.sort(key=lambda t: t.get("last_timestamp") or "", reverse=True)
        return tasks

    def get_task_history(self, task_id: str, project_id: Optional[str] = None) -> Dict[str, Any]:
        """Ambil ringkasan task spesifik dari .aether/log/.

        Args:
            task_id: identifier task.
            project_id: project terkait (opsional).

        Returns:
            Task info dict (status "incomplete" bila log belum punya event).

        Raises:
            NotFoundError: bila file log task benar-benar tidak ditemukan.
        """
        reader = self._reader_for_task(task_id, project_id)
        if reader is None:
            raise NotFoundError(f"Task '{task_id}' tidak ditemukan di log.")
        info = reader.get_task_info()
        if info is None:
            # File log ada tetapi belum berisi event yang bisa diringkas:
            # task tetap dianggap ada (jangan salah jadi "tidak ditemukan").
            return {
                "task_id": task_id,
                "first_timestamp": None,
                "last_timestamp": None,
                "status": "incomplete",
                "task": "",
                "result": None,
                "error": None,
            }
        return info

    # ------------------------------------------------------------------ #
    # Activity API (chronological events per task)
    # ------------------------------------------------------------------ #
    def get_task_activity(
        self,
        task_id: str,
        project_id: Optional[str] = None,
        event_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Ambil seluruh chronological activity satu Task dari .aether/log/.

        Source: .aether/log/<task_id>.log

        Args:
            task_id: identifier task.
            project_id: project terkait (opsional).
            event_types: daftar tipe event yang relevan (opsional).
                Bila None, semua event diambil (termasuk tool activity).

        Returns:
            Daftar event terurut chronological berdasarkan timestamp.

        Raises:
            NotFoundError: bila file log task benar-benar tidak ditemukan.
        """
        reader = self._reader_for_task(task_id, project_id)
        if reader is None:
            raise NotFoundError(f"Task '{task_id}' tidak ditemukan di log.")
        # Seluruh event dikembalikan (termasuk tool_called / tool_completed /
        # observation_received), bukan hanya agent_commentary.
        return reader.get_activity(event_types=event_types)

    # ------------------------------------------------------------------ #
    # Report API
    # ------------------------------------------------------------------ #
    def get_task_report(self, task_id: str, project_id: Optional[str] = None) -> Dict[str, Any]:
        """Ambil final Agent Report dari .aether/log/.

        Source utama: task_completed.data.result.
        Fallback: task_finished.data.result.

        Args:
            task_id: identifier task.
            project_id: project terkait (opsional).

        Returns:
            Dict dengan task_id, status, dan report (teks final).
            `report` bernilai None bila log ada tetapi tidak punya
            `task_completed`/`task_finished` dengan result.

        Raises:
            NotFoundError: bila file log task tidak ditemukan.
        """
        reader = self._reader_for_task(task_id, project_id)
        if reader is None:
            raise NotFoundError(f"Task '{task_id}' tidak ditemukan di log.")
        info = reader.get_task_info() or {}
        return {
            "task_id": task_id,
            "status": info.get("status", "unknown"),
            "report": reader.get_report(),
        }

    def _resolve_project_root(self, project_id: Optional[str]) -> Optional[str]:
        """Tentukan project root untuk membaca .aether/log/.

        Prioritas: project_id dari parameter -> active project dari store.
        """
        if project_id:
            meta = self.project_store.get_project(project_id)
            if meta is not None:
                return meta.get("path") or meta.get("root")
        active_id = self.project_store.get_active_project_id()
        if active_id:
            meta = self.project_store.get_project(active_id)
            if meta is not None:
                return meta.get("path") or meta.get("root")
        return None

    def _candidate_log_roots(self, project_id: Optional[str] = None) -> List[str]:
        """Kandidat root tempat `.aether/log/` dicari (terurut & unik).

        Log task bersifat project-local, tetapi satu task_id bisa berada di
        root yang berbeda dari active project (mis. AETHER workspace tempat
        proses ini berjalan). Karena itu reader mencari beberapa kandidat:
            1. project_id eksplisit (bila diberikan),
            2. active project (bila ada),
            3. AETHER workspace/repo root tempat backend berjalan,
            4. seluruh project yang terdaftar di launcher.
        """
        from pathlib import Path as _Path

        roots: List[str] = []

        def _add(value: Optional[str]) -> None:
            if not value:
                return
            normalized = str(value)
            if normalized not in roots:
                roots.append(normalized)

        _add(self._resolve_project_root(project_id))
        try:
            # api/services.py -> api/ -> django_app/ -> web/ -> repo root
            _add(str(_Path(__file__).resolve().parents[3]))
        except Exception:  # noqa: BLE001
            pass
        try:
            for meta in self.project_store.list_projects():
                _add(meta.get("path") or meta.get("root"))
        except Exception:  # noqa: BLE001
            pass
        return roots

    def _find_log_file(
        self,
        task_id: str,
        root: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Optional[Path]:
        """Cari file log task di `.aether/log/` berdasarkan task_id.

        Bila `root` diberikan, hanya root tersebut yang dicari (kompatibel
        dengan pemanggilan lama). Bila `root` None, pencarian dilakukan di
        seluruh candidate root (`_candidate_log_roots`). Exact match
        `<task_id>.log` dicoba lebih dulu, lalu prefix match (mengakomodasi
        task_id yang dinormalisasi oleh `safe_task_id`).

        Nama file `<task_id>.log` adalah identitas persistent Task; pencarian
        TIDAK mensyaratkan metadata `project_id`/`task_id` ada di dalam event.

        Returns:
            Path file log jika ditemukan, None bila tidak ada.
        """
        from pathlib import Path as _Path
        from agent_ai.projects.aether_store import safe_task_id

        safe_id = safe_task_id(task_id)
        if not safe_id:
            return None
        candidates = [root] if root else self._candidate_log_roots(project_id)
        for candidate in candidates:
            if not candidate:
                continue
            log_dir = _Path(candidate) / ".aether" / "log"
            if not log_dir.exists():
                continue
            exact = log_dir / f"{safe_id}.log"
            if exact.is_file():
                return exact
            for f in sorted(log_dir.glob("*.log")):
                if f.stem.startswith(safe_id):
                    return f
        return None

    def _reader_for_task(self, task_id: str, project_id: Optional[str] = None) -> Optional[Any]:
        """Buat TaskLogReader yang terikat ke file log yang benar-benar ada.

        Root diturunkan dari path file (`<root>/.aether/log/<task_id>.log`)
        sehingga reader membaca file yang sama persis dengan hasil
        `_find_log_file()`. Tidak ada validasi kedua terhadap isi event yang
        bisa membuat task valid dianggap tidak ditemukan.

        Returns:
            TaskLogReader, atau None bila file log tidak ditemukan.
        """
        from agent_ai.projects.aether_store import AetherProjectStore, TaskLogReader

        log_file = self._find_log_file(task_id, project_id=project_id)
        if log_file is None:
            return None
        # <root>/.aether/log/<task_id>.log -> root = parents[2] dari file dir.
        store = AetherProjectStore(log_file.parent.parent.parent)
        return TaskLogReader(store, task_id=log_file.stem)

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        """Minta penghentian task: sinyal cancellation + tandai CANCELLED.

        Mechanism (cooperative, aman):
            1. set cancellation signal pada token task (bila sedang berjalan),
            2. tandai status record gateway CANCELLED (agar UI langsung tahu),
            3. Agent loop (runtime/orchestrator) melihat signal pada safe
               boundary, berhenti, dan mencatat event `task_cancelled` +
               `task_finished(status=cancelled)` ke `.aether/log` (sumber
               tunggal observability). TIDAK ada thread.kill / force terminate.

        Task yang sudah terminal (completed/failed/cancelled) TIDAK diubah.

        Raises:
            NotFoundError: bila task tidak ditemukan.
        """
        with self._lock:
            record = self._tasks.get(task_id)
            token = self._cancel_tokens.get(task_id)
        if record is None:
            raise NotFoundError(f"Task '{task_id}' tidak ditemukan.")

        # Hanya task yang belum terminal yang bisa dibatalkan. Task yang sudah
        # COMPLETED/FAILED tetap pada statusnya (tidak diubah menjadi CANCELLED).
        if record.status in ("completed", "failed", "cancelled"):
            return record.to_dict()

        # 1) Sinyal kooperatif: Agent loop berhenti di safe boundary.
        if token is not None:
            token.request("user_requested")
        # 2) Status record gateway langsung CANCELLED (UI/HTTP responsif).
        #    Ini juga menyetel queue_state="done" (via _update_task_status),
        #    sehingga task keluar dari antrian aktif.
        self._update_task_status(task_id, "cancelled")
        # Bila task yang dibatalkan BELUM running (tidak ada token), slot tidak
        # pernah terpakai — tetap pump agar antrian bergerak sesuai urutan.
        # Bila task sedang running, slot dilepas oleh _execute_task (finally)
        # setelah cancellation mencapai terminal state.
        if token is None and self.auto_execute:
            self._scheduler_pump()
        return self.get_task(task_id)

    # ------------------------------------------------------------------ #
    # Consultant (AETHER reasoning layer — read-only terhadap CODE PROJECT)
    # ------------------------------------------------------------------ #
    def consult(
        self,
        message: str,
        *,
        session_id: Optional[str] = None,
        provider_instance_id: Optional[str] = None,
        model_id: Optional[str] = None,
        project_id: Optional[str] = None,
        mode: Optional[str] = None,
        images: Optional[List[Dict[str, Any]]] = None,
        provider: Optional[Any] = None,
        root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Jalankan satu giliran konsultasi Consultant.

        Consultant memakai loop & tool AETHER yang sudah ada (read-only terhadap
        CODE PROJECT, read+update terhadap Project Bible). Hasilnya dapat memuat
        Task Proposal yang siap dikirim ke Agent lewat alur task existing.

        Args:
            message: pertanyaan/permintaan user (wajib).
            session_id: id sesi konsultasi (konteks lintas giliran).
            provider_instance_id/model_id: pilihan provider+model dari konfigurasi
                LLM tersimpan (SQLite). Bila kosong, dipakai provider instance
                enabled pertama.
            project_id: project terkait (opsional; default active project).
            mode: mode Consultant ("quick" | "investigate"; default "quick").
                Mengontrol tool yang benar-benar tersedia bagi LLM.
            images: daftar gambar opsional (multimodal) untuk pesan user.
                Setiap item: {"data": "<base64>", "mime_type": "image/png",
                "filename": opsional}. Diteruskan ke ConsultantService yang
                memprosesnya lewat modul vision existing.
            provider: override provider (khusus verifier; tidak dari HTTP).
            root: override root project (khusus verifier; tidak dari HTTP).

        Returns:
            Dict hasil konsultasi: session_id, reply, status, error, iterations,
            tool_events, task_proposal.

        Raises:
            ValidationError: message kosong / provider tidak tersedia / gambar
                tidak valid.
        """
        if not message or not str(message).strip():
            raise ValidationError("Field 'message' wajib diisi dan tidak boleh kosong.")

        normalized_images = self._normalize_consult_images(images)

        if root is None:
            root = self._resolve_workspace_root(project_id)
        if not root:
            # Fallback ke root workspace AETHER (repo tempat backend berjalan)
            # agar Consultant tetap dapat menganalisis project AETHER sendiri
            # walau belum ada project aktif.
            from pathlib import Path as _Path

            root = str(_Path(__file__).resolve().parents[3])

        if provider is None:
            provider = self._build_consultant_provider(provider_instance_id, model_id)

        try:
            result = self.consultant_service.consult(
                str(message).strip(),
                provider=provider,
                root=root,
                session_id=session_id,
                mode=mode,
                images=normalized_images,
            )
        except ValidationError:
            raise
        except ValueError as exc:
            # Payload gambar tidak valid / format tidak didukung -> pesan jelas.
            raise ValidationError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - map error vision ke ValidationError
            from agent_ai.vision.models import VisionError

            if isinstance(exc, VisionError):
                raise ValidationError(f"Gambar tidak dapat diproses: {exc}") from exc
            raise
        return result.to_dict()

    def _normalize_consult_images(
        self, images: Optional[Any]
    ) -> Optional[List[Dict[str, Any]]]:
        """Validasi & normalisasi daftar gambar dari request Consultant.

        Bentuk yang diterima: list of {"data": "<base64>", "mime_type": str,
        "filename": opsional}. Dibatasi jumlah/ukuran agar aman.

        Returns:
            List gambar tervalidasi, atau None bila tidak ada.

        Raises:
            ValidationError: bentuk tidak valid / terlalu banyak / terlalu besar.
        """
        if images is None:
            return None
        if not isinstance(images, list):
            raise ValidationError("Field 'images' harus berupa array.")
        if not images:
            return None
        max_images = 8
        if len(images) > max_images:
            raise ValidationError(f"Jumlah gambar maksimum {max_images}.")
        max_bytes = _MAX_CONSULT_IMAGE_BYTES
        normalized: List[Dict[str, Any]] = []
        for index, item in enumerate(images):
            if not isinstance(item, dict):
                raise ValidationError(f"Gambar[{index}] harus berupa object.")
            data = item.get("data")
            if not data or not isinstance(data, str):
                raise ValidationError(f"Gambar[{index}].data (base64) wajib diisi.")
            # Estimasi ukuran decoded (base64 -> ~3/4 panjang).
            approx_bytes = (len(data) * 3) // 4
            if approx_bytes > max_bytes:
                raise ValidationError(
                    f"Gambar[{index}] terlalu besar (>{max_bytes} bytes)."
                )
            entry: Dict[str, Any] = {
                "data": data,
                "mime_type": str(item.get("mime_type") or "").strip(),
            }
            if item.get("filename"):
                entry["filename"] = str(item["filename"])
            normalized.append(entry)
        return normalized

    def _build_consultant_provider(
        self,
        provider_instance_id: Optional[str],
        model_id: Optional[str],
    ) -> Any:
        """Bangun provider Consultant dari konfigurasi LLM tersimpan (SQLite).

        Bila provider instance tidak dipilih, dipakai instance enabled pertama.
        Error konfigurasi dipetakan menjadi ValidationError dengan pesan jelas.
        """
        from agent_ai.providers.base import ProviderError
        from agent_ai.providers.factory import build_provider_from_config

        instance_id = provider_instance_id
        if not instance_id:
            try:
                for inst in self.list_llm_providers():
                    if inst.get("enabled") is not False:
                        instance_id = inst.get("id")
                        break
            except Exception as exc:  # noqa: BLE001 - konfigurasi LLM gagal dibaca
                raise ValidationError(
                    f"Tidak dapat membaca konfigurasi LLM: {exc}"
                ) from exc
        if not instance_id:
            raise ValidationError(
                "Tidak ada Provider Instance yang dikonfigurasi untuk Consultant. "
                "Tambahkan provider di Settings terlebih dahulu."
            )

        try:
            resolved = self.llm_config_service.resolve_runtime_config(
                instance_id, model_id=model_id, include_api_key=True
            )
        except Exception as exc:  # noqa: BLE001 - konfigurasi provider error
            raise ValidationError(str(exc)) from exc

        try:
            return build_provider_from_config(resolved)
        except ProviderError as exc:
            raise ValidationError(str(exc)) from exc

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
