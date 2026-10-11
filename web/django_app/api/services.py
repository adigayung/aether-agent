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
from agent_ai.permission.approval import ApprovalCoordinator
from agent_ai.permission.matrix import MATRIX_ACTIONS, MATRIX_SCOPES
from agent_ai.projects.permissions import (
    ACTION_OPTIONS,
    MATRIX_MODE_VALUES_SET,
    MODE_OPTIONS,
)
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


# Execution mode (Task 01 — hanya parameter task, belum parallel execution).
# Nilai valid: "queue" | "parallel". Default "queue" agar task lama kompatibel.
_VALID_EXECUTION_MODES = frozenset({"queue", "parallel"})
_DEFAULT_EXECUTION_MODE = "queue"

# Default concurrency untuk execution_mode="parallel". Minimal 2 agar benar-benar
# concurrent. Dapat diubah via settings atau environment variable.
_DEFAULT_PARALLEL_CONCURRENCY = 4


def _normalize_execution_mode(value: Optional[str]) -> str:
    """Normalisasi execution_mode -> 'queue' | 'parallel' (default queue).

    Raises:
        ValidationError: bila nilai tidak termasuk yang valid.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return _DEFAULT_EXECUTION_MODE
    v = str(value).strip().lower()
    if v not in _VALID_EXECUTION_MODES:
        raise ValidationError(
            f"execution_mode harus salah satu dari {sorted(_VALID_EXECUTION_MODES)}."
        )
    return v


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
    # Execution mode (Task 01 — hanya parameter/niat execution, belum parallel).
    # Nilai valid: "queue" | "parallel". Default "queue" agar task lama kompatibel.
    execution_mode: str = _DEFAULT_EXECUTION_MODE
    # Agent Execution Policy (fast/balanced/deep) — INFORMASI/strategi kerja,
    # TERPISAH dari `status`/`execution_mode` di atas. requested_mode = mode
    # yang diminta user; effective_mode = mode yang benar-benar dipakai Agent
    # (dapat naik setelah assessment). SENGAJA field gateway (bukan enum
    # TaskStatus core) agar TaskState/TaskLifecycle/.aether log/SSE tidak
    # terpengaruh. None = policy tidak aktif (perilaku lama).
    requested_mode: Optional[str] = None
    effective_mode: Optional[str] = None
    policy_reason: Optional[str] = None
    policy_escalated: bool = False
    policy_escalations: List[Dict[str, Any]] = field(default_factory=list)

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
            "execution_mode": self.execution_mode,
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
            "policy_reason": self.policy_reason,
            "policy_escalated": self.policy_escalated,
            "policy_escalations": [dict(item) for item in self.policy_escalations],
        }


def _extract_matrix_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    """Ambil payload matrix dari body POST policy (kanonik atau dibungkus).

    Menerima:
        - matrix kanonik langsung: {"read_files": {"inside": ...}, ...}
        - dibungkus: {"matrix": {...}} atau {"rules": {...}}
    """
    if not isinstance(body, dict):
        return {}
    for key in ("matrix", "rules"):
        inner = body.get(key)
        if isinstance(inner, dict):
            return inner
    return body


def _validate_matrix_payload(payload: Any) -> None:
    """Validasi payload matrix; raise ValidationError bila tidak valid.

    Menerima payload kosong (matrix default). Untuk setiap aksi/scope yang
    dikirim, nilai WAJIB allow|ask|deny (tidak menurunkan diam-diam).
    """
    if payload is None:
        return
    if not isinstance(payload, dict):
        raise ValidationError("Body policy harus berupa object JSON.")
    allowed_scopes = set(MATRIX_SCOPES)
    unknown_actions = set(payload) - set(MATRIX_ACTIONS)
    if unknown_actions:
        raise ValidationError(
            "Aksi policy tidak dikenal: "
            f"{', '.join(sorted(unknown_actions))}. "
            f"Gunakan salah satu dari {', '.join(MATRIX_ACTIONS)}."
        )
    for action, scopes in payload.items():
        if not isinstance(scopes, dict):
            raise ValidationError(
                f"Nilai '{action}' harus berupa object {{inside, outside}}."
            )
        unknown_scopes = set(scopes) - allowed_scopes
        if unknown_scopes:
            raise ValidationError(
                f"Scope tidak dikenal pada '{action}': "
                f"{', '.join(sorted(unknown_scopes))}. Gunakan inside | outside."
            )
        for scope, value in scopes.items():
            if str(value).strip().lower() not in MATRIX_MODE_VALUES_SET:
                raise ValidationError(
                    f"Nilai '{action}.{scope}' harus salah satu dari "
                    "allow | ask | deny."
                )


class _GatewayConsultantScope:
    """ConsultantProjectScope yang memakai registry + active project GW.

    Sumber kebenaran:
        * project id terdaftar  -> ``GatewayService.projects`` (ProjectRegistry),
        * path root otoritatif  -> ProjectRegistry config (``.root``); fallback
          ke ``GatewayService.project_store`` (SQLite launcher metadata) untuk
          project yang belum ada di registry JSON.
        * active project        -> ``GatewayService.project_store`` (SQLite).
    """

    def __init__(self, gateway: "GatewayService") -> None:
        self._gateway = gateway

    def active_project_id(self) -> Optional[str]:
        try:
            active = self._gateway.project_store.get_active_project_id()
        except Exception:  # noqa: BLE001 - state aktif tidak boleh crash
            return None
        return str(active) if active else None

    def project_root(self, project_id: str) -> Optional[str]:
        if not project_id:
            return None
        # 1) Registry AETHER (sumber struktur project).
        try:
            config = self._gateway.projects.get(project_id)
        except Exception:  # noqa: BLE001 - registry error -> coba store
            config = None
        if config is not None:
            root = getattr(config, "root", None)
            if root:
                return str(root)
        # 2) Fallback: launcher metadata (id -> path). Tetap project terdaftar.
        try:
            meta = self._gateway.project_store.get_project(project_id)
        except Exception:  # noqa: BLE001
            meta = None
        if meta and meta.get("path"):
            return str(meta["path"])
        return None

    def registered_project_ids(self) -> List[str]:
        ids: List[str] = []
        try:
            for config in self._gateway.projects.list():
                pid = getattr(config, "id", None)
                if pid:
                    ids.append(str(pid))
        except Exception:  # noqa: BLE001
            pass
        try:
            for meta in self._gateway.project_store.list_projects():
                pid = meta.get("id")
                if pid and str(pid) not in ids:
                    ids.append(str(pid))
        except Exception:  # noqa: BLE001
            pass
        return ids


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
        parallel_concurrency: Optional[int] = None,
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
        # GitHub Backup (fitur OPTIONAL per project). Lazy: hanya dibangun saat
        # endpoint backup dipakai, sehingga jalur read-only tetap ringan.
        self._github_backup_service = None
        self._tasks: Dict[str, TaskRecord] = {}
        # PreparedTask asli (bukan ringkasan) untuk diteruskan ke runtime.
        self._prepared: Dict[str, Any] = {}
        # Attachment gambar per task (image content parts provider-agnostic,
        # SUDAH dipreprocess). Disimpan TERPISAH dari TaskRecord.to_dict() agar
        # base64 TIDAK bocor ke API list/history/activity/log. Ini mekanisme
        # penyimpanan attachment level-task (in-memory, seumur eksekusi task) —
        # BUKAN storage subsystem baru. Dipakai jalur Agent Task (vision).
        self._task_attachments: Dict[str, List[Dict[str, Any]]] = {}
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
        # Concurrency pool untuk execution_mode="parallel".
        # Nilai default: 4 (dapat dioverride via constructor).
        # Task parallel TIDAK memakai slot queue serial, melainkan pool
        # terpisah dengan batas konfigurasi agar benar-benar concurrent.
        self._parallel_concurrency = (
            parallel_concurrency if parallel_concurrency is not None
            else _DEFAULT_PARALLEL_CONCURRENCY
        )
        # Koordinator approval untuk action yang butuh approval (ASK/mode
        # `require_approval`). Ini BUKAN sistem permission kedua: keputusan
        # tetap dibuat PermissionManager existing; koordinator HANYA menahan
        # eksekusi lalu meneruskan keputusan user (Allow/Deny) ke gate. Event
        # approval memakai event system existing (SessionStore).
        self._approvals = ApprovalCoordinator(sink=self._on_approval_event)

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

        Sesi Consultant di-SCOPE per project: storage project-local di
        ``<project_path>/.aether/consultant/sessions.json``. Resolusi project
        otoritatif disuntikkan lewat ``ConsultantProjectScope`` (registry +
        active project state) sehingga core tidak bergantung pada layer web.
        """
        if self._consultant_service is None:
            from agent_ai.consultant import ConsultantService

            self._consultant_service = ConsultantService(
                project_scope=_GatewayConsultantScope(self)
            )
        return self._consultant_service

    # ------------------------------------------------------------------ #
    # Consultant Session Management (pass-through to ConsultantService)
    # ------------------------------------------------------------------ #
    #
    # ISOLASI PER PROJECT: setiap operasi sesi WAJIB punya project_id. Bila
    # tidak diberikan, fallback HANYA ke active project; bila tetap tidak ada,
    # raise ValidationError yang jelas (TIDAK pernah lintas-project).
    def _consultant_session_project_id(self, project_id: Optional[str]) -> str:
        """Resolve project_id efektif untuk operasi sesi Consultant."""
        candidate = (project_id or "").strip()
        if candidate:
            return candidate
        try:
            active = self.project_store.get_active_project_id()
        except Exception:  # noqa: BLE001 - project state tidak boleh crash
            active = None
        if active:
            return str(active)
        raise ValidationError(
            "project_id wajib untuk operasi sesi Consultant dan tidak ada "
            "active project. Pilih project terlebih dahulu."
        )

    def list_consultant_sessions(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List consultant sessions untuk SATU project, terbaru lebih dulu."""
        pid = self._consultant_session_project_id(project_id)
        return self.consultant_service.list_sessions(project_id=pid)

    def get_consultant_session(
        self, session_id: str, project_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Get a consultant session by ID pada SATU project."""
        pid = self._consultant_session_project_id(project_id)
        return self.consultant_service.get_session(session_id, project_id=pid)

    def create_consultant_session(
        self, project_id: Optional[str] = None, title: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a new consultant session untuk SATU project."""
        pid = self._consultant_session_project_id(project_id)
        return self.consultant_service.create_session(project_id=pid, title=title)

    def rename_consultant_session(
        self, session_id: str, title: str, project_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Rename a consultant session pada SATU project."""
        pid = self._consultant_session_project_id(project_id)
        return self.consultant_service.rename_session(session_id, title, project_id=pid)

    def delete_consultant_session(
        self, session_id: str, project_id: Optional[str] = None
    ) -> bool:
        """Delete a consultant session pada SATU project."""
        pid = self._consultant_session_project_id(project_id)
        return self.consultant_service.delete_session(session_id, project_id=pid)

    def reset_consultant_session(
        self, session_id: str, project_id: Optional[str] = None
    ) -> bool:
        """Clear a consultant session's turns pada SATU project."""
        pid = self._consultant_session_project_id(project_id)
        return self.consultant_service.reset_session(session_id, project_id=pid)

    def _consultant_identity_sources(self) -> List[Any]:
        """Sumber identitas project TAMBAHAN untuk migrasi legacy.

        Registry AETHER (``projects/*/project.json``) adalah sumber struktur
        utama. Bila registry KOSONG (mis. project.json belum dibuat) tapi
        launcher SQLite (``data/aether.db`` -> tabel ``projects``) masih
        menyimpan pemetaan id->path yang OTORITATIF, sumber itu disediakan agar
        sesi legacy yang pemiliknya dapat dibuktikan tetap bisa dipulihkan.
        """
        from agent_ai.consultant.migration import StaticIdentitySource

        identities: Dict[str, str] = {}
        try:
            for meta in self.project_store.list_projects():
                pid = meta.get("id")
                path = meta.get("path")
                if pid and path:
                    identities[str(pid)] = str(path)
        except Exception:  # noqa: BLE001 - audit tidak boleh crash karena store
            identities = {}
        return [StaticIdentitySource(identities, label="launcher-db")]

    def audit_consultant_legacy_migration(
        self,
        *,
        legacy_path: Optional[str] = None,
        dry_run: bool = False,
        orphan_mapping: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Audit + pulihkan sesi Consultant legacy (GLOBAL -> per project).

        READ-ONLY terhadap file legacy. Registry kosong ditangani dengan
        menambahkan sumber identitas OTORITATIF alternatif (launcher SQLite).
        Sesi tanpa identitas yang dapat dibuktikan tetap orphan; pemulihan
        eksplisit memakai ``orphan_mapping`` (``{session_id: project_id}``)
        yang dikonfirmasi pengguna.

        Args:
            legacy_path: path file legacy (default: data/consultant_sessions.json).
            dry_run: bila True, hitung laporan tanpa menulis apa pun.
            orphan_mapping: pemetaan eksplisit sesi orphan -> project_id
                (hanya dipakai bila project_id tujuan TERVERIFIKASI).

        Returns:
            Dict laporan migrasi (lihat ``MigrationReport.to_dict``).
        """
        from agent_ai.consultant.migration import audit_legacy_migration

        scope = _GatewayConsultantScope(self)
        report = audit_legacy_migration(
            scope,
            legacy_path=legacy_path,
            dry_run=dry_run,
            identity_sources=self._consultant_identity_sources(),
            orphan_mapping=orphan_mapping,
        )
        return report.to_dict()

    @property
    def github_backup_service(self) -> Any:
        """GithubBackupService (lazy) — fitur OPTIONAL per project.

        Memakai ulang Git Awareness Foundation AETHER (read) + `.aether/github`
        project-local store + proteksi credential Windows (DPAPI). Dibuat lazy
        agar jalur read-only gateway tetap ringan. Bukan subsystem kedua:
        checkpoint/commit history tetap milik Git, bukan DB checkpoint baru.
        """
        if self._github_backup_service is None:
            from api.github_backup import GithubBackupService

            self._github_backup_service = GithubBackupService()
        return self._github_backup_service

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

        # Default terpilih DARI Provider Instance DB (aturan yang sama dipakai
        # Consultant & runtime; lihat `_select_default_llm`).
        selection = self._select_default_llm(instances)

        from agent_ai.config.settings import agent_default_mode
        from agent_ai.runtime.policy import available_modes

        return {
            "providers": registry.list_providers(),
            "provider_instances": instances,
            "provider_instance_id": selection["instance_id"],
            "model_id": selection["model_id"],
            "mode": agent_default_mode(),
            "modes": available_modes(),
        }

    @staticmethod
    def _select_default_llm(instances: List[Dict[str, Any]]) -> Dict[str, str]:
        """Pilih Provider Instance + Model default dari instance tersimpan.

        Aturan deterministik (SATU tempat, dipakai /api/config & Consultant):
            1. instance enabled yang punya model enabled (Default UI New Task)
               -> pakai model enabled pertama,
            2. instance enabled yang TIDAK membutuhkan model konkret
               (mis. "custom"/"9router") -> tanpa model,
            3. fallback: instance enabled pertama.

        Returns:
            {"instance_id": str, "model_id": str} (kosong bila tak ada instance
            enabled). Dipakai juga untuk memastikan Consultant TANPA
            `provider_instance_id` memakai instance DB yang sama dengan Settings
            (TIDAK fallback diam-diam ke konfigurasi .env lama).
        """
        enabled = [i for i in instances if i.get("enabled") is not False]
        # 1) instance enabled dengan model enabled.
        for inst in enabled:
            models = [
                m for m in (inst.get("models") or []) if m.get("enabled") is not False
            ]
            if models:
                return {
                    "instance_id": inst.get("id") or "",
                    "model_id": models[0].get("id") or "",
                }
        # 2) instance yang tidak butuh model konkret (custom/9router).
        for inst in enabled:
            if inst.get("requires_model") is False:
                return {"instance_id": inst.get("id") or "", "model_id": ""}
        # 3) fallback: instance enabled pertama.
        if enabled:
            return {"instance_id": enabled[0].get("id") or "", "model_id": ""}
        return {"instance_id": "", "model_id": ""}

    # ------------------------------------------------------------------ #
    # Global Settings (`data/settings.json` — SATU sumber konfigurasi global)
    #
    # Gateway HANYA meneruskan baca/tulis ke loader konfigurasi AETHER yang
    # sudah ada (`agent_ai.config.settings`). TIDAK ada skema/file konfigurasi
    # kedua: `data/settings.json` tetap sumber tunggal, dan penulisan bersifat
    # MERGE (key lain tidak hilang).
    # ------------------------------------------------------------------ #
    def get_global_settings(self) -> Dict[str, Any]:
        """Nilai aktual konfigurasi global user-facing (dari `data/settings.json`)."""
        from agent_ai.config.settings import global_settings

        return {"settings": global_settings()}

    def update_global_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Simpan perubahan konfigurasi global (deep-merge, tanpa menghapus key).

        Raises:
            ValidationError: payload tidak valid (tipe/rentang/key tak dikenal).
        """
        from agent_ai.config.settings import SettingsWriteError, update_global_settings

        try:
            updated = update_global_settings(updates if isinstance(updates, dict) else {})
        except SettingsWriteError as exc:
            raise ValidationError(str(exc)) from exc
        return {"settings": updated}

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

    # ---- Test Connection ----
    def test_llm_provider(self, provider_id: str) -> Dict[str, Any]:
        """Test koneksi ke provider instance.

        Melakukan request HTTP ringan ke endpoint provider untuk memverifikasi
        bahwa API key dan base URL valid. Untuk provider seperti 9Router yang
        tidak membutuhkan model, test dilakukan tanpa model.
        """
        from agent_ai.providers.base import ProviderError
        from agent_ai.providers.factory import build_provider_from_config

        if not provider_id:
            raise ValidationError("Parameter 'provider_id' wajib diisi.")

        try:
            # Nilai api_key diperlukan untuk benar-benar memanggil endpoint;
            # `include_api_key` default True sehingga jalur ini identik dengan
            # yang dipakai Consultant & Agent Runtime (SATU resolver).
            resolved = self.llm_config_service.resolve_runtime_config(provider_id)
            provider = build_provider_from_config(resolved)
        except Exception as exc:
            raise ValidationError(str(exc)) from exc

        # Test: kirim request chat sederhana.
        try:
            result = provider.generate(prompt="ping")
            if result and result.text is not None:
                return {"status": "ok", "detail": "Connection successful."}
            return {"status": "ok", "detail": "Connection successful (empty response)."}
        except ProviderError as exc:
            return {"status": "error", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 - error koneksi
            return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}

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

        Bila path belum ada, directory dibuat secara recursive (termasuk
        parent yang belum ada). Project yang dibuat adalah PURE EMPTY project:
        TIDAK ada template aplikasi/source yang dibuat di sini (hanya metadata
        & infrastructure AETHER yang diwajibkan oleh mekanisme registration).
        Bila path menunjuk ke FILE, operasi DITOLAK tanpa menghapus/memindahkan/
        mengubah file tersebut.

        Raises:
            ValidationError: bila name/path kosong atau path tidak valid.
        """
        if not name or not isinstance(name, str) or not name.strip():
            raise ValidationError("Field 'name' wajib diisi dan tidak boleh kosong.")
        if not path or not isinstance(path, str) or not path.strip():
            raise ValidationError("Field 'path' wajib diisi dan tidak boleh kosong.")

        # Validasi + normalisasi path (Windows-aware via pathlib, path API
        # yang sudah dipakai project).
        from pathlib import Path as _Path

        root = _Path(path.strip())
        if root.exists() and not root.is_dir():
            raise ValidationError(
                f"Project path menunjuk ke file, bukan directory: {root}"
            )
        if not root.exists():
            # Kanonikkan dulu (hilangkan drive/folder relatif yang ambigu)
            # agar mkdir recursive berjalan pada path yang benar.
            root = root.expanduser().resolve()
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ValidationError(
                    f"Gagal membuat directory project '{root}': {exc}"
                ) from exc

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

    # ------------------------------------------------------------------ #
    # Project Policy / Permission (PROJECT-LOCAL)
    #
    # Gateway HANYA mengorkestrasi: policy disimpan di
    # `<root project target>/.aether/settings/permissions.json` (project-local)
    # memakai `ProjectPermissionStore` AETHER. TIDAK ada sistem permission
    # kedua: mode/scope dipetakan ke `PermissionConfig`/`PolicyMode` existing
    # dan di-enforce oleh PermissionManager yang sudah ada.
    # ------------------------------------------------------------------ #
    def _project_policy_store(self, project_id: str):
        """Store policy project-local untuk project_id (root divalidasi)."""
        from agent_ai.projects.permissions import ProjectPermissionStore

        return ProjectPermissionStore(root=self._project_root_by_id(project_id))

    def get_project_policy(self, project_id: str) -> Dict[str, Any]:
        """GET Project Permission Matrix aktual dari
        `<root>/.aether/settings/permissions.json`.

        Mengembalikan nilai policy AKTUAL project tersebut (bukan default
        global). Bila file belum ada, default policy (matrix default) dipakai
        tanpa merusak project.

        Raises:
            ValidationError: bila project_id kosong.
            NotFoundError: bila project tidak ditemukan.
        """
        store = self._project_policy_store(project_id)
        policy = store.load()
        data = policy.to_ui_dict()
        data["options"] = {
            "actions": ACTION_OPTIONS,
            "scopes": list(MATRIX_SCOPES),
            "modes": MODE_OPTIONS,
        }
        data["path"] = str(store.path)
        data["exists"] = store.exists()
        data["project_id"] = str(project_id)
        return data

    def save_project_policy(
        self, project_id: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST Project Permission Matrix -> simpan ke
        `<root>/.aether/settings/permissions.json`.

        Body menerima matrix kanonik (langsung atau dibungkus `{"matrix": {...}}`):

            {"read_files": {"inside": "allow", "outside": "allow"}, ...}

        Policy hanya berlaku untuk project ini (project-local); project lain
        tidak terpengaruh.

        Raises:
            ValidationError: bila project_id kosong atau nilai matrix tidak valid.
            NotFoundError: bila project tidak ditemukan.
        """
        from agent_ai.projects.permissions import ProjectPolicy

        body = body or {}
        # Pemisahan konfigurasi: Global Settings AETHER (`data/settings.json`)
        # TIDAK boleh masuk lewat endpoint Project Policy. Hanya field project
        # policy (matrix) yang diterima; key global ditolak eksplisit.
        from agent_ai.config.settings import _EDITABLE_SETTINGS_KEYS

        leaked = set(body) & _EDITABLE_SETTINGS_KEYS
        if leaked:
            raise ValidationError(
                "Field berikut milik Global Settings AETHER (bukan Project "
                f"Policy): {', '.join(sorted(leaked))}. "
                "Kelola dari Sidebar -> Settings."
            )

        matrix_payload = _extract_matrix_payload(body)
        _validate_matrix_payload(matrix_payload)

        store = self._project_policy_store(project_id)
        policy = ProjectPolicy.from_dict(matrix_payload)
        store.save(policy)
        data = policy.to_ui_dict()
        data["options"] = {
            "actions": ACTION_OPTIONS,
            "scopes": list(MATRIX_SCOPES),
            "modes": MODE_OPTIONS,
        }
        data["path"] = str(store.path)
        data["exists"] = True
        data["project_id"] = str(project_id)
        return data

    def project_permission_config(self, project_id: Optional[str]):
        """PermissionConfig project-local untuk sebuah project (bila ada).

        Dipakai jalur eksekusi task agar policy project benar-benar berlaku.
        Mengembalikan None bila tidak ada project / policy gagal dibaca
        (backward compatible: executor memakai default).
        """
        candidate = project_id
        if not candidate:
            candidate = self.project_store.get_active_project_id()
        if not candidate:
            return None
        try:
            store = self._project_policy_store(candidate)
        except GatewayError:
            return None
        try:
            return store.load().to_permission_config()
        except Exception:  # noqa: BLE001 - policy tidak boleh crash eksekusi
            return None

    def project_permission_matrix(self, project_id: Optional[str]):
        """Project Permission Matrix project-local (bila ada).

        Dipakai jalur eksekusi task agar matrix (aksi x inside/outside) benar-
        benar berlaku. Mengembalikan None bila tidak ada project / matrix gagal
        dibaca (backward compatible: executor memakai policy default).
        """
        candidate = project_id
        if not candidate:
            candidate = self.project_store.get_active_project_id()
        if not candidate:
            return None
        try:
            store = self._project_policy_store(candidate)
        except GatewayError:
            return None
        try:
            return store.load().matrix
        except Exception:  # noqa: BLE001 - policy tidak boleh crash eksekusi
            return None

    # ------------------------------------------------------------------ #
    # Project Settings -> Agents (System Prompt Agent & Consultant)
    #
    # Konfigurasi SYSTEM PROMPT per-project disimpan di
    # `<root project target>/.aether/settings/agent.json` dan
    # `<root project target>/.aether/settings/consultant.json`. Ini
    # PROJECT-LOCAL (isolasi antar-project) dan BUKAN konfigurasi provider/model
    # (provider/model tetap GLOBAL di `data/aether.db`).
    # ------------------------------------------------------------------ #
    def _project_settings_store(self, project_id: str):
        """Store konfigurasi per-project untuk project_id (root divalidasi)."""
        from agent_ai.projects.project_settings import ProjectSettingsStore

        return ProjectSettingsStore(root=self._project_root_by_id(project_id))

    def get_project_agent_settings(self, project_id: str) -> Dict[str, Any]:
        """GET System Prompt Agent/Consultant project-local.

        Mengembalikan nilai AKTUAL project tersebut:
          - `agent.system_prompt` / `consultant.system_prompt` = override
            project bila ada (string), `null` bila project TIDAK mengoverride.
          - `*_default_prompt` = prompt bawaan AETHER (untuk tombol Restore
            default), sehingga UI tetap menampilkan sumber yang benar.
          - `paths` = lokasi file project-local (untuk transparansi UI).

        Raises:
            ValidationError: bila project_id kosong.
            NotFoundError: bila project tidak ditemukan.
        """
        from agent_ai.config.settings import (
            _default_agent_system_prompt,
        )
        from agent_ai.consultant.prompt import build_consultant_system_prompt

        store = self._project_settings_store(project_id)
        data = {
            "project_id": str(project_id),
            "agent": {
                "system_prompt": store.agent_system_prompt(),
                "default_system_prompt": _default_agent_system_prompt(),
            },
            "consultant": {
                "system_prompt": store.consultant_system_prompt(),
                "default_system_prompt": build_consultant_system_prompt(),
            },
            "paths": {
                "agent": str(store.agent_path),
                "consultant": str(store.consultant_path),
            },
            "exists": {
                "agent": store.agent_exists,
                "consultant": store.consultant_exists,
            },
        }
        return data

    def save_project_agent_settings(
        self, project_id: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST System Prompt Agent/Consultant -> simpan project-local.

        Body menerima subset:
            {"agent": {"system_prompt": "..."},
             "consultant": {"system_prompt": "..."}}

        Nilai string kosong/None pada suatu prompt = HAPUS override (default
        AETHER berlaku kembali). Hanya project ini yang terpengaruh.

        Raises:
            ValidationError: bila project_id kosong atau payload tidak valid.
            NotFoundError: bila project tidak ditemukan.
        """
        from agent_ai.projects.project_settings import ProjectSettingsError

        body = body or {}
        # Pemisahan konfigurasi: provider/model/API key TIDAK boleh masuk lewat
        # endpoint Project Settings -> Agents (tetap GLOBAL).
        global_keys = {
            "provider",
            "provider_instance_id",
            "model",
            "model_id",
            "api_key",
            "api_key_env",
            "api_url",
            "base_url",
            "port",
            "compression",
            "api_retry",
        }
        leaked = set(body) & global_keys
        if leaked:
            raise ValidationError(
                "Field berikut milik konfigurasi GLOBAL AETHER (bukan Project "
                f"Settings -> Agents): {', '.join(sorted(leaked))}. "
                "Provider/model dikelola dari Sidebar -> Settings."
            )
        unknown = set(body) - {"agent", "consultant"}
        if unknown:
            raise ValidationError(
                f"Field tidak dikenal: {', '.join(sorted(unknown))}."
            )

        store = self._project_settings_store(project_id)
        try:
            if "agent" in body:
                agent = body.get("agent")
                if not isinstance(agent, dict):
                    raise ValidationError("'agent' harus berupa object.")
                extra = set(agent) - {"system_prompt"}
                if extra:
                    raise ValidationError(
                        f"Field tidak dikenal: {', '.join(sorted(extra))}."
                    )
                store.save_agent_settings(agent)
            if "consultant" in body:
                consultant = body.get("consultant")
                if not isinstance(consultant, dict):
                    raise ValidationError("'consultant' harus berupa object.")
                extra = set(consultant) - {"system_prompt"}
                if extra:
                    raise ValidationError(
                        f"Field tidak dikenal: {', '.join(sorted(extra))}."
                    )
                store.save_consultant_settings(consultant)
        except ProjectSettingsError as exc:
            raise ValidationError(str(exc)) from exc

        return self.get_project_agent_settings(project_id)

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

    def _active_project_root(self):
        """Root active project untuk operasi file workspace (Code Editor).

        Satu sumber path (active project). Boundary workspace TIDAK dibuat
        ulang: ReadFileTool/WriteFileTool AETHER yang memvalidasi path tetap di
        dalam root ini (tidak ada mekanisme security kedua).

        Raises:
            NotFoundError: bila tidak ada active project.
            ValidationError: bila active project tidak punya path.
        """
        from pathlib import Path as _Path

        active = self.get_active_project()
        if active is None:
            raise NotFoundError("Tidak ada active project.")
        root = active.get("root") or active.get("path")
        if not root:
            raise ValidationError("Active project tidak memiliki path.")
        target = _Path(root)
        if not target.exists() or not target.is_dir():
            raise ValidationError(f"Path project tidak ditemukan: {root}")
        return target

    def read_project_file(self, path: str) -> Dict[str, Any]:
        """Baca isi file project aktif via ReadFileTool AETHER.

        Dipakai Code Editor (Workbench) untuk memuat isi file. Read-only dan
        tidak ada abstraksi filesystem baru: tool AETHER existing dipakai apa
        adanya (termasuk batas ukuran file + validasi workspace boundary).

        Raises:
            NotFoundError: bila tidak ada active project.
            ValidationError: bila path kosong / di luar root / tidak ditemukan.
        """
        from agent_ai.tools.base import ToolError
        from agent_ai.tools.filesystem import ReadFileTool

        if not path:
            raise ValidationError("Field 'path' wajib diisi.")

        tool = ReadFileTool(root=self._active_project_root())
        try:
            return tool.execute(path=path)
        except ToolError as exc:
            raise ValidationError(str(exc)) from exc

    def write_project_file(self, path: str, content: str) -> Dict[str, Any]:
        """Simpan isi file project aktif via WriteFileTool AETHER.

        Dipakai Code Editor (Workbench) untuk menyimpan hasil edit. Penulisan
        dilakukan backend (bukan browser) memakai tool AETHER existing, jadi
        validasi workspace boundary + penulisan atomic tetap sama.

        Raises:
            NotFoundError: bila tidak ada active project.
            ValidationError: bila path kosong / di luar root / gagal ditulis.
        """
        from agent_ai.tools.base import ToolError
        from agent_ai.tools.workspace import WriteFileTool

        if not path:
            raise ValidationError("Field 'path' wajib diisi.")
        if content is None:
            raise ValidationError("Field 'content' wajib diisi.")

        tool = WriteFileTool(root=self._active_project_root())
        try:
            return tool.execute(path=path, content=content)
        except ToolError as exc:
            raise ValidationError(str(exc)) from exc

    def create_project_file(self, rel_path: str, content: str = "") -> Dict[str, Any]:
        """Buat file baru di dalam active project (hanya bila BELUM ada).

        Berbeda dengan write_project_file (Code Editor) yang menimpa file
        existing, method ini HANYA membuat file baru dan GAGAL bila target
        sudah ada — dipakai aksi "New File" Explorer agar tidak menimpa
        konten user secara diam-diam. Boundary workspace divalidasi terhadap
        active project root (polanya sama dengan delete_project_entry).

        Raises:
            NotFoundError: bila tidak ada active project.
            ValidationError: bila path kosong / di luar root / file sudah ada.
        """
        from agent_ai.tools.base import ToolError
        from agent_ai.tools.workspace import WriteFileTool

        if not rel_path:
            raise ValidationError("Field 'path' wajib diisi.")
        if content is None:
            content = ""

        root_resolved = self._active_project_root().resolve()
        target = (root_resolved / rel_path).resolve()

        # Validasi: target harus berada di dalam active project root.
        if target == root_resolved or root_resolved not in target.parents:
            raise ValidationError(f"Path '{rel_path}' berada di luar project root.")

        if target.exists() and target.is_file():
            raise ValidationError(f"File sudah ada: {rel_path}")

        # WriteFileTool AETHER existing: workspace boundary + penulisan atomic.
        tool = WriteFileTool(root=root_resolved)
        try:
            return tool.execute(path=rel_path, content=content)
        except ToolError as exc:
            raise ValidationError(str(exc)) from exc

    # ------------------------------------------------------------------ #
    # GitHub Backup (OPTIONAL per project; checkpoint/recovery via Git)
    #
    # Gateway HANYA mengorkestrasi: konfigurasi per project disimpan di
    # `<root>/.aether/github/` (credential terenkripsi Windows DPAPI), sedangkan
    # checkpoint/history/recovery memakai Git yang sudah ada (bukan DB kedua).
    # Token TIDAK pernah dikembalikan ke frontend / dicatat di log.
    # ------------------------------------------------------------------ #
    def _project_root_by_id(self, project_id: str):
        """Root project target berdasarkan id/name (satu sumber: store launcher).

        Raises:
            ValidationError: bila project_id kosong.
            NotFoundError: bila project tidak ditemukan.
            ValidationError: bila path project tidak ada / bukan directory.
        """
        from pathlib import Path as _Path

        if not project_id or not str(project_id).strip():
            raise ValidationError("Field 'project_id' wajib diisi.")
        project_id = str(project_id).strip()

        root = None
        meta = self.project_store.get_project(project_id)
        if meta is not None:
            root = meta.get("path") or meta.get("root")
        if not root:
            try:
                root = self.projects.get(project_id).root
            except ProjectNotFoundError as exc:
                raise NotFoundError(f"Project '{project_id}' tidak ditemukan.") from exc
        if not root:
            raise ValidationError("Project tidak memiliki path.")

        target = _Path(root)
        if not target.exists() or not target.is_dir():
            raise ValidationError(f"Path project tidak ditemukan: {root}")
        return target

    @staticmethod
    def _github_error_to_gateway(exc: Exception) -> GatewayError:
        """Petakan error GitHub Backup -> error gateway (HTTP)."""
        from api.github_backup import GithubBackupError

        message = str(exc)
        if "belum diamankan" in message or "force" in message.lower():
            err = ConflictError(message)
            err.code = "conflict"
            return err
        if isinstance(exc, GithubBackupError):
            return ValidationError(message)
        return GatewayError(message)

    def github_backup_status(self, project_id: str) -> Dict[str, Any]:
        """Status konfigurasi GitHub Backup + ringkasan changes (TANPA token)."""
        root = self._project_root_by_id(project_id)
        try:
            return self.github_backup_service.get_config(root)
        except Exception as exc:  # noqa: BLE001
            raise self._github_error_to_gateway(exc) from exc

    def save_github_backup_config(
        self, project_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Simpan konfigurasi GitHub Backup project (token dienkripsi)."""
        root = self._project_root_by_id(project_id)
        payload = payload or {}
        try:
            return self.github_backup_service.save_config(
                root,
                repository=payload.get("repository"),
                branch=payload.get("branch"),
                exclude=payload.get("exclude"),
                token=payload.get("token") or None,
                clear_token=bool(payload.get("clear_token", False)),
                enabled=payload.get("enabled"),
            )
        except Exception as exc:  # noqa: BLE001
            raise self._github_error_to_gateway(exc) from exc

    def test_github_backup_connection(
        self, project_id: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Uji token/repository/branch. TIDAK commit/push."""
        root = self._project_root_by_id(project_id)
        payload = payload or {}
        try:
            return self.github_backup_service.test_connection(
                root,
                repository=payload.get("repository"),
                branch=payload.get("branch"),
                token=payload.get("token") or None,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._github_error_to_gateway(exc) from exc

    def list_github_checkpoints(self, project_id: str) -> Dict[str, Any]:
        """Daftar checkpoint dari Git history project (bukan DB kedua)."""
        root = self._project_root_by_id(project_id)
        try:
            return {"checkpoints": self.github_backup_service.list_checkpoints(root)}
        except Exception as exc:  # noqa: BLE001
            raise self._github_error_to_gateway(exc) from exc

    def create_github_checkpoint(
        self, project_id: str, description: str
    ) -> Dict[str, Any]:
        """Buat checkpoint (add -> commit -> push) memakai Git AETHER existing."""
        root = self._project_root_by_id(project_id)
        try:
            return self.github_backup_service.create_checkpoint(root, description)
        except Exception as exc:  # noqa: BLE001
            raise self._github_error_to_gateway(exc) from exc

    def restore_github_checkpoint(
        self, project_id: str, commit: str, force: bool = False
    ) -> Dict[str, Any]:
        """Recovery: restore working tree ke sebuah checkpoint."""
        root = self._project_root_by_id(project_id)
        try:
            return self.github_backup_service.restore_checkpoint(
                root, commit, force=bool(force)
            )
        except Exception as exc:  # noqa: BLE001
            raise self._github_error_to_gateway(exc) from exc

    def get_github_backup_commits(
        self, project_id: str, page: int = 1, per_page: int = 20
    ) -> Dict[str, Any]:
        """Paginated Git history (read-only) for backup repo."""
        root = self._project_root_by_id(project_id)
        page = max(1, int(page))
        per_page = max(1, min(int(per_page), 100))
        try:
            return self.github_backup_service.get_commits(root, page, per_page)
        except Exception as exc:  # noqa: BLE001
            raise self._github_error_to_gateway(exc) from exc

    # ------------------------------------------------------------------ #
    # Tasks
    # ------------------------------------------------------------------ #
    def _prepare_task_image_parts(
        self, images: Optional[Any]
    ) -> Optional[List[Dict[str, Any]]]:
        """Normalisasi + proses gambar task -> image content parts (ADDITIVE).

        Memakai normalisasi gambar bersama (`_normalize_images`, batas IDENTIK
        dengan Consultant) dan helper vision bersama
        (`agent_ai.vision.parts.build_image_parts`). TIDAK menulis ulang logika
        image. None/kosong -> None (text-only, perilaku lama tidak berubah).

        Returns:
            List image content part (provider-agnostic) atau None bila tidak ada.

        Raises:
            ValidationError: bentuk/ukuran gambar tidak valid, atau gambar tidak
                dapat diproses modul vision (bukan PNG/JPEG/WebP / rusak).
        """
        normalized = self._normalize_images(images)
        if not normalized:
            return None

        from agent_ai.vision.parts import build_image_parts

        try:
            return build_image_parts(normalized)
        except ValidationError:
            raise
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - map error vision ke ValidationError
            from agent_ai.vision.models import VisionError

            if isinstance(exc, VisionError):
                raise ValidationError(f"Gambar tidak dapat diproses: {exc}") from exc
            raise

    def create_task(
        self,
        task: str,
        project_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        execution_mode: Optional[str] = None,
        images: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Buat task: validasi + siapkan via TaskPreparation, lalu eksekusi.

        Menyiapkan (context + plan) memakai komponen AETHER yang sudah ada,
        menyimpan state task di memori, lalu (bila auto_execute) menjalankan
        eksekusi nyata lewat AETHER Runtime di background thread.

        Args:
            task: deskripsi task (wajib, non-kosong).
            project_id: project terkait (opsional; divalidasi bila diisi).
            metadata: metadata tambahan (opsional).
            execution_mode: 'queue' | 'parallel' (opsional, default 'queue').
            images: daftar gambar opsional (multimodal, ADDITIVE). Setiap item:
                {"data": "<base64>", "mime_type": "image/png", "filename":
                opsional}. Dinormalisasi + diproses modul vision existing
                (batas sama dengan Consultant: maks 8 gambar, JPEG/PNG/WebP)
                lalu diteruskan sebagai image parts ke jalur Agent Task
                (AgentOrchestrator menerima `user_parts`).

        Returns:
            TaskRecord sebagai dict.

        Raises:
            ValidationError: bila task kosong, execution_mode tidak valid, atau
                gambar tidak valid / melebihi batas.
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

        # execution_mode — parameter task (Task 01, belum parallel execution).
        # Diterima sebagai argumen top-level atau di dalam metadata (backward
        # compat). Default 'queue' agar task lama kompatibel.
        raw_mode = execution_mode
        if raw_mode is None and metadata and isinstance(metadata, dict):
            raw_mode = metadata.get("execution_mode")
        execution_mode_norm = _normalize_execution_mode(raw_mode)

        # Attachment gambar (vision, ADDITIVE): normalisasi + proses
        # (preprocess) di sini agar gambar invalid ditolak LEBIH AWAL (400),
        # sama seperti jalur Consultant. Hasil = image content parts AETHER
        # (provider-agnostic). Disimpan per-task, BUKAN di TaskRecord.to_dict().
        image_parts = self._prepare_task_image_parts(images)

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
            execution_mode=execution_mode_norm,
        )
        with self._lock:
            self._queue_seq += 1
            record.queue_order = self._queue_seq
            self._tasks[task_id] = record
            self._prepared[task_id] = prepared
            # Attachment gambar per-task (dipakai jalur eksekusi Agent).
            if image_parts:
                self._task_attachments[task_id] = image_parts

        # Event TASK_CREATED (memakai event AETHER existing).
        self._emit(
            session.session_id,
            "task_created",
            task_id=task_id,
            payload={"task": record.task, "project_id": project_id},
        )

        # Event TASK_QUEUED: task benar-benar masuk queue (pending).
        # Event ini bersifat per-task (task_id + project_id) sehingga QueuePanel
        # dapat bereaksi reactively tanpa polling/refresh.
        self._emit(
            session.session_id,
            "task_queued",
            task_id=task_id,
            payload={
                "task": record.task,
                "project_id": project_id,
                "queue_state": "pending",
                "execution_mode": execution_mode_norm,
            },
        )

        # Dispatch berdasarkan execution_mode (Task 02):
        # - queue: lewat scheduler serial GLOBAL (1 slot, FIFO) — perilaku
        #   existing tetap dipertahankan.
        # - parallel: langsung running dengan concurrency control (configurable
        #   N, default 4). Task parallel TIDAK memakai slot queue serial.
        if self.auto_execute:
            if execution_mode_norm == "parallel":
                self._start_parallel_execution(task_id)
            else:
                self._scheduler_pump()

        return record.to_dict()

    # ------------------------------------------------------------------ #
    # Parallel execution — concurrency-controlled pool (configurable N)
    # ------------------------------------------------------------------ #
    def _start_parallel_execution(self, task_id: str) -> None:
        """Mulai task parallel dengan concurrency control (bukan unlimited).

        Task parallel menggunakan pool terpisah dari queue serial.
        Concurrency dikontrol oleh self._parallel_concurrency (default 4).
        """
        from api.execution import run_in_background

        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                return
            # Hanya task pending yang dapat dipromosikan; running/done diabaikan.
            if rec.queue_state != "pending":
                return
            if rec.status in ("completed", "failed", "cancelled"):
                return

            # Cek apakah ada slot tersedia di parallel pool
            parallel_running = sum(
                1 for t in self._tasks.values()
                if t.execution_mode == "parallel" and t.queue_state == "running"
            )
            if parallel_running >= self._parallel_concurrency:
                # Slot penuh - task tetap pending, akan dipromosikan saat slot bebas
                return

            rec.queue_state = "running"
            token = CancellationToken()
            self._cancel_tokens[task_id] = token
        run_in_background(lambda: self._execute_task(task_id, token))

    def _pump_parallel_pool(self) -> None:
        """Promosikan task parallel pending -> running jika slot tersedia.

        Dipanggil setelah task parallel selesai (di _execute_task finally)
        atau saat task baru dibuat dengan execution_mode="parallel".
        """
        from api.execution import run_in_background

        while True:
            with self._lock:
                # Hitung task parallel yang sedang running
                parallel_running = sum(
                    1 for t in self._tasks.values()
                    if t.execution_mode == "parallel" and t.queue_state == "running"
                )
                if parallel_running >= self._parallel_concurrency:
                    return  # Pool penuh

                # Cari task parallel pending yang eligible
                candidates = [
                    r for r in self._tasks.values()
                    if r.execution_mode == "parallel"
                    and r.queue_state == "pending"
                    and r.status not in ("completed", "failed", "cancelled")
                ]
                if not candidates:
                    return  # Tidak ada task parallel pending

                # Pilih berdasarkan queue_order (FIFO)
                candidate = min(candidates, key=lambda r: (r.queue_order, r.task_id))
                candidate.queue_state = "running"
                task_id = candidate.task_id
                token = CancellationToken()
                self._cancel_tokens[task_id] = token

            # Jalankan di luar lock
            run_in_background(lambda: self._execute_task(task_id, token))

    # ------------------------------------------------------------------ #
    # Serial scheduler GLOBAL (1 execution slot) — HANYA untuk queue mode
    # ------------------------------------------------------------------ #
    def _scheduler_pump(self) -> None:
        """Pilih SATU task `queue` pending eligible berikutnya dan jalankan.

        Hanya task dengan execution_mode == "queue" yang dijadwalkan di sini.
        Task parallel TIDAK pernah mengisi slot serial dan TIDAK dibatasi.
        Dipanggil (idempoten):
            - setelah create_task (queue),
            - setelah status terminal (completed/failed/cancelled),
            - setelah disable/enable/cancel.

        Algoritma (seluruh keputusan di dalam self._lock):
            1. Bila sudah ada task QUEUE RUNNING -> slot terpakai -> return.
            2. Ambil task pending paling awal menurut queue_order (FIFO)
               yang execution_mode == "queue". Task disabled/done/terminal
               otomatis dilewati.
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
            # [1] Slot serial HANYA ditempati task QUEUE. Parallel tidak
            #     memblokir slot queue dan tidak dibatasi jumlahnya.
            has_queue_running = any(
                r.queue_state == "running" and r.execution_mode == "queue"
                for r in self._tasks.values()
            )
            has_queue_token = any(
                tid in self._tasks and self._tasks[tid].execution_mode == "queue"
                for tid in self._cancel_tokens
            )
            if has_queue_running or has_queue_token:
                return
            # [2] Kandidat: pending QUEUE saja, bukan terminal, queue_order paling awal.
            candidates = [
                r
                for r in self._tasks.values()
                if r.queue_state == "pending"
                and r.execution_mode == "queue"
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
        runtime gagal tanpa on_status terminal), lalu memanggil scheduler yang
        sesuai berdasarkan execution_mode task. Ini TIDAK menjadikan
        AgentRuntime sebagai scheduler: runtime tetap tak tahu soal queue.
        """
        try:
            self._run_task_inner(task_id, token)
        finally:
            # --- Release execution slot (COMPLETED/FAILED/CANCELLED/semua path).
            with self._lock:
                self._cancel_tokens.pop(task_id, None)
                # Attachment gambar tidak lagi dibutuhkan setelah eksekusi
                # (hindari menahan base64 di memori).
                self._task_attachments.pop(task_id, None)
                rec = self._tasks.get(task_id)
                # Bila runtime crash tanpa pernah mengirim status terminal,
                # jangan biarkan slot "nyangkut" running selamanya.
                if rec is not None and rec.queue_state == "running":
                    if rec.status in ("completed", "failed", "cancelled"):
                        rec.queue_state = "done"
                    else:
                        rec.queue_state = "done"
                # Simpan execution_mode sebelum keluar lock
                exec_mode = rec.execution_mode if rec else "queue"
            # Slot bebas -> pump scheduler yang sesuai:
            # - queue mode: scheduler serial FIFO (1 slot).
            # - parallel mode: promosikan task parallel pending jika slot ada.
            if exec_mode == "parallel":
                self._pump_parallel_pool()
            self._scheduler_pump()

    def _run_task_inner(self, task_id: str, token: CancellationToken) -> None:
        """Isi eksekusi task (dipisah agar slot release di `_execute_task`)."""
        with self._lock:
            record = self._tasks.get(task_id)
            prepared = self._prepared.get(task_id)
            # Attachment gambar (image content parts) untuk task ini, bila ada.
            # Dibaca di dalam lock agar konsisten dengan penyimpanan saat
            # create_task.
            user_parts = self._task_attachments.get(task_id)
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

        run_kwargs: Dict[str, Any] = {
            "session_id": record.session_id,
            "task_id": task_id,
            "on_status": on_status,
            "workspace_root": workspace_root,
            "provider_name": provider_name,
            "model_name": model_name,
            "provider_instance_id": provider_instance_id,
            "model_id": model_id,
            "cancel_token": token,
        }
        # Hanya kirim `user_parts` bila ADA attachment gambar. Ini menjaga
        # kompatibilitas dengan TaskExecutor/verifier lama yang signature-nya
        # belum mengenal parameter ini (perilaku text-only tidak berubah).
        if user_parts:
            run_kwargs["user_parts"] = user_parts
        # Policy project-local (`<root>/.aether/settings/permissions.json`) HANYA
        # untuk project task ini. Hanya dikirim bila policy ADA, agar verifier/
        # executor lama yang belum mengenal parameter ini tetap bekerja.
        project_config = self.project_permission_config(record.project_id)
        if project_config is not None:
            run_kwargs["project_permission_config"] = project_config
        # Project Permission Matrix project-local (aksi x inside/outside). Bila
        # project punya `.aether/settings/permissions.json`, matrix-nya di-enforce
        # pada execution path (DENY menahan, ASK menahan + butuh approval). Bila
        # tidak ada, perilaku existing tidak berubah.
        project_matrix = self.project_permission_matrix(record.project_id)
        if project_matrix is not None:
            run_kwargs["project_permission_matrix"] = project_matrix

        # Gate approval (ASK): action yang butuh approval DITAHAN lalu dimintakan
        # keputusan user. Gate terikat ke task/session ini sehingga approval TIDAK
        # tertukar antar task. Hanya memengaruhi action ber-mode ask/require_approval.
        # Dikirim HANYA bila executor mendukungnya (verifier/executor lama tetap
        # bekerja tanpa parameter ini — backward compatible).
        if self._run_accepts("approval_gate"):
            run_kwargs["approval_gate"] = self.approval_gate_for(
                task_id, record.session_id
            )

        # Agent Execution Policy (fast/balanced/deep): mode dari metadata task
        # diteruskan sampai runtime (requested_mode). Metadata/strategi saja dan
        # TIDAK mengubah keputusan loop LLM. Dikirim HANYA bila executor
        # mendukungnya; nilai dibaca lewat helper policy existing agar
        # normalisasi mode TIDAK diduplikasi di gateway.
        requested_mode = self._task_requested_mode(meta)
        if requested_mode and self._run_accepts("requested_mode"):
            run_kwargs["requested_mode"] = requested_mode

        try:
            summary = self.task_executor.run(prepared, **run_kwargs)
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
                # Ringkasan policy (INFO) pada record runtime, bila tersedia.
                # Additive: ringkasan dari executor lama tanpa kunci ini -> None.
                policy_summary = summary.get("policy") if isinstance(summary, dict) else None
                if isinstance(policy_summary, dict):
                    self._apply_policy_summary(rec, policy_summary)

    @staticmethod
    def _task_requested_mode(metadata: Dict[str, Any]) -> Optional[str]:
        """Ambil mode policy (fast/balanced/deep) dari metadata task.

        Menerima kunci `agent_mode`/`policy_mode`/`mode` (prioritas berurutan)
        dan menormalisasi memakai resolver policy existing (alias mis.
        "minimal" -> "fast"), sehingga gateway TIDAK menduplikasi aturan mode.
        Mengembalikan None bila metadata tidak membawa mode (perilaku lama).
        """
        from agent_ai.runtime.policy import (
            DEFAULT_MODE,
            ExecutionPolicyResolver,
            MODE_METADATA_KEYS,
        )

        if not isinstance(metadata, dict):
            return None
        raw = None
        for key in MODE_METADATA_KEYS:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                raw = value
                break
        if raw is None:
            return None
        state = ExecutionPolicyResolver().resolve(raw)
        return state.requested_mode or DEFAULT_MODE

    @staticmethod
    def _apply_policy_summary(record: "TaskRecord", policy_summary: Dict[str, Any]) -> None:
        """Simpan ringkasan policy efektif pada TaskRecord (metadata tampilan).

        request/effective + alasan/escalation disimpan apa adanya; tidak ada
        nilai yang diinterpretasi ulang dan tidak ada status task yang diubah.
        """
        requested = policy_summary.get("requested_mode")
        effective = policy_summary.get("effective_mode")
        record.requested_mode = str(requested) if requested else None
        record.effective_mode = str(effective) if effective else None
        reason = policy_summary.get("reason")
        record.policy_reason = str(reason) if reason else None
        record.policy_escalated = bool(policy_summary.get("escalated"))
        escalations = policy_summary.get("escalations")
        record.policy_escalations = [
            dict(item) for item in escalations if isinstance(item, dict)
        ] if isinstance(escalations, list) else []

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
        from agent_ai.llm_config.providers import get_provider_type as _get_ptype

        try:
            if instance_id:
                instance = self.llm_config_service.get_provider_instance(instance_id)
                spec = _get_ptype(instance.provider_type)
                # Provider yang tidak membutuhkan model (9Router) tidak perlu
                # divalidasi model-nya.
                if spec is not None and not spec.requires_model:
                    return
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

    # ------------------------------------------------------------------ #
    # Approval (ASK) — koordinasi tahan-lanjut untuk mode require_approval.
    #
    # Ini BUKAN sistem permission kedua: keputusan tetap dibuat
    # PermissionManager existing. Gateway HANYA menjadi jembatan antara
    # execution thread (yang menahan action) dan HTTP request user (Allow/Deny),
    # memakai ApprovalCoordinator (primitif sinkron bounded) + event system
    # existing (SessionStore).
    # ------------------------------------------------------------------ #
    def _on_approval_event(
        self, session_id: str, event_type: str, payload: Dict[str, Any]
    ) -> None:
        """Sink approval -> SessionStore (event streaming existing).

        Event ditandai dengan task_id dari payload sehingga UI dapat mengaitkan
        approval ke task/execution yang benar (tidak tertukar antar task).
        """
        try:
            task_id = (payload or {}).get("task_id") or None
            self._emit(session_id, event_type, task_id=task_id, payload=payload)
        except Exception:  # noqa: BLE001 - event tidak boleh crash eksekusi
            return

    def approval_gate_for(self, task_id: str, session_id: str):
        """Bangun gate approval terikat ke satu task/session (untuk TaskExecutor).

        Gate dipanggil pada execution thread saat sebuah action butuh approval:
        ia menahan eksekusi, memancarkan `approval_requested`, lalu menunggu
        keputusan user (bounded timeout; timeout -> DENY, tidak pernah auto-allow).
        """
        from agent_ai.permission.approval import make_approval_gate

        return make_approval_gate(self._approvals, task_id=task_id, session_id=session_id)

    def _run_accepts(self, param: str) -> bool:
        """True bila ``task_executor.run`` menerima keyword ``param``.

        Dipakai untuk kompatibilitas: verifier/executor lama yang belum mengenal
        parameter baru (mis. ``approval_gate``) tetap dipanggil dengan signature
        lamanya (tidak error).
        """
        import inspect

        run = getattr(self.task_executor, "run", None)
        if run is None:
            return False
        try:
            params = inspect.signature(run).parameters
        except (TypeError, ValueError):
            return False
        if param in params:
            return True
        return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

    def list_approvals(self, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Daftar approval yang masih PENDING (opsional difilter task_id)."""
        return [r.to_dict() for r in self._approvals.pending(task_id)]

    def resolve_approval(self, request_id: str, allow: bool) -> Dict[str, Any]:
        """Selesaikan approval (Allow/Deny) -> lanjutkan/batalkan action tertahan.

        Raises:
            ValidationError: bila request_id kosong.
            NotFoundError: bila approval tidak ditemukan.
        """
        if not request_id or not str(request_id).strip():
            raise ValidationError("Field 'request_id' wajib diisi.")
        record = self._approvals.resolve(str(request_id).strip(), bool(allow))
        if record is None:
            raise NotFoundError(f"Approval '{request_id}' tidak ditemukan.")
        return record.to_dict()

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

    def list_tasks(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Daftar task yang dibuat (in-memory).
        
        Args:
            project_id: Filter task berdasarkan project (opsional). Bila None,
                mengembalikan semua task (perilaku lama untuk backward compat).
        """
        with self._lock:
            if project_id is not None:
                return [r.to_dict() for r in self._tasks.values() if r.project_id == project_id]
            return [r.to_dict() for r in self._tasks.values()]

    # ------------------------------------------------------------------ #
    # Task Queue (TAMPILAN/kontrol UI — bukan scheduler eksekusi)
    # ------------------------------------------------------------------ #
    # CATATAN: pada tahap ini BELUM ada scheduler serial. queue_state adalah
    # proyeksi UI dari TaskRecord (pending/running/disabled/done) + niat user
    # (disable = jangan dieksekusi). TIDAK ada TaskManager/queue subsystem
    # kedua: sumber data tetap self._tasks (satu queue GLOBAL AETHER).
    _QUEUE_ACTIVE = ("pending", "running", "disabled")

    def list_queue(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Daftar antrian task (aktif saja: pending/running/disabled).

        Filter opsional berdasarkan project_id (bila diberikan) untuk diterapkan pada
        task yang eligible (pending/running/disabled). Task yang tidak memiliki
        project_id (legacy) dipertahankan untuk ditampilkan bila filter tidak
        diset.

        Args:
            project_id: Filter antrian per project (opsional).

        Returns:
            Daftar dict task yang terfilter.
        """
        with self._lock:
            if project_id is not None:
                records = [
                    r for r in self._tasks.values()
                    if r.queue_state in self._QUEUE_ACTIVE and r.project_id == project_id
                ]
            else:
                records = [
                    r for r in self._tasks.values() if r.queue_state in self._QUEUE_ACTIVE
                ]
            # Urutkan menurut queue_order / FIFO.
            records.sort(key=lambda r: r.queue_order)
            return [r.to_dict() for r in records]

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
            # Parallel task yang di-enable harus langsung running (bypass queue).
            parallel_to_start = (
                task_id
                if record.execution_mode == "parallel" and queue_state == "pending"
                else None
            )
            result = record.to_dict()
        # Disable/Enable memengaruhi eligibility scheduler -> pump.
        # Enable queue: bisa langsung mempromosikan task ke RUNNING bila tidak ada
        # blocker di depannya (FIFO). Enable parallel: langsung running tanpa slot.
        if self.auto_execute:
            if parallel_to_start is not None:
                self._start_parallel_execution(parallel_to_start)
            else:
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

        Isolasi: bila `project_id` diberikan, HANYA log di root project itu yang
        dibaca (task project lain tidak pernah bocor). Bila `project_id` tidak
        diberikan, pencarian mencakup seluruh candidate root (project_id/active
        project, AETHER workspace, project terdaftar) lalu di-dedupe per task_id.

        Args:
            project_id: project terkait (opsional).

        Returns:
            Daftar task info terurut terbaru ke terlama.
        """
        from agent_ai.projects.aether_store import AetherProjectStore, TaskLogReader

        # Isolasi per project: bila project_id diberikan, HANYA baca root project
        # ini. JANGAN menambahkan repo root AETHER / project terdaftar lain
        # (perilaku `_candidate_log_roots`), karena itu membuat task milik
        # project lain ikut muncul di Sidebar -> Tasks (task "stale" dari
        # project sebelumnya). Tanpa project_id (perilaku lama / daftar semua)
        # tetap memakai seluruh candidate root.
        if project_id is not None:
            roots: List[str] = []
            meta = self.project_store.get_project(project_id)
            if meta is not None:
                root = meta.get("path") or meta.get("root")
                if root:
                    roots.append(str(root))
        else:
            roots = self._candidate_log_roots(None)

        by_task: Dict[str, Dict[str, Any]] = {}
        for root in roots:
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
        # Validate task ownership before reading log
        self._validate_task_project_ownership(task_id, project_id)
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
        # Validate task ownership before reading log
        self._validate_task_project_ownership(task_id, project_id)
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
        # Validate task ownership before reading log
        self._validate_task_project_ownership(task_id, project_id)
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
        # Project ownership boundary: when project_id is explicitly provided,
        # ONLY search within that project's root. Never fall back to workspace
        # or other projects (prevents cross-project data leakage).
        if root:
            candidates = [root]
        elif project_id is not None:
            meta = self.project_store.get_project(project_id)
            candidates = []
            if meta is not None:
                project_root = meta.get("path") or meta.get("root")
                if project_root:
                    candidates = [str(project_root)]
        else:
            candidates = self._candidate_log_roots(None)
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

    def _validate_task_project_ownership(self, task_id: str, project_id: Optional[str]) -> None:
        """Validate that task_id belongs to project_id.

        Used by History/Activity/Report APIs to enforce cross-project isolation.
        If project_id is None, skip validation (backward compat for global views).
        """
        if project_id is None:
            return
        with self._lock:
            record = self._tasks.get(task_id)
        if record is None:
            # Check persistent log if not in memory
            reader = self._reader_for_task(task_id, project_id)
            if reader is None:
                raise NotFoundError(f"Task '{task_id}' tidak ditemukan di log.")
            # If found in log but project_id was provided, we still allow access
            # (log may not have project_id metadata embedded)
            return
        if record.project_id != project_id:
            raise NotFoundError(f"Task '{task_id}' tidak ditemukan di project '{project_id}'.")

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
        # 1b) Tolak approval PENDING milik task ini agar action tertahan tidak
        #     menggantung (gate menerima DENY -> action dibatalkan segera).
        try:
            self._approvals.cancel_task(task_id)
        except Exception:  # noqa: BLE001 - cleanup tidak boleh gagal cancel
            pass
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

        normalized_images = self._normalize_images(images)

        # Sesi Consultant WAJIB ter-scope ke satu project (ISOLASI). Bila
        # project_id kosong, fallback HANYA ke active project; bila tetap tidak
        # ada, raise error jelas (TIDAK ada sesi lintas-project).
        project_id = self._consultant_session_project_id(project_id)

        if root is None:
            root = self._resolve_workspace_root(project_id)
        if not root:
            # Fallback ke root workspace AETHER (repo tempat backend berjalan)
            # agar Consultant tetap dapat menganalisis project AETHER sendiri.
            from pathlib import Path as _Path

            root = str(_Path(__file__).resolve().parents[3])

        if provider is None:
            provider = self._build_consultant_provider(provider_instance_id, model_id)

        # Emit live: teruskan event runtime Consultant ke event system AETHER
        # existing (SessionStore) memakai mekanisme emit_event yang sudah ada,
        # sehingga UI dapat menampilkan status nyata selama konsultasi lewat
        # SSE /api/events. Tidak membuat transport/store kedua.
        def _consult_event_emit(
            emit_session_id: str, event_type: str, payload: Dict[str, Any]
        ) -> None:
            self.emit_event(emit_session_id, event_type, payload=payload)

        try:
            result = self.consultant_service.consult(
                str(message).strip(),
                provider=provider,
                root=root,
                session_id=session_id,
                project_id=project_id,
                mode=mode,
                images=normalized_images,
                event_emit=_consult_event_emit,
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

    def _normalize_images(
        self, images: Optional[Any]
    ) -> Optional[List[Dict[str, Any]]]:
        """Validasi & normalisasi daftar gambar (GENERALIZED: Consultant + Task).

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

    def _normalize_consult_images(
        self, images: Optional[Any]
    ) -> Optional[List[Dict[str, Any]]]:
        """Alias backward-compatible untuk `_normalize_images`.

        Dipertahankan agar pemanggil/verifier lama (jalur Consultant) tetap
        bekerja; implementasi tunggal ada di `_normalize_images` sehingga
        batas jumlah/ukuran gambar IDENTIK di jalur Consultant dan Agent Task.
        """
        return self._normalize_images(images)

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
            # Default = Provider Instance DB yang SAMA dengan Settings
            # (`_select_default_llm`), bukan fallback diam-diam ke .env lama.
            try:
                instances = self.list_llm_providers()
            except Exception as exc:  # noqa: BLE001 - konfigurasi LLM gagal dibaca
                raise ValidationError(
                    f"Tidak dapat membaca konfigurasi LLM: {exc}"
                ) from exc
            instance_id = self._select_default_llm(instances)["instance_id"]
        if not instance_id:
            raise ValidationError(
                "Tidak ada Provider Instance yang dikonfigurasi untuk Consultant. "
                "Tambahkan provider di Settings terlebih dahulu."
            )

        try:
            # `include_api_key` default True (nilai api_key dipakai runtime).
            resolved = self.llm_config_service.resolve_runtime_config(
                instance_id, model_id=model_id
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
    # ------------------------------------------------------------------ #
    # Extension Management (Task 07) — thin facade over ExtensionManager
    # ------------------------------------------------------------------ #
    def _get_extension_manager(self):  # type: ignore[no-untyped-def]
        """Return singleton ExtensionManager bound to GatewayService shared registries.

        Registries are loaded once via ExtensionLoader (filesystem + lifecycle).
        All subsequent operations reuse same registry/capability/lifecycle objects,
        so catalog reflects live state (install/enable/disable/update/uninstall).
        No duplicated lifecycle logic: all via ExtensionManager.
        """
        if hasattr(self, "_ext_manager") and getattr(self, "_ext_manager", None) is not None:
            return self._ext_manager  # type: ignore[attr-defined]
        from agent_ai.extensions.capabilities import CapabilityRegistry
        from agent_ai.extensions.lifecycle import get_lifecycle_store
        from agent_ai.extensions.loader import ExtensionLoader
        from agent_ai.extensions.manager import ExtensionManager
        from agent_ai.extensions.registry import ExtensionRegistry

        cap_reg = getattr(self, "_ext_capability_registry", None)
        ext_reg = getattr(self, "_ext_registry", None)
        lifecycle = getattr(self, "_ext_lifecycle_store", None)
        if cap_reg is None or ext_reg is None:
            try:
                from agent_ai.extensions.agent_bridge import get_agent_extension_manager

                mgr = get_agent_extension_manager()
                self._ext_capability_registry = mgr.capability_registry  # type: ignore[attr-defined]
                self._ext_registry = mgr.registry  # type: ignore[attr-defined]
                self._ext_lifecycle_store = mgr.lifecycle_store  # type: ignore[attr-defined]
                self._ext_tool_registry = mgr.tool_registry  # type: ignore[attr-defined]
                self._ext_manager = mgr  # type: ignore[attr-defined]
                return mgr
            except Exception:
                pass
        if cap_reg is None or ext_reg is None:
            cap_reg = CapabilityRegistry()
            ext_reg = ExtensionRegistry()
            lifecycle = get_lifecycle_store()
            loader = ExtensionLoader(
                registry=ext_reg,
                capability_registry=cap_reg,
                lifecycle_store=lifecycle,
                enable_entry_points=False,
            )
            try:
                loader.load_all()
            except Exception:
                pass
            self._ext_capability_registry = cap_reg  # type: ignore[attr-defined]
            self._ext_registry = ext_reg  # type: ignore[attr-defined]
            self._ext_lifecycle_store = lifecycle  # type: ignore[attr-defined]
        else:
            if lifecycle is None:
                from agent_ai.extensions.lifecycle import get_lifecycle_store as _gls

                lifecycle = _gls()
                self._ext_lifecycle_store = lifecycle  # type: ignore[attr-defined]

        tool_reg = getattr(self, "_ext_tool_registry", None)
        if tool_reg is None:
            try:
                from agent_ai.tools.registry import get_extension_tool_registry

                tool_reg = get_extension_tool_registry()
            except Exception:
                tool_reg = None
            self._ext_tool_registry = tool_reg  # type: ignore[attr-defined]

        config_store = None
        try:
            from agent_ai.extensions.config import get_config_store

            config_store = get_config_store()
        except Exception:
            config_store = None

        mgr = ExtensionManager(
            registry=ext_reg,
            capability_registry=cap_reg,
            lifecycle_store=lifecycle,
            tool_registry=tool_reg,
            config_store=config_store,
        )
        self._ext_manager = mgr  # type: ignore[attr-defined]
        return mgr

    @staticmethod
    def _extension_error_to_gateway(exc: Exception):  # type: ignore[no-untyped-def]
        """Map Extension errors to GatewayError HTTP semantics."""
        from agent_ai.extensions.errors import (
            ExtensionCompatibilityError,
            ExtensionInstallError,
            ExtensionLifecycleError,
            ExtensionUninstallError,
            ExtensionUpdateError,
            ExtensionValidationError,
        )
        from agent_ai.extensions.manifest import DuplicateExtensionError

        msg = str(exc) or exc.__class__.__name__
        low = msg.lower()
        # Not found -> 404
        if isinstance(exc, (ExtensionLifecycleError, ExtensionUninstallError, ExtensionUpdateError)):
            if "not found" in low:
                from api.services import NotFoundError as _NF

                return _NF(msg)
        if isinstance(exc, DuplicateExtensionError):
            from api.services import ConflictError as _CF

            return _CF(msg)
        if isinstance(exc, ExtensionInstallError):
            if "duplicate" in low or "already installed" in low or "already exists" in low or "folder collision" in low:
                from api.services import ConflictError as _CF

                return _CF(msg)
            # git clone / validation etc. -> 400
            from api.services import ValidationError as _VE

            return _VE(msg)
        if isinstance(exc, (ExtensionValidationError, ExtensionCompatibilityError)):
            from api.services import ValidationError as _VE

            return _VE(msg)
        if isinstance(exc, ExtensionUpdateError):
            if "not found" in low:
                from api.services import NotFoundError as _NF

                return _NF(msg)
            from api.services import ValidationError as _VE

            return _VE(msg)
        if isinstance(exc, ExtensionUninstallError):
            if "not found" in low:
                from api.services import NotFoundError as _NF

                return _NF(msg)
            from api.services import ValidationError as _VE

            return _VE(msg)
        if isinstance(exc, ExtensionLifecycleError):
            from api.services import ValidationError as _VE

            return _VE(msg)
        # Generic duplicate message
        if "duplicate" in low or "already installed" in low:
            from api.services import ConflictError as _CF

            return _CF(msg)
        if "not found" in low:
            from api.services import NotFoundError as _NF

            return _NF(msg)
        # Fallback 400 for validation-like, 500 otherwise -> map to GatewayError 500 but we prefer 400 for extension errors
        from api.services import ValidationError as _VE

        return _VE(msg)

    def list_extensions(self) -> Dict[str, Any]:
        """List installed extensions (generic catalog, UI-friendly)."""
        mgr = self._get_extension_manager()
        items = mgr.list_installed()
        # Enrich with capability counts
        for item in items:
            try:
                caps = self._ext_capability_registry.list_by_extension(item["id"])  # type: ignore[attr-defined]
                summary: Dict[str, int] = {}
                for rec in caps:
                    summary[rec.type] = summary.get(rec.type, 0) + 1
                item["capabilities"] = summary
                item["capability_count"] = len(caps)
            except Exception:
                item.setdefault("capabilities", {})
                item.setdefault("capability_count", 0)
        # Include failed extensions not in registry but known via failures / lifecycle
        try:
            fails = self._ext_registry.failures()  # type: ignore[attr-defined]
            existing_ids = {i["id"] for i in items}
            for f in fails:
                fid = f.get("id")
                err = f.get("error", "")
                src = f.get("source", "")
                if fid and fid not in existing_ids:
                    items.append(
                        {
                            "id": fid,
                            "name": fid,
                            "version": "",
                            "description": "",
                            "api_version": "",
                            "status": "failed",
                            "enabled": False,
                            "error": err,
                            "installed_version": "",
                            "source": src,
                            "capabilities": {},
                            "capability_count": 0,
                        }
                    )
                    existing_ids.add(fid)
                elif not fid:
                    key = src or "unknown"
                    if key not in existing_ids:
                        items.append(
                            {
                                "id": key,
                                "name": key,
                                "version": "",
                                "description": "",
                                "api_version": "",
                                "status": "failed",
                                "enabled": False,
                                "error": err,
                                "installed_version": "",
                                "source": src,
                                "capabilities": {},
                                "capability_count": 0,
                            }
                        )
        except Exception:
            pass
        try:
            rows = self._ext_lifecycle_store.all_rows()  # type: ignore[attr-defined]
            ids = {i["id"] for i in items}
            for r in rows:
                eid = r.get("extension_id")
                if r.get("status") == "failed" and eid not in ids:
                    items.append(
                        {
                            "id": eid,
                            "name": eid,
                            "version": r.get("installed_version") or "",
                            "description": "",
                            "api_version": "",
                            "status": "failed",
                            "enabled": False,
                            "error": r.get("error") or "",
                            "installed_version": r.get("installed_version") or "",
                            "source": "",
                            "capabilities": {},
                            "capability_count": 0,
                        }
                    )
        except Exception:
            pass
        items_sorted = sorted(items, key=lambda x: str(x.get("id", "")).lower())
        return {"count": len(items_sorted), "extensions": items_sorted}

    def get_extension(self, extension_id: str) -> Dict[str, Any]:
        """Detail for one extension (generic, secret-safe)."""
        if not extension_id or not str(extension_id).strip():
            raise ValidationError("Field 'extension_id' wajib diisi.")
        mgr = self._get_extension_manager()
        try:
            data = mgr.get_status(str(extension_id).strip())
        except Exception as exc:
            raise self._extension_error_to_gateway(exc) from exc
        # Capability summary
        try:
            caps = self._ext_capability_registry.list_by_extension(data["id"])  # type: ignore[attr-defined]
            summary: Dict[str, int] = {}
            for rec in caps:
                summary[rec.type] = summary.get(rec.type, 0) + 1
            data["capabilities"] = summary
            data["capability_count"] = len(caps)
        except Exception:
            data.setdefault("capabilities", {})
            data.setdefault("capability_count", 0)
        # UI contributions (generic)
        try:
            from agent_ai.extensions.ui import UICatalog

            cat = UICatalog(self._ext_capability_registry, self._ext_registry)  # type: ignore[attr-defined]
            contribs = cat.list_contributions(extension_id=data["id"], enabled_only=False)
            data["ui_contributions"] = [c.to_dict() for c in contribs]
            data["ui_count"] = len(contribs)
        except Exception:
            data.setdefault("ui_contributions", [])
            data.setdefault("ui_count", 0)
        return data

    def install_extension(self, repository_url: str, ref: Optional[str] = None) -> Dict[str, Any]:
        if not repository_url or not str(repository_url).strip():
            raise ValidationError("Field 'repository_url' wajib diisi.")
        mgr = self._get_extension_manager()
        try:
            result = mgr.install(str(repository_url).strip(), ref=str(ref).strip() if ref and str(ref).strip() else None)
        except Exception as exc:
            raise self._extension_error_to_gateway(exc) from exc
        result["restart_required"] = False
        return result

    def enable_extension(self, extension_id: str) -> Dict[str, Any]:
        if not extension_id or not str(extension_id).strip():
            raise ValidationError("Field 'extension_id' wajib diisi.")
        mgr = self._get_extension_manager()
        try:
            result = mgr.enable(str(extension_id).strip())
        except Exception as exc:
            raise self._extension_error_to_gateway(exc) from exc
        return result

    def disable_extension(self, extension_id: str) -> Dict[str, Any]:
        if not extension_id or not str(extension_id).strip():
            raise ValidationError("Field 'extension_id' wajib diisi.")
        mgr = self._get_extension_manager()
        try:
            result = mgr.disable(str(extension_id).strip())
        except Exception as exc:
            raise self._extension_error_to_gateway(exc) from exc
        return result

    def update_extension(self, extension_id: str, repository_url: Optional[str] = None, ref: Optional[str] = None) -> Dict[str, Any]:
        if not extension_id or not str(extension_id).strip():
            raise ValidationError("Field 'extension_id' wajib diisi.")
        mgr = self._get_extension_manager()
        try:
            result = mgr.update(str(extension_id).strip(), repository_url=str(repository_url).strip() if repository_url and str(repository_url).strip() else None, ref=str(ref).strip() if ref and str(ref).strip() else None)
        except Exception as exc:
            raise self._extension_error_to_gateway(exc) from exc
        # Task 06 explicitly requires restart for Python code activation — never hot reload
        result["restart_required"] = True
        return result

    def uninstall_extension(self, extension_id: str) -> Dict[str, Any]:
        if not extension_id or not str(extension_id).strip():
            raise ValidationError("Field 'extension_id' wajib diisi.")
        mgr = self._get_extension_manager()
        try:
            result = mgr.uninstall(str(extension_id).strip())
        except Exception as exc:
            raise self._extension_error_to_gateway(exc) from exc
        return result

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
