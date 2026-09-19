"""Model data untuk AETHER Consultant (provider-agnostic, JSON-friendly).

Model di sini HANYA representasi data; tidak menyimpan chain-of-thought dan
tidak menduplikasi model Agent/Runtime/Task yang sudah ada.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: Mode Consultant:
#:   quick       -> percakapan cepat berbasis Project Bible + conversation saja
#:                  (TANPA tool investigasi source/project).
#:   investigate -> Project Bible sebagai konteks awal, lalu boleh memakai tool
#:                  project existing bila perlu verifikasi/investigasi.
MODE_QUICK = "quick"
MODE_INVESTIGATE = "investigate"

#: Mode default Consultant (percakapan cepat).
DEFAULT_CONSULTANT_MODE = MODE_QUICK

#: Mode Consultant yang valid.
VALID_CONSULTANT_MODES = (MODE_QUICK, MODE_INVESTIGATE)


def normalize_consultant_mode(mode: Optional[str]) -> str:
    """Normalisasi mode Consultant.

    Nilai kosong / tidak dikenal dipetakan ke mode default (``quick``), sehingga
    pemanggil lama (tanpa mode) tetap memakai mode default yang aman.
    """
    if not mode:
        return DEFAULT_CONSULTANT_MODE
    value = str(mode).strip().lower()
    if value in VALID_CONSULTANT_MODES:
        return value
    return DEFAULT_CONSULTANT_MODE


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
        mode: mode Consultant yang dipakai ("quick" | "investigate").
    """

    session_id: str
    reply: str = ""
    status: str = "done"
    error: Optional[str] = None
    iterations: int = 0
    tool_events: List[Dict[str, Any]] = field(default_factory=list)
    task_proposal: Optional[str] = None
    mode: str = DEFAULT_CONSULTANT_MODE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "reply": self.reply,
            "status": self.status,
            "error": self.error,
            "iterations": self.iterations,
            "tool_events": self.tool_events,
            "task_proposal": self.task_proposal,
            "mode": self.mode,
        }
