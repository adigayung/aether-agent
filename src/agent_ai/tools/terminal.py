"""Terminal Executor tool (menjalankan command di dalam workspace).

Menyediakan satu tool:
    - run_command : menjalankan command dengan working directory = project root.

Keamanan & desain:
    - Working directory SELALU project root (workspace boundary).
    - Command Windows CMD builtins (dir, echo, set, dll.) dijalankan
      melalui shell=True (cmd.exe) secara otomatis.
    - Command native (python, git, npm, dll.) dijalankan dengan shell=False.
    - Pada Windows, shim .cmd/.bat (mis. npm -> npm.CMD) di-resolve secara
      eksplisit (PATHEXT-aware) karena CreateProcess tidak menerapkan
      PATHEXT sehingga shim batch gagal di-resolve saat shell=False.
    - Shell syntax (&&, ||, |, >, >>) dideteksi dan dijalankan via shell.
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
import shutil
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


# CMD builtins Windows yang tidak memiliki executable standalone
# dan HARUS dijalankan melalui shell (cmd.exe /c).
# NOTE: `where` TIDAK termasuk di sini — `where.exe` adalah executable
# Windows yang harus dijalankan via shell=False (native subprocess).
_WINDOWS_CMD_BUILTINS = frozenset({
    "assoc", "attrib", "break", "chcp", "cls", "color", "copy",
    "date", "del", "dir", "doskey", "echo", "endlocal", "erase",
    "exit", "for", "ftype", "goto", "if", "md", "mkdir", "mklink",
    "move", "path", "pause", "popd", "prompt", "pushd", "rd",
    "rename", "ren", "rmdir", "set", "setlocal", "shift",
    "start", "time", "title", "type", "ver", "verify", "vol",
})


def _command_needs_shell(command: str) -> bool:
    """True bila command mengandung shell syntax yang membutuhkan shell=True.

    Mendeteksi:
      - Windows CMD builtins (dir, echo, set, cls, dll.)
      - operator chaining: &&, ||
      - pipe: | (yang bukan bagian dari path/argumen)
      - redirect: >, >>
      - cd sebagai command bawaan shell (di awal command)

    Quote-aware: operator di dalam string kutip (misal python -c "print('a|b')")
    TIDAK memicu shell=True.
    """
    stripped = command.strip()

    # Cek apakah command pertamanya adalah CMD builtin Windows.
    # Gunakan _split_command untuk tokenisasi yang quote-aware.
    try:
        first_token = _split_command(stripped)[0].lower().rstrip(">")
    except ToolValidationError:
        first_token = ""
    if first_token in _WINDOWS_CMD_BUILTINS:
        return True

    # Cek operator chaining, pipe, redirect — hanya di luar quoted strings.
    # Gunakan state machine yang melacak quote state (seperti _split_command).
    if _has_shell_operator_outside_quotes(stripped):
        return True

    # cd sebagai command pertama (diikuti spasi atau end of string)
    if re.match(r"^cd(?:\s|$)", stripped):
        return True

    return False


def _has_shell_operator_outside_quotes(command: str) -> bool:
    """True bila command mengandung operator shell (&&, ||, |, >, >>)
    di luar string kutip (quoted strings).

    Menggunakan state machine quote-tracking yang konsisten dengan
    _split_command(): tanda kutip tunggal dan ganda melingkupi argumen,
    dan kutip bersarang (quote sejenis di tengah isi tanpa spasi/akhir
    string) diperlakukan sebagai literal.
    """
    n = len(command)
    i = 0
    quote: Optional[str] = None

    while i < n:
        ch = command[i]

        if quote is not None:
            # Di dalam quoted string: cari penutup kutip
            if ch == quote:
                nxt = command[i + 1] if i + 1 < n else ""
                if nxt == "" or nxt.isspace():
                    quote = None  # penutup kutip yang valid
                # else: kutip bersarang, tetap di dalam quote
            i += 1
            continue

        # Di luar quoted string: periksa operator shell
        # Cek && dan || (2-char operators)
        if i + 1 < n:
            two = command[i : i + 2]
            if two == "&&" or two == "||":
                return True

        # Cek pipe | (harus dikelilingi spasi atau di awal/akhir)
        if ch == "|":
            prev = command[i - 1] if i > 0 else " "
            nxt = command[i + 1] if i + 1 < n else " "
            if prev.isspace() or prev == "" or prev == "|":
                if nxt.isspace() or nxt == "" or nxt == "|":
                    return True

        # Cek redirect > dan >> (harus dikelilingi spasi atau di awal/akhir)
        if ch == ">":
            # Cek apakah ini >> (redirect append)
            if i + 1 < n and command[i + 1] == ">":
                # >> operator
                prev = command[i - 1] if i > 0 else " "
                nxt = command[i + 2] if i + 2 < n else " "
                if prev.isspace() or prev == "" or prev == ">":
                    if nxt.isspace() or nxt == "" or nxt == ">":
                        return True
                i += 2
                continue
            else:
                # > operator (single redirect)
                prev = command[i - 1] if i > 0 else " "
                nxt = command[i + 1] if i + 1 < n else " "
                if prev.isspace() or prev == "" or prev == ">":
                    if nxt.isspace() or nxt == "" or nxt == ">":
                        return True

        # Cek awal kutip — masuk ke dalam quoted string
        if ch in ('"', "'"):
            quote = ch

        i += 1

    return False


def _resolve_windows_shim(program: str, cwd: Path) -> Optional[str]:
    """Resolve command Windows menjadi path shim ``.cmd``/``.bat`` (PATHEXT).

    ``subprocess`` dengan ``shell=False`` memakai ``CreateProcess``. Berbeda
    dari ``cmd.exe``, ``CreateProcess`` TIDAK menerapkan ``PATHEXT``: lookup
    nama tanpa ekstensi hanya mencoba ``.exe``. Akibatnya command yang di
    Windows hanya tersedia sebagai shim batch (mis. ``npm`` -> ``npm.CMD``)
    gagal dengan ``FileNotFoundError`` walau bisa dijalankan dari terminal.

    Fungsi ini mengembalikan path absolut HANYA bila ``program`` ter-resolve
    ke file ``.cmd``/``.bat`` (kasus yang memang butuh shim). Untuk executable
    biasa (``.exe``) atau command yang sudah ditangani, kembalikan ``None``
    agar perilaku lama tidak berubah.
    """
    if os.name != "nt":
        return None
    if not program:
        return None

    # cmd.exe mencari current directory lebih dulu; ikutkan cwd efektif.
    search = os.pathsep.join([str(cwd), os.environ.get("PATH", "")])
    try:
        resolved = shutil.which(program, path=search)
    except (TypeError, ValueError):
        return None

    if not resolved:
        return None
    if Path(resolved).suffix.lower() in (".cmd", ".bat"):
        return resolved
    return None


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
    description = (
        "Menjalankan command di dalam project workspace dan menangkap output. "
        "Pada Windows, command harus kompatibel dengan Windows: "
        "gunakan executable native (python, git, npm, node, ffmpeg, where.exe) "
        "atau Windows CMD builtins (dir, echo, set, cls, type, copy, del, mkdir, dll.). "
        "JANGAN gunakan command Unix/Linux (find, grep, head, tail, cat, wc, ls, sed, awk, xargs, chmod, rm, cp, mv, touch). "
        "Untuk inspeksi source code dan workspace, gunakan search_code, list_files, atau read_file. "
        "Gunakan cwd untuk working directory (path relatif terhadap project root atau absolut). "
        "Jangan gunakan 'cd' di dalam command; cwd sudah disediakan sebagai parameter terpisah."
    )
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
                )
            else:
                argv = _split_command(str(command))
                # Windows: resolusi shim .cmd/.bat yang tidak ditangani
                # CreateProcess (mis. npm -> npm.CMD). Tanpa ini, command
                # yang hanya ada sebagai shim batch gagal dijalankan.
                if os.name == "nt":
                    shim = _resolve_windows_shim(argv[0], cwd)
                    if shim:
                        argv[0] = shim
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
            failed_cmd = str(command).split()[0] if use_shell else argv[0]
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
