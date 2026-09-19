"""Model data untuk AETHER Consultant (provider-agnostic, JSON-friendly).

Model di sini HANYA representasi data; tidak menyimpan chain-of-thought dan
tidak menduplikasi model Agent/Runtime/Task yang sudah ada.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ConsultantTurn:
    """Satu giliran percakapan konsultasi (session context)."""

    role: str  # "user" | "consultant"
    text: str

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "text": self.text}


@dataclass
class ConsultantResult:
    """Hasil satu giliran konsultasi.

    Attributes:
        session_id: id sesi konsultasi (dipakai untuk konteks lintas giliran).
        reply: jawaban final Consultant (markdown teks).
        status: "done" bila selesai, "failed" bila gagal.
        error: pesan error (bila failed).
        iterations: jumlah langkah reasoning/tool yang dipakai.
        tool_events: ringkasan aktivitas tool (tool, target, success).
        task_proposal: Task Proposal siap kirim ke Agent (bila ADA).
    """

    session_id: str
    reply: str = ""
    status: str = "done"
    error: Optional[str] = None
    iterations: int = 0
    tool_events: List[Dict[str, Any]] = field(default_factory=list)
    task_proposal: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "reply": self.reply,
            "status": self.status,
            "error": self.error,
            "iterations": self.iterations,
            "tool_events": self.tool_events,
            "task_proposal": self.task_proposal,
        }
