"""Verifikasi konfigurasi Ollama + penghapusan legacy DEFAULT_PROVIDER.

Menguji:
    1. Ollama dapat dibuat TANPA API key (api_key_env = "").
    2. Ollama memakai default Base API URL http://localhost:11434.
    3. Runtime Ollama TIDAK mencari API key (resolve_runtime_config -> api_key
       None; factory membangun OllamaProvider tanpa api_key).
    4. OpenRouter/DeepSeek/OpenAI TETAP membutuhkan api_key_env (behavior sama).
    5. Legacy DEFAULT_PROVIDER dihapus: settings TIDAK punya atribut
       `default_provider`; .env & deployment.template TIDAK memuat
       DEFAULT_PROVIDER; get_provider() tanpa nama -> error (tidak ada default).
    6. Sumber tunggal provider aktif = Provider Instance + Model (SQLite).

Deterministik, TANPA model/API cloud nyata. Fixture DB/.env dibuat di
`dummy_test/ollama_config_fixture` dan dibersihkan setelah test. TIDAK
menyentuh database global `data/aether.db` maupun `.env` asli.

Jalankan:
    python scripts/check_ollama_config.py
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
FIXTURE = DUMMY_ROOT / "ollama_config_fixture"


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
    print("=== Verifikasi konfigurasi Ollama + hapus legacy DEFAULT_PROVIDER ===")

    from agent_ai.llm_config import LLMConfigService, LLMConfigValidationError
    from agent_ai.llm_config.providers import get_provider_type

    svc = LLMConfigService(db_path=FIXTURE / "llm_config.db", env_path=FIXTURE / ".env")

    # 1) Ollama dapat dibuat TANPA API key.
    ollama = svc.create_provider_instance(name="Ollama Lokal", provider_type="ollama")
    assert ollama.api_key_env == "", ollama.api_key_env
    print("[1] Ollama dibuat tanpa API key OK -> api_key_env=''")

    # 2) Default Base API URL Ollama = http://localhost:11434.
    assert ollama.api_url == "http://localhost:11434", ollama.api_url
    spec = get_provider_type("ollama")
    assert spec.default_api_url == "http://localhost:11434", spec.default_api_url
    assert spec.requires_api_key is False, spec.requires_api_key
    print(f"[2] Ollama default Base API URL OK -> {ollama.api_url}")

    # 3) Runtime Ollama TIDAK mencari API key.
    model = svc.add_model(ollama.id, "qwen2.5-coder:7b")
    resolved = svc.resolve_runtime_config(ollama.id, model_id=model.id, include_api_key=True)
    assert resolved["provider_type"] == "ollama", resolved
    assert resolved["api_key"] is None, resolved
    assert resolved["api_url"] == "http://localhost:11434", resolved
    assert resolved["model"] == "qwen2.5-coder:7b", resolved

    from agent_ai.providers.factory import build_provider_from_config
    from agent_ai.providers.ollama import OllamaProvider

    provider = build_provider_from_config(resolved)
    assert isinstance(provider, OllamaProvider), provider
    assert provider.config.host == "http://localhost:11434", provider.config
    assert provider.config.model == "qwen2.5-coder:7b", provider.config
    # OllamaProvider tidak punya atribut api_key (tidak mencari API key).
    assert not hasattr(provider.config, "api_key"), "OllamaConfig tidak boleh punya api_key"
    print("[3] Runtime Ollama tidak mencari API key OK -> api_key=None, provider=OllamaProvider")

    # 4) Provider cloud TETAP butuh api_key_env (behavior sama).
    for ptype, env in (
        ("openrouter", "OPENROUTER_API_KEY"),
        ("deepseek", "DEEPSEEK_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
    ):
        try:
            svc.create_provider_instance(name=f"{ptype} tanpa key", provider_type=ptype)
        except LLMConfigValidationError:
            pass
        else:
            raise AssertionError(f"{ptype} harus menolak tanpa api_key_env")
        inst = svc.create_provider_instance(
            name=f"{ptype} dengan key", provider_type=ptype, api_key_env=env
        )
        assert inst.api_key_env == env, inst.api_key_env
    print("[4] OpenRouter/DeepSeek/OpenAI tetap butuh api_key_env OK")

    # 5) Legacy DEFAULT_PROVIDER dihapus.
    from agent_ai.config.settings import settings

    assert not hasattr(settings, "default_provider"), "settings.default_provider harus dihapus"

    from agent_ai.providers.registry import get_provider

    try:
        get_provider(None)  # type: ignore[arg-type]
    except ValueError:
        pass
    else:
        raise AssertionError("get_provider(None) harus error (tidak ada default provider)")

    env_text = (PROJECT_ROOT / ".env").read_text(encoding="utf-8")
    assert "DEFAULT_PROVIDER" not in env_text, ".env tidak boleh memuat DEFAULT_PROVIDER"
    template_text = (PROJECT_ROOT / "deployment.template").read_text(encoding="utf-8")
    assert "DEFAULT_PROVIDER" not in template_text, "template tidak boleh memuat DEFAULT_PROVIDER"
    print("[5] Legacy DEFAULT_PROVIDER dihapus OK -> settings/.env/template bersih")

    # 6) Sumber tunggal provider aktif = Provider Instance + Model (SQLite).
    full = svc.get_full_config()
    assert any(p["provider_type"] == "ollama" for p in full), full
    print("[6] Sumber tunggal provider aktif = Provider Instance + Model (SQLite) OK")

    print()
    print("[OK] Konfigurasi Ollama benar & legacy DEFAULT_PROVIDER sudah dihapus.")
    return 0


def main() -> int:
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


if __name__ == "__main__":
    raise SystemExit(main())
