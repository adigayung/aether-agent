"""Verifier: parity Ollama Local vs provider lain pada AETHER Consultant.

Masalah yang diperbaiki (root cause):
    Ollama memakai context window SERVER-SIDE yang defaultnya kecil (~4096) dan
    MEMOTONG PROMPT DARI DEPAN tanpa error. Prompt Consultant Quick = system
    prompt + Project Bible (besar) + pertanyaan user, sehingga system prompt dan
    awal Bible HILANG; model menjawab generik ("tidak punya akses ke Project
    Bible") atau berhalusinasi. Provider cloud memakai context window modelnya
    sendiri sehingga konteks yang sama "kebetulan" diterima — jadi gap-nya ada
    di provider adapter Ollama, bukan di prompt/registry Consultant.

Kontrak yang diuji OFFLINE (deterministik, tanpa jaringan):
    [1] OllamaConfig punya `num_ctx` (default 32768; override env OLLAMA_NUM_CTX).
    [2] OllamaProvider MENGIRIM `options.num_ctx`; num_ctx=0 -> tidak dikirim.
    [3] Provider cloud TIDAK berubah (knowledge_budget_tokens() = None).
    [4] Anggaran konteks pengetahuan Ollama = num_ctx//2 - reserve (konservatif).
    [5] AgentOrchestrator memotong konteks pengetahuan PADA BATAS BARIS hanya
        bila provider melaporkan anggaran; provider tanpa anggaran -> APA ADANYA.
    [6] Mekanisme Bible SAMA untuk semua provider: Consultant mengirimkan Bible
        sebagai SYSTEM MESSAGE (bukan sistem Bible khusus Ollama), dan tool
        Project Map dikirim sebagai tool definitions.
    [7] Registry Consultant Quick tetap: atlas_query, rig_query,
        project_map_status, update_project_bible (TANPA refresh_project_map).

Dengan flag `--real` (butuh server Ollama lokal NYATA + model terpasang):
    [R1] Request Consultant Quick benar-benar memuat `num_ctx`.
    [R2] Request benar-benar memuat system prompt Consultant + Bible (system msg).
    [R3] Prompt TIDAK dipotong server (prompt_eval_count <= num_ctx/2).
    [R4] "project saya tentang apa?" dijawab DARI Bible, bukan jawaban generik.
    [R5] Pertanyaan navigasi memicu tool Project Map NYATA (atlas_query) dan
         hasilnya dipakai pada jawaban akhir.

Jalankan:
    python scripts/check_consultant_ollama_parity.py            # offline
    python scripts/check_consultant_ollama_parity.py --real      # + Ollama nyata
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from agent_ai.config.settings import OllamaConfig  # noqa: E402
from agent_ai.providers.base import (  # noqa: E402
    BaseProvider,
    GenerateOptions,
    GenerateResult,
)
from agent_ai.providers.ollama import (  # noqa: E402
    _OLLAMA_PROMPT_BUDGET_DIVISOR,
    _OLLAMA_PROMPT_RESERVE_TOKENS,
    OllamaProvider,
)

BIBLE_MARKER = "MARKER_BIBLE_AETHER_CONSULTANT_XYZ"


# --------------------------------------------------------------------------- #
# Fake providers (offline)
# --------------------------------------------------------------------------- #
class RecordingProvider(BaseProvider):
    """Provider palsu yang merekam messages/tools yang benar-benar dikirim."""

    name = "recording"

    def __init__(self, budget_tokens=None) -> None:
        self.budget_tokens = budget_tokens
        self.calls = []

    def knowledge_budget_tokens(self):  # noqa: D102 - hook provider
        return self.budget_tokens

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):  # noqa: D102
        self.calls.append({"messages": messages, "tools": tools})
        return GenerateResult(
            text="final: jawaban uji",
            model="recording-model",
            provider=self.name,
            raw={"content": "final: jawaban uji"},
        )


class FakeBrain:
    """Brain palsu (brain.get_context().text) untuk menguji pemotongan konteks."""

    def __init__(self, text: str) -> None:
        self._text = text

    def get_context(self, *args, **kwargs):  # noqa: D102
        text = self._text

        class _Ctx:
            pass

        ctx = _Ctx()
        ctx.text = text
        return ctx

    def learn(self, observations):  # noqa: D102- tidak dipakai (brain_learning=False)
        return None


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def check_config() -> None:
    default = OllamaConfig()
    assert default.num_ctx == 32768, default.num_ctx
    print("[1] OllamaConfig.num_ctx default OK -> %d" % default.num_ctx)

    os.environ["OLLAMA_NUM_CTX"] = "8192"
    try:
        assert OllamaConfig().num_ctx == 8192, "env OLLAMA_NUM_CTX tidak dihormati"
    finally:
        del os.environ["OLLAMA_NUM_CTX"]
    assert OllamaConfig().num_ctx == 32768
    print("[1b] override env OLLAMA_NUM_CTX OK")


def check_payload_num_ctx() -> None:
    provider = OllamaProvider(config=OllamaConfig(host="http://localhost:11434", model="m"))
    payload = provider._build_payload(None, [{"role": "user", "content": "hai"}], None)
    assert payload["options"]["num_ctx"] == provider.config.num_ctx, payload.get("options")
    # Extra per-request tetap bisa menimpa (tanpa mengubah struktur payload).
    payload_extra = provider._build_payload(
        None, [{"role": "user", "content": "hai"}], GenerateOptions(extra={"num_ctx": 4096})
    )
    assert payload_extra["options"]["num_ctx"] == 4096, payload_extra["options"]
    print("[2] OllamaProvider mengirim options.num_ctx OK -> %s" % payload["options"])

    off = OllamaProvider(config=OllamaConfig(host="http://localhost:11434", model="m", num_ctx=0))
    payload_off = off._build_payload(None, [{"role": "user", "content": "hai"}], None)
    assert "num_ctx" not in (payload_off.get("options") or {}), payload_off.get("options")
    assert off.knowledge_budget_tokens() is None
    print("[2b] num_ctx=0 -> tidak dikirim & tanpa anggaran OK")


def check_cloud_unchanged() -> None:
    from agent_ai.config.settings import DeepSeekConfig, OpenAIConfig, OpenRouterConfig
    from agent_ai.providers.deepseek import DeepSeekProvider
    from agent_ai.providers.openai_compatible import OpenAICompatibleProvider
    from agent_ai.providers.openrouter import OpenRouterProvider

    cloud = [
        OpenAICompatibleProvider(config=OpenAIConfig(api_key="k", model="m")),
        DeepSeekProvider(config=DeepSeekConfig(api_key="k", model="m")),
        OpenRouterProvider(config=OpenRouterConfig(api_key="k", model="m")),
    ]
    for provider in cloud:
        assert provider.knowledge_budget_tokens() is None, provider.name
    print("[3] provider cloud tidak berubah (tanpa anggaran) OK -- %s" % ", ".join(p.name for p in cloud))


def check_budget_math() -> None:
    provider = OllamaProvider(config=OllamaConfig(host="h", model="m", num_ctx=32768))
    expected = 32768 // _OLLAMA_PROMPT_BUDGET_DIVISOR - _OLLAMA_PROMPT_RESERVE_TOKENS
    assert provider.knowledge_budget_tokens() == expected, provider.knowledge_budget_tokens()
    print("[4] anggaran konteks pengetahuan OK -> %d token (dari num_ctx=32768)" % expected)


def check_fitting() -> None:
    from agent_ai.core.orchestrator import _KNOWLEDGE_CHARS_PER_TOKEN, AgentOrchestrator
    from agent_ai.core.models import AgentStatus

    long_text = "# Project Intelligence\n" + "\n".join(
        "- baris pengetahuan %04d" % i for i in range(4000)
    )
    assert len(long_text) > 60_000

    # (a) Provider TANPA anggaran -> konteks apa adanya (perilaku lama).
    plain = RecordingProvider(budget_tokens=None)
    AgentOrchestrator(use_continuous_loop=False, provider=plain, brain=FakeBrain(long_text)).run("tanya")
    sent_plain = "\n".join(m.content for m in plain.calls[0]["messages"])
    assert long_text in sent_plain, "konteks tanpa anggaran harus dikirim apa adanya"
    print("[5a] provider tanpa anggaran -> konteks apa adanya OK")

    # (b) Provider DENGAN anggaran -> dipotong pada batas baris + penanda.
    budget_tokens = 500
    bounded = RecordingProvider(budget_tokens=budget_tokens)
    AgentOrchestrator(use_continuous_loop=False, provider=bounded, brain=FakeBrain(long_text)).run("tanya")
    fitted = [
        m.content
        for m in bounded.calls[0]["messages"]
        if m.role == "system" and "Project Intelligence" in (m.content or "")
    ]
    assert fitted, "system message konteks pengetahuan tidak ditemukan"
    text_out = fitted[0]
    limit = budget_tokens * _KNOWLEDGE_CHARS_PER_TOKEN
    assert len(text_out) <= limit + 200, (len(text_out), limit)
    assert text_out.startswith("# Project Intelligence"), "awal konteks harus dipertahankan"
    assert "\n- baris pengetahuan" in text_out, "pemotongan harus pada batas baris"
    assert not text_out.rstrip().endswith("pengetahuan"), "pemotongan tidak boleh di tengah item"
    assert "dipotong otomatis" in text_out, "penanda pemotongan tidak ada"
    print("[5b] provider dengan anggaran -> dipotong pada batas baris OK (%d -> %d char)" % (len(long_text), len(text_out)))


def check_consultant_mechanism(tmp_root: Path) -> None:
    """Bible dikirim sebagai SYSTEM MESSAGE + tool map dikirim (mekanisme sama)."""
    from agent_ai.consultant.service import ConsultantService
    from agent_ai.consultant.tools import build_consultant_registry
    from agent_ai.projects.intelligence import ProjectIntelligence
    from agent_ai.projects.learning import IntelligenceLearner

    # Fixture Bible (memakai API existing, bukan sistem Bible baru).
    intelligence = ProjectIntelligence.for_project(tmp_root)
    IntelligenceLearner(intelligence).add_verified(
        "architecture", BIBLE_MARKER + " project ini memakai Vue + Django.", source="verifier"
    )

    provider = RecordingProvider(budget_tokens=None)
    ConsultantService().consult("baca bible project", provider=provider, root=str(tmp_root), mode="quick")

    call = provider.calls[0]
    roles = [m.get("role") for m in call["messages"]]
    assert roles[0] == "system", roles
    assert "AETHER Consultant" in call["messages"][0]["content"], "system prompt Consultant hilang"
    bible_msgs = [
        m for m in call["messages"] if m.get("role") == "system" and BIBLE_MARKER in (m.get("content") or "")
    ]
    assert bible_msgs, "Project Bible tidak dikirim sebagai system message"
    names = sorted(
        getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else "")
        for t in (call["tools"] or [])
    )
    assert names == ["atlas_query", "project_map_status", "rig_query", "update_project_bible"], names
    print("[6] Bible dikirim sebagai system message + 4 tool map dikirim OK")
    print("    roles=%s tools=%s" % (roles, names))

    # Registry Quick: policy map read-only tetap sama (tanpa refresh, tanpa source).
    registry = build_consultant_registry(tmp_root, mode="quick")
    registered = sorted(spec["name"] for spec in registry.specs())
    assert registered == ["atlas_query", "project_map_status", "rig_query", "update_project_bible"], registered
    assert "refresh_project_map" not in registered
    print("[7] registry Consultant Quick tidak berubah OK -> %s" % registered)

    # Prompt Quick tidak memaksa pemanggilan map setiap kali.
    from agent_ai.consultant.prompt import build_consultant_system_prompt

    prompt = build_consultant_system_prompt("quick").lower()
    assert "hanya bila perlu" in prompt, "prompt Quick harus menyatakan map opsional"
    assert "wajib" not in prompt.replace("wajib dipatuhi", ""), "prompt tidak boleh memaksa tool"
    print("[8] prompt Quick tetap 'map hanya bila perlu' (tidak dipaksa) OK")


def check_thinking_fallback() -> None:
    """Model thinking (Qwen3) kadang menjawab di kanal reasoning (content kosong)."""
    import requests

    from agent_ai.providers.retry import InfrastructureRetryPolicy

    no_retry = InfrastructureRetryPolicy(
        enabled=False, max_retries=0, base_delay=0.0, max_delay=0.0, backoff_factor=1.0
    )

    class FakeResponse:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    class _PatchPost:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            self._original = requests.post
            requests.post = lambda url, **kw: FakeResponse(self.payload)
            return self

        def __exit__(self, *exc):
            requests.post = self._original
            return False

    provider = OllamaProvider(
        config=OllamaConfig(host="http://localhost:11434", model="m", num_ctx=0),
        retry_policy=no_retry,
    )

    # (a) Turn final: content kosong + thinking berisi jawaban -> jawaban dipakai.
    final_turn = {"message": {"role": "assistant", "content": "", "thinking": "jawaban dari reasoning"}}
    with _PatchPost(final_turn):
        assert provider.generate(prompt="hai").text == "jawaban dari reasoning"

    # (b) Turn tool call: reasoning TIDAK boleh dipakai sebagai content.
    tool_turn = {
        "message": {
            "role": "assistant",
            "content": "",
            "thinking": "saya akan memanggil tool",
            "tool_calls": [{"id": "c1", "function": {"name": "atlas_query", "arguments": {"query": "x"}}}],
        }
    }
    with _PatchPost(tool_turn):
        assert provider.generate(prompt="hai").text == "", "reasoning masuk ke turn tool call"
    print("[9] fallback reasoning->content (tanpa tool call) OK; turn tool call tidak terpengaruh")


# --------------------------------------------------------------------------- #
# Real Ollama (opsional, --real)
# --------------------------------------------------------------------------- #
class RecordingOllama(OllamaProvider):
    """OllamaProvider yang merekam payload + raw response untuk pembuktian."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.payloads = []
        self.raws = []

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        self.payloads.append(self._build_payload(prompt, messages, options, tools, tool_choice))
        result = super().generate(prompt, messages, options, tools, tool_choice)
        self.raws.append(result.raw)
        return result


def _real_provider() -> RecordingOllama:
    """Bangun provider Ollama dari konfigurasi tersimpan (UI LLM settings)."""
    from agent_ai.llm_config import LLMConfigService
    from agent_ai.providers.factory import build_provider_from_config

    service = LLMConfigService(db_path=PROJECT_ROOT / "data" / "aether.db")
    last_error = None
    for instance in service.list_provider_instances():
        if instance.provider_type != "ollama" or instance.enabled is False:
            continue
        for model in service.list_models(instance.id):
            try:
                resolved = service.resolve_runtime_config(
                    instance.id, model_id=model.id, include_api_key=True
                )
                provider = build_provider_from_config(resolved)
            except Exception as exc:  # noqa: BLE001 - coba model berikutnya
                last_error = exc
                continue
            if isinstance(provider, OllamaProvider) and provider.is_available():
                return RecordingOllama(config=provider.config)
    raise RuntimeError(
        "Tidak ada provider instance Ollama yang siap dipakai"
        + (f" (error terakhir: {last_error})" if last_error else "")
    )


def check_real() -> None:
    from agent_ai.consultant.service import ConsultantService

    provider = _real_provider()
    print("    provider: %s model=%s num_ctx=%s" % (provider.name, provider.config.model, provider.config.num_ctx))
    num_ctx = int(provider.config.num_ctx)

    service = ConsultantService(max_steps=12)

    # --- R1..R4: Bible -----------------------------------------------------
    result = service.consult("project saya tentang apa?", provider=provider, root=str(PROJECT_ROOT), mode="quick")
    payload = provider.payloads[-1]
    raw = provider.raws[-1] or {}

    assert (payload.get("options") or {}).get("num_ctx") == num_ctx, payload.get("options")
    print("[R1] request benar-benar memuat num_ctx OK -> %s" % num_ctx)

    messages = payload.get("messages") or []
    assert messages and messages[0].get("role") == "system" and "AETHER Consultant" in messages[0]["content"]
    bible = [m for m in messages if m.get("role") == "system" and "Project Intelligence" in (m.get("content") or "")]
    assert bible, "Bible tidak ada pada request yang benar-benar dikirim"
    print("[R2] request memuat system prompt + Bible OK (Bible %d char)" % len(bible[0]["content"]))

    eval_count = int(raw.get("prompt_eval_count") or 0)
    limit = num_ctx // _OLLAMA_PROMPT_BUDGET_DIVISOR
    assert eval_count and eval_count <= limit, (
        f"prompt dipotong server: prompt_eval_count={eval_count} > batas ~{limit}"
    )
    print("[R3] prompt TIDAK dipotong server OK -> prompt_eval_count=%d (batas ~%d)" % (eval_count, limit))

    reply = (result.reply or "").strip()
    lowered = reply.lower()
    assert reply, "jawaban Consultant kosong"
    assert "tidak memiliki akses" not in lowered and "don't have access" not in lowered
    tokens = ["vue", "django", "src/agent_ai", "workbench", "consultant chat", "gateway", ".aether"]
    hits = [t for t in tokens if t in lowered]
    assert len(hits) >= 2, f"jawaban tidak menunjukkan pemakaian Bible (hits={hits}): {reply[:400]}"
    print("[R4] jawaban memakai Bible OK -> hits=%s" % hits)
    print("    reply[0:300]: %s" % reply[:300].replace("\n", " "))

    # --- R5: Project Map (atlas_query) nyata -------------------------------
    nav_question = (
        "Di mana class GatewayService didefinisikan? Gunakan tool atlas_query untuk "
        "mencari lokasinya, lalu sebutkan nama file-nya."
    )
    last_reply = ""
    last_tools = []
    for attempt in range(1, 3):
        nav = service.consult(nav_question, provider=provider, root=str(PROJECT_ROOT), mode="quick")
        last_tools = [e["tool"] for e in nav.tool_events if e.get("tool")]
        last_reply = nav.reply or ""
        assert "atlas_query" in last_tools, (
            f"Ollama tidak memakai Project Map: tool_events={last_tools}"
        )
        if attempt == 1:
            print("[R5] Ollama memakai Project Map NYATA OK -> tool_events=%s" % last_tools)
        nav_reply = last_reply.lower()
        if "services.py" in nav_reply or "django_app/api" in nav_reply:
            print("[R5b] hasil atlas_query dipakai pada jawaban OK (attempt %d) -> %s"
                  % (attempt, last_reply[:160].replace("\n", " ")))
            break
    else:
        raise AssertionError(
            "hasil atlas_query tidak dipakai pada jawaban akhir (2x percobaan): "
            f"tool_events={last_tools} reply={last_reply[:400]!r}"
        )


def main() -> int:
    real = "--real" in sys.argv
    print("=== Verifikasi Parity Ollama Local (Consultant Quick: Bible + Project Map) ===")
    tmp_root = DUMMY_ROOT / "consultant_ollama_parity"
    shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    try:
        check_config()
        check_payload_num_ctx()
        check_cloud_unchanged()
        check_budget_math()
        check_fitting()
        check_consultant_mechanism(tmp_root)
        check_thinking_fallback()
        if real:
            print()
            print("--- verifikasi Ollama NYATA (--real) ---")
            check_real()
        else:
            print()
            print("[i] bagian Ollama nyata dilewati (jalankan dengan --real bila server siap).")
        print()
        print("[OK] Parity Ollama Local Consultant Quick (Bible + Project Map) terverifikasi.")
        return 0
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        if DUMMY_ROOT.exists() and not any(DUMMY_ROOT.iterdir()):
            try:
                DUMMY_ROOT.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
