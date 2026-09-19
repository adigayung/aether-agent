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


def _build_image_parts(images: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """Proses gambar user -> content parts provider-agnostic (ADDITIVE).

    Memakai modul vision existing (ImagePreprocessor) TANPA menulis ulang
    logika image. Setiap item input: {"data": "<base64>", "mime_type": ...,
    "filename": opsional}. Base64 di-decode, dipreprocess (resize/kompresi
    bounded), lalu dibungkus menjadi content part image AETHER
    ({"type": "image", "mime_type":..., "encoding":"base64", "data":...}).

    Args:
        images: daftar gambar (base64). None/kosong -> None (text-only).

    Returns:
        List content part image, atau None bila tidak ada image.

    Raises:
        UnsupportedImageFormatError / InvalidImageError: gambar tidak valid.
        ValueError: payload gambar tidak berbentuk dict / data tidak valid.
    """
    if not images:
        return None

    import base64
    import binascii

    from agent_ai.vision.preprocessing import ImagePreprocessor

    preprocessor = ImagePreprocessor()
    parts: List[Dict[str, Any]] = []
    for index, item in enumerate(images):
        if not isinstance(item, dict):
            raise ValueError(f"Gambar[{index}] harus berupa object.")
        raw = item.get("data")
        if not raw:
            raise ValueError(f"Gambar[{index}] tidak punya data.")
        mime = str(item.get("mime_type") or "").strip()
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"Gambar[{index}] bukan base64 yang valid: {exc}") from exc
        processed = preprocessor.process(data, mime_type=mime)
        parts.append(dict(processed.payload))
    return parts or None


class ConsultantSession:
    """Konteks satu sesi konsultasi (percakapan lintas giliran)."""

    def __init__(self, session_id: Optional[str] = None) -> None:
        self.session_id = session_id or uuid.uuid4().hex
        self.turns: List[ConsultantTurn] = []

    def add(self, role: str, text: str) -> None:
        """Tambahkan satu giliran ke konteks sesi (bounded)."""
        self.turns.append(ConsultantTurn(role=role, text=text or ""))
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
    def _get_session(self, session_id: Optional[str]) -> ConsultantSession:
        """Ambil/buat sesi konsultasi (thread-safe)."""
        with self._lock:
            if session_id and session_id in self._sessions:
                return self._sessions[session_id]
            session = ConsultantSession(session_id=session_id)
            self._sessions[session.session_id] = session
            return session

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Kembalikan konteks sesi (bila ada)."""
        with self._lock:
            session = self._sessions.get(session_id)
            return session.to_dict() if session is not None else None

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

        session = self._get_session(session_id)

        registry = build_consultant_registry(root, mode=effective_mode)
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

        orchestrator = AgentOrchestrator(
            provider=provider,
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
