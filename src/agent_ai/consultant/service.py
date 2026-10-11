"""AETHER Consultant Service: reasoning loop + session context.

Consultant memakai loop & tool AETHER yang SUDAH ADA:
    - reasoning/tool loop : `agent_ai.core.orchestrator.AgentOrchestrator`
                            (continuous loop Native Tool Calling).
    - tool execution      : `agent_ai.core.executor.ToolExecutor` + registry
                            yang dikurasi (read-only + Bible).
    - Project Knowledge   : `agent_ai.projects.brain.ProjectBrain` (Bible).
    - boundary            : `agent_ai.consultant.policy`.

Service TIDAK membuat Agent/loop/tool/store baru; ia merakit komponen existing
dengan boundary & prompt Consultant. Session context disimpan di memori proses
(in-memory) — BUKAN storage subsystem baru; ini konteks percakapan, bukan
persistent knowledge (persistent knowledge tetap Project Bible).
"""

from __future__ import annotations

import re
import threading
import uuid
from typing import Any, Dict, List, Optional

from agent_ai.consultant.guard import (
    ConsultantBoundProvider,
    ConsultantRetrievalGuard,
)
from agent_ai.consultant.models import (
    ConsultantResult,
    ConsultantTurn,
    normalize_consultant_mode,
)
from agent_ai.consultant.policy import build_consultant_permission_manager
from agent_ai.consultant.prompt import build_consultant_system_prompt
from agent_ai.consultant.tools import build_consultant_registry
from agent_ai.core.executor import ToolExecutor
from agent_ai.core.models import AgentStatus
from agent_ai.core.orchestrator import AgentOrchestrator
# `_build_image_parts` di-REUSE dari modul vision bersama (dipakai baik jalur
# Consultant maupun Agent Task). Satu implementasi tunggal; alias dipertahankan
# agar pemanggil lama tetap bekerja.
from agent_ai.vision.parts import build_image_parts as _build_image_parts
from agent_ai.consultant.store import (
    ConsultantSessionStore,
    project_sessions_path,
)

#: Batas langkah reasoning/tool per giliran konsultasi (safety, bukan target).
_DEFAULT_MAX_STEPS = 40

#: Batas jumlah giliran percakapan yang disertakan sebagai konteks (anti unbounded).
_MAX_CONTEXT_TURNS = 12

#: Pola Task Proposal: blok berpagar bahasa `task`/`task-proposal`.
_TASK_FENCE_RE = re.compile(r"```[ \t]*task(?:-proposal)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_task_proposal(text: Optional[str]) -> Optional[str]:
    """Ambil Task Proposal dari jawaban Consultant (blok berpagar `task`).

    Returns:
        Isi Task Proposal (string) atau None bila tidak ada.
    """
    if not text:
        return None
    match = _TASK_FENCE_RE.search(text)
    if not match:
        return None
    body = (match.group(1) or "").strip()
    return body or None


# `_build_image_parts` = `agent_ai.vision.parts.build_image_parts` (diimpor
# sebagai `_build_image_parts` di atas). Logika image tunggal & dipakai bersama
# jalur Consultant dan Agent Task.


def _auto_title(text: str) -> str:
    """Buat judul otomatis dari pesan user pertama (truncate ~40 char)."""
    if not text:
        return "New Chat"
    cleaned = " ".join(str(text).split())
    if not cleaned:
        return "New Chat"
    return cleaned[:40] if len(cleaned) <= 40 else cleaned[:37] + "..."


class ConsultantSession:
    """Konteks satu sesi konsultasi (percakapan lintas giliran).

    Metadata tambahan (project_id, title, created_at, updated_at) memungkinkan
    panel Sessions UI menampilkan daftar sesi per project, mengurutkan, dan
    memungkinkan rename. Sesi DIPERSISTENKAN project-local di
    ``<project_path>/.aether/consultant/sessions.json`` (lihat store.py).
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        title: Optional[str] = None,
        created_at: Optional[float] = None,
        updated_at: Optional[float] = None,
        turns: Optional[List[ConsultantTurn]] = None,
        on_change: Optional[Any] = None,
    ) -> None:
        import time

        self.session_id = session_id or uuid.uuid4().hex
        self.project_id: Optional[str] = project_id
        self.title: str = title or "New Chat"
        now = time.time()
        self.created_at: float = created_at if created_at is not None else now
        self.updated_at: float = updated_at if updated_at is not None else self.created_at
        self.turns: List[ConsultantTurn] = list(turns or [])
        # Optional callback invoked whenever session state mutates (write-through).
        self._on_change = on_change

    def _notify(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change(self)
            except Exception:  # noqa: BLE001 - persistensi tidak boleh menggagalkan operasi
                pass

    def touch(self) -> None:
        """Perbarui timestamp sesi (dipanggil pada setiap interaksi)."""
        import time

        self.updated_at = time.time()

    @property
    def first_user_turn(self) -> Optional[str]:
        """Kembalikan teks pesan user pertama (untuk auto-title / preview)."""
        for turn in self.turns:
            if turn.role == "user":
                return turn.text
        return None

    def add(self, role: str, text: str) -> None:
        """Tambahkan satu giliran ke konteks sesi (full retention)."""
        self.turns.append(ConsultantTurn(role=role, text=text or ""))
        self.touch()
        # Auto-title: jika masih default & ada pesan user pertama, turunkan
        # dari teks user pertama (hanya sekali).
        if self.title == "New Chat" and role == "user" and text and text.strip():
            self.title = _auto_title(text)
        # Retain every turn. Context bounding is applied only when building the
        # task sent to the LLM, so the UI can always resume the full transcript.
        self._notify()

    def build_task(self, message: str) -> str:
        """Build task with only the bounded recent context window."""
        if not self.turns:
            return message
        effective_turns = self.turns[-_MAX_CONTEXT_TURNS * 2 :]
        lines = ["# Percakapan konsultasi sebelumnya", ""]
        for turn in effective_turns:
            who = "User" if turn.role == "user" else "Consultant"
            lines.append(f"{who}: {turn.text}")
        lines.append("")
        lines.append("# Permintaan konsultasi baru")
        lines.append(message)
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "project_id": self.project_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turns": [t.to_dict() for t in self.turns],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConsultantSession":
        """Restore a ConsultantSession from a serialized dict."""
        raw_turns = data.get("turns") or []
        turns = [
            ConsultantTurn(role=t.get("role", ""), text=t.get("text", ""))
            for t in raw_turns
            if isinstance(t, dict)
        ]
        return cls(
            session_id=data.get("session_id"),
            project_id=data.get("project_id"),
            title=data.get("title"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            turns=turns,
        )


class ConsultantProjectScope:
    """Resolusi project OTORITATIF untuk scoping sesi Consultant.

    Sesi Consultant HANYA boleh diakses pada satu project. Karena core
    (`agent_ai`) tidak boleh bergantung pada layer web, scope di-INJECT oleh
    pemanggil (mis. GatewayService) yang memiliki akses ke project registry.

    Kontrak minimal:
        active_project_id() -> Optional[str]
            project id yang sedang aktif (fallback aman bila pemanggil tidak
            menyebutkan project_id).
        project_root(project_id) -> Optional[str]
            path root OTORITATIF untuk sebuah project id (dari registry).
            Mengembalikan None bila project tidak dikenal.

    Implementasi default ``None`` (tanpa scope) berarti service TIDAK dapat
    memetakan project_id -> root; dalam mode itu hanya ``store_path`` eksplisit
    (untuk test/verifier, satu file) yang dipakai.
    """

    def active_project_id(self) -> Optional[str]:
        return None

    def project_root(self, project_id: str) -> Optional[str]:
        return None

    def registered_project_ids(self) -> List[str]:
        """Daftar project id yang TERVERIFIKASI terdaftar (untuk migrasi)."""
        return []


class ConsultantScopeError(ValueError):
    """Operasi sesi Consultant tanpa project scope yang jelas/dikenal."""


class _ConsultantProjectContext:
    """Store + in-memory cache untuk SATU project (isolasi keras).

    Cache key = ``(project_id, session_id)``. Kunci tuple penting untuk mode
    store eksplisit (satu file melayani banyak project_id): cache tidak boleh
    mencampur sesi id-sama dari project berbeda.
    """

    __slots__ = ("store", "sessions", "persist_callback")

    def __init__(self, store: ConsultantSessionStore, persist_callback: Any) -> None:
        self.store = store
        self.sessions: Dict[Any, ConsultantSession] = {}
        self.persist_callback = persist_callback


class ConsultantService:
    """Menjalankan satu giliran konsultasi memakai komponen AETHER existing.

    ISOLASI PER PROJECT
        Sesi Consultant disimpan PROJECT-LOCAL di
        ``<project_path>/.aether/consultant/sessions.json`` dan HANYA diakses
        lewat ``project_id`` yang dapat dipetakan ke root project otoritatif.
        Tidak ada jalur yang mencampur sesi antar-project (tidak ada pencarian
        lintas-project, tidak ada daftar global).

    Args:
        max_steps: batas langkah reasoning/tool per giliran (safety).
        store_path: path file sessions eksplisit (SINGLE-project, untuk
            test/verifier). Bila diisi, seluruh project_id dipetakan ke file
            yang sama (isolasi tetap dijaga di dalam file via project_id).
        project_scope: resolver project otoritatif (lihat
            :class:`ConsultantProjectScope`). Wajib untuk mode project-local.
    """

    def __init__(
        self,
        max_steps: int = _DEFAULT_MAX_STEPS,
        store_path: Optional[str] = None,
        project_scope: Optional[ConsultantProjectScope] = None,
    ) -> None:
        self.max_steps = max_steps
        self._project_scope = project_scope
        # Explicit single-file store (test/verifier). None => project-local.
        self._explicit_store_path = store_path
        # One context (store + in-memory cache) PER project root. Keyed by the
        # resolved project root (or "" for the explicit single-file mode).
        self._contexts: Dict[str, "_ConsultantProjectContext"] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Project scoping
    # ------------------------------------------------------------------ #
    def _resolve_project_id(self, project_id: Optional[str]) -> str:
        """Tentukan project id efektif, atau raise untuk mode project-scoped.

        Aturan:
            * ``project_id`` diberikan -> dipakai.
            * tidak diberikan, scope ada + active project -> fallback active.
            * tidak diberikan, scope ada tapi tanpa active -> error (isolasi).
            * tidak diberikan, TANPA scope & TANPA store_path (usage bare
              core: verifier/script) -> namespace anonim "" (ephemeral,
              in-memory) agar perilaku API lama tetap bekerja. Tidak ada file
              global yang ditulis, sehingga tidak ada kebocoran lintas-project.
        """
        candidate = (project_id or "").strip()
        if candidate:
            return candidate
        scope = self._project_scope
        if scope is not None:
            active = (scope.active_project_id() or "").strip()
            if active:
                return active
            raise ConsultantScopeError(
                "project_id wajib untuk operasi sesi Consultant. Tidak ada active "
                "project yang dapat dipakai sebagai fallback."
            )
        if self._explicit_store_path is not None:
            # Explicit store = persistent sessions => scoping WAJIB.
            raise ConsultantScopeError(
                "project_id wajib untuk operasi sesi Consultant pada store "
                "eksplisit (isolasi per project)."
            )
        # Bare core usage (tanpa scope & tanpa path): sesi ephemeral in-memory.
        return ""

    def _resolve_root(self, project_id: str) -> Optional[str]:
        """Root OTORITATIF untuk project_id (None bila tidak dikenal)."""
        if self._project_scope is None:
            return None
        root = self._project_scope.project_root(project_id)
        if not root:
            return None
        return str(root)

    def _context_for_project(self, project_id: str) -> "_ConsultantProjectContext":
        """Ambil/buat context (store + cache) untuk satu project."""
        with self._lock:
            if self._explicit_store_path is not None:
                # Single-file mode: one context for all project_ids.
                key = "explicit"
                ctx = self._contexts.get(key)
                if ctx is None:
                    ctx = _ConsultantProjectContext(
                        ConsultantSessionStore(self._explicit_store_path),
                        self._make_persist_callback_for_key(key),
                    )
                    self._contexts[key] = ctx
                return ctx

            if self._project_scope is None:
                # Bare core usage (no scope, no path): EPHEMERAL in-memory store.
                # Nothing is written to disk, so no cross-project file leak.
                key = "ephemeral"
                ctx = self._contexts.get(key)
                if ctx is None:
                    ctx = _ConsultantProjectContext(
                        ConsultantSessionStore(ephemeral=True),
                        self._make_persist_callback_for_key(key),
                    )
                    self._contexts[key] = ctx
                return ctx

            root = self._resolve_root(project_id)
            if not root:
                raise ConsultantScopeError(
                    f"Project '{project_id}' tidak terdaftar (root otoritatif "
                    "tidak ditemukan). Sesi Consultant tidak dapat diakses."
                )
            ctx = self._contexts.get(root)
            if ctx is None:
                ctx = _ConsultantProjectContext(
                    ConsultantSessionStore(project_sessions_path(root)),
                    self._make_persist_callback_for_key(root),
                )
                self._contexts[root] = ctx
            return ctx

    def _make_persist_callback_for_key(self, key: str):
        """Write-through callback yang menulis ke context ber-key ``key``."""
        def _persist(session: ConsultantSession) -> None:
            ctx = self._contexts.get(key)
            if ctx is not None:
                ctx.store.save_session(session.to_dict())
        return _persist

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #
    def _get_session(
        self, session_id: Optional[str], project_id: Optional[str] = None
    ) -> ConsultantSession:
        """Ambil/buat sesi konsultasi pada SATU project (thread-safe).

        ``project_id`` TIDAK lagi opsional secara bebas: bila kosong, fallback
        hanya ke active project (bila scope menyediakannya); jika tidak, raise
        :class:`ConsultantScopeError`. Tidak ada fallback lintas-project.
        """
        effective_pid = self._resolve_project_id(project_id)
        ctx = self._context_for_project(effective_pid)
        key = (effective_pid, session_id)
        with self._lock:
            if session_id:
                existing = ctx.sessions.get(key)
                if existing is not None:
                    return existing
                stored = ctx.store.get_session(session_id, project_id=effective_pid)
                if stored is not None:
                    session = ConsultantSession.from_dict(stored)
                    session.project_id = effective_pid
                    session._on_change = ctx.persist_callback
                    ctx.sessions[key] = session
                    return session
            session = ConsultantSession(
                session_id=session_id,
                project_id=effective_pid,
                on_change=ctx.persist_callback,
            )
            ctx.sessions[(effective_pid, session.session_id)] = session
            ctx.store.save_session(session.to_dict())
            return session

    def _find_session(
        self, session_id: str, project_id: Optional[str] = None
    ) -> Optional[ConsultantSession]:
        """Cari sesi pada SATU project; None bila tidak ada.

        Tidak ada varian lintas-project: ``project_id`` selalu diwajibkan
        (fallback hanya ke active project via scope).
        """
        effective_pid = self._resolve_project_id(project_id)
        ctx = self._context_for_project(effective_pid)
        key = (effective_pid, session_id)
        with self._lock:
            session = ctx.sessions.get(key)
            if session is not None:
                return session
            stored = ctx.store.get_session(session_id, project_id=effective_pid)
            if stored is not None:
                session = ConsultantSession.from_dict(stored)
                session.project_id = effective_pid
                session._on_change = ctx.persist_callback
                ctx.sessions[key] = session
                return session
        return None

    def create_session(
        self, project_id: Optional[str] = None, title: Optional[str] = None
    ) -> Dict[str, Any]:
        """Buat sesi Consultant baru (ter-scope ke project) dan simpan ke disk."""
        effective_pid = self._resolve_project_id(project_id)
        ctx = self._context_for_project(effective_pid)
        with self._lock:
            session = ConsultantSession(
                project_id=effective_pid,
                title=title,
                on_change=ctx.persist_callback,
            )
            ctx.sessions[(effective_pid, session.session_id)] = session
            ctx.store.save_session(session.to_dict())
            return session.to_dict()

    def list_sessions(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Daftar metadata sesi untuk SATU project, terbaru lebih dulu.

        ``project_id`` diwajibkan (fallback hanya ke active project). Tidak ada
        daftar lintas-project.
        """
        effective_pid = self._resolve_project_id(project_id)
        ctx = self._context_for_project(effective_pid)
        with self._lock:
            by_id: Dict[str, ConsultantSession] = {
                s.session_id: s
                for (pid, _sid), s in ctx.sessions.items()
                if pid == effective_pid
            }
            # Include sessions from the store that were not yet cached.
            for meta in ctx.store.list_sessions(effective_pid):
                sid = meta.get("session_id")
                if sid and sid not in by_id:
                    data = ctx.store.get_session(sid, project_id=effective_pid)
                    if data:
                        session = ConsultantSession.from_dict(data)
                        session.project_id = effective_pid
                        session._on_change = ctx.persist_callback
                        ctx.sessions[(effective_pid, sid)] = session
                        by_id[sid] = session
            result = list(by_id.values())
            result.sort(key=lambda s: s.updated_at, reverse=True)
            return [s.to_dict() for s in result]

    def get_session(
        self, session_id: str, project_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Kembalikan konteks sesi pada SATU project (bila ada)."""
        session = self._find_session(session_id, project_id)
        return session.to_dict() if session is not None else None

    def rename_session(
        self, session_id: str, title: str, project_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Ubah judul sesi pada SATU project; None bila tidak ditemukan."""
        effective_pid = self._resolve_project_id(project_id)
        ctx = self._context_for_project(effective_pid)
        session = self._find_session(session_id, effective_pid)
        if session is None:
            return None
        with self._lock:
            session.title = str(title or "New Chat").strip() or "New Chat"
            session.touch()
            ctx.store.save_session(session.to_dict())
            return session.to_dict()

    def delete_session(
        self, session_id: str, project_id: Optional[str] = None
    ) -> bool:
        """Hapus sesi Consultant pada SATU project."""
        return self.reset_session(session_id, project_id=project_id)

    def reset_session(
        self, session_id: str, project_id: Optional[str] = None
    ) -> bool:
        """Hapus konteks sesi pada SATU project. Returns True bila ada."""
        effective_pid = self._resolve_project_id(project_id)
        ctx = self._context_for_project(effective_pid)
        key = (effective_pid, session_id)
        with self._lock:
            existed = key in ctx.sessions
            if not existed:
                existed = ctx.store.get_session(session_id, project_id=effective_pid) is not None
            if not existed:
                return False
            ctx.sessions.pop(key, None)
            ctx.store.delete_session(session_id, project_id=effective_pid)
            return True

    # ------------------------------------------------------------------ #
    # Consult
    # ------------------------------------------------------------------ #
    def consult(
        self,
        message: str,
        *,
        provider: Any,
        root: Optional[str] = None,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        max_steps: Optional[int] = None,
        mode: Optional[str] = None,
        images: Optional[List[Dict[str, Any]]] = None,
        event_emit: Optional[Any] = None,
    ) -> ConsultantResult:
        """Jalankan satu giliran konsultasi dan kembalikan hasilnya.

        Args:
            message: pertanyaan/permintaan user (wajib).
            provider: instance BaseProvider (dibangun pemanggil dari konfigurasi
                LLM tersimpan; Consultant tidak memilih provider sendiri).
            root: root project target. Bila diisi, tool dibatasi ke root itu dan
                Project Bible dibaca/ditulis di `<root>/.aether/bible/`.
            session_id: id sesi konsultasi (untuk konteks lintas giliran).
            project_id: id project terkait. WAJIB (atau dapat di-resolve dari
                active project via ``project_scope``): sesi disimpan
                project-local dan MENGISOLASI konteks per project. Bila kosong
                dan tidak ada active project, raise :class:`ConsultantScopeError`.
            max_steps: override batas langkah.
            mode: mode Consultant ("quick" | "investigate"). Default "quick".
                Mode menentukan tool yang benar-benar tersedia bagi LLM dan
                instruksi prompt (bukan sekadar prompt saja).
            images: daftar gambar opsional untuk pesan user (multimodal).
                Setiap item: {"data": "<base64>", "mime_type": "image/png",
                "filename": opsional}. Diproses lewat modul vision existing
                (ImagePreprocessor) menjadi payload provider-agnostic, lalu
                dilampirkan pada pesan user (content parts). Kosong/None =
                perilaku text-only tidak berubah.
            event_emit: callable opsional ``(session_id, event_type, payload)``
                yang dipanggil untuk memancarkan event runtime Consultant
                (tool_called/tool_completed) ke event system AETHER yang SUDAH
                ADA (SessionStore, lewat Gateway). Observability saja: TIDAK
                mengubah reasoning/loop, tidak ada transport/store kedua.
                Bila None, perilaku TIDAK berubah (hanya mengisi tool_events).

        Returns:
            ConsultantResult.

        Raises:
            ValueError: message kosong / provider kosong.
            UnsupportedImageFormatError / InvalidImageError: gambar tidak valid.
        """
        if not message or not str(message).strip():
            raise ValueError("Pesan konsultasi ('message') wajib diisi.")
        if provider is None:
            raise ValueError("Consultant butuh provider (BaseProvider).")

        effective_mode = normalize_consultant_mode(mode)

        session = self._get_session(session_id, project_id=project_id)

        # Safety/control layer Consultant: bound retrieval Project Map per
        # giliran (ATLAS: 3 query untuk quick / 6 untuk investigate). Guard ini
        # HANYA milik Consultant dan tidak memengaruhi budget/perilaku Agent.
        # Satu guard per panggilan consult() -> batas dihitung per pertanyaan.
        retrieval_guard = ConsultantRetrievalGuard(mode=effective_mode)

        registry = build_consultant_registry(
            root, mode=effective_mode, guard=retrieval_guard
        )
        executor = ToolExecutor(
            registry=registry,
            permission_manager=build_consultant_permission_manager(),
        )

        # Project Bible sebagai context awal (READ). Learning TIDAK dilakukan di
        # sini: Consultant memutuskan sendiri kapan menyimpan knowledge lewat
        # tool update_project_bible.
        brain = None
        if root:
            try:
                from agent_ai.projects.brain import ProjectBrain

                brain = ProjectBrain.for_project(root, provider=provider)
            except Exception:  # noqa: BLE001 - konteks Bible tidak boleh menggagalkan konsultasi
                brain = None

        tool_events: List[Dict[str, Any]] = []

        def _sink(event_type: str, payload: Dict[str, Any]) -> None:
            if event_type == "tool_called":
                tool_events.append(
                    {
                        "tool": payload.get("tool", ""),
                        "target": payload.get("target", ""),
                        "success": None,
                    }
                )
            elif event_type == "tool_completed":
                tool_events.append(
                    {
                        "tool": payload.get("tool", ""),
                        "target": payload.get("target", ""),
                        "success": payload.get("success"),
                        "error": payload.get("error"),
                    }
                )
            # Observability LIVE: teruskan event runtime yang SAMA ke event
            # system AETHER existing (SessionStore via Gateway) agar UI dapat
            # menampilkan status nyata selama konsultasi. Ini TIDAK mengubah
            # reasoning/loop; hanya emit tambahan. Kegagalan emit tidak boleh
            # menggagalkan konsultasi.
            if event_emit is not None and event_type in ("tool_called", "tool_completed"):
                try:
                    event_emit(
                        session.session_id,
                        event_type,
                        {
                            "tool": payload.get("tool", ""),
                            "target": payload.get("target", ""),
                            "success": payload.get("success"),
                            "error": payload.get("error"),
                        },
                    )
                except Exception:  # noqa: BLE001 - observability tidak boleh crash
                    pass

        # Provider dibungkus proxy Consultant: begitu bound retrieval tercapai,
        # tool map (atlas_query/rig_query) dilepas dari penawaran ke LLM
        # sehingga LLM berhenti mencari map dan menyusun jawaban final —
        # konsultasi selesai NORMAL (bukan FAILED karena menyentuh max_steps).
        # Provider asli tetap dipakai untuk ProjectBrain (konteks Bible).
        orchestrator = AgentOrchestrator(
            provider=ConsultantBoundProvider(provider, retrieval_guard),
            executor=executor,
            system_prompt=build_consultant_system_prompt(effective_mode),
            brain=brain,
            brain_learning=False,
            event_sink=_sink,
        )

        task_text = session.build_task(str(message).strip())
        user_parts = _build_image_parts(images)
        result = orchestrator.run_continuous_loop(
            task_text,
            max_steps=max(1, int(max_steps or self.max_steps)),
            user_parts=user_parts,
        )

        reply = (result.result or "").strip()
        if result.status != AgentStatus.DONE and not reply:
            reply = result.error or "Consultant tidak dapat menyelesaikan konsultasi."

        proposal = extract_task_proposal(reply)

        # Simpan giliran ke konteks sesi (untuk konsultasi berikutnya). Write
        # through terjadi via callback session (ctx.store). Derive project id
        # efektif dari session (bila project_id kosong -> fallback active).
        session.add("user", str(message).strip())
        session.add("consultant", reply)
        # Write-through: persist updated session (with new turns) to disk.
        ctx = self._context_for_project(session.project_id or self._resolve_project_id(project_id))
        ctx.store.save_session(session.to_dict())

        return ConsultantResult(
            session_id=session.session_id,
            reply=reply,
            status="done" if result.status == AgentStatus.DONE else "failed",
            error=None if result.status == AgentStatus.DONE else result.error,
            iterations=int(getattr(result, "iterations", 0) or 0),
            tool_events=tool_events,
            task_proposal=proposal,
            mode=effective_mode,
        )
