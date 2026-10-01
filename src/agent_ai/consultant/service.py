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
    ) -> None:
        import time

        self.session_id = session_id or uuid.uuid4().hex
        self.project_id: Optional[str] = project_id
        self.title: str = title or "New Chat"
        self.created_at: float = time.time()
        self.updated_at: float = self.created_at
        self.turns: List[ConsultantTurn] = []

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
        """Tambahkan satu giliran ke konteks sesi (bounded)."""
        self.turns.append(ConsultantTurn(role=role, text=text or ""))
        self.touch()
        # Auto-title: jika masih default & ada pesan user pertama, turunkan
        # dari teks user pertama (hanya sekali).
        if self.title == "New Chat" and role == "user" and text and text.strip():
            self.title = _auto_title(text)
        if len(self.turns) > _MAX_CONTEXT_TURNS * 2:
            # Simpan hanya N pasangan turn terakhir (hindari konteks tanpa batas).
            self.turns = self.turns[-_MAX_CONTEXT_TURNS * 2 :]

    def build_task(self, message: str) -> str:
        """Bangun teks task untuk loop, menyertakan konteks sesi sebelumnya."""
        if not self.turns:
            return message
        lines = ["# Percakapan konsultasi sebelumnya", ""]
        for turn in self.turns:
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


class ConsultantService:
    """Menjalankan satu giliran konsultasi memakai komponen AETHER existing.

    Args:
        max_steps: batas langkah reasoning/tool per giliran (safety).
    """

    def __init__(self, max_steps: int = _DEFAULT_MAX_STEPS) -> None:
        self.max_steps = max_steps
        self._sessions: Dict[str, ConsultantSession] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #
    def _get_session(
        self, session_id: Optional[str], project_id: Optional[str] = None
    ) -> ConsultantSession:
        """Ambil/buat sesi konsultasi (thread-safe).

        Tanpa session_id tetap membuat sesi ephemeral agar perilaku API lama
        tidak berubah. Pembuatan sesi bernama dapat dilakukan lewat create_session.
        """
        with self._lock:
            if session_id and session_id in self._sessions:
                return self._sessions[session_id]
            session = ConsultantSession(session_id=session_id, project_id=project_id)
            self._sessions[session.session_id] = session
            return session

    def create_session(
        self, project_id: Optional[str] = None, title: Optional[str] = None
    ) -> Dict[str, Any]:
        """Buat sesi Consultant in-memory baru."""
        with self._lock:
            session = ConsultantSession(project_id=project_id, title=title)
            self._sessions[session.session_id] = session
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

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Kembalikan konteks sesi (bila ada)."""
        with self._lock:
            session = self._sessions.get(session_id)
            return session.to_dict() if session is not None else None

    def rename_session(self, session_id: str, title: str) -> Optional[Dict[str, Any]]:
        """Ubah judul sesi; None bila sesi tidak ditemukan."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session.title = str(title or "New Chat").strip() or "New Chat"
            session.touch()
            return session.to_dict()

    def delete_session(self, session_id: str) -> bool:
        """Hapus sesi Consultant (alias terarah untuk reset_session)."""
        return self.reset_session(session_id)

    def reset_session(self, session_id: str) -> bool:
        """Hapus konteks sesi (mulai konsultasi baru). Returns True bila ada."""
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def reset_session(self, session_id: str) -> bool:
        """Hapus konteks sesi (mulai konsultasi baru). Returns True bila ada."""
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

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
