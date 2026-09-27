"""Activity Phase (UI-facing) — aktivitas NYATA yang sedang dilakukan Agent.

Konsep ini SENGAJA dipisahkan dari `TaskPhase` internal runtime
(preparation/planning/execution/tool_execution/validation/finalization, lihat
`agent_ai.tasks.models.TaskPhase`). `TaskPhase` menggambarkan tahap eksekusi
runtime; `ActivityPhase` menggambarkan AKTIVITAS Agent yang ingin ditampilkan
ke frontend lewat event `phase_changed`.

    internal runtime phase  !=  UI activity phase

Terminal task status (completed/failed/cancelled) BUKAN activity phase.

Klasifikasi bersifat DETERMINISTIK dari aktivitas nyata (tool call aktual),
BUKAN dari timer atau tebakan. Modul ini tidak membuat subsystem baru: ia hanya
memetakan nama tool AETHER yang sudah ada menjadi aktivitas.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Mapping, Optional


class ActivityPhase(str, Enum):
    """Aktivitas Agent yang dikirim ke frontend (bukan `TaskPhase` internal)."""

    PLANNING = "planning"
    INSPECTING = "inspecting"
    EDITING = "editing"
    RUNNING = "running"
    VALIDATING = "validating"


#: Nama tool AKTUAL (registry AETHER) -> activity phase.
#:
#: - Inspecting = memahami/mencari informasi project (read-only knowledge).
#: - Editing    = mengubah workspace (mutasi file).
#: - Running    = menjalankan command/process operasional lewat terminal.
#:
#: `run_command` dipetakan ke `RUNNING` sebagai default; ia bisa dipromosikan
#: ke `VALIDATING` oleh `classify_tool_activity` bila command-nya jelas
#: merupakan verifikasi hasil kerja (lihat `is_validation_command`).
_TOOL_ACTIVITY = {
    # --- Inspecting (retrieval / pemahaman project) ---
    "read_file": ActivityPhase.INSPECTING,
    "search_code": ActivityPhase.INSPECTING,
    "list_files": ActivityPhase.INSPECTING,
    # Project Map (Agent): termasuk refresh_project_map. refresh_project_map
    # menulis artefak map (.aether/map) tetapi konsepnya "project map /
    # inspection" (memahami project), sehingga dikelompokkan sebagai inspecting.
    "atlas_query": ActivityPhase.INSPECTING,
    "rig_query": ActivityPhase.INSPECTING,
    "project_map_status": ActivityPhase.INSPECTING,
    "refresh_project_map": ActivityPhase.INSPECTING,
    # --- Editing (mutasi workspace) ---
    "write_file": ActivityPhase.EDITING,
    "edit_file": ActivityPhase.EDITING,
    "delete_file": ActivityPhase.EDITING,
    "move_file": ActivityPhase.EDITING,
    # --- Running (terminal) ---
    "run_command": ActivityPhase.RUNNING,
}


#: Token command yang menandakan VERIFIKASI hasil pekerjaan. Dipakai HANYA
#: untuk membedakan `run_command` running vs validating pada activity phase.
#:
#: Ini BUKAN subsystem validation baru: runtime validation existing
#: (`validation_started`/`validation_completed`) tetap menjadi sumber resmi
#: bila aktif. Heuristik ini hanya memberi sinyal activity dari aktivitas nyata
#: saat command yang jelas memverifikasi (test/lint/type-check/check script)
#: dijalankan. Deterministik dari string command (bukan tebakan bebas).
_VALIDATION_COMMAND_PATTERNS = (
    # Test runners.
    r"\bpytest\b",
    r"\bpy\.test\b",
    r"\bunittest\b",
    r"\bnose2?\b",
    r"\btox\b",
    r"\bjest\b",
    r"\bvitest\b",
    r"\bmocha\b",
    r"\b(?:npm|yarn|pnpm)\s+(?:run\s+)?test\b",
    r"\b(?:go|cargo|mvn|gradle)\s+test\b",
    # Linter / type-checker.
    r"\bruff\b",
    r"\bflake8\b",
    r"\bpylint\b",
    r"\bmypy\b",
    r"\bpyright\b",
    r"\beslint\b",
    r"\bstylelint\b",
    r"\btsc\b",
    r"\b(?:npm|yarn|pnpm)\s+run\s+lint\b",
    r"\blint\b",
    r"\btypecheck\b",
    r"\btype-check\b",
    # Compile/check (verifikasi hasil pekerjaan).
    r"\bpy_compile\b",
    r"\bcompileall\b",
    r"\bcheck[_\-][a-z0-9_\-]+\.(?:py|sh|bat|ps1|js|mjs|cjs|ts)\b",
)

_VALIDATION_COMMAND_RE = re.compile(
    "|".join(_VALIDATION_COMMAND_PATTERNS), re.IGNORECASE
)


def is_validation_command(command: Any) -> bool:
    """True bila command `run_command` jelas merupakan verifikasi hasil kerja.

    Heuristik deterministik pada string command (test/lint/type-check/check
    script). BUKAN subsystem validation baru: source utama tetap event
    `validation_started`/`validation_completed` milik runtime (bila aktif).
    """
    text = str(command or "").strip()
    if not text:
        return False
    return _VALIDATION_COMMAND_RE.search(text) is not None


def classify_tool_activity(
    tool_name: Optional[str], arguments: Optional[Mapping[str, Any]] = None
) -> Optional[ActivityPhase]:
    """Tentukan activity phase dari sebuah tool call (deterministik).

    Args:
        tool_name: nama tool AKTUAL (mis. "read_file"). Case-insensitive.
        arguments: argumen tool. Dipakai khusus untuk `run_command`
            (membedakan running vs validating dari string `command`).

    Returns:
        `ActivityPhase` bila tool dikenali; `None` bila tool tidak
        diklasifikasikan (mis. tool yang tidak memetakan ke aktivitas UI).
    """
    if not tool_name:
        return None
    phase = _TOOL_ACTIVITY.get(str(tool_name).strip().lower())
    if phase is None:
        return None
    if phase is ActivityPhase.RUNNING:
        command = None
        if isinstance(arguments, Mapping):
            command = arguments.get("command")
        if is_validation_command(command):
            return ActivityPhase.VALIDATING
    return phase


__all__ = ["ActivityPhase", "classify_tool_activity", "is_validation_command"]
