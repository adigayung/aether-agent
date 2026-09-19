"""Verifikasi sistem konfigurasi LLM AETHER (`agent_ai.llm_config`).

Menguji:
    - katalog provider type (openrouter/deepseek/openai/ollama).
    - persistensi provider instance + model ke SQLite GLOBAL (data/aether.db).
    - API key TIDAK PERNAH disimpan di SQLite (hanya nama variabel .env).
    - validasi relasi API Key (.env) -> Provider Instance (prefix env).
    - CRUD model (1 provider instance -> banyak model) + cascade delete.
    - manajemen .env yang aman (hanya baris terkait berubah, komentar utuh).
    - discovery credential berpola provider + `_API_KEY` (yang lain diabaikan).
    - pembacaan konfigurasi untuk runtime (resolve_runtime_config).

Fixture (DB & .env sementara) dibuat di `dummy_test/` lalu dibersihkan.
TIDAK menyentuh database global `data/aether.db` maupun `.env` asli.

Jalankan:
    python scripts/check_llm_config.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.llm_config import (  # noqa: E402
    LLMConfigConflictError,
    LLMConfigService,
    LLMConfigValidationError,
    is_api_key_env_name,
    list_provider_types,
    parse_api_key_env_name,
)

# ---------------------------------------------------------------------------
# Fixture .env (sengaja berisi komentar, variabel lain, dan credential provider).
# ---------------------------------------------------------------------------
ENV_FIXTURE = """# Dummy .env untuk verifikasi (JANGAN dipakai produksi)
AETHER_ENV=development
OLLAMA_HOST=http://127.0.0.1:11434

# Provider keys
OPENROUTER_API_KEY=sk-or-v1-EXAMPLE-SECRET-1234567890
ANOTHER_SECRET=jangan-sentuh
"""

SECRET_OPENROUTER = "sk-or-v1-EXAMPLE-SECRET-1234567890"
SECRET_TEMAN = "sk-or-v1-SECOND-ACC-SECRET-0987654321"

RELEVANT_ENV_NAMES = (
    "OPENROUTER_API_KEY",
    "OPENROUTER_API_KEY_AKUN_TEMAN",
    "DEEPSEEK_API_KEY",
)


def main() -> int:
    print("=== Verifikasi Konfigurasi LLM AETHER ===")

    # Isolasi: jangan biarkan environment proses mengganggu penentuan is_set.
    for name in RELEVANT_ENV_NAMES:
        os.environ.pop(name, None)

    dummy_root = PROJECT_ROOT / "dummy_test"
    dummy_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="llm_config_", dir=str(dummy_root)))
    db_path = workdir / "llm_config_test.db"
    env_path = workdir / ".env"
    env_path.write_text(ENV_FIXTURE, encoding="utf-8")

    try:
        # ------------------------------------------------------------- #
        # 1) Katalog provider type + pola env API key
        # ------------------------------------------------------------- #
        types = {t.key for t in list_provider_types()}
        print(f"provider types      : {sorted(types)}")
        assert types >= {"openrouter", "deepseek", "openai", "ollama"}, types
        assert is_api_key_env_name("OPENROUTER_API_KEY")
        assert is_api_key_env_name("OPENROUTER_API_KEY_AKUN_TEMAN")
        assert not is_api_key_env_name("OPENROUTER_MODEL")
        assert not is_api_key_env_name("OLLAMA_HOST")
        assert not is_api_key_env_name("PATH")
        assert parse_api_key_env_name("OPENROUTER_API_KEY_AKUN_TEMAN") == (
            "OPENROUTER",
            "_AKUN_TEMAN",
        )
        print("pola env API key    : OK (hanya provider + _API_KEY yang cocok)")
        print()

        svc = LLMConfigService(db_path=db_path, env_path=env_path)

        # Tabel dibuat di DB global (di sini: DB sementara, skema sama).
        with closing(sqlite3.connect(str(db_path))) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        print(f"tabel dibuat        : {sorted(tables)}")
        assert {"llm_provider_instances", "llm_models"} <= tables
        print()

        # ------------------------------------------------------------- #
        # 2) Validasi relasi API Key -> Provider Instance
        # ------------------------------------------------------------- #
        for kwargs, desc in (
            (dict(name="", provider_type="openrouter", api_key_env="OPENROUTER_API_KEY"), "nama kosong"),
            (dict(name="X", provider_type="unknown", api_key_env="UNKNOWN_API_KEY"), "provider type asing"),
            (dict(name="X", provider_type="deepseek"), "deepseek tanpa api_key_env"),
            (dict(name="X", provider_type="deepseek", api_key_env="OPENROUTER_API_KEY"), "prefix tidak cocok"),
            (dict(name="X", provider_type="openrouter", api_key_env="OPENROUTER_MODEL"), "bukan pola _API_KEY"),
        ):
            try:
                svc.create_provider_instance(**kwargs)
            except LLMConfigValidationError as exc:
                print(f"tolak ({desc}) : {exc}")
            else:
                raise AssertionError(f"seharusnya ditolak: {desc}")
        print("validasi relasi     : OK")
        print()

        # ------------------------------------------------------------- #
        # 3) CRUD provider instance
        # ------------------------------------------------------------- #
        primary = svc.create_provider_instance(
            name="OpenRouter Utama",
            provider_type="openrouter",
            api_key_env="OPENROUTER_API_KEY",
        )
        print(f"create instance     : id={primary.id} name={primary.name!r}")
        assert primary.api_url == "https://openrouter.ai/api/v1"
        assert primary.provider_type == "openrouter"
        assert primary.api_key_env == "OPENROUTER_API_KEY"

        # Nama unik.
        try:
            svc.create_provider_instance(
                name="OpenRouter Utama",
                provider_type="openrouter",
                api_key_env="OPENROUTER_API_KEY",
            )
        except LLMConfigConflictError as exc:
            print(f"tolak nama duplikat : {exc}")
        else:
            raise AssertionError("nama duplikat seharusnya ditolak")

        # Provider lokal tanpa API key boleh.
        ollama = svc.create_provider_instance(name="Ollama Lokal", provider_type="ollama")
        print(f"create ollama       : id={ollama.id} api_key_env={ollama.api_key_env!r}")
        assert ollama.api_key_env == ""
        # Default Base API URL Ollama (lokal, tanpa API key).
        assert ollama.api_url == "http://localhost:11434"

        # Update.
        primary = svc.update_provider_instance(
            primary.id, name="OpenRouter Primary", api_url="https://openrouter.ai/api/v1/x"
        )
        assert primary.name == "OpenRouter Primary"
        assert primary.api_url.endswith("/x")
        print(f"update instance     : name={primary.name!r} api_url={primary.api_url!r}")
        print()

        # ------------------------------------------------------------- #
        # 4) CRUD model (relasi 1 provider -> banyak model)
        # ------------------------------------------------------------- #
        m1 = svc.add_model(primary.id, "openai/gpt-4o-mini")
        m2 = svc.add_model(primary.id, "anthropic/claude-3.5-sonnet")
        svc.add_model(ollama.id, "qwen2.5-coder:7b")
        models_primary = svc.list_models(primary.id)
        print(f"models primary      : {[m.model_name for m in models_primary]}")
        assert {m.model_name for m in models_primary} == {
            "openai/gpt-4o-mini",
            "anthropic/claude-3.5-sonnet",
        }
        assert len(svc.list_models()) == 3

        try:
            svc.add_model(primary.id, "openai/gpt-4o-mini")
        except LLMConfigConflictError as exc:
            print(f"tolak model duplikat: {exc}")
        else:
            raise AssertionError("model duplikat seharusnya ditolak")

        m1 = svc.update_model(m1.id, enabled=False)
        assert m1.enabled is False
        print(f"update model        : {m1.model_name!r} enabled={m1.enabled}")
        print()

        # ------------------------------------------------------------- #
        # 5) API key TIDAK PERNAH disimpan di SQLite
        # ------------------------------------------------------------- #
        raw_db = db_path.read_bytes()
        assert SECRET_OPENROUTER.encode() not in raw_db, "SECRET bocor ke SQLite!"
        with closing(sqlite3.connect(str(db_path))) as conn:
            row = conn.execute(
                "SELECT api_key_env FROM llm_provider_instances WHERE id = ?",
                (primary.id,),
            ).fetchone()
        print(f"db menyimpan         : api_key_env={row[0]!r} (bukan nilai secret)")
        assert row[0] == "OPENROUTER_API_KEY"
        print("kebocoran secret    : TIDAK ADA (nilai hanya nama variabel)")
        print()

        # ------------------------------------------------------------- #
        # 6) Discovery credential (.env) + masking
        # ------------------------------------------------------------- #
        creds = {c.name: c for c in svc.list_credentials()}
        print(f"credentials         : {sorted(creds)}")
        assert "OPENROUTER_API_KEY" in creds
        assert "ANOTHER_SECRET" not in creds and "OLLAMA_HOST" not in creds
        assert creds["OPENROUTER_API_KEY"].is_set is True
        assert creds["OPENROUTER_API_KEY"].masked != SECRET_OPENROUTER
        assert SECRET_OPENROUTER not in creds["OPENROUTER_API_KEY"].masked
        assert "OpenRouter Primary" in creds["OPENROUTER_API_KEY"].used_by
        print(f"masked              : {creds['OPENROUTER_API_KEY'].masked!r} (aman ditampilkan)")
        print()

        # ------------------------------------------------------------- #
        # 7) Manajemen .env AMAN (hanya baris terkait berubah)
        # ------------------------------------------------------------- #
        # set: tambah credential baru (akun kedua).
        svc.set_api_key("OPENROUTER_API_KEY_AKUN_TEMAN", SECRET_TEMAN)

        # set: ubah nilai yang sudah ada (posisi/baris lain dipertahankan).
        svc.set_api_key("OPENROUTER_API_KEY", "sk-or-v1-ROTATED-SECRET-0000000000")

        env_text = env_path.read_text(encoding="utf-8")
        print("--- .env setelah edit ---")
        print(env_text.strip())
        print("-------------------------")
        assert "# Dummy .env untuk verifikasi" in env_text, "komentar hilang!"
        assert "AETHER_ENV=development" in env_text, "variabel lain hilang!"
        assert "OLLAMA_HOST=http://127.0.0.1:11434" in env_text
        assert "ANOTHER_SECRET=jangan-sentuh" in env_text, "variabel lain berubah!"
        assert "OPENROUTER_API_KEY_AKUN_TEMAN=" in env_text
        assert "sk-or-v1-ROTATED-SECRET-0000000000" in env_text
        assert not list(workdir.glob("*.tmp")), "file sementara tertinggal!"
        assert svc.get_api_key("OPENROUTER_API_KEY") == "sk-or-v1-ROTATED-SECRET-0000000000"
        print("edit .env aman      : OK (komentar + variabel lain utuh)")
        print()

        # relasi credential -> provider instance (akun kedua).
        second = svc.create_provider_instance(
            name="OpenRouter Akun Teman",
            provider_type="openrouter",
            api_key_env="OPENROUTER_API_KEY_AKUN_TEMAN",
        )
        creds = {c.name: c for c in svc.list_credentials()}
        assert "OpenRouter Akun Teman" in creds["OPENROUTER_API_KEY_AKUN_TEMAN"].used_by
        assert creds["OPENROUTER_API_KEY_AKUN_TEMAN"].suffix_label == "akun teman"
        print(f"relasi credential   : {creds['OPENROUTER_API_KEY_AKUN_TEMAN'].to_dict()}")
        print()

        # hapus credential yang masih dipakai -> ditolak (tanpa force).
        try:
            svc.delete_api_key("OPENROUTER_API_KEY_AKUN_TEMAN")
        except LLMConfigValidationError as exc:
            print(f"tolak hapus (dipakai): {exc}")
        else:
            raise AssertionError("hapus credential terpakai seharusnya ditolak")
        # dengan force -> berhasil, dan tidak menyentuh baris lain.
        assert svc.delete_api_key("OPENROUTER_API_KEY_AKUN_TEMAN", force=True) is True
        env_text = env_path.read_text(encoding="utf-8")
        assert "OPENROUTER_API_KEY_AKUN_TEMAN" not in env_text
        assert "ANOTHER_SECRET=jangan-sentuh" in env_text
        print("hapus .env (force)  : OK, baris lain tetap utuh")
        print()

        # ------------------------------------------------------------- #
        # 8) Pembacaan konfigurasi untuk runtime
        # ------------------------------------------------------------- #
        runtime_cfg = svc.resolve_runtime_config(primary.id)
        print(f"resolve_runtime     : {runtime_cfg}")
        assert runtime_cfg["provider_type"] == "openrouter"
        assert runtime_cfg["api_url"].startswith("https://openrouter.ai/api/v1")
        assert runtime_cfg["api_key"] == "sk-or-v1-ROTATED-SECRET-0000000000"
        # m1 disabled -> model enabled pertama = claude.
        assert runtime_cfg["model"] == "anthropic/claude-3.5-sonnet"

        runtime_m1 = svc.resolve_runtime_config(primary.id, model_id=m1.id)
        assert runtime_m1["model"] == "openai/gpt-4o-mini"

        safe_cfg = svc.get_provider_config(primary.id)
        assert "api_key" not in safe_cfg
        assert safe_cfg["api_key_present"] is True
        print(f"get_provider_config : api_key_present={safe_cfg['api_key_present']} models={len(safe_cfg['models'])}")
        print(f"get_full_config     : {len(svc.get_full_config())} instance(s)")
        print()

        # ------------------------------------------------------------- #
        # 9) Cascade delete (model ikut terhapus)
        # ------------------------------------------------------------- #
        assert svc.store.count_models(primary.id) == 2
        assert svc.delete_provider_instance(primary.id) is True
        assert svc.store.get_provider_instance(primary.id) is None
        assert svc.store.count_models(primary.id) == 0, "cascade delete gagal!"
        assert svc.delete_provider_instance("tidak-ada") is False
        print("cascade delete      : OK (model ikut terhapus)")
        print()

        print("[OK] Sistem konfigurasi LLM (CRUD, relasi, .env aman, pembacaan) bekerja.")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        # Bersihkan dummy_test bila kosong (fixture harus bersih setelah test).
        try:
            if dummy_root.exists() and not any(dummy_root.iterdir()):
                dummy_root.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
