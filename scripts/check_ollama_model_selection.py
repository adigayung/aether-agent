"""Verifier: pemilihan model Ollama mengikuti source of truth SQLite.

Membuktikan bahwa alur Ollama TIDAK memakai model default hardcode
(`qwen2.5-coder:7b`) dan mengikuti Provider Instance -> Model dari SQLite,
sama seperti DeepSeek/OpenRouter.

Kontrak yang dibuktikan:
    1. Instance Ollama TANPA model di DB -> `build_provider_from_config`
       GAGAL dengan ProviderNotConfiguredError (bukan fallback diam-diam ke
       qwen2.5-coder:7b).
    2. Instance Ollama DENGAN model di DB -> provider memakai model tersebut
       (bukan default hardcode).
    3. `resolve_runtime_config` mengembalikan model dari DB.
    4. DeepSeek/OpenRouter tetap memakai model dari DB (tidak berubah).

Deterministik, TANPA network/API cloud. Fixture DB/.env dibuat di
`dummy_test/ollama_model_selection_fixture` dan dibersihkan setelah test.
TIDAK menyentuh database global `data/aether.db` maupun `.env` asli.

Jalankan:
    python scripts/check_ollama_model_selection.py
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"

for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "ollama_model_selection_fixture"

TARGET_MODEL = "LisyNeko/qwen3.8-9b-coder:latest"


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / ".env").write_text("# dummy env (tanpa secret)\n", encoding="utf-8")


def teardown_fixture() -> None:
    import gc

    for _ in range(10):
        gc.collect()
        shutil.rmtree(FIXTURE, ignore_errors=True)
        if not FIXTURE.exists():
            break
        time.sleep(0.1)
    try:
        if DUMMY_ROOT.exists() and not any(DUMMY_ROOT.iterdir()):
            DUMMY_ROOT.rmdir()
    except OSError:
        pass


def _run() -> int:
    print("=== Verifikasi Pemilihan Model Ollama (source of truth SQLite) ===")

    from agent_ai.llm_config import LLMConfigService
    from agent_ai.providers.base import ProviderNotConfiguredError
    from agent_ai.providers.factory import build_provider_from_config
    from agent_ai.providers.ollama import OllamaProvider

    svc = LLMConfigService(db_path=FIXTURE / "llm_config.db", env_path=FIXTURE / ".env")

    # 1) Instance Ollama TANPA model -> error jelas (bukan fallback hardcode).
    ollama = svc.create_provider_instance(name="Ollama Lokal", provider_type="ollama")
    resolved_empty = svc.resolve_runtime_config(ollama.id, include_api_key=True)
    assert resolved_empty["model"] == "", resolved_empty
    print(f"[1] resolve tanpa model -> model={resolved_empty['model']!r} (kosong)")

    try:
        build_provider_from_config(resolved_empty)
    except ProviderNotConfiguredError as exc:
        print(f"[1] OK: tanpa model -> ProviderNotConfiguredError: {exc}")
    else:
        print("[1] FAIL: seharusnya ProviderNotConfiguredError (bukan fallback hardcode)")
        return 1

    # 2) Instance Ollama DENGAN model -> provider memakai model dari DB.
    model = svc.add_model(ollama.id, TARGET_MODEL)
    resolved = svc.resolve_runtime_config(ollama.id, model_id=model.id, include_api_key=True)
    assert resolved["model"] == TARGET_MODEL, resolved
    print(f"[2] resolve dengan model -> model={resolved['model']!r}")

    provider = build_provider_from_config(resolved)
    assert isinstance(provider, OllamaProvider), provider
    assert provider.config.model == TARGET_MODEL, provider.config
    assert provider.config.model != "qwen2.5-coder:7b", "TIDAK boleh fallback ke hardcode"
    print(f"[2] OK: provider memakai model dari DB -> {provider.config.model!r}")

    # 3) Model dari DB benar-benar dipakai di payload (bukan default provider).
    payload = provider._build_payload(
        prompt=None,
        messages=[__import__("agent_ai.providers.base", fromlist=["Message"]).Message(role="user", content="hi")],
        options=None,
    )
    assert payload["model"] == TARGET_MODEL, payload["model"]
    print(f"[3] OK: payload.model dari DB -> {payload['model']!r}")

    # 4) DeepSeek/OpenRouter tetap memakai model dari DB (tidak berubah).
    ds = svc.create_provider_instance(
        name="DeepSeek", provider_type="deepseek", api_key_env="DEEPSEEK_API_KEY"
    )
    ds_model = svc.add_model(ds.id, "deepseek-chat")
    ds_resolved = svc.resolve_runtime_config(ds.id, model_id=ds_model.id, include_api_key=True)
    assert ds_resolved["model"] == "deepseek-chat", ds_resolved
    print(f"[4] OK: DeepSeek tetap pakai model DB -> {ds_resolved['model']!r}")

    orouter = svc.create_provider_instance(
        name="OpenRouter", provider_type="openrouter", api_key_env="OPENROUTER_API_KEY"
    )
    or_model = svc.add_model(orouter.id, "openai/gpt-4o-mini")
    or_resolved = svc.resolve_runtime_config(orouter.id, model_id=or_model.id, include_api_key=True)
    assert or_resolved["model"] == "openai/gpt-4o-mini", or_resolved
    print(f"[4] OK: OpenRouter tetap pakai model DB -> {or_resolved['model']!r}")

    print()
    print("[OK] Pemilihan model Ollama mengikuti source of truth SQLite "
          "(tanpa fallback hardcode qwen2.5-coder:7b).")
    return 0


def main() -> int:
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


if __name__ == "__main__":
    raise SystemExit(main())
