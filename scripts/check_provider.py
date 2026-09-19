"""Script verifikasi end-to-end untuk provider AI.

Mengambil provider dari registry yang sudah ada, memanggil provider.generate(...)
dengan prompt coding sederhana, lalu menampilkan response ke terminal.

Jalankan:
    python scripts/check_provider.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Pastikan folder src/ ada di sys.path agar import agent_ai.* bekerja
# walau script dijalankan dari dalam folder scripts/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.config.settings import settings  # noqa: E402
from agent_ai.providers.base import GenerateOptions  # noqa: E402

from agent_ai.providers.registry import get_provider, registry  # noqa: E402

PROMPT = (
    "Buat satu fungsi Python sederhana bernama `is_palindrome(s)` yang "
    "mengembalikan True jika string s adalah palindrome, dan False jika tidak. "
    "Sertakan docstring singkat. Hanya tampilkan kode Python-nya saja."
)








def check_registry() -> int:
    """Verifikasi registry mengenali provider dan status ketersediaannya.

    Tidak melakukan request cloud. Provider cloud hanya dicek konfigurasinya.
    """
    print("=== Verifikasi registry provider ===")
    print(f"Provider terdaftar: {', '.join(registry.list_providers())}")
    print()






    expected = ["ollama", "deepseek", "openai"]
    missing = [name for name in expected if not registry.has(name)]
    if missing:
        print(f"[ERROR] Provider tidak terdaftar di registry: {', '.join(missing)}")
        return 1









    for name in expected:
        provider = get_provider(name)
        available = provider.is_available()
        status = "tersedia" if available else "tidak tersedia (credential belum diisi)"
        print(f"  - {name:<9}: {status}")

    # Provider cloud TIDAK boleh dianggap tersedia tanpa API key.
    for name in ("deepseek", "openai"):
        if get_provider(name).is_available():
            print(
                f"[ERROR] Provider '{name}' dianggap tersedia padahal API key kosong. "
                f"Jangan memanggil API cloud tanpa credential."
            )
            return 1

























    print("[OK] Registry mengenali ollama, deepseek, dan openai.")
    print("[OK] Provider cloud tidak dianggap tersedia tanpa API key.")
    print()
    return 0


def main() -> int:
    if check_registry() != 0:
        return 1

    # Provider dipilih eksplisit (bukan dari .env). Pemilihan provider aktif
    # di aplikasi berasal dari Provider Instance + Model (SQLite).
    provider_name = "ollama"
    print("=== Verifikasi provider.generate() end-to-end ===")
    print(f"Provider : {provider_name}")
    print(f"Model    : {settings.ollama.model}")
    print(f"Host     : {settings.ollama.host}")
    print()

    # 1) Ambil provider dari registry (tanpa tahu implementasi konkretnya).
    try:
        provider = get_provider(provider_name)
    except KeyError as exc:
        print(f"[ERROR] Provider tidak ditemukan di registry: {exc}")
        return 1

    # 2) Cek ketersediaan provider sebelum memanggil.
    if not provider.is_available():
        print(
            f"[ERROR] Provider '{provider.name}' tidak tersedia. "
            f"Pastikan server Ollama berjalan di {settings.ollama.host} "
            f"dan model '{settings.ollama.model}' sudah di-pull."
        )
        return 1

    print(f"Prompt   : {PROMPT}")
    print("-" * 60)

    # 3) Panggil generate() lewat interface BaseProvider.
    try:
        result = provider.generate(
            prompt=PROMPT,
            options=GenerateOptions(temperature=0.2, max_tokens=512),
        )
    except Exception as exc:  # noqa: BLE001 - tampilkan pesan error yang jelas
        print(f"[ERROR] Gagal memanggil provider.generate(): {type(exc).__name__}: {exc}")
        return 1

    # 4) Tampilkan response.
    print(result.text.strip() if result.text else "(response kosong)")
    print("-" * 60)
    print(f"Provider : {result.provider}")
    print(f"Model    : {result.model}")

    if not result.text:
        print("[WARNING] Response teks kosong diterima dari provider.")
        return 1

    print("[OK] Response dari Qwen berhasil diterima.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
