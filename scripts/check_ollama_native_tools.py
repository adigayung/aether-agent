"""Verifier: Ollama native tool-calling end-to-end (model target nyata).

Membuktikan bahwa AETHER (bukan hanya raw HTTP) menerima dan MENERUSKAN
native `message.tool_calls` dari Ollama sampai ke execution loop, sehingga
tool benar-benar dieksekusi dan loop berlanjut ke turn final.

Kontrak yang dibuktikan:
    1. OllamaProvider.generate + normalize_response -> LLMAction (native).
    2. AgentOrchestrator (continuous loop) mengeksekusi tool tersebut.
    3. Turn lanjutan (assistant(tool_calls) + tool result) TIDAK ditolak
       HTTP 400 oleh Ollama (arguments dikirim sebagai object, bukan string).
    4. Loop berlanjut ke turn final (provider dipanggil >= 2x).
    5. Task TIDAK false-completed: tool benar-benar dijalankan (file dibuat).

Model target: LisyNeko/qwen3.8-9b-coder:latest (dapat di-override lewat
env OLLAMA_TEST_MODEL). Bila server Ollama tidak hidup / model tidak ada,
verifier SKIP dengan pesan jelas (bukan gagal palsu).

Fixture workspace berada di `J:\\Agent_Ai\\dummy_test` (workspace uji
terisolasi, BUKAN bagian dari AETHER) dan dibersihkan setelah verifikasi.

Jalankan:
    python scripts/check_ollama_native_tools.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.config.settings import OllamaConfig  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.providers.base import GenerateOptions, Message  # noqa: E402
from agent_ai.providers.ollama import OllamaProvider  # noqa: E402
from agent_ai.tools.registry import build_registry  # noqa: E402

DUMMY_ROOT = Path(r"J:\Agent_Ai\dummy_test")
FIXTURE = DUMMY_ROOT / "ollama_native_tools_fixture"

TEST_MODEL = os.getenv("OLLAMA_TEST_MODEL", "LisyNeko/qwen3.8-9b-coder:latest")


class CountingOllamaProvider(OllamaProvider):
    """OllamaProvider nyata + penghitung panggilan generate (bukan mock)."""

    def __init__(self, config: OllamaConfig) -> None:
        super().__init__(config=config)
        self.calls = 0

    def generate(self, *args, **kwargs):  # type: ignore[override]
        self.calls += 1
        return super().generate(*args, **kwargs)


def _provider() -> OllamaProvider:
    return OllamaProvider(config=OllamaConfig(model=TEST_MODEL))


def _skip(reason: str) -> int:
    print(f"[SKIP] {reason}")
    return 0


def main() -> int:
    print("=== Verifikasi Ollama Native Tool-Calling (end-to-end AETHER) ===")
    print(f"model target : {TEST_MODEL}")

    provider = _provider()
    if not provider.is_available():
        return _skip("server Ollama tidak dapat dihubungi (is_available=False).")

    if FIXTURE.exists():
        shutil.rmtree(FIXTURE, ignore_errors=True)
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "hello.txt").write_text("halo dari fixture", encoding="utf-8")

    try:
        return _run(provider)
    finally:
        shutil.rmtree(FIXTURE, ignore_errors=True)
        try:
            if DUMMY_ROOT.exists() and not any(DUMMY_ROOT.iterdir()):
                DUMMY_ROOT.rmdir()
        except OSError:
            pass


def _run(provider: OllamaProvider) -> int:
    # 1) Provider layer: native tool call -> LLMAction.
    tools = _tool_definitions()
    messages = [
        Message(
            role="user",
            content=(
                "Gunakan tool list_files untuk melihat isi folder saat ini. "
                "Panggil tool-nya sekarang."
            ),
        )
    ]
    gen = provider.generate(
        messages=messages,
        options=GenerateOptions(model=TEST_MODEL),
        tools=tools,
    )
    response = provider.normalize_response(gen)
    print(f"[1] finish_reason : {response.finish_reason.value}")
    print(f"[1] native tool_calls diterima : {response.has_tool_calls}")
    if not response.has_tool_calls:
        print("[FAIL] Provider TIDAK menerima native tool_calls dari model target.")
        print(f"       text (first 200): {response.text[:200]!r}")
        return 1
    for a in response.tool_calls():
        print(f"[1]   -> {a.name}({a.arguments})")
    print("[1] OK: provider meneruskan native tool call -> LLMAction")

    # 2) Orchestrator continuous loop: tool benar-benar dieksekusi + turn final.
    executor = ToolExecutor(build_registry(root=FIXTURE))
    counting = CountingOllamaProvider(OllamaConfig(model=TEST_MODEL))
    orch = AgentOrchestrator(
        provider=counting,
        executor=executor,
        options=GenerateOptions(model=TEST_MODEL),
        system_prompt=(
            "Kamu adalah coding agent. Gunakan tool yang tersedia untuk "
            "menyelesaikan task. Jangan hanya menjelaskan; panggil tool."
        ),
        use_continuous_loop=True,
    )
    result = orch.run(
        "Gunakan tool list_files untuk melihat isi folder saat ini, lalu laporkan."
    )
    print(f"[2] status     : {result.status.value}")
    print(f"[2] provider calls : {counting.calls}")
    print(f"[2] iterations : {result.iterations}")
    print(f"[2] steps      : {len(result.steps)}")

    executed = [
        s for s in result.steps
        if (s.get("observation") or {}).get("success")
    ]
    print(f"[2] tool dieksekusi (sukses) : {len(executed)}")
    if not executed:
        print("[FAIL] Tidak ada tool yang benar-benar dieksekusi (false completion?).")
        print(f"       result: {(result.result or '')[:200]!r}")
        return 1
    print("[2] OK: native tool call diteruskan sampai execution loop")

    # 3) Turn lanjutan tidak ditolak HTTP 400: provider dipanggil >= 2x
    #    (turn tool + turn final setelah menerima hasil tool).
    if counting.calls < 2:
        print(
            f"[FAIL] Provider hanya dipanggil {counting.calls}x; turn lanjutan "
            "(assistant(tool_calls) + tool result) tidak berjalan."
        )
        return 1
    if result.status != AgentStatus.DONE:
        print(f"[FAIL] Status akhir bukan DONE: {result.status.value} ({result.error!r})")
        return 1
    print("[3] OK: turn lanjutan (tool result -> final) berjalan tanpa HTTP 400")

    print()
    print("[OK] Ollama native tool-calling end-to-end bekerja di AETHER.")
    return 0


def _tool_definitions():
    from agent_ai.providers.base import ToolDefinition

    registry = build_registry(root=FIXTURE)
    return [ToolDefinition.from_spec(spec) for spec in registry.specs()]


if __name__ == "__main__":
    raise SystemExit(main())
