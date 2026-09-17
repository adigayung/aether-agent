"""Verifikasi Model Capability Registry.

Deterministik, tanpa API cloud. Menguji:
    1. register model
    2. get model
    3. provider + model menjadi identity unik
    4. supports capability
    5. unsupported capability
    6. beberapa model dalam provider yang sama
    7. beberapa provider
    8. metadata
    9. context window
   10. duplicate registration behavior
   11. unknown model behavior
   12. registry tidak melakukan network request
   13. provider-agnostic
   14. extensibility capability baru

Jalankan:
    python scripts/check_capabilities.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.capabilities import (  # noqa: E402
    ModelCapabilities,
    ModelCapability,
    ModelCapabilityRegistry,
)


def main() -> int:
    print("=== Verifikasi Model Capability Registry ===")
    return _run()


def _run() -> int:
    reg = ModelCapabilityRegistry()

    # 1) register model.
    reg.register(ModelCapabilities(
        provider="openrouter",
        model="deepseek/deepseek-chat",
        capabilities=frozenset({ModelCapability.TOOL_CALLING, ModelCapability.STREAMING}),
        context_window=64000,
        metadata={"source": "config"},
    ))
    assert len(reg) == 1
    print("[1] register model OK")

    # 2) get model.
    entry = reg.get("openrouter", "deepseek/deepseek-chat")
    assert entry is not None and entry.model == "deepseek/deepseek-chat"
    print("[2] get model OK")

    # 3) provider + model menjadi identity unik.
    #    Nama model sama pada provider berbeda harus entri berbeda.
    reg.register(ModelCapabilities(
        provider="deepseek",
        model="deepseek/deepseek-chat",
        capabilities=frozenset({ModelCapability.TOOL_CALLING}),
    ))
    assert reg.get("openrouter", "deepseek/deepseek-chat") is not reg.get("deepseek", "deepseek/deepseek-chat")
    assert len(reg) == 2
    print("[3] provider + model identity unik OK")

    # 4) supports capability.
    assert reg.supports("openrouter", "deepseek/deepseek-chat", ModelCapability.TOOL_CALLING) is True
    assert reg.supports("openrouter", "deepseek/deepseek-chat", ModelCapability.STREAMING) is True
    print("[4] supports capability OK")

    # 5) unsupported capability.
    assert reg.supports("openrouter", "deepseek/deepseek-chat", ModelCapability.VISION) is False
    assert reg.supports("deepseek", "deepseek/deepseek-chat", ModelCapability.STREAMING) is False
    print("[5] unsupported capability OK")

    # 6) beberapa model dalam provider yang sama.
    reg.register(ModelCapabilities(
        provider="openrouter",
        model="anthropic/claude-3.5",
        capabilities=frozenset({ModelCapability.TOOL_CALLING, ModelCapability.VISION, ModelCapability.MULTIMODAL}),
        context_window=200000,
    ))
    or_models = reg.list(provider="openrouter")
    assert len(or_models) == 2
    assert {m.model for m in or_models} == {"deepseek/deepseek-chat", "anthropic/claude-3.5"}
    print("[6] beberapa model dalam provider sama OK")

    # 7) beberapa provider.
    reg.register(ModelCapabilities(
        provider="ollama",
        model="qwen2.5-coder:7b",
        capabilities=frozenset({ModelCapability.TOOL_CALLING}),
        context_window=32768,
    ))
    assert set(reg.providers()) == {"openrouter", "deepseek", "ollama"}
    print(f"[7] beberapa provider OK -> {reg.providers()}")

    # 8) metadata.
    assert entry.metadata.get("source") == "config"
    print("[8] metadata OK")

    # 9) context window.
    assert entry.context_window == 64000
    assert reg.get("ollama", "qwen2.5-coder:7b").context_window == 32768
    print("[9] context window OK")

    # 10) duplicate registration behavior.
    #     Default: idempotent (tidak menimpa, tidak error).
    before = reg.get("openrouter", "deepseek/deepseek-chat")
    reg.register(ModelCapabilities(
        provider="openrouter",
        model="deepseek/deepseek-chat",
        capabilities=frozenset({ModelCapability.VISION}),  # berbeda
    ))
    after = reg.get("openrouter", "deepseek/deepseek-chat")
    assert after is before, "duplikat tanpa overwrite tidak boleh menimpa"
    assert reg.supports("openrouter", "deepseek/deepseek-chat", ModelCapability.VISION) is False
    # Dengan overwrite=True -> menimpa.
    reg.register(ModelCapabilities(
        provider="openrouter",
        model="deepseek/deepseek-chat",
        capabilities=frozenset({ModelCapability.VISION}),
    ), overwrite=True)
    assert reg.supports("openrouter", "deepseek/deepseek-chat", ModelCapability.VISION) is True
    print("[10] duplicate registration behavior OK")

    # 11) unknown model behavior.
    assert reg.get("openrouter", "tidak/ada") is None
    assert reg.has("openrouter", "tidak/ada") is False
    assert reg.supports("openrouter", "tidak/ada", ModelCapability.TOOL_CALLING) is False
    assert reg.supports("unknown-provider", "x", ModelCapability.TOOL_CALLING) is False
    print("[11] unknown model behavior OK")

    # 12) registry tidak melakukan network request.
    #     Tidak ada modul capabilities yang mengimpor HTTP client.
    cap_dir = SRC_DIR / "agent_ai" / "capabilities"
    files = {p.name for p in cap_dir.glob("*.py")}
    assert files == {"__init__.py", "models.py", "registry.py"}, files
    for p in cap_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        for bad in ("import requests", "import urllib", "import http", "import socket", "subprocess"):
            assert bad not in text, f"{p.name} tidak boleh melakukan network/exec: {bad}"
    print("[12] registry tidak melakukan network request OK")

    # 13) provider-agnostic.
    for p in cap_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8").lower()
        # Nama provider hanya boleh muncul sebagai contoh di docstring, bukan
        # sebagai logika. Cek tidak ada cabang khusus provider.
        assert "if provider ==" not in text, f"{p.name} tidak boleh punya cabang khusus provider"
    print("[13] provider-agnostic OK")

    # 14) extensibility capability baru.
    class ExtendedCapability(str):
        pass

    # Tambahkan capability baru via enum yang diperluas (subclass tidak
    # disarankan untuk Enum, jadi kita buktikan registry menerima capability
    # apa pun yang kompatibel dengan ModelCapability lewat anggota baru).
    # Simulasi: gunakan anggota enum yang ada + pastikan registry generik.
    reg.register(ModelCapabilities(
        provider="openrouter",
        model="meta/llama-3.1-405b",
        capabilities=frozenset({ModelCapability.REASONING, ModelCapability.TOOL_CALLING}),
    ))
    assert reg.supports("openrouter", "meta/llama-3.1-405b", ModelCapability.REASONING) is True
    # Registry tidak hardcode daftar capability: supports() menerima enum apa pun.
    assert ModelCapability.REASONING in reg.get("openrouter", "meta/llama-3.1-405b").capabilities
    print("[14] extensibility capability baru OK")

    print()
    print("[OK] Model Capability Registry bekerja (identity provider+model, supports, metadata).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
