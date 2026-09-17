"""Verifikasi Agent Core -> Registry -> Ollama -> Qwen.

Memastikan Agent Core memanggil provider melalui abstraction (tanpa
mengetahui provider konkret) dan mengembalikan response terstruktur.

Jalankan:
    python scripts/check_agent.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Pastikan folder src/ ada di sys.path agar import agent_ai.* bekerja.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.config.settings import settings  # noqa: E402
from agent_ai.core import Agent  # noqa: E402
from agent_ai.providers.base import GenerateOptions  # noqa: E402

TASK = (
    "Buat satu fungsi Python sederhana bernama `add(a, b)` yang mengembalikan "
    "hasil penjumlahan a + b. Hanya tampilkan kode Python-nya saja."
)


def main() -> int:
    print("=== Verifikasi Agent Core -> Registry -> Ollama -> Qwen ===")
    print(f"Default provider : {settings.default_provider}")
    print()

    # Agent tanpa hardcode provider: provider diambil dari registry
    # berdasarkan settings.default_provider.
    agent = Agent(options=GenerateOptions(temperature=0.2, max_tokens=256))
    print(f"Provider aktif   : {agent.provider_name}")
    print(f"Tersedia         : {agent.is_available()}")
    print()

    if not agent.is_available():
        print(
            f"[ERROR] Provider '{agent.provider_name}' tidak tersedia. "
            f"Pastikan server Ollama berjalan."
        )
        return 1

    print(f"Task     : {TASK}")
    print("-" * 60)

    try:
        response = agent.run(task=TASK)
    except Exception as exc:  # noqa: BLE001 - tampilkan error apa adanya
        print(f"[ERROR] Agent gagal menjalankan task: {type(exc).__name__}: {exc}")
        return 1

    print(response.text.strip() if response.text else "(response kosong)")
    print("-" * 60)
    print(f"Provider : {response.provider}")
    print(f"Model    : {response.model}")

    if not response.text:
        print("[WARNING] Response teks kosong diterima dari provider.")
        return 1

    print("[OK] Agent Core berhasil memanggil provider dan menerima response.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
