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
        llm_config_service: Optional[Any] = None,
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
        self._tasks: Dict[str, TaskRecord] = {}
        # PreparedTask asli (bukan ringkasan) untuk diteruskan ke runtime.
        self._prepared: Dict[str, Any] = {}
        self._lock = threading.Lock()

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
