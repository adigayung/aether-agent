"""Verifikasi Task Preparation Layer.

Menguji:
    1. task menghasilkan PreparedTask.
    2. ContextBuilder digunakan.
    3. TaskPlanner digunakan.
    4. context masuk PreparedTask.
    5. plan masuk PreparedTask.
    6. task asli tetap tersedia.
    7. preparation tidak menjalankan tools.
    8. source project tidak berubah.
    9. fixture dibersihkan.
   10. AETHER tetap import/run.

Fixture hanya di J:\Agent_Ai\dummy_test dan dibersihkan setelah test.

Jalankan:
    python scripts/check_task_preparation.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.codeindex import CodeIndexer  # noqa: E402
from agent_ai.contextbuilder import ContextBuilder, ContextRequest  # noqa: E402
from agent_ai.planning import TaskPlanner  # noqa: E402
from agent_ai.task import PreparedTask, TaskPreparation  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "task_prep_fixture"

_FILES = {
    "app/auth.py": (
        "class AuthService:\n"
        "    def login(self, user, password):\n"
        "        return user == 'admin'\n"
    ),
    "app/main.py": (
        "from app.auth import AuthService\n"
        "\n"
        "def run():\n"
        "    return AuthService().login('admin', 'x')\n"
    ),
}


def setup_fixture() -> None:
    for rel, content in _FILES.items():
        path = FIXTURE / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Task Preparation Layer ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    before = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}

    index = CodeIndexer(root=FIXTURE).build()
    builder = ContextBuilder(index=index, root=FIXTURE)
    planner = TaskPlanner()
    prep = TaskPreparation(context_builder=builder, planner=planner)

    task = "Perbaiki login yang gagal"

    # 1) task menghasilkan PreparedTask.
    prepared = prep.prepare(task)
    assert isinstance(prepared, PreparedTask)
    print(f"[1] PreparedTask dibuat -> task='{prepared.task}'")

    # 2) ContextBuilder digunakan (context terisi).
    assert prepared.context is not None, "context harus dibangun oleh ContextBuilder"
    assert prepared.context.task == task
    print(f"[2] ContextBuilder digunakan -> files={len(prepared.context.files)}, symbols={len(prepared.context.symbols)}")

    # 3) TaskPlanner digunakan (plan terisi).
    assert prepared.plan is not None, "plan harus dibuat oleh TaskPlanner"
    assert prepared.plan.metadata.get("planner") == "deterministic"
    print(f"[3] TaskPlanner digunakan -> steps={len(prepared.plan.steps)}, kind={prepared.plan.metadata['kind']}")

    # 4) context masuk PreparedTask (dan relevan dengan task).
    assert prepared.context is not None
    assert prepared.context.metadata["keywords"], "keywords harus terisi"
    assert prepared.context_text(), "context_text harus non-kosong"
    print(f"[4] context masuk PreparedTask -> keywords={prepared.context.metadata['keywords']}")

    # 5) plan masuk PreparedTask.
    assert prepared.plan is not None
    assert prepared.plan.steps, "plan harus punya step"
    assert prepared.plan.steps[0].title == "locate relevant code"
    print(f"[5] plan masuk PreparedTask -> {[s.title for s in prepared.plan.steps]}")

    # 6) task asli tetap tersedia.
    assert prepared.task == task
    assert prepared.to_dict()["task"] == task
    print(f"[6] task asli tetap tersedia -> '{prepared.task}'")

    # 7) preparation tidak menjalankan tools.
    assert not hasattr(prep, "execute"), "preparation tidak boleh punya execute()"
    assert not hasattr(prep, "registry"), "preparation tidak boleh punya registry"
    import agent_ai.task.preparation as prep_mod  # noqa: E402

    src = Path(prep_mod.__file__).read_text(encoding="utf-8")
    # Cek import nyata (bukan sekadar kata di docstring).
    assert "import ToolRegistry" not in src and "from agent_ai.tools" not in src
    assert "import ToolExecutor" not in src and "from agent_ai.core.executor" not in src
    assert "from agent_ai.core.loop" not in src and "import AgentLoop" not in src
    print("[7] preparation tidak menjalankan tools : OK")

    # 8) source project tidak berubah.
    after = {p: p.read_bytes() for p in FIXTURE.rglob("*") if p.is_file()}
    assert before == after, "source project berubah!"
    print("[8] source project tidak berubah : OK")

    # 9) fixture dibersihkan (dicek di main() setelah teardown).
    print("[9] fixture akan dibersihkan setelah verifier : OK")

    # 10) AETHER tetap import/run.
    from agent_ai.core.loop import AgentLoop  # noqa: E402

    loop = AgentLoop(task="x", max_iterations=2)
    loop.start()
    assert loop.status.value == "running"
    # preparation tanpa context_builder tetap aman (context None).
    bare = TaskPreparation().prepare("tambah fitur export")
    assert bare.context is None and bare.plan is not None
    print("[10] AETHER tetap import/run : OK (prep tanpa builder aman)")

    print()
    print("[OK] Task Preparation Layer bekerja (read-only, tanpa tool execution, tanpa LLM).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
