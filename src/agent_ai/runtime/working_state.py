"""Working State: pemahaman kerja yang bertahan sepanjang satu task.

Working State adalah STATE INTERNAL yang menjaga pemahaman Agent tentang
pekerjaan yang sedang dikerjakan. Bedanya dengan activity/history:

    - Activity/history = TELEMETRY untuk UI. Append-only, informatif, boleh
      berisi detail mentah apa yang terjadi.
    - Working State  = KONDISI KERJA yang aktif. Ringkas, terstruktur,
      menjadi sumber utama pemahaman kerja Agent pada round berikutnya.

Working State TIDAK membuat keputusan apa pun. LLM tetap satu-satunya
pengambil keputusan; AETHER hanya menyediakan tempat untuk menyimpan dan
mengembalikan state tersebut ke LLM.

Prinsip:
    - Provider-agnostic, tool-agnostic, tidak ada dependency baru.
    - Plain data (dataclass) + operasi mutasi yang eksplisit dan idempotent.
    - Tidak menyimpan hidden chain-of-thought: hanya fakta/niatan kerja yang
      dinyatakan sendiri (goal, requirement, keputusan, hipotesis, dst).
    - Tidak memengaruhi loop, completion, tool policy, atau runtime lain.
      State dapat di-inject ke prompt LLM bila pemanggil memintanya
      (advisory context, mirip plan).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


class PlanEntryStatus(str, Enum):
    """Status sebuah langkah dalam living plan Working State."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    BLOCKED = "blocked"


#: Status terminal: tidak lagi berubah kecuali replan eksplisit.
TERMINAL_PLAN_ENTRY_STATUSES = {
    PlanEntryStatus.COMPLETED,
    PlanEntryStatus.SKIPPED,
    PlanEntryStatus.FAILED,
}


@dataclass
class PlanEntry:
    """Satu langkah hidup dalam living plan Working State.

    Representasi state (BUKAN penggerak eksekusi): AETHER tidak memaksa
    LLM mengikuti plan secara kaku — LLM tetap bebas membuat, mengubah,
    menambah, menghapus, mengurutkan ulang, atau melewati step.

    Attributes:
        id: identifier unik langkah.
        title: judul singkat langkah.
        description: penjelasan langkah (opsional).
        status: status langkah (default PENDING).
        order: posisi urutan langkah dalam plan (0-based).
        metadata: info tambahan bebas.
        notes: catatan perubahan/replanning (opsional, struktural).
        updated_at: waktu terakhir langkah diubah.
    """

    title: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    description: str = ""
    status: PlanEntryStatus = PlanEntryStatus.PENDING
    order: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "order": self.order,
            "metadata": dict(self.metadata),
            "notes": self.notes,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlanEntry":
        raw_status = str(data.get("status") or PlanEntryStatus.PENDING.value)
        try:
            status = PlanEntryStatus(raw_status)
        except ValueError:
            status = PlanEntryStatus.PENDING
        return cls(
            title=str(data.get("title") or ""),
            id=str(data.get("id") or uuid.uuid4().hex),
            description=str(data.get("description") or ""),
            status=status,
            order=int(data.get("order") or 0),
            metadata=dict(data.get("metadata") or {}),
            notes=str(data.get("notes") or ""),
            updated_at=float(data.get("updated_at") or 0.0) or time.time(),
        )

    def to_text(self) -> str:
        """Render satu langkah untuk konteks LLM (ringkas, deterministik)."""
        marker = {
            PlanEntryStatus.PENDING: "[ ]",
            PlanEntryStatus.RUNNING: "[>]",
            PlanEntryStatus.COMPLETED: "[x]",
            PlanEntryStatus.SKIPPED: "[-]",
            PlanEntryStatus.FAILED: "[!]",
            PlanEntryStatus.BLOCKED: "[?]",
        }.get(self.status, "[ ]")
        line = f"{marker} {self.title}"
        if self.description:
            line += f" — {self.description}"
        if self.notes:
            line += f" (catatan: {self.notes})"
        return line


@dataclass
class WorkingState:
    """Snapshot pemahaman kerja untuk satu task.

    Attributes:
        goal: tujuan utama task (ringkas, satu kalimat).
        requirements: requirement/batasan yang harus dipenuhi.
        decisions: keputusan yang sudah diambil Agent beserta alasannya.
        files_inspected: file yang sudah dibaca/dipahami Agent.
        files_changed: file yang sudah ditulis/diubah/dihapus Agent.
        hypotheses: hipotesis kerja Agent (apakah benar, sudah diuji atau belum).
        open_questions: pertanyaan yang masih terbuka / belum terjawab.
        completed_steps: langkah yang sudah selesai dikerjakan.
        current_focus: apa yang sedang dikerjakan sekarang.
        plan: living plan — daftar PlanEntry (id/status/urutan/progress/
            catatan perubahan). Representasi state, bukan penggerak eksekusi.
    """

    goal: str = ""
    requirements: List[str] = field(default_factory=list)
    decisions: List[str] = field(default_factory=list)
    files_inspected: List[str] = field(default_factory=list)
    files_changed: List[str] = field(default_factory=list)
    hypotheses: List[str] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)
    completed_steps: List[str] = field(default_factory=list)
    current_focus: str = ""
    plan: List[PlanEntry] = field(default_factory=list)
    #: Waktu state terakhir diperbarui (epoch detik). Metadata, bukan state.
    updated_at: float = field(default_factory=time.time)
    #: Jumlah kali state ini diperbarui. Metadata, bukan state.
    revision: int = 0

    def __post_init__(self) -> None:
        """Normalisasi `plan` menjadi List[PlanEntry] (terima string legacy)."""
        if self.plan and not all(isinstance(p, PlanEntry) for p in self.plan):
            self.plan = _as_plan_list(self.plan)

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "requirements": list(self.requirements),
            "decisions": list(self.decisions),
            "files_inspected": list(self.files_inspected),
            "files_changed": list(self.files_changed),
            "hypotheses": list(self.hypotheses),
            "open_questions": list(self.open_questions),
            "completed_steps": list(self.completed_steps),
            "current_focus": self.current_focus,
            "plan": [p.to_dict() for p in self.plan],
            "updated_at": self.updated_at,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkingState":
        """Bangun WorkingState dari dict (toleran terhadap field hilang)."""
        raw_plan = data.get("plan") or []
        plan = []
        for item in raw_plan:
            if isinstance(item, Mapping):
                plan.append(PlanEntry.from_dict(item))
            elif isinstance(item, str):
                # backward compat: legacy plan was List[str]
                plan.append(PlanEntry(title=item))
        return cls(
            goal=str(data.get("goal") or ""),
            requirements=_as_str_list(data.get("requirements")),
            decisions=_as_str_list(data.get("decisions")),
            files_inspected=_as_str_list(data.get("files_inspected")),
            files_changed=_as_str_list(data.get("files_changed")),
            hypotheses=_as_str_list(data.get("hypotheses")),
            open_questions=_as_str_list(data.get("open_questions")),
            completed_steps=_as_str_list(data.get("completed_steps")),
            current_focus=str(data.get("current_focus") or ""),
            plan=plan,
            updated_at=float(data.get("updated_at") or 0.0) or time.time(),
            revision=int(data.get("revision") or 0),
        )

    def copy(self) -> "WorkingState":
        """Salinan dalam (deep untuk list) agar aman dibaca dari luar."""
        return WorkingState.from_dict(self.to_dict())

    @property
    def is_empty(self) -> bool:
        """True bila belum ada isi Apa pun (state baru / belum di-update)."""
        return not (
            self.goal
            or self.requirements
            or self.decisions
            or self.files_inspected
            or self.files_changed
            or self.hypotheses
            or self.open_questions
            or self.completed_steps
            or self.current_focus
            or self.plan
        )

    # ------------------------------------------------------------------ #
    # In-place rendering (untuk konteks LLM)
    # ------------------------------------------------------------------ #
    def to_text(self, *, max_items_per_list: int = 20) -> str:
        """Render Working State menjadi blok teks untuk konteks LLM.

        Adaptif & deterministik: list dipotong per batas agar state tidak
        mendominasi context window. Blok yang kosong tidak dirender sama
        sekali, sehingga state kosong tidak menambah token sia-sia.
        """
        sections: List[str] = []
        if self.goal:
            sections.append(f"Goal: {self.goal}")
        if self.current_focus:
            sections.append(f"Current focus: {self.current_focus}")
        for label, items in (
            ("Requirements", self.requirements),
            ("Decisions", self.decisions),
            ("Files inspected", self.files_inspected),
            ("Files changed", self.files_changed),
            ("Hypotheses", self.hypotheses),
            ("Open questions", self.open_questions),
            ("Completed steps", self.completed_steps),
        ):
            rendered = _render_items(items, max_items_per_list)
            if rendered:
                sections.append(f"{label}:\n{rendered}")
        # Plan dirender sebagai living plan (PlanEntry), bukan list string.
        if self.plan:
            rendered_plan = _render_plan(self.plan, max_items_per_list)
            if rendered_plan:
                sections.append(f"Living Plan:\n{rendered_plan}")
        return "\n\n".join(sections)

    def plan_progress(self) -> Dict[str, Any]:
        """Ringkasan progress living plan (read-only, deterministik).

        Mengembalikan dict:
            total, completed, skipped, failed, pending,
            running, blocked, percent, current_step.
        """
        total = len(self.plan)
        counts = {status: 0 for status in PlanEntryStatus}
        for entry in self.plan:
            if entry.status in counts:
                counts[entry.status] += 1
        terminal = TERMINAL_PLAN_ENTRY_STATUSES
        done_count = sum(counts.get(s, 0) for s in terminal)
        percent = (done_count / total * 100) if total else 0.0
        current = None
        sorted_plan = sorted(self.plan, key=lambda p: p.order)
        for entry in sorted_plan:
            if entry.status not in terminal:
                current = {
                    "id": entry.id,
                    "title": entry.title,
                    "description": entry.description,
                    "status": entry.status.value,
                    "order": entry.order,
                }
                break
        return {
            "total": total,
            "completed": counts.get(PlanEntryStatus.COMPLETED, 0),
            "skipped": counts.get(PlanEntryStatus.SKIPPED, 0),
            "failed": counts.get(PlanEntryStatus.FAILED, 0),
            "pending": counts.get(PlanEntryStatus.PENDING, 0),
            "running": counts.get(PlanEntryStatus.RUNNING, 0),
            "blocked": counts.get(PlanEntryStatus.BLOCKED, 0),
            "percent": round(percent, 1),
            "current_step": current,
        }


def _as_str_list(value: Any) -> List[str]:
    """Normalisasi input bebas menjadi list of non-empty string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Mapping):
        return [f"{k}: {v}" for k, v in value.items()]
    if isinstance(value, Sequence) or isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _render_plan(plan: Sequence[PlanEntry], limit: int) -> str:
    """Render living plan menjadi baris markdown, dipotong per `limit`."""
    if not plan:
        return ""
    # Sort by order first
    sorted_plan = sorted(plan, key=lambda p: p.order)
    shown = list(sorted_plan[:limit])
    lines = [p.to_text() for p in shown]
    if len(plan) > len(shown):
        remaining = len(plan) - len(shown)
        lines.append(f"- ... ({remaining} item lain tidak ditampilkan)")
    return "\n".join(lines)


def _render_items(items: Sequence[str], limit: int) -> str:
    """Render list state menjadi baris markdown, dipotong per `limit`."""
    if not items:
        return ""
    shown = list(items[:limit])
    lines = [f"- {item}" for item in shown]
    if len(items) > len(shown):
        remaining = len(items) - len(shown)
        lines.append(f"- ... ({remaining} item lain tidak ditampilkan)")
    return "\n".join(lines)


class WorkingStateManager:
    """Pemilik mutable dari satu WorkingState untuk satu task berjalan.

    Manager ini adalah adapter tipis yang menyediakan operasi mutasi
    eksplisit. Tidak ada scheduler, tidak ada inferensi otomatis, tidak ada
    keputusan. Semua perubahan SELALU berasal dari pemanggil (backend,
    Agent lewat tool, atau pembaru state internal) — LLM tetap yang
    memutuskan.

    Manager juga menghasilkan snapshot defensif (``snapshot()``) sehingga
    state tidak dapat dimutasi dari luar tanpa sengaja.
    """

    def __init__(self, state: Optional[WorkingState] = None) -> None:
        self._state: WorkingState = state.copy() if state is not None else WorkingState()

    # ------------------------------------------------------------------ #
    # Access
    # ------------------------------------------------------------------ #
    def snapshot(self) -> WorkingState:
        """Salinan defensif state saat ini."""
        return self._state.copy()

    def to_dict(self) -> Dict[str, Any]:
        return self._state.to_dict()

    @property
    def revision(self) -> int:
        return self._state.revision

    @property
    def is_empty(self) -> bool:
        return self._state.is_empty

    def to_text(self, **kwargs: Any) -> str:
        return self._state.to_text(**kwargs)

    # ------------------------------------------------------------------ #
    # Mutation
    # ------------------------------------------------------------------ #
    def _touch(self) -> None:
        self._state.updated_at = time.time()
        self._state.revision += 1

    def reset(self, goal: Optional[str] = None) -> WorkingState:
        """Buat working state baru (untuk task baru). Goal opsional di-set."""
        self._state = WorkingState(goal=goal or "")
        return self.snapshot()

    def set(
        self,
        *,
        goal: Optional[str] = None,
        current_focus: Optional[str] = None,
        requirements: Optional[Sequence[str]] = None,
        decisions: Optional[Sequence[str]] = None,
        hypotheses: Optional[Sequence[str]] = None,
        open_questions: Optional[Sequence[str]] = None,
        completed_steps: Optional[Sequence[str]] = None,
        plan: Optional[Sequence[Any]] = None,
    ) -> WorkingState:
        """Set field skalar + GANTI seluruh list sekaligus (tidak merge)."""
        s = self._state
        if goal is not None:
            s.goal = goal.strip()
        if current_focus is not None:
            s.current_focus = current_focus.strip()
        if requirements is not None:
            s.requirements = _as_str_list(requirements)
        if decisions is not None:
            s.decisions = _as_str_list(decisions)
        if hypotheses is not None:
            s.hypotheses = _as_str_list(hypotheses)
        if open_questions is not None:
            s.open_questions = _as_str_list(open_questions)
        if completed_steps is not None:
            s.completed_steps = _as_str_list(completed_steps)
        if plan is not None:
            s.plan = _as_plan_list(plan)
        self._touch()
        return self.snapshot()

    def update(
        self,
        *,
        goal: Optional[str] = None,
        current_focus: Optional[str] = None,
        requirements: Optional[Sequence[str]] = None,
        decisions: Optional[Sequence[str]] = None,
        hypotheses: Optional[Sequence[str]] = None,
        open_questions: Optional[Sequence[str]] = None,
        completed_steps: Optional[Sequence[str]] = None,
        plan: Optional[Sequence[str]] = None,
        files_inspected: Optional[Sequence[str]] = None,
        files_changed: Optional[Sequence[str]] = None,
    ) -> WorkingState:
        """Partial merge: lists yang diberikan di-APPEND (dedup), bukan diganti.

        Berguna untuk pembaruan progres (mis. menambah file yang baru dibaca)
        tanpa kehilangan isi list yang sudah tercatat.
        """
        s = self._state
        if goal is not None:
            s.goal = goal.strip()
        if current_focus is not None:
            s.current_focus = current_focus.strip()
        _extend_unique(s.requirements, requirements)
        _extend_unique(s.decisions, decisions)
        _extend_unique(s.hypotheses, hypotheses)
        _extend_unique(s.open_questions, open_questions)
        _extend_unique(s.completed_steps, completed_steps)
        _extend_unique(s.plan, plan)
        _extend_unique(s.files_inspected, files_inspected)
        _extend_unique(s.files_changed, files_changed)
        self._touch()
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Recording (kontrak jelas untuk sumber yang berbeda)
    # ------------------------------------------------------------------ #
    def record_requirement(self, *items: str) -> WorkingState:
        return self.update(requirements=items)

    def record_decision(self, *items: str) -> WorkingState:
        return self.update(decisions=items)

    def record_hypothesis(self, *items: str) -> WorkingState:
        return self.update(hypotheses=items)

    def record_open_question(self, *items: str) -> WorkingState:
        return self.update(open_questions=items)

    def record_completed_step(self, *items: str) -> WorkingState:
        return self.update(completed_steps=items)

    def record_files_inspected(self, *items: str) -> WorkingState:
        return self.update(files_inspected=items)

    def record_files_changed(self, *items: str) -> WorkingState:
        return self.update(files_changed=items)

    def record_plan(self, *items: str) -> WorkingState:
        return self.update(plan=items)

    # Living Plan management methods
    def create_plan_entry(self, *, title: str, description: str = "",
                          order: Optional[int] = None,
                          metadata: Optional[Dict[str, Any]] = None) -> WorkingState:
        """Create a new plan entry and append to plan (or at `order`)."""
        s = self._state
        entry = PlanEntry(
            title=title.strip(),
            description=description.strip(),
            metadata=dict(metadata or {}),
        )
        if order is not None and 0 <= order <= len(s.plan):
            entry.order = order
            for e in s.plan:
                if e.order >= order:
                    e.order += 1
            s.plan.insert(order, entry)
        else:
            entry.order = len(s.plan)
            s.plan.append(entry)
        self._touch()
        return self.snapshot()

    def update_plan_entry(self, entry_id: str, *, 
                         title: Optional[str] = None,
                         description: Optional[str] = None,
                         status: Optional[PlanEntryStatus] = None,
                         metadata: Optional[Dict[str, Any]] = None,
                         notes: Optional[str] = None) -> WorkingState:
        """Update an existing plan entry by ID."""
        s = self._state
        for entry in s.plan:
            if entry.id == entry_id:
                if title is not None:
                    entry.title = title.strip()
                if description is not None:
                    entry.description = description.strip()
                if status is not None:
                    entry.status = status
                if metadata is not None:
                    entry.metadata.update(metadata)
                if notes is not None:
                    entry.notes = notes.strip()
                entry.updated_at = time.time()
                self._touch()
                return self.snapshot()
        raise KeyError(f"Plan entry with id '{entry_id}' not found")

    def add_plan_entry(self, *, title: str, description: str = "",
                       after_id: Optional[str] = None,
                       before_id: Optional[str] = None,
                       metadata: Optional[Dict[str, Any]] = None) -> WorkingState:
        """Add a new plan entry after/before existing entry, or at end."""
        s = self._state
        entry = PlanEntry(
            title=title.strip(),
            description=description.strip(),
            metadata=dict(metadata or {}),
        )
        
        # Find insertion point
        insert_index = len(s.plan)  # default to end
        if after_id:
            for i, e in enumerate(s.plan):
                if e.id == after_id:
                    insert_index = i + 1
                    break
        elif before_id:
            for i, e in enumerate(s.plan):
                if e.id == before_id:
                    insert_index = i
                    break
        
        # Update orders
        entry.order = insert_index
        for i in range(insert_index, len(s.plan)):
            s.plan[i].order += 1
            
        s.plan.insert(insert_index, entry)
        self._touch()
        return self.snapshot()

    def update_plan_entry(self, entry_id: str, *, 
                         title: Optional[str] = None,
                         description: Optional[str] = None,
                         status: Optional[PlanEntryStatus] = None,
                         metadata: Optional[Dict[str, Any]] = None,
                         notes: Optional[str] = None) -> WorkingState:
        """Update an existing plan entry by ID."""
        s = self._state
        for entry in s.plan:
            if entry.id == entry_id:
                if title is not None:
                    entry.title = title.strip()
                if description is not None:
                    entry.description = description.strip()
                if status is not None:
                    entry.status = status
                if metadata is not None:
                    entry.metadata.update(metadata)
                if notes is not None:
                    entry.notes = notes.strip()
                entry.updated_at = time.time()
                self._touch()
                return self.snapshot()
        raise KeyError(f"Plan entry with id '{entry_id}' not found")

    def add_plan_entry(self, *, title: str, description: str = "",
                      after_id: Optional[str] = None,
                      before_id: Optional[str] = None,
                      metadata: Optional[Dict[str, Any]] = None) -> WorkingState:
        """Add a new plan entry after/before existing entry, or at end."""
        s = self._state
        entry = PlanEntry(
            title=title.strip(),
            description=description.strip(),
            metadata=dict(metadata or {}),
        )
        
        # Find insertion point
        insert_index = len(s.plan)  # default to end
        if after_id:
            for i, e in enumerate(s.plan):
                if e.id == after_id:
                    insert_index = i + 1
                    break
        elif before_id:
            for i, e in enumerate(s.plan):
                if e.id == before_id:
                    insert_index = i
                    break
        
        # Update orders
        entry.order = insert_index
        for i in range(insert_index, len(s.plan)):
            s.plan[i].order += 1
            
        s.plan.insert(insert_index, entry)
        self._touch()
        return self.snapshot()

    def remove_plan_entry(self, entry_id: str) -> WorkingState:
        """Remove plan entry by ID."""
        s = self._state
        for i, entry in enumerate(s.plan):
            if entry.id == entry_id:
                del s.plan[i]
                # Reorder remaining entries
                for j, e in enumerate(s.plan[i:], start=i):
                    e.order = j
                self._touch()
                return self.snapshot()
        raise KeyError(f"Plan entry with id '{entry_id}' not found")

    def reorder_plan_entry(self, entry_id: str, new_order: int) -> WorkingState:
        """Move plan entry to new order position."""
        s = self._state
        # Find entry
        entry_idx = None
        for i, entry in enumerate(s.plan):
            if entry.id == entry_id:
                entry_idx = i
                break
        if entry_idx is None:
            raise KeyError(f"Plan entry with id '{entry_id}' not found")
            
        entry = s.plan[entry_idx]
        old_order = entry.order
        
        # Validate new_order
        if not 0 <= new_order <= len(s.plan):
            raise ValueError(f"new_order must be between 0 and {len(s.plan)}")
            
        # Remove from old position
        del s.plan[entry_idx]
        
        # Insert at new position
        if new_order > old_order:
            new_order -= 1  # Adjust for removal
        s.plan.insert(new_order, entry)
        
        # Reorder all entries
        for i, e in enumerate(s.plan):
            e.order = i
            
        self._touch()
        return self.snapshot()

    def skip_plan_entry(self, entry_id: str, reason: str = "") -> WorkingState:
        """Mark plan entry as skipped."""
        s = self._state
        for entry in s.plan:
            if entry.id == entry_id:
                entry.status = PlanEntryStatus.SKIPPED
                if reason:
                    entry.notes = reason.strip()
                entry.updated_at = time.time()
                self._touch()
                return self.snapshot()
        raise KeyError(f"Plan entry with id '{entry_id}' not found")

    def complete_plan_entry(self, entry_id: str, outcome: str = "") -> WorkingState:
        """Mark plan entry as completed."""
        s = self._state
        for entry in s.plan:
            if entry.id == entry_id:
                entry.status = PlanEntryStatus.COMPLETED
                if outcome:
                    entry.metadata["outcome"] = outcome.strip()
                entry.updated_at = time.time()
                self._touch()
                return self.snapshot()
        raise KeyError(f"Plan entry with id '{entry_id}' not found")

    def set_goal(self, goal: str) -> WorkingState:
        return self.update(goal=goal)

    def set_current_focus(self, focus: str) -> WorkingState:
        return self.update(current_focus=focus)

    def remove(self, *, completed_steps: Optional[Sequence[str]] = None,
               open_questions: Optional[Sequence[str]] = None) -> WorkingState:
        """Hapus entri dari list tertentu (mis. pertanyaan yang terjawab)."""
        s = self._state
        if completed_steps is not None:
            s.completed_steps = [i for i in s.completed_steps
                                 if i not in set(completed_steps)]
        if open_questions is not None:
            s.open_questions = [i for i in s.open_questions
                                if i not in set(open_questions)]
        self._touch()
        return self.snapshot()


def _as_plan_list(value: Any) -> List[PlanEntry]:
    """Normalisasi input bebas menjadi list of PlanEntry.

    Menerima: PlanEntry, Mapping (dengan/ tanpa "title"/"step"), atau string.
    String polos -> PlanEntry(title=...) (backward compat dengan plan lama
    yang berupa List[str]).
    """
    if value is None:
        return []
    if isinstance(value, PlanEntry):
        return [PlanEntry.from_dict(value.to_dict())]
    if isinstance(value, Mapping):
        return [PlanEntry.from_dict(value)]
    if isinstance(value, str):
        stripped = value.strip()
        return [PlanEntry(title=stripped)] if stripped else []
    if isinstance(value, Sequence) or isinstance(value, Iterable):
        entries: List[PlanEntry] = []
        for item in value:
            entries.extend(_as_plan_list(item))
        return entries
    text = str(value).strip()
    return [PlanEntry(title=text)] if text else []


def _extend_unique(target: List[str], items: Optional[Sequence[str]]) -> None:
    """Tambahkan item baru (tanpa duplikat, urutan terjaga) ke list target."""
    if not items:
        return
    existing = set(target)
    for item in _as_str_list(items):
        if item not in existing:
            target.append(item)
            existing.add(item)