"""Verifikasi normalisasi response provider -> LLMResponse.

Menguji:
    - Ollama live: raw response -> LLMResponse (text).
    - Normalisasi raw response sintetis dengan tool_calls.
    - provider/model terisi.
    - serialization aman (tanpa raw/secret secara default).

Jalankan:
    python scripts/check_normalize.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.providers.base import GenerateOptions, GenerateResult  # noqa: E402
from agent_ai.providers.registry import get_provider  # noqa: E402


def main() -> int:
    print("=== Verifikasi Normalisasi Response -> LLMResponse ===")

    provider = get_provider("ollama")
    print(f"Provider : {provider.name}")
    print()

    # 1) Live: Ollama -> raw -> LLMResponse (text).
    if provider.is_available():
        result = provider.generate(
            prompt="Balas singkat: apa itu Python?",
            options=GenerateOptions(temperature=0.2, max_tokens=64),
        )
        print(f"raw keys : {list(result.raw.keys()) if isinstance(result.raw, dict) else type(result.raw)}")
        resp = provider.normalize_response(result)
        print(f"LLMResponse -> text={resp.text[:60]!r}...")
        print(f"  provider={resp.provider}, model={resp.model}, finish={resp.finish_reason.value}")
        assert resp.text, "text kosong"
        assert resp.provider == "ollama"
        assert resp.model
    else:
        print("[WARNING] Ollama tidak tersedia; melewati uji live.")
    print()

    # 2) Normalisasi raw sintetis dengan tool_calls (tanpa request jaringan).
    fake_raw = {
        "message": {
            "content": "",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "src/main.py"}}},
                {"function": {"name": "list_files", "arguments": '{"path": "src"}'}},
            ],
        },
        "done_reason": "stop",
    }
    fake_result = GenerateResult(text="", model="qwen2.5-coder:7b", provider="ollama", raw=fake_raw)
    resp2 = provider.normalize_response(fake_result)
    print(f"tool_calls -> {[a.name for a in resp2.tool_calls()]}")
    print(f"  arguments[0] = {resp2.tool_calls()[0].arguments} (dict: {isinstance(resp2.tool_calls()[0].arguments, dict)})")
    print(f"  arguments[1] = {resp2.tool_calls()[1].arguments} (dict: {isinstance(resp2.tool_calls()[1].arguments, dict)})")
    print(f"  finish_reason = {resp2.finish_reason.value}")
    assert resp2.has_tool_calls
    assert all(isinstance(a.arguments, dict) for a in resp2.tool_calls())
    print()

    # 3) Serialization aman: raw tidak bocor secara default.
    safe = resp2.to_dict()
    print(f"to_dict() keys = {list(safe.keys())}")
    assert "raw" not in safe, "raw bocor di to_dict() default"
    assert "api_key" not in str(safe).lower()
    print()

    print("[OK] Normalisasi provider -> LLMResponse bekerja & serialization aman.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
