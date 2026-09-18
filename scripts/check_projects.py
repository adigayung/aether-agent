"""Verifikasi Project Intelligence / AI Project Bible.

Menguji:
    - register project (name + absolute root).
    - project.json tersimpan di workspace Agent-Ai.
    - AI Project Bible (`.aether/bible`) dibuat project-local di root target.
    - TIDAK ada storage intelligence kedua di workspace Agent-Ai.
    - add/read/update intelligence entry (Bible project-local).
    - project dapat dimuat kembali (simulasi proses baru).
    - project.json/intelligence legacy TIDAK ditulis ke root project target.

Jalankan:
    python scripts/check_projects.py
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

from agent_ai.projects import (  # noqa: E402
    BIBLE_CATEGORIES,
    IntelligenceEntry,
    ProjectRegistry,
)


def main() -> int:
    print("=== Verifikasi Project Intelligence ===")

    # Workspace Agent-Ai sementara (di dalam repo, folder projects/).
    workspace = PROJECT_ROOT / "projects"
    # Root project target sementara (di luar workspace Agent-Ai).
    target = Path(tempfile.mkdtemp(prefix="target_project_"))

    try:
        reg = ProjectRegistry(workspace=workspace)

        # 1) Register project.
        config = reg.register(name="DemoApp", root=str(target))
        print(f"registered  : id={config.id} name={config.name}")
        print(f"root        : {config.root}")
        print(f"permission  : {config.permission_mode}")
        assert config.permission_mode == "workspace"
        print()

        # 2) project.json tersimpan di Agent-Ai.
        project_json = workspace / config.id / "project.json"
        print(f"project.json exists in Agent-Ai : {project_json.exists()}")
        assert project_json.exists()
        assert str(project_json).startswith(str(workspace))
        print()

        # 3) AI Project Bible dibuat project-local di root target.
        bible_dir = target / ".aether" / "bible"
        created = sorted(p.name for p in bible_dir.glob("*.md"))
        print(f"bible files : {created}  (dir: {bible_dir})")
        assert (bible_dir / "index.md").exists(), "missing index.md"
        for category in BIBLE_CATEGORIES:
            assert (bible_dir / f"{category}.md").exists(), f"missing {category}.md"
        # TIDAK ada storage intelligence kedua di workspace Agent-Ai.
        assert not (workspace / config.id / "intelligence").exists()
        print()

        # 4) Root project target hanya berisi .aether (Bible), tidak ada
        #    project.json / intelligence legacy.
        target_entries = sorted(p.name for p in target.iterdir())
        print(f"target root entries : {target_entries}")
        assert target_entries == [".aether"], target_entries
        assert not (target / "project.json").exists()
        assert not (target / "intelligence").exists()
        print()

        # 5) add/read/update intelligence entry (Bible project-local).
        intel = reg.intelligence(config.id)
        entry = intel.add_entry(
            "facts",
            IntelligenceEntry(content="Project memakai Python 3.11", source="ai", confidence=0.9),
        )
        print(f"added entry : id={entry.id} content={entry.content!r}")

        read_back = intel.read_category("facts")
        print(f"read facts  : {len(read_back)} entry")
        assert len(read_back) == 1 and read_back[0].content == "Project memakai Python 3.11"
        assert (bible_dir / "facts.md").exists()
        assert "Project memakai Python 3.11" in (bible_dir / "facts.md").read_text(encoding="utf-8")
        # Alias kategori lama ("rules") memetakan ke "conventions".
        intel.add_entry("rules", IntelligenceEntry(content="Ikuti struktur src/", confidence=0.7))
        assert (bible_dir / "conventions.md").exists()
        assert intel.read_category("rules")[-1].content == "Ikuti struktur src/"

        updated = intel.update_entry("facts", entry.id, content="Project memakai Python 3.12", confidence=0.95)
        print(f"updated     : content={updated.content!r} confidence={updated.confidence}")
        assert updated.content == "Project memakai Python 3.12"
        assert updated.confidence == 0.95
        print()

        # 6) Project dapat dimuat kembali (simulasi proses baru).
        reg2 = ProjectRegistry(workspace=workspace)
        loaded = reg2.get(config.id)
        print(f"reloaded    : id={loaded.id} name={loaded.name}")
        assert loaded.id == config.id
        intel2 = reg2.intelligence(config.id)
        assert intel2.read_category("facts")[0].content == "Project memakai Python 3.12"
        print("reload setelah proses baru -> OK")
        print()

        # 7) Root project target tidak mendapat project.json/intelligence legacy.
        assert not (target / "intelligence").exists()
        assert not (target / "project.json").exists()
        print("target root bersih (hanya .aether Bible, tanpa project.json/intelligence) -> OK")
        print()

        print("[OK] Project Intelligence (registry, Bible storage, add/read/update, reload) bekerja.")
        return 0
    finally:
        # Bersihkan artefak verifikasi.
        shutil.rmtree(target, ignore_errors=True)
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
