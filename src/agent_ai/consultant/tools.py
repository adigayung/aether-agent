"""Tool yang tersedia untuk AETHER Consultant.

Boundary Consultant: READ-ONLY terhadap CODE PROJECT, READ+UPDATE terhadap
Project Bible. Karena itu registry Consultant DIKURASI dan bergantung MODE:

    mode "quick"       -> atlas_query, rig_query, project_map_status
                          (Project Map READ-ONLY: paham struktur project lewat
                          peta) + update_project_bible.
                          Bible read lewat konteks; TANPA tool source/runtime
                          (tidak ada read_file/search_code/list_files/
                          run_command).
    mode "investigate" -> atlas_query, rig_query, project_map_status,
                          list_files, read_file, search_code, run_command
                          (READ-ONLY terhadap source/project) +
                          update_project_bible.

Tool tulis/hapus/pindah (`write_file`, `edit_file`, `delete_file`,
`move_file`) SENGAJA tidak pernah didaftarkan, sehingga Consultant tidak dapat
memodifikasi source lewat mekanisme tool. Demikian pula
`refresh_project_map` (regenerate/menulis file map) TIDAK pernah didaftarkan:
Consultant tetap read-only terhadap Project Map (boleh query map, tidak boleh
mengubah/meregenerasi map).

Perbedaan utama Quick vs Investigate:
    Quick       -> Bible + Map (atlas_query/rig_query/project_map_status).
                   TIDAK membaca source/runtime.
    Investigate -> Bible + Map + Source + Runtime (read-only).

Modul ini TIDAK membuat subsystem baru:
    - read/search tools  : memakai tools filesystem existing.
    - Project Map        : memakai capability existing (tools/project_map.py),
                           hanya versi READ-ONLY (tanpa refresh).
    - run_command        : memakai RunCommandTool existing (Windows-aware).
    - Bible              : memakai BibleStore / IntelligenceLearner existing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from agent_ai.consultant.models import (
    DEFAULT_CONSULTANT_MODE,
    MODE_INVESTIGATE,
    normalize_consultant_mode,
)
from agent_ai.tools.base import BaseTool, ToolValidationError
from agent_ai.tools.terminal import RunCommandTool, _split_command


# --------------------------------------------------------------------------- #
# Guard command (best-effort; boundary utama = registry tanpa tool tulis)
# --------------------------------------------------------------------------- #
# Subcommand `git` yang MENGUBAH repository (write/destructive) -> ditolak.
_DESTRUCTIVE_GIT_SUBCOMMANDS = frozenset(
    {
        "commit",
        "push",
        "reset",
        "checkout",
        "clean",
        "revert",
        "rm",
        "mv",
        "stash",
        "merge",
        "rebase",
        "cherry-pick",
        "apply",
        "am",
        "switch",
        "restore",
        "gc",
        "prune",
        "filter-branch",
        "update-ref",
        "config",  # git config dapat mengubah state; tolak demi aman
    }
)

# Command yang memodifikasi file/directory (Windows CMD builtins + Unix).
_FILE_MUTATING_COMMANDS = frozenset(
    {
        # Windows CMD
        "del",
        "erase",
        "rmdir",
        "rd",
        "move",
        "ren",
        "rename",
        "md",
        "mkdir",
        "copy",
        "xcopy",
        "robocopy",
        "mklink",
        "attrib",
        "replace",
        # Unix (seharusnya tidak dipakai di Windows, ditolak untuk aman)
        "rm",
        "mv",
        "cp",
        "touch",
        "chmod",
        "chown",
        "truncate",
        "ln",
        "dd",
        "tee",
        "sed",
        "patch",
        "install",
    }
)


def _has_redirect_outside_quotes(command: str) -> bool:
    """True bila command memuat redirect `>`/`>>` di luar string kutip.

    Redirect menulis file; tidak diizinkan untuk Consultant (memodifikasi
    project). Quote-aware agar `python -c "print('a>b')"` tidak salah ditolak.
    """
    n = len(command)
    i = 0
    quote: Optional[str] = None
    while i < n:
        ch = command[i]
        if quote is not None:
            if ch == quote:
                nxt = command[i + 1] if i + 1 < n else ""
                if nxt == "" or nxt.isspace():
                    quote = None
            i += 1
            continue
        if ch == ">":
            return True
        if ch in ('"', "'"):
            quote = ch
        i += 1
    return False


def consultant_command_violation(command: str) -> Optional[str]:
    """Kembalikan alasan bila `command` dilarang untuk Consultant, else None.

    Deterministik & best-effort: menolak operasi yang jelas mengubah/menghapus
    project (git write, file mutating commands, redirect). Bukan sandbox penuh.
    """
    text = (command or "").strip()
    if not text:
        return "command kosong"

    if _has_redirect_outside_quotes(text):
        return "redirect '>' menulis file (modifikasi project)"

    try:
        tokens = _split_command(text)
    except ToolValidationError:
        return None
    if not tokens:
        return "command kosong"

    first = tokens[0].lower()
    if first.endswith(".exe"):
        first = first[:-4]
    if first in _FILE_MUTATING_COMMANDS:
        return f"command '{first}' memodifikasi/menghapus file"

    if first == "git" and len(tokens) >= 2:
        sub = tokens[1].lower()
        if sub in _DESTRUCTIVE_GIT_SUBCOMMANDS:
            return f"git {sub} mengubah repository (git write)"

    return None


class ConsultantRunCommandTool(RunCommandTool):
    """run_command untuk Consultant: diagnosis/validasi, bukan modifikasi.

    Mewarisi RunCommandTool existing (routing Windows-aware, timeout, output
    terstruktur) dan menambahkan guard anti-destruktif. Command yang ditolak
    dikembalikan sebagai tool error (loop Consultant tetap berjalan).
    """

    name = "run_command"
    description = (
        "[Consultant] Menjalankan command DIAGNOSTIK/VALIDASI di dalam project "
        "dan menangkap output (contoh: git status, git diff, git log, python "
        "checker.py, pytest, npm run build). READ-ONLY terhadap source code: "
        "command yang memodifikasi/menghapus/memindahkan file (del/rmdir/move/"
        "ren/copy/mkdir/redirect '>') atau git write (commit/push/reset/clean/"
        "checkout) DITOLAK. Pada Windows gunakan executable native (python, git, "
        "node, pytest) atau CMD builtins; JANGAN pakai command Unix. Gunakan "
        "search_code/read_file/list_files untuk inspeksi source. Gunakan cwd "
        "untuk working directory; jangan pakai 'cd' di dalam command."
    )

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        command = arguments.get("command")
        reason = consultant_command_violation(str(command or ""))
        if reason:
            raise ToolValidationError(
                f"Consultant run_command menolak perintah ({reason}). "
                "Consultant bersifat READ-ONLY terhadap source project; gunakan "
                "command diagnostik/validasi atau tool read-only."
            )
        return super().execute(**arguments)


class ConsultantBibleTool(BaseTool):
    """Simpan knowledge terverifikasi ke AI Project Bible (Project Knowledge).

    Ini SATU-SATUNYA jalur Consultant menulis: ke Project Bible project-local
    (`<root>/.aether/bible/<kategori>.md`), bukan ke source code. Memakai
    IntelligenceLearner/BibleStore yang sudah ada (bukan store baru).
    """

    name = "update_project_bible"
    description = (
        "Menyimpan knowledge project yang sudah TERVERIFIKASI ke AI Project "
        "Bible (persistent Project Knowledge untuk konsultasi berikutnya). "
        "Gunakan hanya untuk fakta/keputusan/pelajaran nyata tentang project - "
        "jangan mengarang. Kategori: architecture, ui, conventions, decisions, "
        "facts, learnings, problems, known_bugs, known_gaps."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": (
                    "Kategori Bible: architecture | ui | conventions | decisions | "
                    "facts | learnings | problems | known_bugs | known_gaps"
                ),
            },
            "content": {
                "type": "string",
                "description": "Knowledge faktual & ringkas tentang project.",
            },
            "confidence": {
                "type": "number",
                "description": "Tingkat keyakinan 0..1 (opsional, default 1.0).",
            },
        },
        "required": ["category", "content"],
    }

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root).resolve() if root else None

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        if self.root is None:
            raise ToolValidationError(
                "Project root tidak tersedia; Project Bible tidak dapat diperbarui."
            )

        category = arguments.get("category")
        content = arguments.get("content")
        if not category or not str(category).strip():
            raise ToolValidationError("Argumen 'category' wajib diisi.")
        if content is None or (isinstance(content, str) and not content.strip()):
            raise ToolValidationError("Argumen 'content' wajib diisi.")

        confidence = arguments.get("confidence", 1.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            raise ToolValidationError("Argumen 'confidence' harus berupa angka 0..1.")
        if not (0.0 <= confidence <= 1.0):
            raise ToolValidationError("Argumen 'confidence' harus berada di 0..1.")

        from agent_ai.projects.intelligence import ProjectIntelligence
        from agent_ai.projects.learning import IntelligenceLearner, LearningError

        intelligence = ProjectIntelligence.for_project(self.root)
        learner = IntelligenceLearner(intelligence)
        try:
            entry = learner.add_verified(
                str(category).strip(), content, confidence=confidence, source="consultant"
            )
        except LearningError as exc:
            raise ToolValidationError(str(exc)) from exc

        return {
            "saved": entry is not None,
            "duplicate": entry is None,
            "category": str(category).strip(),
        }


def build_consultant_registry(
    root: Optional[Path] = None,
    provider: Any = None,
    options: Any = None,
    mode: str = DEFAULT_CONSULTANT_MODE,
):
    """Bangun ToolRegistry Consultant (kurasi READ-ONLY + Bible) sesuai mode.

    Tool yang TIDAK didaftarkan benar-benar tidak tersedia bagi LLM (arsitektur
    Native Tool Calling hanya menawarkan tool dari registry), jadi pembatasan
    mode bersifat struktural — bukan sekadar instruksi prompt.

    Args:
        root: root project target (opsional). Bila diisi, semua tool dibatasi
            ke root tersebut. Bila None, tool memakai default root-nya.
        provider/options: tidak dipakai saat ini (disediakan untuk ekstensi),
            dipertahankan agar signature stabil.
        mode: "quick" | "investigate" (nilai tak dikenal -> default quick).
            - quick       : capability Project Map READ-ONLY (atlas_query,
                            rig_query, project_map_status) + update_project_bible
                            (Bible read via konteks + Bible update via tool).
                            TANPA tool source/runtime (read_file/search_code/
                            list_files/run_command) dan TANPA refresh_project_map.
            - investigate : tool Consultant existing (list_files, read_file,
                            search_code, run_command) + capability Project Map
                            READ-ONLY (atlas_query, rig_query,
                            project_map_status) + update_project_bible.
                            TANPA refresh_project_map.

    Returns:
        ToolRegistry berisi tool yang AMAN untuk Consultant (tanpa tool tulis).
    """
    from agent_ai.tools.registry import ToolRegistry

    resolved = Path(root).resolve() if root is not None else None
    normalized = normalize_consultant_mode(mode)

    registry = ToolRegistry()

    # Project Map READ-ONLY tersedia di SEMUA mode Consultant (quick maupun
    # investigate): atlas_query, rig_query, project_map_status. `include_refresh
    # =False` -> `refresh_project_map` TIDAK pernah tersedia untuk Consultant,
    # sehingga Consultant tetap read-only terhadap Project Map (boleh query,
    # tidak boleh regenerate/menulis). Quick memakai map tanpa boleh membaca
    # source.
    from agent_ai.tools.project_map import build_project_map_tools

    for tool in build_project_map_tools(root=resolved, include_refresh=False):
        registry.register(tool)

    if normalized == MODE_INVESTIGATE:
        from agent_ai.tools.filesystem import (
            ListFilesTool,
            ReadFileTool,
            SearchCodeTool,
        )

        registry.register(ListFilesTool(root=resolved))
        registry.register(ReadFileTool(root=resolved))
        registry.register(SearchCodeTool(root=resolved))
        registry.register(ConsultantRunCommandTool(root=resolved))

    # Project Bible update tersedia di SEMUA mode (satu-satunya jalur tulis
    # Consultant). Project Bible READ dilakukan via konteks system message.
    registry.register(ConsultantBibleTool(root=resolved))
    return registry
