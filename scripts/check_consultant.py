"""Verifikasi AETHER Consultant (deterministik, tanpa API key/model cloud).

Menguji:
    1. Consultant core: reasoning loop menghasilkan reply + Task Proposal.
    2. Project Bible terpakai sebagai konteks AND tool update_project_bible
       menyimpan knowledge ke `<root>/.aether/bible/` (store existing).
    3. Boundary: registry Consultant TIDAK memuat tool tulis/hapus/pindah.
    4. Boundary: permission policy menolak write/delete-move.
    5. Boundary: run_command Consultant menolak command destruktif dan membaca
       (git read) diizinkan.
    6. Session context: konteks lintas giliran dipertahankan (session_id stabil).
    7. Gateway/HTTP: POST /api/consultant/consult mengembalikan task_proposal
       dan validasi input (400).

Memakai FakeProvider (bukan provider cloud).
Jalankan:
    python scripts/check_consultant.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_ai.consultant import (  # noqa: E402
    ConsultantService,
    build_consultant_permission_manager,
    build_consultant_registry,
    extract_task_proposal,
)
from agent_ai.consultant.tools import (  # noqa: E402
    ConsultantRunCommandTool,
    consultant_command_violation,
)
from agent_ai.core.response import FinishReason, LLMAction, LLMResponse  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.types import ToolCall  # noqa: E402
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402
from agent_ai.tools.base import ToolValidationError  # noqa: E402


class FakeProvider(BaseProvider):
    """Provider deterministik yang mengikuti script LLMResponse."""

    name = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.last_messages = None

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        self.last_messages = messages
        return GenerateResult(text="", model="fake-model", provider="fake")

    def normalize_response(self, result):
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[idx]


def _final(text: str) -> LLMResponse:
    return LLMResponse(text=text, finish_reason=FinishReason.STOP)


def _tool(name: str, **args) -> LLMResponse:
    return LLMResponse(
        actions=[LLMAction(name=name, arguments=dict(args))],
        finish_reason=FinishReason.TOOL_CALLS,
    )


def main() -> int:
    print("=== Verifikasi AETHER Consultant ===")
    root = Path(tempfile.mkdtemp(prefix="consultant_fixture_"))
    try:
        return _run(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run(root: Path) -> int:
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    # 1) Consultant core: loop + Task Proposal.
    script = [
        _tool("search_code", query="def add"),
        _tool(
            "update_project_bible",
            category="facts",
            content="Project fixture punya fungsi add(a,b) di app.py",
        ),
        _final(
            "Findings: fixture menggunakan app.py.\n\n"
            "```task\n"
            "Goal: tambahkan fungsi subtract(a, b)\n"
            "Affected files: app.py\n"
            "Acceptance criteria: subtract(5, 3) == 2\n"
            "```\n"
        ),
    ]
    service = ConsultantService()
    provider = FakeProvider(script)
    result = service.consult("Tolong analisa fixture ini dan buat task", provider=provider, root=str(root))

    assert result.status == "done", result.status
    assert "Findings" in result.reply, result.reply
    assert result.task_proposal and "subtract" in result.task_proposal, result.task_proposal
    tools_used = {e["tool"] for e in result.tool_events}
    assert "search_code" in tools_used, tools_used
    assert "update_project_bible" in tools_used, tools_used
    # Project Bible dibaca sebagai konteks awal (system message ke LLM).
    joined = "\n".join(
        (m.get("content") or "") for m in (provider.last_messages or []) if isinstance(m, dict)
    )
    assert "# Project Intelligence" in joined, "Project Bible harus jadi konteks awal"
    # System prompt Consultant dikirim (boundary read-only + Task Proposal).
    assert "CONSULTANT" in joined.upper(), "system prompt Consultant harus dikirim"
    print("[1] Consultant core OK -> reply + Task Proposal + tool usage")
    print("[1b] Project Bible dibaca sebagai konteks awal + system prompt Consultant OK")
    print(f"    tool_events: {[ (e['tool'], e['success']) for e in result.tool_events ]}")

    # 2) Project Bible updated (store existing, bukan store baru).
    facts = root / ".aether" / "bible" / "facts.md"
    assert facts.exists(), "facts.md harus dibuat"
    assert "add(a,b)" in facts.read_text(encoding="utf-8"), "knowledge harus tersimpan di Bible"
    print("[2] Project Bible update OK -> .aether/bible/facts.md")

    # 3) Boundary: registry tanpa tool tulis/hapus/pindah.
    reg = build_consultant_registry(root)
    names = set(reg.list())
    for forbidden in ("write_file", "edit_file", "delete_file", "move_file"):
        assert forbidden not in names, f"registry tidak boleh memuat {forbidden}"
    for required in ("list_files", "read_file", "search_code", "run_command", "update_project_bible"):
        assert required in names, f"registry harus memuat {required}"
    print(f"[3] registry READ-ONLY OK -> {sorted(names)}")

    # 4) Boundary: permission policy menolak write / delete-move.
    pm = build_consultant_permission_manager()
    assert pm.check("write_file", {"path": "a.txt", "content": "x"}).allowed is False
    assert pm.check("edit_file", {"path": "a.txt"}).allowed is False
    assert pm.check("delete_file", {"path": "a.txt"}).allowed is False
    assert pm.check("move_file", {"src": "a", "dst": "b"}).allowed is False
    assert pm.check("read_file", {"path": "app.py"}).allowed is True
    assert pm.check("run_command", {"command": "git status"}).allowed is True
    print("[4] permission boundary OK -> write/delete-move DITOLAK, read/command diizinkan")

    # 5) Boundary: guard command destruktif vs baca.
    assert consultant_command_violation("git commit -m x") is not None
    assert consultant_command_violation("git push") is not None
    assert consultant_command_violation("git reset --hard") is not None
    assert consultant_command_violation("del app.py") is not None
    assert consultant_command_violation("mkdir new_dir") is not None
    assert consultant_command_violation("git status") is None
    assert consultant_command_violation("git diff") is None
    assert consultant_command_violation("python -m pytest") is None
    assert consultant_command_violation("python -c \"print('a>b')\"") is None

    tool = ConsultantRunCommandTool(root=root)
    blocked = False
    try:
        tool.execute(command="git commit -m hack")
    except ToolValidationError:
        blocked = True
    assert blocked, "git commit harus ditolak"
    ok = tool.execute(command="git status")
    assert isinstance(ok, dict) and "exit_code" in ok, ok
    print("[5] run_command guard OK -> destructive ditolak, git read diizinkan")

    # 5b) Executor-level: tool tulis tidak dapat dieksekusi Consultant.
    executor = ToolExecutor(registry=reg, permission_manager=pm)
    payload = executor.execute_tool_call(ToolCall.create("write_file", {"path": "x.py", "content": "y"}))
    assert not payload.is_success, "write_file TIDAK boleh sukses untuk Consultant"
    assert not (root / "x.py").exists(), "Consultant tidak boleh menulis file source"
    print("[5b] executor boundary OK -> write_file ditolak (tidak ada file dibuat)")

    # 6) Session context lintas giliran.
    script2 = [_final("Lanjutan: berdasarkan analisa sebelumnya, task sudah jelas.")]
    provider2 = FakeProvider(script2)
    result2 = service.consult("Buat task-nya sekarang", provider=provider2, root=str(root), session_id=result.session_id)
    assert result2.session_id == result.session_id, "session_id harus stabil"
    session = service.get_session(result.session_id)
    assert session is not None and len(session["turns"]) >= 4, session
    assert any("fixture" in t["text"] for t in session["turns"]), "konteks sebelumnya harus tersimpan"
    print("[6] session context OK -> konteks lintas giliran dipertahankan")

    # 7) Gateway + HTTP endpoint.
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    # Django test client mengirim Host: testserver; paksa agar host diizinkan
    # walau environment sudah menyetel DJANGO_ALLOWED_HOSTS (hardening #57).
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
    import django

    django.setup()
    from django.test import Client

    import api.services as services_mod
    from agent_ai.consultant import ConsultantService as CoreConsultantService

    gw_service = services_mod.GatewayService(
        auto_execute=False, consultant_service=CoreConsultantService()
    )
    gw_provider = FakeProvider([_final("Rekomendasi:\n```task\nGoal: demo task\n```")])
    gw_service._build_consultant_provider = lambda *a, **k: gw_provider  # type: ignore[assignment]
    services_mod._default_service = gw_service

    client = Client()
    # validasi: message kosong -> 400
    bad = client.post("/api/consultant/consult", data="{}", content_type="application/json")
    assert bad.status_code == 400, bad.status_code
    # sukses: -> 200 + task_proposal
    good = client.post(
        "/api/consultant/consult",
        data='{"message": "buat task demo"}',
        content_type="application/json",
    )
    assert good.status_code == 200, (good.status_code, good.content)
    body = good.json()
    assert body["task_proposal"] == "Goal: demo task", body
    assert body["session_id"], body
    print("[7] Gateway/HTTP OK -> POST /api/consultant/consult + validasi 400")

    # Task Proposal dapat dikirim lewat alur task EXISTING (create_task).
    rec = gw_service.create_task(task=body["task_proposal"])
    assert rec["task"] == "Goal: demo task", rec
    print("[7b] Task Proposal -> create_task (alur Agent existing) OK")

    print()
    print("[OK] AETHER Consultant bekerja (reasoning + Bible + boundary + Task Proposal).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
