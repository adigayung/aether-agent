"""Verifikasi Environment Context project-local (`.aether/ENVIRONMENT.md`).

Menguji (terisolasi, tanpa network):
    1. Deteksi + pembuatan file `<root>/.aether/ENVIRONMENT.md` (struktur ada).
    2. Path benar (di dalam `.aether/`, bukan workspace AETHER).
    3. Idempotent: file yang sudah ada di-LOAD, bukan ditimpa.
    4. Adaptif (bukan hardcode): isi berasal dari deteksi runtime & mengikuti
       tipe project (marker requirements.txt -> Python, package.json -> Node.js).
    5. Injection: AgentRuntime continuous loop mengirim Environment Context
       sebagai system message sebelum task.
    6. Sekali per session: task kedua pada runtime yang sama TIDAK memuat ulang
       dan TIDAK mengirim ulang Environment Context.
    7. Best-effort: root project tanpa project_root tidak melempar error.

Semua fixture dibuat di `J:\\Agent_Ai\\dummy_test` dan dibersihkan setelah test.

Jalankan:
    python scripts/verify_environment_context.py
"""

from __future__ import annotations

import platform
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

#: Workspace testing terisolasi (BUKAN bagian source AETHER).
DUMMY_ROOT = PROJECT_ROOT / "dummy_test"

import agent_ai.projects.environment as env_mod  # noqa: E402
from agent_ai.projects.aether_store import AetherProjectStore  # noqa: E402
from agent_ai.projects.environment import (  # noqa: E402
    build_or_load_environment,
    detect_environment,
    environment_path,
    render_environment,
)
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402
from agent_ai.runtime.models import RuntimeStatus  # noqa: E402
from agent_ai.runtime.runtime import AgentRuntime  # noqa: E402
from agent_ai.task.models import PreparedTask  # noqa: E402

MARKERS = ("# Environment Context", "## os", "## shell", "## python", "## virtualenv", "## tools", "## project")


class CapturingProvider(BaseProvider):
    """Provider palsu: kembalikan teks final + rekam messages tiap panggilan."""

    name = "capturing"

    def __init__(self, text="DONE"):
        self._text = text
        self.calls: list = []

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        self.calls.append(list(messages or []))
        return GenerateResult(text=self._text, model="fake", provider=self.name, raw={})


def _make_fixture(name: str, marker_files) -> Path:
    """Buat fixture project di dummy_test (dibersihkan oleh pemanggil)."""
    DUMMY_ROOT.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=str(DUMMY_ROOT), prefix=name))
    for filename in marker_files:
        (root / filename).write_text("", encoding="utf-8")
    return root


def main() -> int:
    print("=== Verifikasi Environment Context (project-local) ===")
    assert DUMMY_ROOT.exists() or True
    py_fixture = _make_fixture("env_py_", ["requirements.txt"])
    node_fixture = _make_fixture("env_node_", ["package.json"])
    try:
        # 1) Path benar (di dalam `.aether/`).
        expected_path = py_fixture / ".aether" / "ENVIRONMENT.md"
        assert environment_path(py_fixture) == expected_path
        assert AetherProjectStore(py_fixture).environment_path() == expected_path
        print(f"path        : {expected_path}")
        print("OK: path = <root>/.aether/ENVIRONMENT.md")

        # 2) Pembuatan file + struktur terdeteksi.
        text = build_or_load_environment(py_fixture)
        assert expected_path.exists(), "ENVIRONMENT.md tidak dibuat"
        assert all(marker in text for marker in MARKERS), "struktur konteks tidak lengkap"
        assert (py_fixture / ".aether").is_dir()
        print("OK: ENVIRONMENT.md dibuat dengan struktur os/shell/python/virtualenv/tools/project")

        # 3) Adaptif: berasal dari deteksi runtime (bukan hardcode).
        assert platform.system() in text, "OS tidak sesuai deteksi runtime"
        assert platform.python_version() in text, "versi Python tidak sesuai deteksi runtime"
        assert sys.executable and sys.executable in text, "executable Python tidak terdeteksi"
        assert str(py_fixture) in text, "root project tidak tercatat"
        print("OK: isi berasal dari deteksi runtime (OS/python/executable/root)")

        # 4) Adaptif terhadap tipe project (marker berbeda -> tipe berbeda).
        py_text = build_or_load_environment(py_fixture)
        node_text = build_or_load_environment(node_fixture)
        assert "detected_types: Python" in py_text, "requirements.txt seharusnya -> Python"
        assert "detected_types: Node.js" in node_text, "package.json seharusnya -> Node.js"
        print("OK: tipe project terdeteksi dari file penanda (Python vs Node.js)")

        # 5) Idempotent: file yang sudah ada di-LOAD, bukan ditimpa.
        sentinel = "# SENTINEL (jangan ditimpa)\n"
        expected_path.write_text(sentinel, encoding="utf-8")
        loaded = build_or_load_environment(py_fixture)
        assert loaded == sentinel, "file yang ada harus di-load, bukan ditimpa"
        print("OK: file existing di-load (tidak ditimpa)")

        # 6) Injection: Environment Context dikirim sebagai system message.
        expected_path.unlink()  # biar dibuat ulang oleh runtime
        provider = CapturingProvider()
        runtime = AgentRuntime(provider=provider, project_root=str(py_fixture), project_brain=False)
        result = runtime.run(PreparedTask(task="task-1"))
        assert result.status == RuntimeStatus.COMPLETED, f"runtime gagal: {result.error}"
        first_call = provider.calls[0]
        system_msgs = [m for m in first_call if m.get("role") == "system"]
        joined = "\n".join(m.get("content", "") for m in first_call)
        assert any("# Environment Context" in m.get("content", "") for m in system_msgs), \
            "Environment Context tidak dikirim sebagai system message"
        assert "task-1" in joined, "task tidak dikirim ke LLM"
        print(f"OK: system messages task-1 = {len(system_msgs)} (Environment Context terkirim)")

        # 7) Sekali per session: task kedua tidak memuat/mengirim ulang.
        counter = {"n": 0}
        original = env_mod.build_or_load_environment

        def counting(root):
            counter["n"] += 1
            return original(root)

        env_mod.build_or_load_environment = counting
        try:
            runtime2 = AgentRuntime(provider=provider, project_root=str(py_fixture), project_brain=False)
            runtime2.run(PreparedTask(task="task-a"))
            runtime2.run(PreparedTask(task="task-b"))
        finally:
            env_mod.build_or_load_environment = original
        assert counter["n"] == 1, f"Environment Context dimuat {counter['n']}x (harus 1x per session)"
        call_a = provider.calls[-2]
        call_b = provider.calls[-1]
        assert any("# Environment Context" in m.get("content", "") for m in call_a if m.get("role") == "system"), \
            "Environment Context tidak ada pada task pertama"
        assert not any("# Environment Context" in m.get("content", "") for m in call_b if m.get("role") == "system"), \
            "Environment Context terkirim ulang pada task kedua (harus sekali per session)"
        print("OK: sekali per session (task pertama kirim, task kedua tidak)")

        # 8) Best-effort: tanpa project_root tidak error & tidak mengirim context.
        provider3 = CapturingProvider()
        runtime3 = AgentRuntime(provider=provider3, project_root=None, project_brain=False)
        r3 = runtime3.run(PreparedTask(task="no-root"))
        assert r3.status == RuntimeStatus.COMPLETED
        assert not any(
            "# Environment Context" in m.get("content", "")
            for m in provider3.calls[0]
            if m.get("role") == "system"
        ), "tanpa project_root tidak boleh ada Environment Context"
        print("OK: tanpa project_root -> tidak ada Environment Context (best-effort)")

        # 9) Isolasi: fixture berada di dummy_test, bukan workspace AETHER.
        assert str(expected_path).startswith(str(DUMMY_ROOT)), "fixture harus di dummy_test"
        assert not (SRC_DIR / "agent_ai" / ".aether").exists(), "tidak boleh menulis .aether ke src"
        print(f"OK: terisolasi di {DUMMY_ROOT}")

        print("\n[OK] Environment Context bekerja (project-local, adaptif, sekali per session).")
        return 0
    finally:
        shutil.rmtree(py_fixture, ignore_errors=True)
        shutil.rmtree(node_fixture, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
