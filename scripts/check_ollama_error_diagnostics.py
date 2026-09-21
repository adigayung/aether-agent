"""Verifier: pesan `ProviderAPIError` DIAGNOSABLE (status + endpoint + body).

Tujuan (lihat task "ProviderAPIError HTTP 404 diagnosable"):
    Membuktikan bahwa kegagalan HTTP dari provider kini menghasilkan pesan yang
    memuat (1) HTTP status, (2) URL endpoint yang benar-benar dipanggil, dan
    (3) potongan body respons — sehingga 404 "model not found" dapat dibedakan
    dari 404 "page not found".

Kontrak yang diuji:
    1. Prefix pesan LAMA "Provider '<name>' mengembalikan HTTP <status>"
       DIPERTAHANKAN (kompatibilitas substring verifier lama).
    2. Body JSON bentuk Ollama  {"error": "model 'x' not found"} -> pesan ringkas.
    3. Body JSON bentuk OpenAI  {"error": {"message": "..."}}  -> pesan ringkas.
    4. Body NON-JSON (HTML/teks, mis. "404 page not found") -> potongan apa adanya.
    5. Body kosong / respons tanpa atribut `.text` -> pesan tetap valid (tanpa crash).
    6. Body sangat panjang -> dipotong (+ elipsis).
    7. TIDAK ada kebocoran secret: header Authorization / API key tidak pernah
       masuk pesan; token bergaya "Bearer <token>" di body disamarkan.
    8. `status_code` & `retryable` TIDAK berubah (404 non-retry, 429/500/529 retry).
    9. `endpoint` + `response_body` tersedia sebagai atribut error (diagnostik).
   10. Provider lain (openai_compatible / deepseek / openrouter) memakai gaya
       pesan yang sama & tetap kompatibel (prefix lama ada).
   11. `generate()` TIDAK menambah request jaringan (tidak memanggil requests.get).

Offline & deterministik: `requests.post` diganti fake response; `requests.get`
diganti agar MELEMPAR bila dipanggil. Tidak menyentuh server Ollama nyata,
tidak mengubah konfigurasi user, tidak menulis file apa pun.

Jalankan:
    python scripts/check_ollama_error_diagnostics.py
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
    DeepSeekConfig,
    OllamaConfig,
    OpenAIConfig,
    OpenRouterConfig,
)
from agent_ai.providers.base import (  # noqa: E402
    ProviderAPIError,
    build_provider_api_error,
)
from agent_ai.providers.deepseek import DeepSeekProvider  # noqa: E402
from agent_ai.providers.ollama import OllamaProvider  # noqa: E402
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.providers.openrouter import OpenRouterProvider  # noqa: E402
from agent_ai.providers.retry import InfrastructureRetryPolicy  # noqa: E402

OLLAMA_HOST = "http://127.0.0.1:11434"
OLLAMA_URL = f"{OLLAMA_HOST}/api/chat"

#: Body 404 nyata dari server Ollama native ("model not found").
OLLAMA_MODEL_NOT_FOUND = '{"error":"model \'qwen2.5-coder:7b-instruct\' not found"}'
#: Body 404 nyata bila path endpoint SALAH ("page not found", teks biasa).
PAGE_NOT_FOUND = "404 page not found"

#: Policy retry mati -> tidak ada sleep/delay nyata di verifier ini.
NO_RETRY = InfrastructureRetryPolicy(
    enabled=False, max_retries=0, base_delay=0.0, max_delay=0.0, backoff_factor=1.0
)


class FakeResponse:
    """Response HTTP palsu (status + body), tanpa jaringan."""

    def __init__(self, status_code: int, text: str = "", payload=None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class BodylessResponse:
    """Response tanpa atribut `.text`/`.content` (mis. fake minimal)."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def json(self):
        return {}


def _returns(response):
    """Bangun `requests.post` palsu yang selalu mengembalikan `response`."""

    def _post(url, **kwargs):  # noqa: ANN001
        return response

    return _post


@contextmanager
def patch_post(fake):
    """Patch `requests.post` sementara (dipakai semua provider)."""
    original = requests.post
    requests.post = fake
    try:
        yield
    finally:
        requests.post = original


@contextmanager
def patch_get_raises():
    """Patch `requests.get` agar MELEMPAR -> bukti tidak ada request tambahan."""

    def _boom(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("generate() TIDAK boleh melakukan request GET tambahan")

    original = requests.get
    requests.get = _boom
    try:
        yield
    finally:
        requests.get = original


def _ollama_provider() -> OllamaProvider:
    cfg = OllamaConfig(host=OLLAMA_HOST, model="qwen2.5-coder:7b-instruct", timeout=5)
    return OllamaProvider(config=cfg, retry_policy=NO_RETRY)


def _openai_provider() -> OpenAICompatibleProvider:
    cfg = OpenAIConfig(
        api_key="sk-topsecret-abcdef", base_url="http://test.local/v1", model="m", timeout=5
    )
    return OpenAICompatibleProvider(config=cfg, retry_policy=NO_RETRY)


def _generate_and_catch(provider) -> ProviderAPIError:
    """Panggil generate() dan kembalikan ProviderAPIError yang dilempar."""
    try:
        provider.generate(prompt="hai")
    except ProviderAPIError as exc:
        return exc
    raise AssertionError("harus raise ProviderAPIError")


def _run() -> int:
    print("=== Verifikasi ProviderAPIError diagnosable (status + endpoint + body) ===")

    # 1) Body JSON bentuk Ollama -> pesan ringkas + prefix lama dipertahankan.
    provider = _ollama_provider()
    with patch_post(lambda url, **kw: FakeResponse(404, OLLAMA_MODEL_NOT_FOUND)), patch_get_raises():
        exc = _generate_and_catch(provider)
    message = str(exc)
    assert "Provider 'ollama' mengembalikan HTTP 404" in message, message
    assert OLLAMA_URL in message, message
    assert "model 'qwen2.5-coder:7b-instruct' not found" in message, message
    assert exc.status_code == 404, exc.status_code
    assert exc.retryable is False, exc.retryable
    assert exc.endpoint == OLLAMA_URL, exc.endpoint
    assert exc.response_body and "not found" in exc.response_body, exc.response_body
    print(f"[1] body JSON (model not found) OK -> {message}")

    # 2) Body JSON bentuk OpenAI-compatible -> pesan error diekstrak.
    openai_body = (
        '{"error":{"message":"Invalid model","type":"invalid_request_error"},'
        '"status":404}'
    )
    with patch_post(lambda url, **kw: FakeResponse(404, openai_body)), patch_get_raises():
        exc = _generate_and_catch(_openai_provider())
    assert "Provider 'openai' mengembalikan HTTP 404" in str(exc), str(exc)
    assert "Invalid model" in str(exc), str(exc)
    assert "invalid_request_error" not in str(exc), str(exc)  # ringkas, bukan dump mentah
    print(f"[2] body JSON (error.message) OK -> {exc}")

    # 3) Body NON-JSON (path salah) -> potongan apa adanya; beda jelas dari #1.
    with patch_post(lambda url, **kw: FakeResponse(404, PAGE_NOT_FOUND)), patch_get_raises():
        exc = _generate_and_catch(_ollama_provider())
    assert PAGE_NOT_FOUND in str(exc), str(exc)
    assert "model" not in str(exc), str(exc)
    print(f"[3] body non-JSON (page not found) OK -> {exc}")

    # 4) Body kosong -> pesan tetap valid, berakhir titik, tanpa kurung kosong.
    with patch_post(lambda url, **kw: FakeResponse(404, "")), patch_get_raises():
        exc = _generate_and_catch(_ollama_provider())
    message = str(exc)
    assert "Provider 'ollama' mengembalikan HTTP 404" in message, message
    assert message.endswith("."), message
    assert "()" not in message, message
    assert exc.response_body is None, exc.response_body
    print(f"[4] body kosong OK -> {message}")

    # 5) Respons tanpa atribut `.text` (fake minimal) -> tidak crash.
    with patch_post(lambda url, **kw: BodylessResponse(500)), patch_get_raises():
        exc = _generate_and_catch(_ollama_provider())
    assert "Provider 'ollama' mengembalikan HTTP 500" in str(exc), str(exc)
    assert exc.retryable is True, exc.retryable  # 500 tetap infrastruktur
    print(f"[5] respons tanpa body OK -> {exc}")

    # 6) Body sangat panjang -> dipotong (+ elipsis), tidak membanjiri pesan.
    long_body = "x" * 5000
    with patch_post(lambda url, **kw: FakeResponse(400, long_body)), patch_get_raises():
        exc = _generate_and_catch(_ollama_provider())
    message = str(exc)
    assert message.endswith("…"), message[-20:]
    assert len(message) < 600, len(message)
    assert long_body not in message, "body tidak boleh dikirim utuh"
    print(f"[6] body panjang dipotong OK -> len(message)={len(message)}")

    # 7) TIDAK ada kebocoran secret (API key / Bearer token).
    leak_body = '{"error":"unauthorized: Bearer sk-bodyleak-9999"}'
    with patch_post(lambda url, **kw: FakeResponse(401, leak_body)), patch_get_raises():
        exc = _generate_and_catch(_openai_provider())
    message = str(exc)
    assert "Authorization" not in message, message
    assert "sk-topsecret-abcdef" not in message, "API key provider bocor!"
    assert "sk-bodyleak-9999" not in message, "token di body bocor!"
    assert "Bearer <redacted>" in message, message
    print(f"[7] tanpa kebocoran secret OK -> {message}")

    # 8) Status non-2xx lain tetap konsisten (retryable hanya 429/500/529).
    for status, expected_retryable in ((403, False), (429, True), (529, True), (503, False)):
        body = '{"error":{"message":"boom"}}'
        with patch_post(_returns(FakeResponse(status, body))), patch_get_raises():
            exc = _generate_and_catch(_openai_provider())
        assert exc.status_code == status, exc.status_code
        assert exc.retryable is expected_retryable, (status, exc.retryable)
    print("[8] status_code & retryable tidak berubah OK (403/429/529/503)")

    # 9) Provider lain (deepseek/openrouter subclass) memakai gaya pesan sama.
    for cls, cfg, name, expect_url in (
        (
            DeepSeekProvider,
            DeepSeekConfig(api_key="k", base_url="http://ds.local/v1", model="m", timeout=5),
            "deepseek",
            "http://ds.local/v1/chat/completions",
        ),
        (
            OpenRouterProvider,
            OpenRouterConfig(api_key="k", base_url="http://or.local/api/v1", model="m", timeout=5),
            "openrouter",
            "http://or.local/api/v1/chat/completions",
        ),
    ):
        provider = cls(config=cfg)
        with patch_post(lambda url, **kw: FakeResponse(404, '{"error":{"message":"nope"}}')), patch_get_raises():
            exc = _generate_and_catch(provider)
        assert f"Provider '{name}' mengembalikan HTTP 404" in str(exc), str(exc)
        assert expect_url in str(exc), str(exc)
    print("[9] deepseek/openrouter pesan kompatibel OK")

    # 10) Kompatibilitas konstruktor lama ProviderAPIError.
    assert ProviderAPIError("x").retryable is False
    assert ProviderAPIError("x", 429).retryable is True
    assert ProviderAPIError("x", status_code=429).endpoint is None
    assert ProviderAPIError("x", status_code=429).response_body is None
    # Helper publik: prefix lama tetap utuh meski tanpa detail apa pun.
    bare = build_provider_api_error("ollama", 404)
    assert str(bare) == "Provider 'ollama' mengembalikan HTTP 404.", str(bare)
    print(f"[10] kompatibilitas ProviderAPIError lama OK -> {bare}")

    # 11) is_available() tetap boolean & TIDAK melempar saat jaringan gagal.
    with patch_get_raises():
        try:
            requests.get("http://127.0.0.1:11434/api/tags", timeout=1)
            raise AssertionError("patch_get_raises gagal")
        except AssertionError:
            pass

    def _raising_get(*args, **kwargs):  # noqa: ANN001
        raise requests.RequestException("connection refused")

    original_get = requests.get
    requests.get = _raising_get
    try:
        available = _ollama_provider().is_available()
    finally:
        requests.get = original_get
    assert available is False, available
    assert isinstance(available, bool), type(available)
    print("[11] is_available() tetap boolean & tidak melempar OK")

    print()
    print("[OK] Pesan ProviderAPIError diagnosable (status + endpoint + body, tanpa secret).")
    return 0


def main() -> int:
    return _run()


if __name__ == "__main__":
    raise SystemExit(main())
