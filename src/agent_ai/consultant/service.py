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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
from agent_ai.consultant.store import ConsultantSessionStore

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
    memungkinkan rename. Session tetap in-memory (MVP); lihat docstrings
    ConsultantService untuk kebijakan persistensi.
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


class ConsultantService:
    """Menjalankan satu giliran konsultasi memakai komponen AETHER existing.

    Args:
        max_steps: batas langkah reasoning/tool per giliran (safety).
    """

    def __init__(self, max_steps: int = _DEFAULT_MAX_STEPS, store_path: Optional[str] = None) -> None:
        self.max_steps = max_steps
        # Persistent store for Consultant sessions (write-through JSON).
        self._store = ConsultantSessionStore(store_path)
        # In-memory cache keyed by (project_id, session_id) for fast access.
        # Loaded from store at init; kept in sync via write-through.
        self._sessions: Dict[Tuple[str, str], ConsultantSession] = {}
        self._lock = threading.Lock()
        self._load_sessions_from_store()

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #
    def _make_persist_callback(self):
        """Create a write-through callback for ConsultantSession.add()."""
        def _persist(session: ConsultantSession) -> None:
            self._store.save_session(session.to_dict())
        return _persist

    def _get_session(
        self, session_id: Optional[str], project_id: Optional[str] = None
    ) -> ConsultantSession:
        """Ambil/buat sesi konsultasi (thread-safe).

        Sesi di-key oleh ``(project_id, session_id)`` sehingga sesi Consultant
        TERISOLASI per project: id sesi yang sama pada project berbeda tidak
        pernah berbagi konteks. Tanpa session_id tetap membuat sesi ephemeral
        agar perilaku API lama tidak berubah. Pembuatan sesi bernama dapat
        dilakukan lewat create_session.
        """
        project_key = project_id or ""
        with self._lock:
            if session_id:
                existing = self._sessions.get((project_key, session_id))
                if existing is not None:
                    return existing
                # Fallback: restore from persistent store (e.g. after restart or
                # when the session was created under a different in-memory map).
                stored = self._store.get_session(
                    session_id, project_id=project_id if project_id else None
                )
                if stored is not None:
                    session = ConsultantSession.from_dict(stored)
                    session._on_change = self._make_persist_callback()
                    self._sessions[(project_key, session_id)] = session
                    return session
                # Adopsi sesi anonim (project belum ditetapkan) dengan session_id
                # yang sama, lalu kaitkan ke project sekarang. Menjaga kontinuitas
                # sesi yang dibuat lewat create_session() tanpa project.
                anonymous = self._sessions.pop(("", session_id), None)
                if anonymous is not None and project_id:
                    anonymous.project_id = project_id
                    self._sessions[(project_key, session_id)] = anonymous
                    return anonymous
            session = ConsultantSession(
                session_id=session_id,
                project_id=project_id,
                on_change=self._make_persist_callback(),
            )
            self._sessions[(project_key, session.session_id)] = session
            self._store.save_session(session.to_dict())
            return session

    def _find_session(
        self, session_id: str, project_id: Optional[str] = None
    ) -> Optional[ConsultantSession]:
        """Cari objek sesi (project_id None = cari lintas project)."""
        if project_id is not None:
            session = self._sessions.get((project_id, session_id))
            if session is not None:
                return session
            stored = self._store.get_session(session_id, project_id=project_id)
            if stored is not None:
                session = ConsultantSession.from_dict(stored)
                session._on_change = self._make_persist_callback()
                self._sessions[(project_id, session_id)] = session
                return session
            return None
        for session in self._sessions.values():
            if session.session_id == session_id:
                return session
        stored = self._store.get_session(session_id)
        if stored is not None:
            session = ConsultantSession.from_dict(stored)
            session._on_change = self._make_persist_callback()
            self._sessions[(stored.get("project_id") or "", session.session_id)] = session
            return session
        return None

    def _load_sessions_from_store(self) -> None:
        """Muat sesi dari persistent store ke cache in-memory (saat init)."""
        try:
            metas = self._store.list_sessions()
        except Exception:  # noqa: BLE001 - store tidak boleh menggagalkan startup
            return
        persist_cb = self._make_persist_callback()
        for meta in metas:
            sid = meta.get("session_id")
            pid = meta.get("project_id") or ""
            if not sid:
                continue
            if (pid, sid) in self._sessions:
                continue
            data = self._store.get_session(sid, project_id=pid if pid else None)
            if data:
                session = ConsultantSession.from_dict(data)
                session._on_change = persist_cb
                self._sessions[(pid, sid)] = session

    def _load_sessions_from_store(self) -> None:
        """Muat sesi dari persistent store ke cache in-memory (saat init)."""
        try:
            metas = self._store.list_sessions()
        except Exception:  # noqa: BLE001 - store tidak boleh menggagalkan startup
            return
        for meta in metas:
            sid = meta.get("session_id")
            pid = meta.get("project_id") or ""
            if not sid:
                continue
            if (pid, sid) in self._sessions:
                continue
            data = self._store.get_session(sid, project_id=pid if pid else None)
            if data:
                self._sessions[(pid, sid)] = ConsultantSession.from_dict(data)

    def create_session(
        self, project_id: Optional[str] = None, title: Optional[str] = None
    ) -> Dict[str, Any]:
        """Buat sesi Consultant baru (ter-scope ke project) dan simpan ke disk."""
        with self._lock:
            session = ConsultantSession(
                project_id=project_id,
                title=title,
                on_change=self._make_persist_callback(),
            )
            self._sessions[(project_id or "", session.session_id)] = session
            self._store.save_session(session.to_dict())
            return session.to_dict()

    def list_sessions(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Daftar metadata sesi, terbaru lebih dulu; dapat difilter project."""
        with self._lock:
            sessions = [
                s for s in self._sessions.values()
                if project_id is None or s.project_id == project_id
            ]
            sessions.sort(key=lambda s: s.updated_at, reverse=True)
            return [s.to_dict() for s in sessions]

    def get_session(
        self, session_id: str, project_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Kembalikan konteks sesi (bila ada).

        Bila project_id diberikan, sesi dicari pada project tersebut; bila tidak,
        sesi dicari lintas project (kompatibel dengan pemanggil lama).
        """
        with self._lock:
            session = self._find_session(session_id, project_id)
            return session.to_dict() if session is not None else None

    def rename_session(
        self, session_id: str, title: str, project_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Ubah judul sesi; None bila sesi tidak ditemukan."""
        with self._lock:
            session = self._find_session(session_id, project_id)
            if session is None:
                return None
            session.title = str(title or "New Chat").strip() or "New Chat"
            session.touch()
            self._store.save_session(session.to_dict())
            return session.to_dict()

    def delete_session(
        self, session_id: str, project_id: Optional[str] = None
    ) -> bool:
        """Hapus sesi Consultant (alias terarah untuk reset_session)."""
        return self.reset_session(session_id, project_id=project_id)

    def reset_session(
        self, session_id: str, project_id: Optional[str] = None
    ) -> bool:
        """Hapus konteks sesi (mulai konsultasi baru). Returns True bila ada."""
        with self._lock:
            deleted = False
            if project_id is not None:
                deleted = self._sessions.pop((project_id, session_id), None) is not None
            else:
                for key, session in list(self._sessions.items()):
                    if session.session_id == session_id:
                        del self._sessions[key]
                        deleted = True
                        break
            if deleted:
                self._store.delete_session(session_id, project_id=project_id)
            return deleted

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
    ) -> ConsultantResult:
        """Jalankan satu giliran konsultasi dan kembalikan hasilnya.

        Args:
            message: pertanyaan/permintaan user (wajib).
            provider: instance BaseProvider (dibangun pemanggil dari konfigurasi
                LLM tersimpan; Consultant tidak memilih provider sendiri).
            root: root project target. Bila diisi, tool dibatasi ke root itu dan
                Project Bible dibaca/ditulis di `<root>/.aether/bible/`.
            session_id: id sesi konsultasi (untuk konteks lintas giliran).
            project_id: id project terkait. Dipakai untuk MENGISOLASI sesi per
                project: sesi dengan id sama pada project berbeda tidak berbagi
                konteks. Bila None, sesi berada pada scope global (perilaku lama).
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

        # Simpan giliran ke konteks sesi (untuk konsultasi berikutnya).
        session.add("user", str(message).strip())
        session.add("consultant", reply)
        # Write-through: persist updated session (with new turns) to disk.
        self._store.save_session(session.to_dict())

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
