"""Terminal Executor tool (menjalankan command di dalam workspace).

Menyediakan satu tool:
    - run_command : menjalankan command dengan working directory = project root.

Keamanan & desain:
    - Working directory SELALU project root (workspace boundary).
    - Tidak memakai shell parsing (shell=False); command di-split secara aman.
    - Timeout wajib (default) agar agent tidak menggantung.
    - stdout/stderr/exit_code/duration ditangkap dan dikembalikan terstruktur.
    - Field `outcome` membedakan secara eksplisit:
        "success"         : command dieksekusi & exit_code == 0.
        "command_failure" : command dieksekusi tetapi exit_code != 0.
        "timeout"         : command melewati batas waktu.
        "spawn_error"     : command gagal dijalankan (mis. executable tidak ada).
    - Command gagal / timeout TIDAK melempar exception ke agent; dikembalikan
      sebagai hasil terstruktur (success=False) agar bisa diproses LLM.
    - Tidak mencetak/meneruskan environment secrets (env diwarisi apa adanya,
      tidak pernah di-log).
    - Generic: tidak membatasi bahasa/framework (Python, Node, PHP, Laravel,
      Unity, Android, dll).

Tidak ada Git/web/proxy/UI/RAG/provider baru.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_ai.tools.base import BaseTool, ToolExecutionError, ToolValidationError
from agent_ai.tools.filesystem import _DEFAULT_ROOT

_DEFAULT_TIMEOUT = 60.0
_MAX_TIMEOUT = 600.0
_MAX_OUTPUT_CHARS = 100_000  # batasi output agar tidak membanjiri context


def _split_command(command: str) -> List[str]:
    """Split string command menjadi argv tanpa shell parsing (shell=False).

    Parser toleran untuk command Windows/PowerShell/Python -c:
      - Tanda kutip ganda (") dan tunggal (') mengelompokkan sebuah argumen.
      - Kutip bersarang (nested) didukung: tanda kutip sejenis yang berada di
        tengah isi (tidak diikuti spasi/akhir string) diperlakukan sebagai
        literal, bukan penutup. Contoh `python -c "s=s.replace("x","y")"`
        tetap menghasilkan satu argumen kode utuh.
      - Kutip yang tidak tertutup TIDAK melempar error ("No closing
        quotation"); sisa string diperlakukan sebagai bagian token (recover).
      - Backslash dan karakter HTML (<, >, /) tidak diperlakukan istimewa,
        sehingga path Windows dan string panjang tetap utuh.
    """
    tokens: List[str] = []
    buf: List[str] = []
    started = False
    quote: Optional[str] = None
    n = len(command)

    i = 0
    while i < n:
        ch = command[i]

        if quote is not None:
            if ch == quote:
                nxt = command[i + 1] if i + 1 < n else ""
                if nxt == "" or nxt.isspace():
                    quote = None  # penutup kutip (hanya di boundary argumen)
                else:
                    buf.append(ch)  # kutip bersarang -> literal
            else:
                buf.append(ch)
            i += 1
            continue

        if ch in ('"', "'"):
            quote = ch
            started = True
        elif ch.isspace():
            if started:
                tokens.append("".join(buf))
                buf = []
                started = False
        else:
            buf.append(ch)
            started = True
        i += 1

    if started:
        tokens.append("".join(buf))

    if not tokens:
        raise ToolValidationError("Command kosong.")
    return tokens


def _command_needs_shell(command: str) -> bool:
    """True bila command mengandung shell syntax yang membutuhkan shell=True.

    Mendeteksi:
      - operator chaining: &&, ||
      - pipe: | (yang bukan bagian dari path/argumen)
      - redirect: >, >>
      - cd sebagai command bawaan shell (di awal command)
    """
    stripped = command.strip()

    # Cek operator chaining dan pipe/redirect
    if "&&" in stripped or "||" in stripped:
        return True

    # Cek pipe atau redirect — hati-hati agar tidak false positive
    # pada path yang mengandung karakter ini.
    # Pipe: | yang dikelilingi spasi atau di awal/akhir
    # Redirect: > atau >> yang dikelilingi spasi atau di akhir
    # Kami menggunakan regex sederhana untuk menghindari path seperti "C:\\dir"

    # Pipe: | dengan spasi di sekitarnya atau di awal/akhir
    if re.search(r"(?:^|\s)\|(?:\s|$)", stripped):
        return True

    # Redirect: > atau >> dengan spasi di sekitarnya atau di akhir
    # Hindari false positive pada path Windows seperti "C:\dir>file"
    if re.search(r"(?:^|\s)>>?(?:\s|$)", stripped):
        return True

    # cd sebagai command pertama (diikuti spasi atau end of string)
    if re.match(r"^cd(?:\s|$)", stripped):
        return True

    return False


def _resolve_cwd(cwd: Optional[str], root: Path) -> Path:
    """Validasi dan kembalikan working directory yang aman.

    Jika cwd diberikan, pastikan berada dalam workspace root.
    Jika cwd tidak diberikan, kembalikan root.

    Raises:
        ToolValidationError: bila cwd berada di luar workspace root.
        ToolExecutionError: bila cwd tidak ditemukan.
    """
    root_resolved = root.resolve()

    if cwd is None or str(cwd).strip() == "":
        return root_resolved

    cwd_path = Path(cwd)
    if not cwd_path.is_absolute():
        cwd_path = root_resolved / cwd_path

    resolved = cwd_path.resolve()

    # Validasi: cwd harus berada di dalam workspace root
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ToolValidationError(
            f"Working directory '{cwd}' berada di luar project root dan ditolak."
        )

    if not resolved.exists():
        raise ToolExecutionError(f"Working directory tidak ditemukan: {resolved}")

    if not resolved.is_dir():
        raise ToolExecutionError(f"Working directory bukan directory: {resolved}")

    return resolved


def _truncate(text: str) -> str:
    """Batasi panjang output agar aman untuk context LLM."""
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + f"\n...[truncated {len(text) - _MAX_OUTPUT_CHARS} chars]"


class RunCommandTool(BaseTool):
    """Menjalankan command di dalam project workspace (working directory = root)."""

    name = "run_command"
    description = "Menjalankan command di dalam project workspace dan menangkap output."
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command yang dijalankan."},
            "timeout": {"type": "number", "description": "Timeout detik (opsional)."},
            "cwd": {
                "type": "string",
                "description": "Working directory untuk command (opsional). Harus berada di dalam project root.",
            },
        },
        "required": ["command"],
    }

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT

    def execute(self, **arguments: Any) -> Dict[str, Any]:
        command = arguments.get("command")
        if not command or not str(command).strip():
            raise ToolValidationError("Argumen 'command' wajib diisi.")

        timeout = arguments.get("timeout", _DEFAULT_TIMEOUT)
        try:
            timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ToolValidationError("Argumen 'timeout' harus berupa angka.") from exc
        if timeout <= 0:
            raise ToolValidationError("Argumen 'timeout' harus > 0.")
        timeout = min(timeout, _MAX_TIMEOUT)

        cwd_arg = arguments.get("cwd")
        try:
            cwd = _resolve_cwd(cwd_arg, self.root)
        except (ToolValidationError, ToolExecutionError) as exc:
            return {
                "command": command,
                "stdout": "",
                "stderr": "",
                "exit_code": None,
                "success": False,
                "timed_out": False,
                "outcome": "spawn_error",
                "duration": 0.0,
                "error": str(exc),
            }

        # Enforce workspace boundary: cwd must be within project root.
        root_resolved = self.root.resolve()
        cwd = cwd.resolve()
        if cwd != root_resolved and root_resolved not in cwd.parents:
            raise ToolValidationError(
                f"Working directory '{cwd}' berada di luar project root dan ditolak."
            )
        cwd = root_resolved if cwd == root_resolved else cwd

        # Workspace boundary: working directory = project root (default).
        cwd = self.root.resolve() if cwd_arg is None or str(cwd_arg).strip() == "" else cwd

        # Tentukan apakah command membutuhkan shell execution
        use_shell = _command_needs_shell(str(command))
        shell_executable = sys.executable if use_shell else None

        start = time.perf_counter()
        try:
            if use_shell:
                completed = subprocess.run(
                    str(command),
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    shell=True,
                    executable=shell_executable if os.name != "nt" else None,
                )
            else:
                argv = _split_command(str(command))
                completed = subprocess.run(
                    argv,
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    shell=False,
                )
        except subprocess.TimeoutExpired as exc:
            duration = time.perf_counter() - start
            return {
                "command": command,
                "stdout": _truncate(exc.stdout or ""),
                "stderr": _truncate(exc.stderr or ""),
                "exit_code": None,
                "success": False,
                "timed_out": True,
                "outcome": "timeout",
                "duration": round(duration, 4),
                "error": f"Command timeout setelah {timeout} detik.",
            }
        except FileNotFoundError as exc:
            duration = time.perf_counter() - start
            failed_cmd = argv[0] if not use_shell else str(command).split()[0]
            return {
                "command": command,
                "stdout": "",
                "stderr": "",
                "exit_code": None,
                "success": False,
                "timed_out": False,
                "outcome": "spawn_error",
                "duration": round(duration, 4),
                "error": f"Command tidak ditemukan: {failed_cmd}",
            }
        except OSError as exc:
            raise ToolExecutionError(f"Gagal menjalankan command: {exc}") from exc

        duration = time.perf_counter() - start
        return {
            "command": command,
            "stdout": _truncate(completed.stdout or ""),
            "stderr": _truncate(completed.stderr or ""),
            "exit_code": completed.returncode,
            "success": completed.returncode == 0,
            "timed_out": False,
            "outcome": "success" if completed.returncode == 0 else "command_failure",
            "duration": round(duration, 4),
        }
