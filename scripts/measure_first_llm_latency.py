"""Ukur waktu sampai `provider_request` PERTAMA (TASK: hapus full snapshot).

Skenario yang diukur (tanpa network; provider fake deterministik):

  1. `simple_no_mutation`
     Task sederhana tanpa mutasi pada workspace BESAR (banyak file).
     Membuktikan tidak ada lagi penelusuran/hashing seluruh workspace yang
     menahan request LLM pertama.

  2. `coding_edit_via_tool`
     Task yang mengubah satu file lewat tool filesystem. Mengukur biaya
     baseline LAZY (per-path) dan memastikan laporan perubahan tetap akurat.

  3. `run_command`
     Task yang menjalankan `run_command`. Mengukur biaya jalur command +
     deteksi terarah.

Jalankan:
    python scripts/measure_first_llm_latency.py [jumlah_file]
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(PROJECT_ROOT / "src"), str(PROJECT_ROOT / "web" / "django_app")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

from agent_ai.changes.tracker import ChangeTracker, clear_fingerprint_cache  # noqa: E402
from agent_ai.core.response import ActionType, FinishReason, LLMAction, LLMResponse  # noqa: E402
from agent_ai.permission import PermissionConfig, PermissionManager, PermissionPolicy  # noqa: E402
from agent_ai.permission.models import PolicyMode  # noqa: E402
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402
from agent_ai.session.store import InMemorySessionStore  # noqa: E402
from agent_ai.task.models import PreparedTask  # noqa: E402

from api.execution import TaskExecutor  # noqa: E402


class _RecordingProvider(BaseProvider):
    """Provider fake yang mencatat waktu request PERTAMA lalu memberi final.

    Mengikuti kontrak provider AETHER yang dipakai jalur produksi:
    `generate()` mengembalikan hasil mentah, `normalize_response()` yang
    menerjemahkannya menjadi `LLMResponse` (tool call / final).
    """

    name = "measure"

    def __init__(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        validation_command: Optional[str] = None,
    ) -> None:
        self._tool_name = tool_name
        self._tool_args = tool_args
        self._validation_command = validation_command
        self.calls = 0
        self.first_request_epoch: Optional[float] = None

    def generate(
        self, prompt=None, messages=None, options=None, tools=None, tool_choice=None
    ):  # noqa: D102
        self.calls += 1
        if self.first_request_epoch is None:
            self.first_request_epoch = time.perf_counter()
        return GenerateResult(text="", provider="measure", model="measure-1")

    def is_available(self) -> bool:  # noqa: D102
        return True

    def normalize_response(self, result):  # noqa: D102
        if self._tool_name and self.calls == 1:
            return LLMResponse(
                text="",
                actions=[
                    LLMAction(
                        name=self._tool_name,
                        arguments=dict(self._tool_args),
                        type=ActionType.TOOL_CALL,
                    )
                ],
                finish_reason=FinishReason.TOOL_CALLS,
                provider="measure",
                model="measure-1",
            )
        # Perubahan source (.py) memerlukan bukti validasi (completion gate):
        # jalankan command validasi sukses sebelum final.
        if self._validation_command is not None and self.calls == 2:
            return LLMResponse(
                text="",
                actions=[
                    LLMAction(
                        name="run_command",
                        arguments={"command": self._validation_command},
                        type=ActionType.TOOL_CALL,
                    )
                ],
                finish_reason=FinishReason.TOOL_CALLS,
                provider="measure",
                model="measure-1",
            )
        return LLMResponse(
            text="selesai",
            actions=[],
            finish_reason=FinishReason.STOP,
            provider="measure",
            model="measure-1",
        )


def _build_workspace(root: Path, file_count: int) -> None:
    """Buat workspace berisi `file_count` file source kecil."""
    root.mkdir(parents=True, exist_ok=True)
    for i in range(file_count):
        sub = root / f"pkg{i % 50}"
        sub.mkdir(exist_ok=True)
        (sub / f"mod_{i}.py").write_text(f"value_{i} = {i}\n", encoding="utf-8")


def _run(
    workspace: Path,
    provider: _RecordingProvider,
    task_id: str,
) -> Dict[str, Any]:
    store = InMemorySessionStore()
    session = store.create_session()
    pm = PermissionManager(
        policy=PermissionPolicy(PermissionConfig(workspace_write=PolicyMode.ALLOW))
    )
    executor = TaskExecutor(
        store, provider_factory=lambda: provider, permission_manager=pm
    )
    prepared = PreparedTask(task="task pengukuran", task_id=task_id)
    started = time.perf_counter()
    result = executor.run(
        prepared,
        session_id=session.session_id,
        task_id=task_id,
        workspace_root=str(workspace),
    )
    finished = time.perf_counter()
    return {
        "result": result,
        "total_seconds": finished - started,
        "first_request_seconds": (
            (provider.first_request_epoch - started)
            if provider.first_request_epoch is not None
            else None
        ),
    }


def main() -> int:
    file_count = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    base = Path(tempfile.mkdtemp(prefix="aether-measure-"))
    print(f"workspace: {file_count} file")
    print("=" * 68)

    try:
        # --- 1) task sederhana TANPA mutasi -----------------------------------
        ws1 = base / "simple"
        _build_workspace(ws1, file_count)
        clear_fingerprint_cache(ws1)
        r1 = _run(ws1, _RecordingProvider("", {}), "measure-simple")
        print("[1] simple_no_mutation (tanpa mutasi)")
        print(f"    waktu sampai provider_request pertama : {r1['first_request_seconds']:.3f} s")
        print(f"    total eksekusi task                   : {r1['total_seconds']:.3f} s")
        print(f"    status                                : {r1['result']['status']}")
        print(f"    changes dilaporkan                    : {r1['result'].get('changes')}")

        # --- 2) task coding lewat tool filesystem -----------------------------
        ws2 = base / "coding"
        _build_workspace(ws2, file_count)
        (ws2 / "target.py").write_text("value = 1\n", encoding="utf-8")
        clear_fingerprint_cache(ws2)
        r2 = _run(
            ws2,
            _RecordingProvider(
                "edit_file",
                {"path": "target.py", "old_text": "value = 1", "new_text": "value = 2"},
                validation_command='python -c "print(1)"',
            ),
            "measure-coding",
        )
        print()
        print("[2] coding_edit_via_tool (edit satu file)")
        print(f"    waktu sampai provider_request pertama : {r2['first_request_seconds']:.3f} s")
        print(f"    total eksekusi task                   : {r2['total_seconds']:.3f} s")
        print(f"    status                                : {r2['result']['status']}")
        print(f"    changes dilaporkan                    : {r2['result'].get('changes')}")

        # --- 3) run_command ---------------------------------------------------
        ws3 = base / "command"
        _build_workspace(ws3, file_count)
        (ws3 / "seed.py").write_text("value = 1\n", encoding="utf-8")
        clear_fingerprint_cache(ws3)
        script = "open('made_by_cmd.txt','w').write('x\\n')"
        r3 = _run(
            ws3,
            _RecordingProvider("run_command", {"command": f'python -c "{script}"'}),
            "measure-command",
        )
        print()
        print("[3] run_command (shell bisa memutasi banyak file)")
        print(f"    waktu sampai provider_request pertama : {r3['first_request_seconds']:.3f} s")
        print(f"    total eksekusi task                   : {r3['total_seconds']:.3f} s")
        print(f"    status                                : {r3['result']['status']}")
        print(f"    changes dilaporkan                    : {r3['result'].get('changes')}")

        print()
        print("=" * 68)
        print("Ringkasan: waktu sampai request LLM PERTAMA")
        for label, payload in (
            ("simple_no_mutation ", r1),
            ("coding_edit_via_tool", r2),
            ("run_command        ", r3),
        ):
            value = payload["first_request_seconds"]
            text = f"{value:.3f} s" if value is not None else "n/a"
            print(f"  {label} : {text}")
        return 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
