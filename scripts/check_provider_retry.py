"""Verifikasi Provider Infrastructure Retry (technical only).

Deterministik, tanpa API cloud, tanpa delay nyata. Menguji retry INFRASTRUKTUR
di layer provider (network/timeout/connection + HTTP 429/500/529) dengan
backoff eksponensial TERBATAS, tanpa menyentuh loop/history/tool/command.

Yang diuji:
    1. klasifikasi error infrastruktur (hanya technical)
    2. flag retryable pada ProviderError & subclass
    3. retry pada connection error (RequestException)
    4. retry pada timeout (ReadTimeout/ConnectTimeout)
    5. retry pada HTTP 429/500/529
    6. HTTP 400/401/403/404 TIDAK di-retry
    7. bounded: connection error berulang -> ProviderUnavailableError
    8. bounded: status retryable berulang -> response dikembalikan (diputuskan caller)
    9. backoff eksponensial dibatasi max_delay + policy disable -> 0 retry
   10. policy default dibaca dari settings (tidak di-hardcode provider)
   11. integrasi provider OpenAI-compatible: 500,500,200 -> sukses (1 generate)
   12. integrasi provider: 401 non-retryable -> ProviderAPIError (0 retry)
   13. integrasi provider: 429 habis kuota -> ProviderAPIError (retryable)
   14. integrasi provider Ollama: 500,200 -> sukses
   15. tidak ada duplicate chat turn: retry terjadi DI DALAM satu generate()
   16. architecture boundary bersih (tanpa runtime/executor kedua, tanpa network di core)
   17. deterministik (sequence sama -> perilaku sama)

Jalankan:
    python scripts/check_provider_retry.py
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import requests  # noqa: E402

from agent_ai.config.settings import (  # noqa: E402
    OllamaConfig,
    OpenAIConfig,
    settings,
)
from agent_ai.core import AgentOrchestrator, AgentStatus  # noqa: E402
from agent_ai.providers.base import (  # noqa: E402
    ProviderAPIError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from agent_ai.providers.ollama import OllamaProvider  # noqa: E402
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.providers.retry import (  # noqa: E402
    InfrastructureRetryPolicy,
    is_infrastructure_error,
    post_with_infrastructure_retry,
)

AGENT_AI = SRC_DIR / "agent_ai"
RETRY_FILE = AGENT_AI / "providers" / "retry.py"


# ---------------------------------------------------------------------------
# Fake transport (deterministik, tanpa jaringan)
# ---------------------------------------------------------------------------
class FakeResponse:
    """Response HTTP palsu minimal untuk provider."""

    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def openai_ok(text: str = "selesai") -> FakeResponse:
    return FakeResponse(
        200,
        {"model": "test-model", "choices": [{"message": {"content": text}, "finish_reason": "stop"}]},
    )


def ollama_ok(text: str = "selesai") -> FakeResponse:
    return FakeResponse(200, {"model": "test-model", "message": {"content": text}, "done": True})


def make_post(script, counter):
    """Bangun fungsi requests.post dari scripted list.

    Item script: int (status HTTP), ("json", resp), atau Exception (di-raise).
    Item terakhir diulang bila script habis.
    """

    def _post(url, **kwargs):  # noqa: ANN001 - signature meniru requests.post
        idx = counter["calls"]
        counter["calls"] += 1
        item = script[min(idx, len(script) - 1)]
        if isinstance(item, BaseException) or (isinstance(item, type) and issubclass(item, BaseException)):
            raise item if isinstance(item, BaseException) else item()
        return item if isinstance(item, FakeResponse) else FakeResponse(int(item))

    return _post


@contextmanager
def patch_post(fake):
    """Patch requests.post sementara (dipakai provider openai/ollama)."""
    original = requests.post
    requests.post = fake
    try:
        yield
    finally:
        requests.post = original


class CountingOpenAIProvider(OpenAICompatibleProvider):
    """OpenAICompatibleProvider yang menghitung pemanggilan generate()."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.generate_calls = 0

    def generate(self, *args, **kwargs):
        self.generate_calls += 1
        return super().generate(*args, **kwargs)


NO_SLEEP = lambda _seconds: None  # noqa: E731 - sengaja: hindari delay nyata
FAST_POLICY = InfrastructureRetryPolicy(
    enabled=True, max_retries=3, base_delay=0.0, max_delay=0.0, backoff_factor=1.0
)


def main() -> int:
    print("=== Verifikasi Provider Infrastructure Retry ===")
    return _run()


def _run() -> int:
    # 1) klasifikasi error infrastruktur (hanya technical).
    assert is_infrastructure_error(ProviderUnavailableError("conn")) is True
    assert is_infrastructure_error(ProviderAPIError("429", status_code=429)) is True
    assert is_infrastructure_error(ProviderAPIError("500", status_code=500)) is True
    assert is_infrastructure_error(ProviderAPIError("529", status_code=529)) is True
    # Kegagalan logika agent / permanen BUKAN infrastruktur.
    assert is_infrastructure_error(ProviderAPIError("400", status_code=400)) is False
    assert is_infrastructure_error(ProviderAPIError("401", status_code=401)) is False
    assert is_infrastructure_error(ProviderResponseError("bad json")) is False
    assert is_infrastructure_error(RuntimeError("tool gagal")) is False
    assert is_infrastructure_error(KeyError("x")) is False
    # RequestException mentah dari requests dianggap infrastruktur.
    assert is_infrastructure_error(requests.ConnectionError("x")) is True
    assert is_infrastructure_error(requests.ReadTimeout("x")) is True
    print("[1] klasifikasi error infrastruktur OK")

    # 2) flag retryable pada ProviderError & subclass.
    assert ProviderError.retryable is False
    assert ProviderNotConfiguredError("x").retryable is False
    assert ProviderResponseError("x").retryable is False
    assert ProviderUnavailableError("x").retryable is True
    assert ProviderAPIError("x", status_code=429).retryable is True
    assert ProviderAPIError("x", status_code=500).retryable is True
    assert ProviderAPIError("x", status_code=404).retryable is False
    assert ProviderAPIError("x").retryable is False  # tanpa status -> aman: tidak retry
    print("[2] flag retryable pada ProviderError OK")

    # 3) retry pada connection error.
    counter = {"calls": 0}

    def send_conn():
        counter["calls"] += 1
        if counter["calls"] < 3:
            raise requests.ConnectionError("boom")
        return FakeResponse(200)

    resp = post_with_infrastructure_retry(
        send_conn, policy=FAST_POLICY, provider_name="x", endpoint="http://x", sleep=NO_SLEEP
    )
    assert resp.status_code == 200 and counter["calls"] == 3, counter
    print(f"[3] retry connection error OK -> calls={counter['calls']}")

    # 4) retry pada timeout.
    counter = {"calls": 0}

    def send_timeout():
        counter["calls"] += 1
        if counter["calls"] == 1:
            raise requests.ReadTimeout("timeout")
        if counter["calls"] == 2:
            raise requests.ConnectTimeout("timeout")
        return FakeResponse(200)

    resp = post_with_infrastructure_retry(
        send_timeout, policy=FAST_POLICY, provider_name="x", endpoint="http://x", sleep=NO_SLEEP
    )
    assert resp.status_code == 200 and counter["calls"] == 3, counter
    print(f"[4] retry timeout OK -> calls={counter['calls']}")

    # 5) retry pada HTTP 429/500/529.
    for status in (429, 500, 529):
        counter = {"calls": 0}
        send = make_post([status, status, FakeResponse(200)], counter)
        resp = post_with_infrastructure_retry(
            lambda: send("http://x"),
            policy=FAST_POLICY,
            provider_name="x",
            endpoint="http://x",
            sleep=NO_SLEEP,
        )
        assert resp.status_code == 200 and counter["calls"] == 3, (status, counter)
    print("[5] retry HTTP 429/500/529 OK")

    # 6) HTTP 400/401/403/404 TIDAK di-retry (langsung dikembalikan).
    for status in (400, 401, 403, 404):
        counter = {"calls": 0}
        send = make_post([status], counter)
        resp = post_with_infrastructure_retry(
            lambda: send("http://x"),
            policy=FAST_POLICY,
            provider_name="x",
            endpoint="http://x",
            sleep=NO_SLEEP,
        )
        assert resp.status_code == status and counter["calls"] == 1, (status, counter)
    print("[6] HTTP 400/401/403/404 tidak di-retry OK")

    # 7) bounded: connection error berulang -> ProviderUnavailableError.
    counter = {"calls": 0}

    def send_fail():
        counter["calls"] += 1
        raise requests.ConnectionError("refused")

    policy = InfrastructureRetryPolicy(enabled=True, max_retries=3, base_delay=0.0, max_delay=0.0)
    try:
        post_with_infrastructure_retry(
            send_fail, policy=policy, provider_name="x", endpoint="http://x", sleep=NO_SLEEP
        )
        raise AssertionError("harus raise ProviderUnavailableError")
    except ProviderUnavailableError as exc:
        # Pesan tidak boleh memuat header/API key.
        assert "Bearer" not in str(exc) and "Authorization" not in str(exc)
    assert counter["calls"] == 4, counter  # 1 percobaan + 3 retry
    print(f"[7] bounded connection error OK -> calls={counter['calls']}")

    # 8) bounded: status retryable berulang -> response dikembalikan apa adanya.
    counter = {"calls": 0}
    send429 = make_post([429], counter)
    resp = post_with_infrastructure_retry(
        lambda: send429("http://x"),
        policy=InfrastructureRetryPolicy(enabled=True, max_retries=2, base_delay=0.0, max_delay=0.0),
        provider_name="x",
        endpoint="http://x",
        sleep=NO_SLEEP,
    )
    assert resp.status_code == 429 and counter["calls"] == 3, counter  # 1 + 2 retry
    print(f"[8] bounded retryable status OK -> calls={counter['calls']}")

    # 9) backoff eksponensial dibatasi max_delay + policy disable -> 0 retry.
    p = InfrastructureRetryPolicy(enabled=True, max_retries=5, base_delay=1.0, max_delay=4.0, backoff_factor=2.0)
    assert p.delay_for(0) == 1.0 and p.delay_for(1) == 2.0
    assert p.delay_for(2) == 4.0 and p.delay_for(3) == 4.0  # dibatasi max_delay
    assert p.effective_max_retries == 5
    off = InfrastructureRetryPolicy(enabled=False, max_retries=5)
    assert off.effective_max_retries == 0
    counter = {"calls": 0}

    def send_fail2():
        counter["calls"] += 1
        raise requests.ConnectionError("refused")

    try:
        post_with_infrastructure_retry(
            send_fail2, policy=off, provider_name="x", endpoint="http://x", sleep=NO_SLEEP
        )
        raise AssertionError("harus raise")
    except ProviderUnavailableError:
        pass
    assert counter["calls"] == 1, counter  # disabled -> tidak ada retry
    print("[9] backoff dibatasi + policy disable OK")

    # 10) policy default dibaca dari settings (tidak di-hardcode provider).
    from_settings = InfrastructureRetryPolicy.from_settings()
    assert from_settings.enabled == settings.provider_retry.enabled
    assert from_settings.max_retries == settings.provider_retry.max_retries
    assert from_settings.base_delay == settings.provider_retry.base_delay
    assert from_settings.max_delay == settings.provider_retry.max_delay
    assert from_settings.backoff_factor == settings.provider_retry.backoff_factor
    assert 3 <= settings.provider_retry.max_retries <= 5, settings.provider_retry.max_retries
    print(f"[10] policy dari settings OK -> max_retries={from_settings.max_retries}")

    # 11) integrasi provider OpenAI-compatible: 500,500,200 -> sukses (1 generate).
    cfg = OpenAIConfig(api_key="test-key", base_url="http://test.local/v1", model="test-model", timeout=5)
    provider = OpenAICompatibleProvider(config=cfg, retry_policy=FAST_POLICY)
    counter = {"calls": 0}
    with patch_post(make_post([500, 500, openai_ok("halo")], counter)):
        result = provider.generate(prompt="hai")
    assert result.text == "halo", result.text
    assert counter["calls"] == 3, counter
    print(f"[11] integrasi OpenAI-compatible retry->sukses OK -> calls={counter['calls']}")

    # 12) integrasi provider: 401 non-retryable -> ProviderAPIError (0 retry).
    provider = OpenAICompatibleProvider(config=cfg, retry_policy=FAST_POLICY)
    counter = {"calls": 0}
    with patch_post(make_post([401], counter)):
        try:
            provider.generate(prompt="hai")
            raise AssertionError("harus raise ProviderAPIError")
        except ProviderAPIError as exc:
            assert exc.status_code == 401 and exc.retryable is False
    assert counter["calls"] == 1, counter
    print(f"[12] integrasi 401 non-retryable OK -> calls={counter['calls']}")

    # 13) integrasi provider: 429 habis kuota -> ProviderAPIError (retryable).
    provider = OpenAICompatibleProvider(
        config=cfg,
        retry_policy=InfrastructureRetryPolicy(enabled=True, max_retries=2, base_delay=0.0, max_delay=0.0),
    )
    counter = {"calls": 0}
    with patch_post(make_post([429], counter)):
        try:
            provider.generate(prompt="hai")
            raise AssertionError("harus raise ProviderAPIError")
        except ProviderAPIError as exc:
            assert exc.status_code == 429 and exc.retryable is True
    assert counter["calls"] == 3, counter  # 1 + 2 retry
    print(f"[13] integrasi 429 retryable habis kuota OK -> calls={counter['calls']}")

    # 14) integrasi provider Ollama: 500,200 -> sukses.
    ocfg = OllamaConfig(host="http://127.0.0.1:11434", model="test-model", timeout=5)
    op = OllamaProvider(config=ocfg, retry_policy=FAST_POLICY)
    counter = {"calls": 0}
    with patch_post(make_post([500, ollama_ok("halo-ollama")], counter)):
        result = op.generate(prompt="hai")
    assert result.text == "halo-ollama", result.text
    assert counter["calls"] == 2, counter
    print(f"[14] integrasi Ollama retry->sukses OK -> calls={counter['calls']}")

    # 15) tidak ada duplicate chat turn: retry terjadi DI DALAM satu generate().
    provider = CountingOpenAIProvider(config=cfg, retry_policy=FAST_POLICY)
    counter = {"calls": 0}
    orchestrator = AgentOrchestrator(provider=provider, use_continuous_loop=True)
    with patch_post(make_post([500, openai_ok("jawaban final")], counter)):
        result = orchestrator.run("halo")
    assert result.status == AgentStatus.DONE, result.status
    assert result.result == "jawaban final", result.result
    assert result.steps == [], result.steps
    # 2 panggilan transport, tetapi HANYA 1 pemanggilan generate() -> satu turn
    # LLM. Tidak ada pesan assistant/tool yang terduplikasi di ConversationHistory.
    assert counter["calls"] == 2, counter
    assert provider.generate_calls == 1, provider.generate_calls
    print(
        f"[15] tidak ada duplicate chat turn OK -> transport={counter['calls']}, "
        f"generate={provider.generate_calls}"
    )

    # 16) architecture boundary bersih.
    text = RETRY_FILE.read_text(encoding="utf-8")
    # Retry murni transport: tidak impor core/runtime/concrete provider.
    for bad in (
        "agent_ai.core",
        "agent_ai.runtime",
        "agent_ai.planning",
        "agent_ai.reliability",
        "agent_ai.providers.ollama",
        "agent_ai.providers.deepseek",
        "agent_ai.providers.openrouter",
        "agent_ai.providers.openai_compatible",
        "import subprocess",
        "os.system(",
    ):
        assert bad not in text, f"retry.py tidak boleh: {bad}"
    # Retry tidak membuat runtime/executor/loop kedua.
    for cls in ("class AgentRuntime", "class ToolExecutor", "class AgentLoop", "class AgentOrchestrator"):
        assert cls not in text, f"retry.py tidak boleh membuat {cls}"
    # Retry tidak menambah/mengubah pesan/turn percakapan.
    assert "append" not in text, "retry tidak boleh menambah pesan/turn"
    # Provider hanya memakai helper retry (bukan mengimpor ulang requests langsung
    # di fungsi generate) — tetap lewat satu jalur.
    oc = (AGENT_AI / "providers" / "openai_compatible.py").read_text(encoding="utf-8")
    oll = (AGENT_AI / "providers" / "ollama.py").read_text(encoding="utf-8")
    assert "post_with_infrastructure_retry(" in oc and "post_with_infrastructure_retry(" in oll
    print("[16] architecture boundary bersih OK")

    # 17) deterministik (sequence sama -> perilaku sama).
    def run_once():
        c = {"calls": 0}
        pr = OpenAICompatibleProvider(config=cfg, retry_policy=FAST_POLICY)
        with patch_post(make_post([500, openai_ok("ok")], c)):
            r = pr.generate(prompt="x")
        return c["calls"], r.text

    a = run_once()
    b = run_once()
    assert a == b, (a, b)
    print(f"[17] deterministik OK -> {a}")

    print()
    print("[OK] Provider Infrastructure Retry bekerja (bounded, technical-only, tanpa duplicate turn).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
