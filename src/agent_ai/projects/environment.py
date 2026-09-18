"""Environment Context project-local (`<root>/.aether/ENVIRONMENT.md`).

Mendeteksi environment eksekusi (OS, shell, runtime bahasa, package manager,
virtualenv, dan tipe project) lalu menuliskannya sebagai markdown ke
`<root project target>/.aether/ENVIRONMENT.md`. File ini dibaca sebagai system
message pada awal session agar agent memahami lingkungan kerja project.

Prinsip:
    - Project-local: penulisan dilakukan lewat `AetherProjectStore` (satu-
      satunya penulis `<root>/.aether/`), memakai atomic write yang sama.
    - Konfigurasi-adaptif (bukan hardcode): TIDAK ada informasi spesifik
      mesin/user yang ditanam di kode. Semua nilai berasal dari deteksi runtime
      saat file dibuat (platform, shutil.which, sys, env).
    - Bounded & best-effort: setiap probe dibungkus try/except dengan timeout;
      kegagalan satu probe TIDAK boleh menggagalkan pembuatan konteks.
    - Idempotent: `build_or_load_environment()` mengembalikan file yang sudah
      ada tanpa menimpa (load), dan membuatnya sekali bila belum ada.
    - Tanpa dependency baru.

Modul ini HANYA mendeteksi + merender teks; ia tidak mengirim apa pun dan
tidak menyimpan state session. Injection ke ConversationHistory dilakukan
oleh AgentOrchestrator, sedangkan "sekali per session" diatur AgentRuntime.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from agent_ai.projects.aether_store import AetherProjectStore, _atomic_write

#: Timeout satu probe versi tool (detik). Bounded, tidak menggantung.
_VERSION_TIMEOUT = 5.0

#: Batas panjang satu nilai yang dirender (menjaga konteks tetap ringkas).
_MAX_VALUE_LEN = 300

#: Tool runtime/package manager yang dideteksi (nama -> argumen versi).
_TOOL_PROBES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("node", ("--version",)),
    ("npm", ("--version",)),
    ("pnpm", ("--version",)),
    ("yarn", ("--version",)),
    ("php", ("--version",)),
    ("composer", ("--version",)),
    ("pip", ("--version",)),
    ("git", ("--version",)),
)

#: Penanda tipe project (label -> file penanda di root).
_PROJECT_MARKERS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("Python", ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile")),
    ("Node.js", ("package.json",)),
    ("PHP", ("composer.json",)),
    ("Go", ("go.mod",)),
    ("Rust", ("Cargo.toml",)),
    ("Ruby", ("Gemfile",)),
    ("Java", ("pom.xml", "build.gradle", "build.gradle.kts")),
)

#: Nama folder virtualenv umum yang dicari di root project.
_VENV_DIRS = (".venv", "venv", "env")


# --------------------------------------------------------------------------- #
# Helper deteksi (semua best-effort)
# --------------------------------------------------------------------------- #
def _clip(value: Any, limit: int = _MAX_VALUE_LEN) -> str:
    """Ubah value menjadi teks satu baris terpotong (aman untuk markdown)."""
    text = "" if value is None else str(value)
    text = text.strip().replace("\r", " ").replace("\n", " ")
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _first_version(command: List[str], timeout: float = _VERSION_TIMEOUT) -> str:
    """Ambil baris pertama output `command` (versi). Kembalikan "" bila gagal."""
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:  # noqa: BLE001 - probe tidak boleh menggagalkan konteks
        return ""
    output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if not output:
        return ""
    return output.splitlines()[0].strip()


def _detect_os() -> Dict[str, str]:
    """Info OS dari modul `platform` (tanpa hardcode nama mesin)."""
    return {
        "system": platform.system() or os.name,
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
    }


def _detect_shell() -> Dict[str, Any]:
    """Info shell default + kandidat shell yang tersedia."""
    info: Dict[str, Any] = {}
    if os.name == "nt":
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        info["default"] = comspec
        available: List[str] = []
        for candidate in (comspec, shutil.which("powershell"), shutil.which("pwsh")):
            if candidate and candidate not in available:
                available.append(candidate)
        info["available"] = available
    else:
        info["default"] = os.environ.get("SHELL") or "/bin/sh"
    return info


def _detect_python() -> Dict[str, Any]:
    """Info interpreter Python yang sedang menjalankan AETHER."""
    return {
        "version": platform.python_version(),
        "executable": sys.executable or "",
    }


def _detect_virtualenv(root: Optional[Path]) -> Dict[str, Any]:
    """Info virtualenv: aktif (interpreter) + folder umum di root project."""
    info: Dict[str, Any] = {}
    active_prefix = sys.prefix
    base_prefix = getattr(sys, "base_prefix", active_prefix)
    info["active"] = active_prefix != base_prefix
    if info["active"]:
        info["prefix"] = active_prefix
    env_var = os.environ.get("VIRTUAL_ENV")
    if env_var:
        info["VIRTUAL_ENV"] = env_var
    if root is not None:
        found = [name for name in _VENV_DIRS if (root / name).is_dir()]
        if found:
            info["directories"] = found
    return info


def _detect_tools() -> List[Dict[str, str]]:
    """Deteksi runtime/package manager yang tersedia di PATH (versi + path)."""
    tools: List[Dict[str, str]] = []
    for name, args in _TOOL_PROBES:
        path = shutil.which(name)
        if not path:
            continue
        record: Dict[str, str] = {"name": name, "path": path}
        version = _first_version([path, *args])
        if version:
            record["version"] = version
        tools.append(record)
    return tools


def _detect_project(root: Optional[Path]) -> Dict[str, Any]:
    """Info project: root + tipe yang terdeteksi dari file penanda."""
    info: Dict[str, Any] = {"root": str(root) if root is not None else ""}
    if root is None:
        return info
    detected: List[str] = []
    markers: List[str] = []
    for label, filenames in _PROJECT_MARKERS:
        matched = [name for name in filenames if (root / name).exists()]
        if matched:
            detected.append(label)
            markers.extend(matched)
    info["detected_types"] = detected
    info["markers"] = markers
    return info


def detect_environment(root: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Deteksi environment lengkap (terstruktur, siap dirender ke markdown)."""
    root_path = Path(root) if root is not None else None
    return {
        "os": _detect_os(),
        "shell": _detect_shell(),
        "python": _detect_python(),
        "virtualenv": _detect_virtualenv(root_path),
        "tools": _detect_tools(),
        "project": _detect_project(root_path),
    }


# --------------------------------------------------------------------------- #
# Rendering (markdown machine-readable untuk dibaca LLM)
# --------------------------------------------------------------------------- #
def _is_empty(value: Any) -> bool:
    """True bila value layak dilewati saat render (None/""/koleksi kosong)."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _render_key_values(pairs: List[Tuple[str, Any]]) -> List[str]:
    """Render pasangan (key, value) menjadi baris `- key: value`."""
    lines: List[str] = []
    for key, value in pairs:
        if _is_empty(value):
            continue
        if isinstance(value, bool):
            value = "yes" if value else "no"
        elif isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        lines.append(f"- {key}: {_clip(value)}")
    return lines


def render_environment(data: Mapping[str, Any]) -> str:
    """Render hasil `detect_environment()` menjadi markdown (deterministik)."""
    lines: List[str] = [
        "# Environment Context",
        "",
        "<!-- AETHER context (machine-readable). Dibuat dari deteksi lingkungan",
        "     saat file ini dibuat; jangan ditanam sebagai nilai tetap. -->",
        "<!-- Dibaca sebagai system message pada awal session agent. -->",
        "",
    ]

    os_info = data.get("os") or {}
    lines.append("## os")
    lines.extend(
        _render_key_values(
            [(key, os_info.get(key)) for key in ("system", "release", "version", "machine")]
        )
    )
    lines.append("")

    shell = data.get("shell") or {}
    lines.append("## shell")
    lines.extend(
        _render_key_values(
            [("default", shell.get("default")), ("available", shell.get("available"))]
        )
    )
    lines.append("")

    python_info = data.get("python") or {}
    lines.append("## python")
    lines.extend(
        _render_key_values(
            [("version", python_info.get("version")), ("executable", python_info.get("executable"))]
        )
    )
    lines.append("")

    venv = data.get("virtualenv") or {}
    lines.append("## virtualenv")
    lines.extend(
        _render_key_values(
            [
                ("active", venv.get("active")),
                ("prefix", venv.get("prefix")),
                ("VIRTUAL_ENV", venv.get("VIRTUAL_ENV")),
                ("directories", venv.get("directories")),
            ]
        )
    )
    lines.append("")

    tools = data.get("tools") or []
    lines.append("## tools")
    if tools:
        for tool in tools:
            name = tool.get("name", "")
            version = tool.get("version") or ""
            path = tool.get("path") or ""
            detail = " ".join(
                part for part in (version, f"({path})" if path else "") if part
            )
            lines.append(f"- {name}: {_clip(detail)}")
    else:
        lines.append("- (tidak ada runtime/package manager terdeteksi di PATH)")
    lines.append("")

    project = data.get("project") or {}
    lines.append("## project")
    lines.extend(
        _render_key_values(
            [
                ("root", project.get("root")),
                ("detected_types", project.get("detected_types")),
                ("markers", project.get("markers")),
            ]
        )
    )
    lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def environment_path(root: Union[str, Path]) -> Path:
    """Path file Environment Context (`<root>/.aether/ENVIRONMENT.md`)."""
    return AetherProjectStore(root).environment_path()


def build_or_load_environment(root: Union[str, Path]) -> str:
    """Load ENVIRONMENT.md bila ada, atau buat sekali dari deteksi lalu load.

    Args:
        root: root project target.

    Returns:
        Isi file ENVIRONMENT.md (markdown). Bila penulisan gagal (mis. izin),
        tetap mengembalikan teks hasil deteksi (best-effort, tidak melempar).
    """
    store = AetherProjectStore(root)
    store.ensure()
    path = store.environment_path()
    try:
        if path.exists():
            return path.read_text(encoding="utf-8")
    except OSError:
        pass

    text = render_environment(detect_environment(store.root))
    try:
        _atomic_write(path, text)
    except OSError:
        pass
    return text
