"""Verifikasi native tool-calling protocol pada provider layer AETHER.

Membuktikan:
    1. tool schema dapat dibuat dari BaseTool.to_spec().
    2. provider menerima tools.
    3. OpenAI-compatible payload benar-benar memiliki field tools.
    4. tool_choice tidak dipaksakan jika tidak diberikan.
    5. native tool-call response dapat dinormalisasi menjadi LLMAction.
    6. multiple tool calls didukung.
    7. argument JSON menjadi dict.
    8. invalid arguments ditangani (error jelas, bukan crash tersembunyi).
    9. provider/model tetap teridentifikasi.
   10. tidak ada API key yang tercetak.
   11. Ollama dan DeepSeek tetap dapat di-import.
   12. (opsional) smoke test nyata ke DeepSeek bila API key tersedia.

Tidak memakai mock LLM. Tidak mengubah .env. Tidak mencetak API key.

Jalankan:
    python scripts/check_provider_tools.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_ai.config.settings import settings  # noqa: E402
from agent_ai.core.response import ActionType, FinishReason  # noqa: E402
from agent_ai.providers.base import (  # noqa: E402
    GenerateResult,
    Message,
    ProviderResponseError,
    ToolChoice,
    ToolDefinition,
)
from agent_ai.providers.deepseek import DeepSeekProvider  # noqa: E402
from agent_ai.providers.ollama import OllamaProvider  # noqa: E402
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.tools.filesystem import ReadFileTool  # noqa: E402


def main() -> int:
    print("=== Verifikasi Native Tool-Calling Protocol ===")
    return _run()


def _run() -> int:
    # 1) tool schema dari BaseTool.to_spec().
    spec = ReadFileTool().to_spec()
    assert spec["name"] == "read_file"
    assert "input_schema" in spec
    tool_def = ToolDefinition.from_spec(spec)
    assert tool_def.name == "read_file"
    assert tool_def.parameters == spec["input_schema"]
    print(f"[1] tool schema dari to_spec() OK -> {tool_def.name}")

    # 2) provider menerima tools (via _build_tool_definitions).
    provider = OpenAICompatibleProvider()
    normalized = provider._build_tool_definitions([spec, tool_def])
    assert len(normalized) == 2
    assert all(isinstance(t, ToolDefinition) for t in normalized)
    print(f"[2] provider menerima tools OK -> {len(normalized)} tool dinormalisasi")

    # 3) OpenAI-compatible payload benar-benar memiliki field tools.
    payload = provider._build_payload(
        prompt=None,
        messages=[Message(role="user", content="baca file")],
        options=None,
        tools=[tool_def],
    )
    assert "tools" in payload, "payload harus punya field tools"
    assert payload["tools"][0]["type"] == "function"
    assert payload["tools"][0]["function"]["name"] == "read_file"
    assert payload["tools"][0]["function"]["parameters"] == spec["input_schema"]
    print(f"[3] payload punya tools OK -> {payload['tools'][0]['function']['name']}")

    # 4) tool_choice tidak dipaksakan jika tidak diberikan.
    assert "tool_choice" not in payload, "tool_choice tidak boleh dipaksa default"
    payload_choice = provider._build_payload(
        prompt=None,
        messages=[Message(role="user", content="x")],
        options=None,
        tools=[tool_def],
        tool_choice=ToolChoice(mode="auto"),
    )
    assert payload_choice["tool_choice"] == "auto"
    payload_specific = provider._build_payload(
        prompt=None,
        messages=[Message(role="user", content="x")],
        options=None,
        tools=[tool_def],
        tool_choice=ToolChoice(mode="specific", name="read_file"),
    )
    assert payload_specific["tool_choice"]["function"]["name"] == "read_file"
    print("[4] tool_choice tidak dipaksa default OK (auto/specific bekerja)")

    # 5) native tool-call response -> LLMAction.
    raw_single = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "app/calc.py"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    resp = provider.normalize_response(
        GenerateResult(text="", model="deepseek-coder", provider="deepseek", raw=raw_single)
    )
    assert resp.has_tool_calls
    assert resp.finish_reason == FinishReason.TOOL_CALLS
    action = resp.tool_calls()[0]
    assert action.type == ActionType.TOOL_CALL
    assert action.name == "read_file"
    assert action.id == "call_1"
    print(f"[5] native tool-call -> LLMAction OK -> {action.name}({action.arguments})")

    # 6) multiple tool calls didukung.
    raw_multi = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                        },
                        {
                            "id": "c2",
                            "function": {"name": "read_file", "arguments": '{"path": "b.py"}'},
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    resp_multi = provider.normalize_response(
        GenerateResult(text="", raw=raw_multi)
    )
    assert len(resp_multi.tool_calls()) == 2
    print(f"[6] multiple tool calls OK -> {len(resp_multi.tool_calls())} actions")

    # 7) argument JSON menjadi dict.
    assert isinstance(action.arguments, dict)
    assert action.arguments == {"path": "app/calc.py"}
    print(f"[7] argument JSON -> dict OK -> {action.arguments}")

    # 8) invalid arguments ditangani (error jelas, bukan crash tersembunyi).
    raw_bad = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "read_file", "arguments": "{not json"}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    try:
        provider.normalize_response(GenerateResult(text="", raw=raw_bad))
        print("[ERROR] seharusnya ProviderResponseError untuk JSON invalid")
        return 1
    except ProviderResponseError as exc:
        print(f"[8] invalid arguments ditangani OK -> {exc}")

    # 9) provider/model tetap teridentifikasi.
    ds_resp = DeepSeekProvider().normalize_response(
        GenerateResult(text="", model="deepseek-coder", provider="deepseek", raw=raw_single)
    )
    assert ds_resp.provider == "deepseek"
    assert ds_resp.model == "deepseek-coder"
    print(f"[9] provider/model teridentifikasi OK -> {ds_resp.provider}/{ds_resp.model}")

    # 10) tidak ada API key yang tercetak.
    #     Cek bahwa repr/str provider tidak memuat API key.
    ds = DeepSeekProvider()
    key = ds.config.api_key
    if key:
        assert key not in repr(ds)
        assert key not in str(ds)
        print("[10] tidak ada API key tercetak OK (key tersedia, tidak bocor)")
    else:
        print("[10] tidak ada API key tercetak OK (key kosong)")

    # 11) Ollama dan DeepSeek tetap dapat di-import.
    assert OllamaProvider.name == "ollama"
    assert DeepSeekProvider.name == "deepseek"
    print("[11] Ollama & DeepSeek import OK")

    # 12) smoke test nyata ke DeepSeek bila API key tersedia.
    print()
    print("--- Smoke test DeepSeek ---")
    if not ds.config.api_key:
        print("[SKIP] DEEPSEEK_API_KEY tidak tersedia; smoke test dilewati.")
        print("       (tidak fallback ke Ollama)")
    else:
        _smoke_test_deepseek(ds, tool_def)

    print()
    print("[OK] Native tool-calling protocol provider layer bekerja.")
    return 0


def _smoke_test_deepseek(ds: DeepSeekProvider, tool_def: ToolDefinition) -> None:
    """Smoke test nyata ke DeepSeek: kirim request dengan tool schema."""
    print(f"provider : {ds.name}")
    print(f"model    : {ds.config.model}")
    print(f"base_url : {ds.config.base_url}")
    print(f"api_key  : {'(tersedia)' if ds.config.api_key else '(kosong)'}")

    messages = [
        Message(
            role="system",
            content="Kamu adalah coding agent. Gunakan tool bila perlu.",
        ),
        Message(
            role="user",
            content="Baca isi file app/calc.py menggunakan tool read_file.",
        ),
    ]

    try:
        gen = ds.generate(messages=messages, tools=[tool_def])
    except Exception as exc:  # noqa: BLE001 - laporkan apa adanya
        print(f"[smoke] request gagal: {type(exc).__name__}: {exc}")
        return

    print(f"[smoke] request terkirim -> provider={gen.provider} model={gen.model}")
    resp = ds.normalize_response(gen)
    print(f"[smoke] finish_reason : {resp.finish_reason.value}")
    print(f"[smoke] text          : {resp.text[:120]!r}")
    print(f"[smoke] native tool_calls diterima : {resp.has_tool_calls}")
    if resp.has_tool_calls:
        for a in resp.tool_calls():
            print(f"[smoke]   -> {a.name}({a.arguments})")
    else:
        print("[smoke] DeepSeek TIDAK mengembalikan native tool_calls pada request ini.")


if __name__ == "__main__":
    raise SystemExit(main())
