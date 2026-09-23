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
    - Timeout BENAR-BENAR menghentikan proses: saat habis waktu (atau saat
      cancel diminta) SELURUH process tree dibunuh (Windows: `taskkill /T /F`;
      POSIX: process-group kill). Ini mencegah hang permanen karena grandchild
      yang mewarisi pipe stdout/stderr (kasus `communicate()` yang menunggu EOF
      selamanya pada `subprocess.run(capture_output=True)`).
    - Output dibaca lewat thread reader daemon + bounded join, sehingga proses
      yang selesai tetapi pipe masih dipegang grandchild TIDAK membuat tool
      menggantung.
    - stdout/stderr/exit_code/duration ditangkap dan dikembalikan terstruktur.
    - Field `outcome` membedakan secara eksplisit:
        "success"         : command dieksekusi & exit_code == 0.
        "command_failure" : command dieksekusi tetapi exit_code != 0.
        "timeout"         : command melewati batas waktu (process tree dibunuh).
        "cancelled"       : task diminta Stop; process tree dihentikan.
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
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_ai.tools.base import BaseTool, ToolExecutionError, ToolValidationError
from agent_ai.tools.filesystem import _DEFAULT_ROOT

_DEFAULT_TIMEOUT = 60.0
_MAX_TIMEOUT = 600.0
_MAX_OUTPUT_CHARS = 100_000  # batasi output agar tidak membanjiri context

# ---------------------------------------------------------------------------
# Batas waktu cleanup (di LUAR timeout command).
# ---------------------------------------------------------------------------
# Nilai-nilai ini menjamin `run_command` SELALU kembali dalam waktu terbatas,
# bahkan bila ada grandchild yang tetap memegang pipe stdout/stderr setelah
# proses induk selesai/dibunuh.
_KILL_GRACE_SECONDS = 5.0     # menunggu process tree benar-benar mati setelah kill
_DRAIN_GRACE_SECONDS = 3.0    # menunggu reader thread selesai drain output
_CANCEL_POLL_SECONDS = 0.2    # interval polling token cancel saat menunggu proses


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


def _drain_stream(stream: Any, sink: List[str]) -> None:
    """Baca stream sampai EOF ke dalam `sink` (dijalankan di thread daemon).

    Dibaca di thread terpisah agar proses yang menghasilkan output besar tidak
    memblokir sementara kita menunggu proses selesai/timeout.
    """
    try:
        if stream is None:
            return
        data = stream.read()
        if data:
            sink.append(data)
    except Exception:  # noqa: BLE001 - pembacaan output best-effort
        pass
    finally:
        try:
            if stream is not None:
                stream.close()
        except Exception:  # noqa: BLE001
            pass


def _wait_bounded(process: "subprocess.Popen[Any]", seconds: float) -> None:
    """Tunggu proses selesai maksimal `seconds` (best-effort, tanpa raise)."""
    try:
        process.wait(timeout=max(0.0, float(seconds)))
    except Exception:  # noqa: BLE001 - grace wait best-effort
        pass


def _kill_process_tree(process: "subprocess.Popen[Any]") -> None:
    """Hentikan proses beserta SELURUH descendant-nya (best-effort, bounded).

    Windows: `taskkill /F /T /PID` membunuh process tree (relasi parent-child),
    sehingga grandchild yang mewarisi pipe stdout/stderr ikut mati dan pipe
    segera tertutup. Ini menghilangkan penyebab hang permanen saat menunggu EOF.

    POSIX: proses dijalankan pada session/process-group sendiri
    (`start_new_session=True`) sehingga `os.killpg` membunuh seluruh grup.

    Selalu aman dipanggil: kegagalan terminasi tidak pernah melempar exception
    (fallback membunuh proses langsung).
    """
    if process.poll() is not None:
        # Proses langsung sudah selesai. Bila masih ada grandchild yang
        # memegang pipe, itu ditangani oleh bounded join reader thread.
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=_KILL_GRACE_SECONDS,
                shell=False,
            )
            return
        except Exception:  # noqa: BLE001 - taskkill tidak tersedia/gagal
            pass
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except Exception:  # noqa: BLE001 - fallback ke kill proses langsung
            pass

    # Fallback: bunuh proses langsung (Windows tanpa taskkill / POSIX gagal).
    try:
        process.kill()
    except Exception:  # noqa: BLE001
        pass


def _run_process(
    target: Any,
    *,
    cwd: Path,
    timeout: float,
    shell: bool,
    cancel_token: Optional[Any] = None,
) -> Dict[str, Any]:
    """Jalankan proses dengan timeout yang BENAR-BENAR menghentikan process tree.

    Berbeda dari `subprocess.run(capture_output=True, timeout=...)` — yang pada
    Windows hanya membunuh child langsung lalu memanggil `communicate()` tanpa
    batas (hang permanen bila grandchild memegang pipe) — helper ini:

      - membaca stdout/stderr di thread daemon (proses tidak memblokir),
      - TIDAK pernah memanggil `communicate()` tanpa batas,
      - membunuh SELURUH process tree saat timeout/cancel,
      - memakai bounded join untuk drain sehingga SELALU kembali.

    `cancel_token` (opsional): bila diberikan dan `is_cancelled()` menjadi True
    saat menunggu, proses dihentikan dan hasil ditandai `outcome="cancelled"`.

    Returns:
        dict dengan key: stdout, stderr, exit_code, success, timed_out,
        outcome, error.
    """
    popen_kwargs: Dict[str, Any] = {
        "cwd": str(cwd),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "shell": bool(shell),
    }
    if os.name == "nt":
        # Proses pada grup baru: memisahkan sinyal Ctrl+C dan memudahkan
        # terminasi tree (taskkill /T).
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # Session/process-group baru -> os.killpg membunuh seluruh descendant.
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(target, **popen_kwargs)

    out_chunks: List[str] = []
    err_chunks: List[str] = []
    readers = [
        threading.Thread(
            target=_drain_stream, args=(process.stdout, out_chunks), daemon=True
        ),
        threading.Thread(
            target=_drain_stream, args=(process.stderr, err_chunks), daemon=True
        ),
    ]
    for reader in readers:
        reader.start()

    start = time.perf_counter()
    timed_out = False
    cancelled = False
    try:
        deadline = start + timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                timed_out = True
                break
            try:
                # Poll pendek agar cancel (Stop) terdeteksi cepat, bukan hanya
                # menunggu timeout penuh.
                process.wait(timeout=min(_CANCEL_POLL_SECONDS, remaining))
                break  # proses selesai sendiri.
            except subprocess.TimeoutExpired:
                if cancel_token is not None and cancel_token.is_cancelled():
                    cancelled = True
                    break
        if timed_out or cancelled:
            _kill_process_tree(process)
            _wait_bounded(process, _KILL_GRACE_SECONDS)
    finally:
        # Bounded join (deadline bersama): reader daemon tidak boleh membuat
        # kita menggantung bila grandchild masih memegang pipe (proses induk
        # sudah selesai). Total drain dibatasi _DRAIN_GRACE_SECONDS.
        drain_deadline = time.perf_counter() + _DRAIN_GRACE_SECONDS
        for reader in readers:
            remaining = drain_deadline - time.perf_counter()
            if remaining <= 0:
                break
            reader.join(timeout=remaining)

    stdout = "".join(out_chunks)
    stderr = "".join(err_chunks)

    if cancelled:
        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": None,
            "success": False,
            "timed_out": False,
            "outcome": "cancelled",
            "error": "Command dihentikan karena task dibatalkan (user stop).",
        }
    if timed_out:
        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": None,
            "success": False,
            "timed_out": True,
            "outcome": "timeout",
            "error": f"Command timeout setelah {timeout} detik.",
        }

    exit_code = process.returncode
    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "success": exit_code == 0,
        "timed_out": False,
        "outcome": "success" if exit_code == 0 else "command_failure",
        "error": None,
    }


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

    def __init__(
        self,
        root: Optional[Path] = None,
        *,
        cancel_token: Optional[Any] = None,
    ) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT
        # Token cancel kooperatif (opsional). Bila diisi, run_command
        # MENGHENTIKAN process tree saat Stop diminta (bukan hanya menunggu
        # timeout) sehingga Stop benar-benar membebaskan slot queue. Bila None,
        # perilaku persis seperti sebelumnya (backward compatible).
        self._cancel_token = cancel_token

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

        # Bangun target proses: string (shell=True) atau argv (shell=False).
        # Routing ini TIDAK berubah dari perilaku lama; yang berubah hanya cara
        # eksekusinya (tidak lagi memakai subprocess.run(capture_output=...) yang
        # bisa menggantung permanen bila ada grandchild pemegang pipe).
        argv: Optional[List[str]] = None
        target: Any = str(command)
        if not use_shell:
            argv = _split_command(str(command))
            # Windows: resolusi shim .cmd/.bat yang tidak ditangani
            # CreateProcess (mis. npm -> npm.CMD). Tanpa ini, command
            # yang hanya ada sebagai shim batch gagal dijalankan.
            if os.name == "nt":
                shim = _resolve_windows_shim(argv[0], cwd)
                if shim:
                    argv[0] = shim
            target = argv

        start = time.perf_counter()
        try:
            outcome = _run_process(
                target,
                cwd=cwd,
                timeout=timeout,
                shell=use_shell,
                cancel_token=self._cancel_token,
            )
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
        result = {
            "command": command,
            "stdout": _truncate(outcome["stdout"]),
            "stderr": _truncate(outcome["stderr"]),
            "exit_code": outcome["exit_code"],
            "success": outcome["success"],
            "timed_out": outcome["timed_out"],
            "outcome": outcome["outcome"],
            "duration": round(duration, 4),
        }
        if outcome.get("error"):
            result["error"] = outcome["error"]
        return result
