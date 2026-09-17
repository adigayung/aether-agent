"""Verifikasi Project Intelligence Reader / Context Provider.

Menguji:
    - context hanya berisi kategori yang diminta.
    - semua kategori bekerja jika filtering tidak diberikan.
    - intelligence tidak berubah.
    - project source tidak disentuh.

Jalankan:
    python scripts/check_intelligence_context.py
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
    INTELLIGENCE_CATEGORIES,
    IntelligenceEntry,
    ProjectIntelligence,
    ProjectIntelligenceContext,
)


def main() -> int:
    print("=== Verifikasi Project Intelligence Context ===")
    workspace = Path(tempfile.mkdtemp(prefix="ctx_ws_"))
    source = Path(tempfile.mkdtemp(prefix="ctx_src_"))
    try:
        # Dummy project source (harus tidak disentuh).
        (source / "main.py").write_text("print('x')\n", encoding="utf-8")
        source_before = sorted(str(p.relative_to(source)) for p in source.rglob("*"))

        # Dummy intelligence dengan beberapa kategori.
        intel = ProjectIntelligence(workspace / "proj")
        intel.create()
        intel.add_entry("architecture", IntelligenceEntry(content="Layered: core/providers/tools", confidence=0.8))
        intel.add_entry("facts", IntelligenceEntry(content="Python 3.11", confidence=0.9))
        intel.add_entry("facts", IntelligenceEntry(content="Menggunakan requests", confidence=0.9))
        intel.add_entry("rules", IntelligenceEntry(content="Ikuti struktur src/", confidence=0.7))
        intel.add_entry("problems", IntelligenceEntry(content="Belum ada test", confidence=0.6))

        # Snapshot intelligence sebelum dibaca.
        intel_before = {c: [e.to_dict() for e in intel.read_category(c)] for c in INTELLIGENCE_CATEGORIES}

        ctx = ProjectIntelligenceContext(intel)

        # 1) Context hanya berisi kategori yang diminta.
        text = ctx.to_context_text(categories=["facts", "rules"])
        print("--- context (facts, rules) ---")
        print(text)
        assert "## facts" in text and "## rules" in text
        assert "## architecture" not in text and "## problems" not in text
        assert "Python 3.11" in text and "Ikuti struktur src/" in text
        print()

        # 2) Semua kategori bekerja jika filtering tidak diberikan.
        full = ctx.to_context_text()
        print("--- context (semua kategori) ---")
        print(full)
        assert "## architecture" in full and "## facts" in full and "## rules" in full
        assert "## problems" in full
        # Kategori kosong (decisions/learnings) tidak disertakan secara default.
        assert "## decisions" not in full and "## learnings" not in full
        print()

        # 3) read() mengembalikan content saja (tanpa metadata internal).
        data = ctx.read(categories=["facts"])
        print(f"read facts : {data['facts']}")
        assert data["facts"] == ["Python 3.11", "Menggunakan requests"]
        print()

        # 4) Kategori tidak dikenal -> error jelas.
        try:
            ctx.to_context_text(categories=["unknown_cat"])
            print("[ERROR] kategori tidak dikenal seharusnya ditolak")
            return 1
        except ValueError as exc:
            print(f"kategori tidak dikenal ditolak OK -> {exc}")
        print()

        # 5) Intelligence tidak berubah.
        intel_after = {c: [e.to_dict() for e in intel.read_category(c)] for c in INTELLIGENCE_CATEGORIES}
        print(f"intelligence unchanged : {intel_before == intel_after}")
        assert intel_before == intel_after, "context mengubah intelligence!"
        print()

        # 6) Project source tidak disentuh.
        source_after = sorted(str(p.relative_to(source)) for p in source.rglob("*"))
        print(f"source untouched : {source_before == source_after}")
        assert source_before == source_after, "context menyentuh project source!"
        print()

        print("[OK] Project Intelligence Context bekerja (read-only, deterministic, provider-agnostic).")
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(source, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
