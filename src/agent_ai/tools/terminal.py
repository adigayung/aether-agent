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

import subprocess
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

        cwd = self.root.resolve()
        if not cwd.exists() or not cwd.is_dir():
            raise ToolExecutionError(f"Workspace root tidak valid: {cwd}")

        argv = _split_command(str(command))

        start = time.perf_counter()
        try:
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
            return {
                "command": command,
                "stdout": "",
                "stderr": "",
                "exit_code": None,
                "success": False,
                "timed_out": False,
                "outcome": "spawn_error",
                "duration": round(duration, 4),
                "error": f"Command tidak ditemukan: {argv[0]}",
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
