"""Verifikasi integrasi ProjectBrain ke AgentOrchestrator.

Menguji:
    - orchestrator tanpa brain tetap bekerja seperti sebelumnya.
    - orchestrator dengan fake brain mendapatkan context sebelum LLM dipanggil.
    - LLM menerima project knowledge + task.
    - tool loop tetap bekerja.
    - setelah task selesai brain menerima learning input.
    - learning failure tidak menggagalkan task utama.
    - tidak ada perubahan ke project source.
    - provider tetap provider-agnostic.

Jalankan:
    python scripts/check_orchestrator_brain.py
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

from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.response import ActionType, FinishReason, LLMAction, LLMResponse  # noqa: E402
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402


class ScriptedProvider(BaseProvider):
    """Provider palsu: mengembalikan respons berurutan (tanpa API).

    Setiap item respons bisa berupa:
        - str  : teks final.
        - dict : {"tool": <name>, "arguments": {...}} -> tool call.
    """

    name = "scripted"

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # rekam messages tiap panggilan

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        self.calls.append(messages)
        item = self._responses.pop(0) if self._responses else "FINAL: selesai"
        text = item if isinstance(item, str) else ""
        return GenerateResult(text=text, model="scripted-model", provider=self.name, raw={"item": item})

    def normalize_response(self, result):
        item = result.raw.get("item")
        if isinstance(item, dict) and "tool" in item:
            return LLMResponse(
                text="",
                actions=[LLMAction(name=item["tool"], arguments=item.get("arguments", {}))],
                finish_reason=FinishReason.TOOL_CALLS,
                provider=self.name,
                model=result.model,
            )
        return LLMResponse(
            text=result.text or "",
            actions=[],
            finish_reason=FinishReason.STOP,
            provider=self.name,
            model=result.model,
        )


class FakeBrain:
    """Brain palsu: merekam context request & learning input."""

    def __init__(self, context_text="## facts\n- Python 3.11", fail_learn=False):
        self._context_text = context_text
        self.fail_learn = fail_learn
        self.context_calls = 0
        self.learn_calls = []

    def get_context(self, *args, **kwargs):
        self.context_calls += 1

        class _Ctx:
            text = self._context_text

        return _Ctx()

    def learn(self, observations):
        self.learn_calls.append(observations)
        if self.fail_learn:
            raise RuntimeError("learning gagal (disengaja)")

        class _Result:
            def to_dict(self):
                return {"total_added": 1}

        return _Result()


def main() -> int:
    print("=== Verifikasi Integrasi Orchestrator + Brain ===")
    source = Path(tempfile.mkdtemp(prefix="orch_src_"))
    try:
        (source / "main.py").write_text("print('x')\n", encoding="utf-8")
        source_before = sorted(str(p.relative_to(source)) for p in source.rglob("*"))

        # 1) Tanpa brain: perilaku lama tetap bekerja.
        p1 = ScriptedProvider(["FINAL: hasil tanpa brain"])
        r1 = AgentOrchestrator(provider=p1).run("task biasa")
        print(f"tanpa brain : status={r1.status.value} result={r1.result!r} learning={r1.learning}")
        assert r1.status == AgentStatus.DONE and r1.result == "FINAL: hasil tanpa brain"
        assert r1.learning is None
        print()

        # 2) Dengan brain: context diambil sebelum LLM dipanggil.
        brain = FakeBrain()
        p2 = ScriptedProvider(["FINAL: hasil dengan brain"])
        r2 = AgentOrchestrator(provider=p2, brain=brain).run("task dengan brain")
        print(f"brain context_calls : {brain.context_calls}")
        assert brain.context_calls == 1
        print()

        # 3) LLM menerima project knowledge + task.
        first_messages = p2.calls[0]
        roles = [m.role for m in first_messages]
        contents = "\n".join(m.content for m in first_messages)
        print(f"roles       : {roles}")
        assert "system" in roles
        assert "Python 3.11" in contents, "project knowledge tidak dikirim ke LLM"
        assert "task dengan brain" in contents, "task tidak dikirim ke LLM"
        print("LLM menerima project knowledge + task -> OK")
        print()

        # 4) Tool loop tetap bekerja (tool call -> observation -> FINAL).
        tool_call = {"tool": "read_file", "arguments": {"path": "main.py"}}
        p3 = ScriptedProvider([tool_call, "FINAL: selesai setelah tool"])
        r3 = AgentOrchestrator(provider=p3, brain=FakeBrain()).run("baca file")
        print(f"tool loop : status={r3.status.value} iterations={r3.iterations} steps={len(r3.steps)}")
        assert r3.status == AgentStatus.DONE
        assert r3.iterations >= 1 and len(r3.steps) >= 1
        print()

        # 5) Setelah task selesai brain menerima learning input.
        brain4 = FakeBrain()
        p4 = ScriptedProvider(["FINAL: selesai"])
        r4 = AgentOrchestrator(provider=p4, brain=brain4).run("task learning")
        print(f"learn_calls : {len(brain4.learn_calls)}")
        assert len(brain4.learn_calls) == 1
        assert any("task learning" in o for o in brain4.learn_calls[0])
        assert r4.learning == {"total_added": 1}
        print()

        # 6) Learning failure tidak menggagalkan task utama.
        brain5 = FakeBrain(fail_learn=True)
        p5 = ScriptedProvider(["FINAL: tetap sukses"])
        r5 = AgentOrchestrator(provider=p5, brain=brain5).run("task dengan learning gagal")
        print(f"learning gagal : status={r5.status.value} result={r5.result!r} learning={r5.learning}")
        assert r5.status == AgentStatus.DONE and r5.result == "FINAL: tetap sukses"
        assert r5.learning is None
        print()

        # 7) Tidak ada perubahan ke project source.
        source_after = sorted(str(p.relative_to(source)) for p in source.rglob("*"))
        print(f"source untouched : {source_before == source_after}")
        assert source_before == source_after, "orchestrator menyentuh project source!"
        print()

        print("[OK] Integrasi Orchestrator + Brain bekerja (opsional, terisolasi, provider-agnostic).")
        return 0
    finally:
        shutil.rmtree(source, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
