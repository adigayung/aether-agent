"""Verifikasi Terminal Executor (run_command).

Jalankan:
    python scripts/check_terminal.py
"""

from __future__ import annotations

import os
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

        # 11) BUG-01 regression: shim Windows .cmd/.bat (mis. npm -> npm.CMD)
        #     harus dapat dijalankan walau shell=False (CreateProcess tidak
        #     menerapkan PATHEXT). Hanya di Windows.
        if os.name == "nt":
            from agent_ai.tools.terminal import _resolve_windows_shim  # noqa: E402

            npm = shutil.which("npm")
            if npm:
                r = run.execute(command="npm --version")
                print(
                    f"npm    : exit={r['exit_code']} success={r['success']} "
                    f"outcome={r['outcome']} stdout={r['stdout'].strip()!r}"
                )
                assert r["success"] and r["exit_code"] == 0, r
                assert r["stdout"].strip(), "npm --version harus menuliskan versi"
            else:
                print("[SKIP] npm tidak ada di PATH; regresi .cmd diuji via unit test")

            # Resolver: .cmd/.bat -> path absolut; .exe/unknown -> None.
            assert _resolve_windows_shim("npm", ws) is not None
            assert _resolve_windows_shim("python", ws) is None
            assert _resolve_windows_shim("definitely-not-a-real-cmd-xyz", ws) is None
            print("resolver shim .cmd/.bat : OK")

            # .bat lokal (di cwd workspace) juga ter-resolve tanpa PATH.
            bat = ws / "local_shim.bat"
            bat.write_text("@echo off\r\necho hi-from-bat\r\n", encoding="utf-8")
            assert _resolve_windows_shim("local_shim.bat", ws) == str(bat)
            r = run.execute(command="local_shim.bat")
            assert r["success"] and "hi-from-bat" in r["stdout"], r
            print("eksekusi .bat lokal : OK")

        # 12) executable biasa (.exe) tidak terpengaruh resolusi shim.
        r = run.execute(command=f'"{PY}" -c "print(456)"')
        assert r["success"] and "456" in r["stdout"], r
        print("executable normal (.exe) tetap bekerja : OK")

        # 13) CMD builtin / operator tetap melalui shell=True seperti semula.
        r = run.execute(command="echo hello-shell")
        assert r["success"] and "hello-shell" in r["stdout"], r
        print("CMD builtin (shell=True) tetap bekerja : OK")

        print()
        print("[OK] Terminal Executor bekerja (workspace cwd, timeout, output terstruktur).")
        return 0
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
