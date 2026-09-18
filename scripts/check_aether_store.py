"""Verifikasi project-local `.aether` store (Task Log + AI Project Bible).

Task 5: setiap project yang dikerjakan AETHER punya `<project>/.aether/`:
    - `.aether/log/<task_id>.log`  -> log task (JSON Lines)
    - `.aether/bible/*.md`         -> AI Project Bible (project-local)

Verifier deterministik (tanpa API/model cloud). Fixture dibuat di
J:\\Agent_Ai\\dummy_test\\aether_store_fixture dan dibersihkan setelah test.

Menguji:
    1. project tanpa .aether -> otomatis dibuat (Bible + log).
    2. execution tanpa task_id -> task_id dibuat dan log memakai id tsb.
    3. execution dengan task_id -> memakai ID tersebut.
    4. log tersimpan di <project>/.aether/log/<task_id>.log.
    5. Bible dibuat pertama kali (index + kategori).
    6. ProjectBrain.get_context() dapat membaca Bible.
    7. knowledge baru dapat memperbarui Bible (via learn).
    8. project berikutnya membaca knowledge dari task sebelumnya.
    9. kegagalan update Bible tidak merusak execution.
   10. log task memuat prompt, provider/model, tool/validation, status akhir.
   11. log tidak membocorkan secret.
   12. tidak ada storage intelligence kedua (Bible = source of truth).

Jalankan:
    python scripts/check_aether_store.py
"""

from __future__ import annotations

import json
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
    TaskLog,
)
from agent_ai.projects.aether_store import AetherProjectStore  # noqa: E402
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "aether_store_fixture"

BIBLE_JSON = json.dumps(
    {
        "architecture": [],
        "ui": [{"content": "Workbench adalah layar utama", "confidence": 0.8}],
        "conventions": [{"content": "Gunakan folder src/", "confidence": 0.7}],
        "decisions": [],
        "facts": [{"content": "Bible fact hasil learning", "confidence": 0.9}],
        "learnings": [],
        "problems": [],
    }
)


class BibleProvider(BaseProvider):
    """Provider palsu: selalu mengembalikan JSON Bible (tanpa API)."""

    name = "fake-bible"

    def is_available(self) -> bool:
        return True

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        return GenerateResult(text=BIBLE_JSON, model="fake-bible-1", provider=self.name, raw={})


def _run_task(fixture: Path, task_text: str, task_id=None, provider=None):
    """Jalankan AgentRuntime pada fixture dengan provider sederhana."""
    from agent_ai.core.response import FinishReason, LLMResponse
    from agent_ai.runtime.runtime import AgentRuntime
    from agent_ai.task.models import PreparedTask

    class TaskProvider(BaseProvider):
        name = "fake-task"

        def __init__(self):
            self.calls = 0

        def is_available(self):
            return True

        def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
            self.calls += 1
            # Panggilan ke-1 = task; panggilan berikutnya = learning (gagal).
            if self.calls > 1:
                raise RuntimeError("provider down saat learning")
            return GenerateResult(text="selesai", model="fake-task-1", provider=self.name)

        def normalize_response(self, result):
            return LLMResponse(
                text=result.text or "selesai",
                actions=[],
                finish_reason=FinishReason.STOP,
                provider=self.name,
                model=result.model,
            )

    runtime = AgentRuntime(
        provider=provider or TaskProvider(),
        project_root=str(fixture),
    )
    prepared = PreparedTask(task=task_text, task_id=task_id)
    return runtime, runtime.run(prepared)


def _read_log(path: Path):
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def main() -> int:
    print("=== Verifikasi project-local .aether store (Task Log + Bible) ===")
    shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir(parents=True, exist_ok=True)
    try:
        # 1) Bilamana belum ada -> .aether dibuat otomatis.
        assert not (FIXTURE / ".aether").exists()
        store = AetherProjectStore(FIXTURE)
        assert store.ensure() is True
        assert (FIXTURE / ".aether" / "log").is_dir()
        assert (FIXTURE / ".aether" / "bible").is_dir()
        print("[1] project tanpa .aether -> otomatis dibuat OK")

        # 5) Bible dibuat pertama kali (index + kategori).
        bible = BibleStore(FIXTURE)
        assert bible.ensure() is True
        assert (FIXTURE / ".aether" / "bible" / "index.md").exists()
        for category in BIBLE_CATEGORIES:
            assert (FIXTURE / ".aether" / "bible" / f"{category}.md").exists(), category
        print("[5] Bible (index + kategori) dibuat pertama kali OK")

        # 2) + 4) execution tanpa task_id -> task_id dibuat, log memakai id tsb.
        runtime, result = _run_task(FIXTURE, "tulis modul contoh")
        assert result.status.value == "completed", result.status
        generated_id = runtime._current_task_id
        assert generated_id, "runtime harus membuat task_id bila tidak ada"
        log_path = FIXTURE / ".aether" / "log" / f"{generated_id}.log"
        assert log_path.exists(), log_path
        print(f"[2] task_id dibuat otomatis -> {generated_id[:8]}... OK")
        print(f"[4] log tersimpan di .aether/log/<task_id>.log OK -> {log_path.name}")

        # 10) log memuat prompt/provider/status akhir.
        records = _read_log(log_path)
        events = [r["event"] for r in records]
        assert "task_requested" in events and "task_started" in events, events
        assert "provider_request" in events and "provider_response" in events, events
        assert "task_finished" in events, events
        requested = [r for r in records if r["event"] == "task_requested"][0]
        assert requested["data"]["prompt"] == "tulis modul contoh", requested
        finished = [r for r in records if r["event"] == "task_finished"][0]
        assert finished["data"]["status"] == "completed", finished
        provider_req = [r for r in records if r["event"] == "provider_request"][0]
        assert provider_req["data"]["provider"] == "fake-task", provider_req
        assert all("timestamp" in r and r["task_id"] == generated_id for r in records)
        print(f"[10] log memuat task_id/timestamp/prompt/provider/status OK -> {len(records)} event")

        # 3) execution dengan task_id -> memakai ID tersebut.
        runtime2, result2 = _run_task(FIXTURE, "task dengan id tetap", task_id="fixed-task-123")
        assert result2.status.value == "completed"
        assert runtime2._current_task_id == "fixed-task-123"
        assert (FIXTURE / ".aether" / "log" / "fixed-task-123.log").exists()
        print("[3] execution dengan task_id memakai ID tersebut OK")

        # 11) log tidak membocorkan secret.
        leak_log = TaskLog(FIXTURE, task_id="secret-check")
        leak_log.append(
            "tool_called",
            {"tool": "http", "api_key": "SUPER-SECRET", "authorization": "Bearer TOPSECRET"},
        )
        text = (FIXTURE / ".aether" / "log" / "secret-check.log").read_text(encoding="utf-8")
        assert "SUPER-SECRET" not in text and "TOPSECRET" not in text, text
        assert "[redacted]" in text
        print("[11] log task tidak membocorkan secret OK")

        # 6) + 7) Bible dibaca & diperbarui lewat ProjectBrain.
        brain = ProjectBrain.for_project(FIXTURE, provider=BibleProvider())
        brain.add_verified("facts", "Python 3.12 di project ini", confidence=0.95)
        learned = brain.learn(observations=["Task: setup project", "Result: selesai"])
        assert learned.total_added >= 1, learned.to_dict()
        facts_text = (FIXTURE / ".aether" / "bible" / "facts.md").read_text(encoding="utf-8")
        assert "Python 3.12 di project ini" in facts_text
        assert "Bible fact hasil learning" in facts_text
        # Kategori Bible "ui" dan "conventions" (semantic baru) tersimpan.
        ui_text = (FIXTURE / ".aether" / "bible" / "ui.md").read_text(encoding="utf-8")
        assert "Workbench adalah layar utama" in ui_text
        conventions = (FIXTURE / ".aether" / "bible" / "conventions.md").read_text(encoding="utf-8")
        assert "Gunakan folder src/" in conventions
        ctx = brain.get_context()
        assert "Python 3.12 di project ini" in ctx.text and "Bible fact hasil learning" in ctx.text
        assert "Workbench adalah layar utama" in ctx.text
        print("[6] ProjectBrain.get_context() membaca Bible OK")
        print(f"[7] knowledge baru memperbarui Bible OK -> total_added={learned.total_added}")

        # 8) project berikutnya membaca knowledge dari task sebelumnya.
        brain_next = ProjectBrain.for_project(FIXTURE, provider=BibleProvider())
        ctx_next = brain_next.get_context()
        assert "Python 3.12 di project ini" in ctx_next.text, ctx_next.text
        assert "Gunakan folder src/" in ctx_next.text
        print("[8] instance baru membaca knowledge task sebelumnya OK")

        # 9) kegagalan menulis Bible tidak merusak execution.
        #    Provider TaskProvider gagal pada panggilan learning -> runtime tetap
        #    COMPLETED, dan kegagalan update Bible tercatat.
        runtime3, result3 = _run_task(FIXTURE, "task dengan learning gagal", task_id="learn-fail-1")
        assert result3.status.value == "completed", result3.status
        rec3 = _read_log(FIXTURE / ".aether" / "log" / "learn-fail-1.log")
        ev3 = [r["event"] for r in rec3]
        assert "task_finished" in ev3 and "bible_update_failed" in ev3, ev3
        print("[9] kegagalan update Bible tidak merusak execution OK")

        # 12) tidak ada storage intelligence kedua sebagai source of truth.
        intel = ProjectIntelligence(FIXTURE, root=FIXTURE)
        assert intel.uses_bible is True
        assert not (FIXTURE / "intelligence").exists()
        print("[12] Bible project-local = satu-satunya source knowledge OK")

        print()
        print("[OK] project-local .aether store (Task Log + AI Project Bible) bekerja.")
        return 0
    finally:
        shutil.rmtree(FIXTURE, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
