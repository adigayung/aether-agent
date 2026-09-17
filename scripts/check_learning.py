"""Verifikasi Intelligence Learner / Writer.

Menguji:
    - fake provider menghasilkan beberapa knowledge.
    - knowledge tersimpan ke kategori yang benar.
    - duplicate tidak ditambahkan.
    - invalid JSON ditolak.
    - confidence invalid ditolak.
    - kategori invalid ditolak.
    - intelligence lama tetap ada.
    - project source tidak disentuh.

Jalankan:
    python scripts/check_learning.py
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
    IntelligenceLearner,
    LearningError,
    LearningParseError,
    ProjectIntelligence,
)
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402


class FakeProvider(BaseProvider):
    """Provider palsu: mengembalikan teks terprogram (tanpa API)."""

    name = "fake"

    def __init__(self, text: str):
        self._text = text

    def generate(self, prompt=None, messages=None, options=None):
        return GenerateResult(text=self._text, model="fake-model", provider=self.name, raw={})


VALID_OUTPUT = """{
  "architecture": [{"content": "Layered core/providers/tools", "confidence": 0.8}],
  "facts": [{"content": "Python 3.11", "confidence": 0.9}],
  "decisions": [],
  "rules": [{"content": "Ikuti struktur src/", "confidence": 0.7}],
  "learnings": [],
  "problems": [{"content": "Belum ada test", "confidence": 0.6}]
}"""


def main() -> int:
    print("=== Verifikasi Intelligence Learner ===")
    workspace = Path(tempfile.mkdtemp(prefix="learn_ws_"))
    source = Path(tempfile.mkdtemp(prefix="learn_src_"))
    try:
        # Dummy project source (harus tidak disentuh).
        (source / "main.py").write_text("print('x')\n", encoding="utf-8")
        source_before = sorted(str(p.relative_to(source)) for p in source.rglob("*"))

        intel = ProjectIntelligence(workspace / "proj")
        intel.create()
        # Knowledge lama yang harus tetap ada.
        intel.add_entry("facts", IntelligenceEntry(content="Knowledge lama", confidence=0.5))

        learner = IntelligenceLearner(intel, provider=FakeProvider(VALID_OUTPUT))

        # 1) fake provider menghasilkan beberapa knowledge -> tersimpan.
        result = learner.learn(observations=["Agent membaca main.py", "Project pakai Python"])
        print(f"added         : {result.added}")
        print(f"total_added   : {result.total_added}")
        assert result.total_added == 4
        print()

        # 2) knowledge tersimpan ke kategori yang benar.
        assert intel.read_category("facts")[-1].content == "Python 3.11"
        assert intel.read_category("rules")[-1].content == "Ikuti struktur src/"
        assert intel.read_category("problems")[-1].content == "Belum ada test"
        print("kategori benar -> OK")
        print()

        # 3) duplicate tidak ditambahkan.
        result2 = learner.learn(observations=["sama"])
        print(f"skipped_duplicates : {result2.skipped_duplicates}")
        assert result2.total_added == 0
        assert result2.skipped_duplicates == 4
        print()

        # 4) invalid JSON ditolak.
        bad = IntelligenceLearner(intel, provider=FakeProvider("bukan json"))
        try:
            bad.learn(observations=["x"])
            print("[ERROR] invalid JSON seharusnya ditolak")
            return 1
        except LearningParseError as exc:
            print(f"invalid JSON ditolak OK -> {exc}")

        # 5) confidence invalid ditolak.
        bad_conf = '{"facts": [{"content": "x", "confidence": 2.0}]}'
        try:
            IntelligenceLearner(intel, provider=FakeProvider(bad_conf)).learn(observations=["x"])
            print("[ERROR] confidence invalid seharusnya ditolak")
            return 1
        except LearningParseError as exc:
            print(f"confidence invalid ditolak OK -> {exc}")
        print()

        # 6) kategori invalid ditolak (add_verified).
        try:
            learner.add_verified("unknown_cat", "x")
            print("[ERROR] kategori invalid seharusnya ditolak")
            return 1
        except LearningError as exc:
            print(f"kategori invalid ditolak OK -> {exc}")

        # add_verified: tambah baru + skip duplicate.
        added = learner.add_verified("facts", "Verified fact", confidence=0.95)
        dup = learner.add_verified("facts", "Verified fact", confidence=0.95)
        print(f"add_verified added={added is not None}, duplicate={dup is None}")
        assert added is not None and dup is None
        print()

        # 7) intelligence lama tetap ada.
        facts = [e.content for e in intel.read_category("facts")]
        print(f"facts : {facts}")
        assert "Knowledge lama" in facts
        print()

        # 8) project source tidak disentuh.
        source_after = sorted(str(p.relative_to(source)) for p in source.rglob("*"))
        print(f"source untouched : {source_before == source_after}")
        assert source_before == source_after, "learner menyentuh project source!"
        print()

        print("[OK] Intelligence Learner bekerja (validasi ketat, dedup, add aman).")
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(source, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
