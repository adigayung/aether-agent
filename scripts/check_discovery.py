"""Verifikasi Project Discovery Engine.

Membuat project dummy, menjalankan discovery, dan menguji:
    - discovery root.
    - directory tree.
    - file penting terdeteksi.
    - dependency/config files terdeteksi.
    - framework/language detection dasar.
    - README/config content terbatas dapat dikumpulkan.
    - directory yang di-ignore tidak ikut dipindai.
    - discovery tidak menulis file apa pun ke project target.
    - traversal keluar root ditolak.

Jalankan:
    python scripts/check_discovery.py
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

from agent_ai.projects import DiscoveryEngine  # noqa: E402
from agent_ai.tools.base import ToolValidationError  # noqa: E402


def build_dummy_project(root: Path) -> None:
    """Buat project dummy dengan struktur realistis."""
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (root / "src" / "utils.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "README.md").write_text("# Demo Project\n\nIni project demo.\n", encoding="utf-8")
    (root / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (root / "manage.py").write_text("# django entry\n", encoding="utf-8")
    (root / "package.json").write_text('{"name": "demo"}\n', encoding="utf-8")
    # Directory yang harus di-ignore.
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("secret\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "lib.js").write_text("x\n", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "main.cpython-311.pyc").write_text("binary\n", encoding="utf-8")
    # File binary (tidak boleh dibaca isinya).
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")


def main() -> int:
    print("=== Verifikasi Project Discovery Engine ===")
    target = Path(tempfile.mkdtemp(prefix="discovery_target_"))
    try:
        build_dummy_project(target)
        before = sorted(str(p.relative_to(target)) for p in target.rglob("*"))

        engine = DiscoveryEngine()
        result = engine.discover(str(target), name="Demo")
        d = result.to_dict()

        # 1) Discovery root.
        print(f"root        : {d['root']}")
        assert d["root"] == str(target.resolve())
        print()

        # 2) Directory tree.
        tree_paths = [e["path"] for e in d["directory_tree"]]
        print(f"tree entries: {len(tree_paths)}")
        assert "src" in tree_paths and "src/main.py" in tree_paths
        print()

        # 3) File penting terdeteksi.
        print(f"important   : {d['important_files']}")
        assert "README.md" in d["important_files"]
        print()

        # 4) Dependency/config files terdeteksi.
        print(f"dependency  : {d['dependency_files']}")
        print(f"config      : {d['config_files']}")
        assert "requirements.txt" in d["dependency_files"]
        assert "package.json" in d["dependency_files"]
        assert "pyproject.toml" in d["config_files"]
        print()

        # 5) Framework/language detection dasar.
        print(f"languages   : {d['languages']}")
        print(f"frameworks  : {d['frameworks']}")
        assert "Python" in d["languages"]
        assert "Django" in d["frameworks"]  # dari manage.py
        print()

        # 6) README/config content terbatas dapat dikumpulkan.
        print(f"readme path : {d['readme']['path'] if d['readme'] else None}")
        assert d["readme"] and "Demo Project" in d["readme"]["content"]
        assert "requirements.txt" in d["file_contents"]
        print()

        # 7) Directory yang di-ignore tidak ikut dipindai.
        joined = " ".join(tree_paths)
        for ignored in (".git", "node_modules", "__pycache__"):
            assert ignored not in joined, f"{ignored} ikut dipindai!"
        assert "logo.png" not in d["file_contents"], "file binary dibaca!"
        print("ignored dirs & binary tidak dipindai -> OK")
        print()

        # 8) Discovery tidak menulis file apa pun ke project target.
        after = sorted(str(p.relative_to(target)) for p in target.rglob("*"))
        print(f"target unchanged : {before == after}")
        assert before == after, "discovery mengubah project target!"
        print()

        # 9) Traversal keluar root ditolak.
        try:
            DiscoveryEngine.resolve_within_root("..\\..\\Windows\\win.ini", str(target))
            print("[ERROR] traversal seharusnya ditolak")
            return 1
        except ToolValidationError as exc:
            print(f"traversal ditolak OK -> {exc}")
        print()

        print("[OK] Project Discovery Engine bekerja (read-only, bounded, deterministic).")
        return 0
    finally:
        shutil.rmtree(target, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
