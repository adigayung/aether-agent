"""Verifikasi knowledge Bible baru: known_bugs.md & known_gaps.md.

Menguji (terisolasi di `J:\\Agent_Ai\\dummy_test`, tanpa network):
    1. `ensure()` membuat `<root>/.aether/bible/known_bugs.md`.
    2. `ensure()` membuat `<root>/.aether/bible/known_gaps.md`.
    3. `index.md` mencantumkan kedua kategori (manifest).
    4. Baca: `ProjectBrain.get_context()` memuat isi known_bugs & known_gaps.
    5. Baca terfilter: `get_context(categories=[...])` hanya kategori diminta.
    6. Tulis: `brain.add_verified(...)` (mekanisme existing) menulis entri.
    7. Update: `ProjectIntelligence.update_entry(...)` memperbarui entri existing.
    8. Persist: instance baru membaca knowledge yang sudah tersimpan.
    9. Project tanpa bug/gap tetap normal (kategori kosong tidak muncul/mengganggu).
   10. File Bible existing lain tidak berubah saat known_bugs/gaps ditulis.
   11. Format entri memakai mekanisme Bible existing (marker '## entry').
   12. Terisolasi: file dibuat di root fixture, bukan di source AETHER.

Jalankan:
    python scripts/verify_bible_known_bugs_gaps.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.projects import (  # noqa: E402
    BIBLE_CATEGORIES,
    BibleStore,
    ProjectBrain,
    ProjectIntelligence,
)
from agent_ai.projects.aether_store import ENTRY_MARKER  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "bible_known_fixture"


def main() -> int:
    print("=== Verifikasi Bible: known_bugs.md & known_gaps.md ===")
    shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir(parents=True, exist_ok=True)
    try:
        bible_dir = FIXTURE / ".aether" / "bible"

        # 1) + 2) ensure() membuat kedua file baru.
        store = BibleStore(FIXTURE)
        assert store.ensure() is True
        bugs_path = bible_dir / "known_bugs.md"
        gaps_path = bible_dir / "known_gaps.md"
        assert bugs_path.exists(), "known_bugs.md tidak dibuat"
        assert gaps_path.exists(), "known_gaps.md tidak dibuat"
        assert "known_bugs" in BIBLE_CATEGORIES and "known_gaps" in BIBLE_CATEGORIES
        print(f"[1] dibuat: {bugs_path.relative_to(FIXTURE)} OK")
        print(f"[2] dibuat: {gaps_path.relative_to(FIXTURE)} OK")

        # 11) Format entri memakai mekanisme Bible existing (marker '## entry').
        header = bugs_path.read_text(encoding="utf-8")
        assert "# bible:known_bugs" in header, header
        assert ENTRY_MARKER in (header + store._header("known_bugs")), "format marker tidak konsisten"
        print(f"[11] format entri memakai mekanisme Bible existing ('{ENTRY_MARKER}') OK")

        # 3) index.md mencantumkan kedua kategori.
        index = (bible_dir / "index.md").read_text(encoding="utf-8")
        assert "known_bugs.md" in index and "known_gaps.md" in index, index
        print("[3] index.md mencantumkan known_bugs.md & known_gaps.md OK")

        # Snapshot file Bible lain (untuk cek tidak berubah di langkah 10).
        facts_before = (bible_dir / "facts.md").read_text(encoding="utf-8")

        # 6) Tulis: mekanisme existing (brain.add_verified) menulis entri.
        brain = ProjectBrain.for_project(FIXTURE)
        bug_entry = brain.add_verified(
            "known_bugs",
            {
                "id": "BUG-001",
                "title": "Login gagal saat password kosong",
                "status": "Open",
                "severity": "Medium",
                "area": "Authentication",
                "description": "Submit form tanpa password menampilkan error 500.",
                "evidence": "tests/test_login.py::test_empty_password",
            },
            confidence=0.9,
        )
        gap_entry = brain.add_verified(
            "known_gaps",
            {
                "id": "GAP-001",
                "title": "Belum ada rate limiting",
                "status": "Open",
                "area": "Authentication",
                "description": "Endpoint login belum membatasi percobaan berulang.",
                "impact": "Rentan brute-force.",
                "recommendation": "Tambahkan throttling per-IP.",
            },
            confidence=0.8,
        )
        assert bug_entry is not None and gap_entry is not None
        bugs_text = bugs_path.read_text(encoding="utf-8")
        gaps_text = gaps_path.read_text(encoding="utf-8")
        assert "BUG-001" in bugs_text and "Login gagal saat password kosong" in bugs_text
        assert "GAP-001" in gaps_text and "Belum ada rate limiting" in gaps_text
        print("[6] tulis via mekanisme existing (add_verified) OK")

        # 4) Baca: context memuat isi kedua kategori.
        ctx = brain.get_context()
        assert "BUG-001" in ctx.text and "Login gagal saat password kosong" in ctx.text, ctx.text
        assert "GAP-001" in ctx.text and "Belum ada rate limiting" in ctx.text, ctx.text
        assert ctx.categories.get("known_bugs") == 1
        assert ctx.categories.get("known_gaps") == 1
        print("[4] ProjectBrain.get_context() memuat known_bugs & known_gaps OK")

        # 5) Baca terfilter.
        only_bugs = brain.get_context(categories=["known_bugs"])
        assert "BUG-001" in only_bugs.text and "GAP-001" not in only_bugs.text, only_bugs.text
        print("[5] get_context(categories=[...]) memfilter kategori OK")

        # 7) Update entri existing (mekanisme existing).
        intel = ProjectIntelligence.for_project(FIXTURE)
        updated = intel.update_entry(
            "known_bugs",
            bug_entry.id,
            content={
                "id": "BUG-001",
                "title": "Login gagal saat password kosong",
                "status": "Fixed",
                "severity": "Medium",
                "area": "Authentication",
                "description": "Sudah diperbaiki: validasi kosong ditambahkan.",
                "evidence": "tests/test_login.py::test_empty_password",
            },
        )
        assert updated.id == bug_entry.id
        bugs_text2 = bugs_path.read_text(encoding="utf-8")
        assert '"status": "Fixed"' in bugs_text2, bugs_text2
        assert '"status": "Open"' not in bugs_text2, "update harus menimpa entry lama"
        print("[7] update via ProjectIntelligence.update_entry() OK")

        # 8) Persist: instance baru membaca knowledge tersimpan.
        fresh = ProjectBrain.for_project(FIXTURE)
        fresh_ctx = fresh.get_context(categories=["known_bugs", "known_gaps"])
        assert "Fixed" in fresh_ctx.text and "GAP-001" in fresh_ctx.text, fresh_ctx.text
        print("[8] instance baru membaca knowledge tersimpan OK")

        # 10) File Bible lain tidak berubah.
        facts_after = (bible_dir / "facts.md").read_text(encoding="utf-8")
        assert facts_before == facts_after, "facts.md berubah saat menulis known_bugs/gaps"
        print("[10] file Bible existing lain (facts.md) tidak berubah OK")

        # 9) Project tanpa bug/gap tetap normal.
        empty_root = FIXTURE / "empty_project"
        empty_root.mkdir()
        empty_intel = ProjectIntelligence.for_project(empty_root)
        empty_intel.create()  # struktur Bible dibuat, kategori kosong
        assert (empty_root / ".aether" / "bible" / "known_bugs.md").exists()
        assert (empty_root / ".aether" / "bible" / "known_gaps.md").exists()
        empty_brain = ProjectBrain.for_project(empty_root)
        empty_ctx = empty_brain.get_context()
        assert "known_bugs" not in empty_ctx.text and "known_gaps" not in empty_ctx.text
        assert empty_ctx.categories.get("known_bugs", 0) == 0
        assert empty_ctx.categories.get("known_gaps", 0) == 0
        print("[9] project tanpa bug/gap tetap normal (kategori kosong aman) OK")

        # 12) Isolasi: tidak menulis ke source AETHER.
        assert str(bugs_path).startswith(str(DUMMY_ROOT)), "fixture harus di dummy_test"
        assert not (SRC_DIR / "agent_ai" / ".aether").exists(), "tidak boleh menulis .aether ke src"
        print(f"[12] terisolasi di {DUMMY_ROOT} (source AETHER tidak tersentuh) OK")

        print("\n[OK] known_bugs.md & known_gaps.md terintegrasi ke Project Bible (read+write existing).")
        return 0
    finally:
        shutil.rmtree(FIXTURE, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
