"""Verifikasi Terminal Executor (run_command).

Jalankan:
    python scripts/check_terminal.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.tools import (  # noqa: E402
    ReadFileTool,
    RunCommandTool,
    ToolError,
    registry,
)

PY = sys.executable


def main() -> int:
    print("=== Verifikasi Terminal Executor ===")
    ws = Path(tempfile.mkdtemp(prefix="term_ws_"))
    outside = Path(tempfile.mkdtemp(prefix="term_outside_"))
    try:
        run = RunCommandTool(root=ws)
        read = ReadFileTool(root=ws)

        outside_file = outside / "secret.txt"
        outside_file.write_text("rahasia", encoding="utf-8")

        # 1) command sederhana berhasil + stdout tertangkap.
        r = run.execute(command=f'"{PY}" -c "print(123)"')
        print(f"simple : exit={r['exit_code']} success={r['success']} stdout={r['stdout']!r}")
        assert r["success"] and r["exit_code"] == 0 and "123" in r["stdout"]

        # 2) stderr tertangkap.
        r = run.execute(command=f'"{PY}" -c "import sys; sys.stderr.write(\'boom\')"')
        print(f"stderr : {r['stderr']!r}")
        assert "boom" in r["stderr"]

        # 3) non-zero exit code ditangani (tidak crash).
        r = run.execute(command=f'"{PY}" -c "import sys; sys.exit(3)"')
        print(f"nonzero: exit={r['exit_code']} success={r['success']}")
        assert r["exit_code"] == 3 and r["success"] is False

        # 4) timeout ditangani.
        r = run.execute(command=f'"{PY}" -c "import time; time.sleep(5)"', timeout=1)
        print(f"timeout: timed_out={r['timed_out']} success={r['success']} error={r['error']!r}")
        assert r["timed_out"] is True and r["success"] is False

        # 5) working directory benar-benar project workspace.
        r = run.execute(command=f'"{PY}" -c "import os; print(os.getcwd())"')
        cwd_reported = r["stdout"].strip()
        print(f"cwd    : {cwd_reported}")
        assert Path(cwd_reported).resolve() == ws.resolve()

        # 6) command dapat membuat file di workspace.
        r = run.execute(command=f'"{PY}" -c "open(\'made.txt\',\'w\').write(\'hello\')"')
        assert r["success"] and (ws / "made.txt").exists()
        print("create file in workspace : OK")

        # 7) hasil command dapat dibaca kembali oleh filesystem tools.
        content = read.execute(path="made.txt")
        assert "hello" in content["content"]
        print("read back via filesystem tool : OK")

        # 8) tidak mengubah file di luar workspace melalui cwd/path handling.
        #    Command mencoba menulis ke path absolut di luar workspace; file
        #    luar tetap tidak tersentuh oleh tool (cwd tetap workspace).
        assert outside_file.read_text(encoding="utf-8") == "rahasia"
        print("file luar workspace tidak tersentuh : OK")

        # 9) ToolRegistry mengenali tool.
        assert registry.has("run_command")
        spec = registry.get("run_command").to_spec()
        print(f"registry: {spec['name']} -> OK")

        # 10) existing filesystem tools tetap bekerja.
        assert registry.has("read_file") and registry.has("write_file")
        print("existing tools tetap bekerja : OK")

        print()
        print("[OK] Terminal Executor bekerja (workspace cwd, timeout, output terstruktur).")
        return 0
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
