"""Verifikasi Project Bible Generator.

Menggunakan dummy project + DiscoveryEngine + fake provider (tanpa API).

Menguji:
    - generator menghasilkan intelligence.
    - kategori tersimpan.
    - project source tidak berubah.
    - parsing ketat: output invalid -> BibleParseError.
    - intelligence lama tidak dihapus.

Jalankan:
    python scripts/check_bible.py
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
    BibleParseError,
    DiscoveryEngine,
    ProjectBibleGenerator,
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
  "architecture": [{"content": "Aplikasi Python dengan entry point main.py", "confidence": 0.8}],
  "facts": [{"content": "Menggunakan requests", "confidence": 0.9}],
  "decisions": [],
  "rules": [{"content": "Ikuti struktur src/", "confidence": 0.7}],
  "learnings": [],
  "problems": [{"content": "Belum ada test", "confidence": 0.6}]
}"""


def build_dummy_project(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")
    (root / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")


def main() -> int:
    print("=== Verifikasi Project Bible Generator ===")
    target = Path(tempfile.mkdtemp(prefix="bible_target_"))
    workspace = Path(tempfile.mkdtemp(prefix="bible_ws_"))
    try:
        build_dummy_project(target)
        before = sorted(str(p.relative_to(target)) for p in target.rglob("*"))

        discovery = DiscoveryEngine().discover(str(target), name="Demo")

        # 1) Generator menghasilkan intelligence.
        intel = ProjectIntelligence(workspace / "proj")
        gen = ProjectBibleGenerator(provider=FakeProvider(VALID_OUTPUT), intelligence=intel)
        result = gen.generate(discovery)
        print(f"total entries : {result.total}")
        print(f"categories    : {result.categories}")
        assert result.total == 4
        print()

        # 2) Kategori tersimpan.
        facts = intel.read_category("facts")
        rules = intel.read_category("rules")
        print(f"facts stored  : {[e.content for e in facts]}")
        print(f"rules stored  : {[e.content for e in rules]}")
        assert len(facts) == 1 and facts[0].content == "Menggunakan requests"
        assert len(rules) == 1
        assert intel.read_category("decisions") == []
        print()

        # 3) Project source tidak berubah.
        after = sorted(str(p.relative_to(target)) for p in target.rglob("*"))
        print(f"source unchanged : {before == after}")
        assert before == after, "generator mengubah project source!"
        print()

        # 4) Parsing ketat: output invalid -> BibleParseError.
        bad_gen = ProjectBibleGenerator(provider=FakeProvider("bukan json"), intelligence=intel)
        try:
            bad_gen.generate(discovery)
            print("[ERROR] output invalid seharusnya ditolak")
            return 1
        except BibleParseError as exc:
            print(f"invalid output ditolak OK -> {exc}")

        # Entry tanpa content juga ditolak.
        bad_entry = '{"facts": [{"confidence": 0.5}]}'
        try:
            ProjectBibleGenerator(provider=FakeProvider(bad_entry), intelligence=intel).generate(discovery)
            print("[ERROR] entry tanpa content seharusnya ditolak")
            return 1
        except BibleParseError as exc:
            print(f"entry tanpa content ditolak OK -> {exc}")
        print()

        # 5) Intelligence lama tidak dihapus (append).
        gen2 = ProjectBibleGenerator(provider=FakeProvider(VALID_OUTPUT), intelligence=intel)
        gen2.generate(discovery)
        facts_after = intel.read_category("facts")
        print(f"facts setelah generate kedua : {len(facts_after)} (append, bukan replace)")
        assert len(facts_after) == 2
        print()

        print("[OK] Project Bible Generator bekerja (parse ketat, simpan aman, source utuh).")
        return 0
    finally:
        shutil.rmtree(target, ignore_errors=True)
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
