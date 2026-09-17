"""Verifikasi Project Brain Integration Layer.

Menguji:
    - Brain dapat membaca context.
    - Brain dapat melakukan learning melalui IntelligenceLearner.
    - Brain dapat add_verified.
    - duplicate tetap ditangani oleh Learning layer.
    - intelligence berubah hanya melalui API yang benar.
    - project source tidak disentuh.

Jalankan:
    python scripts/check_brain.py
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
    ProjectBrain,
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
    print("=== Verifikasi Project Brain ===")
    workspace = Path(tempfile.mkdtemp(prefix="brain_ws_"))
    source = Path(tempfile.mkdtemp(prefix="brain_src_"))
    try:
        # Dummy project source (harus tidak disentuh).
        (source / "main.py").write_text("print('x')\n", encoding="utf-8")
        source_before = sorted(str(p.relative_to(source)) for p in source.rglob("*"))

        intel = ProjectIntelligence(workspace / "proj")
        intel.create()
        intel.add_entry("facts", IntelligenceEntry(content="Knowledge lama", confidence=0.5))

        brain = ProjectBrain(intel, provider=FakeProvider(VALID_OUTPUT))

        # 1) Brain dapat membaca context.
        ctx = brain.get_context(categories=["facts"])
        print("--- context (facts) ---")
        print(ctx.text)
        assert "## facts" in ctx.text and "Knowledge lama" in ctx.text
        assert ctx.categories == {"facts": 1}
        print()

        # 2) Brain dapat melakukan learning melalui IntelligenceLearner.
        result = brain.learn(observations=["Agent membaca main.py"])
        print(f"learn added : {result.added}, total={result.total_added}")
        assert result.total_added == 4
        print()

        # 3) Brain dapat add_verified.
        added = brain.add_verified("facts", "Verified fact", confidence=0.95)
        print(f"add_verified : {added.content if added else None}")
        assert added is not None and added.content == "Verified fact"
        print()

        # 4) duplicate tetap ditangani oleh Learning layer.
        dup_learn = brain.learn(observations=["sama"])
        dup_verified = brain.add_verified("facts", "Verified fact", confidence=0.95)
        print(f"dup learn skipped={dup_learn.skipped_duplicates}, dup verified={dup_verified is None}")
        assert dup_learn.total_added == 0 and dup_learn.skipped_duplicates == 4
        assert dup_verified is None
        print()

        # 5) intelligence berubah hanya melalui API yang benar.
        facts = [e.content for e in intel.read_category("facts")]
        print(f"facts : {facts}")
        assert facts == ["Knowledge lama", "Python 3.11", "Verified fact"]
        # Membaca context tidak mengubah intelligence.
        before = {c: [e.to_dict() for e in intel.read_category(c)] for c in INTELLIGENCE_CATEGORIES}
        brain.get_context()
        after = {c: [e.to_dict() for e in intel.read_category(c)] for c in INTELLIGENCE_CATEGORIES}
        assert before == after, "get_context mengubah intelligence!"
        print("get_context read-only -> OK")
        print()

        # 6) project source tidak disentuh.
        source_after = sorted(str(p.relative_to(source)) for p in source.rglob("*"))
        print(f"source untouched : {source_before == source_after}")
        assert source_before == source_after, "brain menyentuh project source!"
        print()

        print("[OK] Project Brain bekerja (facade context + learning, aman).")
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(source, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
