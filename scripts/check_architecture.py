"""Verifikasi Architecture AETHER (audit + hardening).

Deterministik. Memeriksa relationship antar package, bukan sekadar keberadaan
file. Menguji:
    1. importability semua package utama
    2. tidak ada circular import runtime
    3. forbidden dependency direction (provider leakage ke core, dll)
    4. provider-specific code tidak bocor ke core
    5. duplicate executor/command execution logic
    6. workspace/security boundary tetap tersedia & dipakai
    7. struktur package utama
    8. reliability tetap layer pendukung (tidak impor core)
    9. capabilities tetap deklaratif (tidak impor core/providers)
   10. changes tetap observability (tidak impor core/reliability)
   11. public API consistency (provider bawaan diekspor)
   12. Agent Core tetap provider-agnostic
   13. git/session/tasks tetap relatif independent

Jalankan:
    python scripts/check_architecture.py
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

AGENT_AI = SRC_DIR / "agent_ai"

# Package utama yang harus ada.
EXPECTED_PACKAGES = {
    "capabilities",
    "changes",
    "codeindex",
    "config",
    "context",
    "contextbuilder",
    "core",
    "git",
    "mcp",
    "permission",
    "planning",
    "projects",
    "providers",
    "reliability",
    "repointel",
    "resume",
    "runtime",
    "contextbudget",
    "session",
    "task",
    "tasks",
    "tools",
    "validation",
}


def _iter_py(root: Path):
    for p in root.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        yield p


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC_DIR).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports_of(path: Path) -> set:
    """Kumpulkan semua modul agent_ai.* yang diimpor file ini (AST)."""
    deps = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return deps
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("agent_ai"):
            deps.add(node.module)
        elif isinstance(node, ast.Import):
            for n in node.names:
                if n.name.startswith("agent_ai"):
                    deps.add(n.name)
    return deps


def _top(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) >= 2 else module


def _package_edges() -> dict:
    """Bangun graf dependency antar top-level package agent_ai."""
    edges: dict = {}
    for path in _iter_py(AGENT_AI):
        src_pkg = _top(_module_name(path))
        for dep in _imports_of(path):
            dst_pkg = _top(dep)
            if src_pkg != dst_pkg:
                edges.setdefault(src_pkg, set()).add(dst_pkg)
    return edges


def main() -> int:
    print("=== Verifikasi Architecture AETHER ===")
    return _run()


def _run() -> int:
    # 1) importability semua package utama.
    for pkg in sorted(EXPECTED_PACKAGES):
        importlib.import_module(f"agent_ai.{pkg}")
    print(f"[1] importability {len(EXPECTED_PACKAGES)} package OK")

    # 2) tidak ada circular import runtime.
    #    Import ulang di proses bersih tidak boleh gagal.
    import subprocess
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "import importlib;"
        "[importlib.import_module('agent_ai.'+p) for p in %r];"
        "print('ok')"
    ) % (str(SRC_DIR), sorted(EXPECTED_PACKAGES))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0 and "ok" in proc.stdout, f"circular import: {proc.stderr[:300]}"
    print("[2] tidak ada circular import runtime OK")

    edges = _package_edges()

    # 3) forbidden dependency direction.
    #    - reliability/capabilities/changes/validation/tools TIDAK boleh impor core.
    #    - providers TIDAK boleh impor runtime/planning/task/projects.
    forbidden = {
        "reliability": {"core", "runtime", "providers", "planning", "task", "projects"},
        "capabilities": {"core", "runtime", "providers", "planning", "task", "projects"},
        # permission tetap policy layer (tidak impor core/runtime/providers/tools).
        "permission": {"core", "runtime", "providers", "planning", "task", "projects", "tools"},
        "changes": {"core", "runtime", "reliability", "planning", "task", "projects"},
        "tools": {"core", "runtime", "providers", "planning", "task", "projects"},
        "providers": {"runtime", "planning", "task", "projects", "reliability", "changes"},
        # git/session/tasks tetap relatif independent (tidak impor core/runtime).
        "git": {"core", "runtime", "providers", "planning", "task", "tasks", "session"},
        "session": {"core", "runtime", "providers", "planning", "task", "tasks"},
        "tasks": {"core", "runtime", "providers", "planning", "task", "session"},
        # resume membaca fondasi yang ada; tidak impor core/runtime/providers.
        "resume": {"core", "runtime", "providers", "planning", "task", "tasks"},
        # repointel membaca Code Index; tidak impor core/runtime/providers.
        "repointel": {"core", "runtime", "providers", "planning", "task", "tasks"},
        # contextbudget membaca Code Index + repointel; tidak impor core/runtime/providers.
        "contextbudget": {"core", "runtime", "providers", "planning", "task", "tasks"},
    }
    for pkg, bad in forbidden.items():
        actual = edges.get(pkg, set())
        leaked = actual & bad
        assert not leaked, f"forbidden dependency: {pkg} -> {sorted(leaked)}"
    print("[3] forbidden dependency direction OK")

    # 4) provider-specific code tidak bocor ke core.
    core_dir = AGENT_AI / "core"
    for path in _iter_py(core_dir):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        # Cek import nyata (bukan docstring) ke provider konkret.
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for name in ("openrouter", "deepseek", "ollama", "openai_compatible"):
                    assert name not in node.module, f"{path.name} impor provider konkret: {node.module}"
    print("[4] provider-specific code tidak bocor ke core OK")

    # 5) duplicate executor/command execution logic.
    #    Subprocess hanya diizinkan di tools/terminal.py (command execution)
    #    dan git/client.py (menjalankan executable git). Tidak ada duplikasi
    #    command execution generik di tempat lain.
    allowed_subprocess = {
        "tools\\terminal.py", "tools/terminal.py",
        "git\\client.py", "git/client.py",
        "mcp\\transport.py", "mcp/transport.py",
    }
    subprocess_users = []
    for path in _iter_py(AGENT_AI):
        text = path.read_text(encoding="utf-8")
        if "import subprocess" in text or "os.system(" in text or "os.popen(" in text:
            subprocess_users.append(str(path.relative_to(AGENT_AI)))
    unexpected = set(subprocess_users) - allowed_subprocess
    assert not unexpected, (
        f"command execution hanya diizinkan di tools/terminal.py & git/client.py, dapat: {sorted(unexpected)}"
    )
    print("[5] tidak ada duplicate command execution logic OK")

    # 6) workspace/security boundary tetap tersedia & dipakai.
    fs = AGENT_AI / "tools" / "filesystem.py"
    fs_text = fs.read_text(encoding="utf-8")
    assert "def _resolve_within_root" in fs_text, "workspace boundary helper hilang"
    # Tool tulis/hapus harus memakai boundary path.
    ws = (AGENT_AI / "tools" / "workspace.py").read_text(encoding="utf-8")
    assert "_resolve_within_root" in ws, "workspace.py tidak memakai workspace boundary"
    # run_command menegakkan boundary via working directory = project root.
    term = (AGENT_AI / "tools" / "terminal.py").read_text(encoding="utf-8")
    assert "cwd = self.root.resolve()" in term, "terminal.py tidak menegakkan workspace boundary (cwd)"
    assert "shell=False" in term, "terminal.py harus menjalankan command tanpa shell"
    print("[6] workspace/security boundary tersedia & dipakai OK")

    # 7) struktur package utama.
    actual_pkgs = {p.name for p in AGENT_AI.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    missing = EXPECTED_PACKAGES - actual_pkgs
    assert not missing, f"package hilang: {sorted(missing)}"
    print("[7] struktur package utama OK")

    # 8) reliability tetap layer pendukung (tidak impor core).
    rel_edges = edges.get("reliability", set())
    assert "core" not in rel_edges, "reliability tidak boleh impor core"
    print("[8] reliability tetap layer pendukung OK")

    # 9) capabilities tetap deklaratif (tidak impor core/providers).
    cap_edges = edges.get("capabilities", set())
    assert not (cap_edges & {"core", "providers"}), f"capabilities harus deklaratif: {cap_edges}"
    print("[9] capabilities tetap deklaratif OK")

    # 10) changes tetap observability (tidak impor core/reliability).
    ch_edges = edges.get("changes", set())
    assert not (ch_edges & {"core", "reliability"}), f"changes harus observability: {ch_edges}"
    print("[10] changes tetap observability OK")

    # 11) public API consistency: provider bawaan diekspor.
    import agent_ai.providers as providers_pkg
    for name in ("OllamaProvider", "DeepSeekProvider", "OpenAICompatibleProvider", "OpenRouterProvider"):
        assert hasattr(providers_pkg, name), f"providers tidak mengekspor {name}"
    print("[11] public API consistency (provider bawaan diekspor) OK")

    # 12) Agent Core tetap provider-agnostic.
    #     core tidak boleh impor provider konkret (sudah dicek di [4]);
    #     core harus memakai abstraction BaseProvider.
    orch = (AGENT_AI / "core" / "orchestrator.py").read_text(encoding="utf-8")
    assert "from agent_ai.providers.base import" in orch, "orchestrator harus pakai provider abstraction"
    print("[12] Agent Core tetap provider-agnostic OK")

    print()
    print("[OK] Arsitektur AETHER sehat (dependency direction, boundary, provider-agnostic).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
