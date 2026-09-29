"""Verifikasi tool Vision Agent `view_image` (deterministik, tanpa API key).

Membuktikan rantai END-TO-END untuk use case:

    "deskripsikan image ./xxx.png tersebut, tulis deskripsi dalam gambar.txt"

    view_image("./xxx.png") -> bytes image dibaca di Python -> image part
    provider-agnostic -> provider multimodal (image_url data URL) -> Agent
    dapat melanjutkan (mis. write_file ke gambar.txt).

Yang diverifikasi:
  1. Registry Agent memuat tool `view_image` (Agent-only; Consultant TIDAK).
  2. `view_image("./xxx.png")` -> image part (base64 PNG) + metadata (bukan OCR/
     ASCII/statistik).
  3. Image part -> content part provider multimodal (image_url data URL) untuk
     OpenAI-compatible; format Ollama (images) juga benar.
  4. File NON-image (mis. .txt) DITOLAK.
  5. Path DI LUAR workspace DITOLAK (boundary).
  6. Backward compatible: hasil tool biasa tetap SATU pesan role "tool" (text).
  7. END-TO-END orchestrator NYATA: LLM memanggil view_image -> pesan user
     multimodal berisi image part terlihat oleh provider (Agent benar-benar
     MELIHAT gambar), dan base64 TIDAK bocor ke pesan role "tool".

Jalankan:
    python scripts/check_agent_view_image.py
"""

from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _png_bytes(size=(64, 48), color=(10, 200, 90)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _tool_call(call_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _tool_turn(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"content": "", "tool_calls": calls},
                "finish_reason": "tool_calls",
            }
        ]
    }


def _final_turn(text: str = "selesai") -> Dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


def main() -> int:
    print("=== Verifikasi tool Vision Agent (view_image) ===")
    from agent_ai.providers.base import GenerateOptions, GenerateResult, Message
    from agent_ai.providers.openai_compatible import OpenAICompatibleProvider
    from agent_ai.config.settings import OpenAIConfig
    from agent_ai.tools.base import ToolError, MULTIMODAL_PARTS_KEY, split_multimodal_parts
    from agent_ai.tools.registry import build_registry

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "workspace"
        root.mkdir()
        outside = Path(tmp) / "outside"
        outside.mkdir()

        png = _png_bytes()
        (root / "xxx.png").write_bytes(png)
        (root / "notes.txt").write_text("bukan gambar", encoding="utf-8")
        (outside / "secret.png").write_bytes(png)

        registry = build_registry(root=root)

        # 1) Tool terdaftar (Agent) dan schema-nya jelas.
        assert "view_image" in registry.list(), registry.list()
        spec = registry.get("view_image").to_spec()
        assert spec["input_schema"]["required"] == ["path"], spec
        print("[1] registry Agent memuat tool 'view_image' OK")

        # 2) view_image("./xxx.png") -> image part (base64 PNG) + metadata.
        result = registry.execute("view_image", {"path": "./xxx.png"})
        assert isinstance(result, dict), result
        assert result["ok"] is True, result
        assert result["path"] == "xxx.png", result["path"]
        assert result["mime_type"] == "image/png", result["mime_type"]
        parts = result[MULTIMODAL_PARTS_KEY]
        assert isinstance(parts, list) and len(parts) == 1, parts
        part = parts[0]
        assert part["type"] == "image", part
        assert part["encoding"] == "base64", part
        assert part["mime_type"] == "image/png", part
        decoded = base64.b64decode(part["data"])
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n", decoded[:8]
        assert result["width"] and result["height"], result
        print("[2] view_image('./xxx.png') -> image part (base64 PNG) OK")

        # 3) Image part -> provider multimodal (image_url data URL) + Ollama.
        provider = OpenAICompatibleProvider(
            config=OpenAIConfig(api_key="x", base_url="http://localhost", model="m")
        )
        payload = provider._build_payload(
            prompt=None,
            messages=[Message(role="user", content="apa isi gambar ini?", parts=parts)],
            options=None,
            tools=None,
        )
        blocks = payload["messages"][0]["content"]
        assert isinstance(blocks, list), blocks
        assert blocks[0] == {"type": "text", "text": "apa isi gambar ini?"}, blocks[0]
        assert blocks[1]["type"] == "image_url", blocks[1]
        url = blocks[1]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,"), url[:40]
        assert url.split(",", 1)[1] == part["data"], "data URL harus memakai part apa adanya"
        assert "parts" not in payload["messages"][0]

        from agent_ai.providers.ollama import OllamaProvider

        ollama = OllamaProvider.__new__(OllamaProvider)
        ollama_msg = OllamaProvider._to_ollama_message(
            {"role": "user", "content": "q", "parts": parts}
        )
        assert ollama_msg.get("images") == [part["data"]], ollama_msg
        assert "parts" not in ollama_msg
        print("[3] image part -> provider multimodal (image_url + Ollama images) OK")

        # 4) File NON-image DITOLAK.
        try:
            registry.execute("view_image", {"path": "notes.txt"})
            raise AssertionError("file non-image harus ditolak")
        except ToolError:
            pass
        # Argumen kosong juga ditolak.
        try:
            registry.execute("view_image", {"path": "  "})
            raise AssertionError("path kosong harus ditolak")
        except ToolError:
            pass
        print("[4] file non-image / path kosong DITOLAK OK")

        # 5) Path DI LUAR workspace DITOLAK (boundary).
        for bad in (str(outside / "secret.png"), "../outside/secret.png"):
            try:
                registry.execute("view_image", {"path": bad})
                raise AssertionError(f"path di luar workspace harus ditolak: {bad}")
            except ToolError:
                pass
        print("[5] path di luar workspace DITOLAK OK")

        # 6) Backward compatible: hasil tool biasa -> tool message text-only.
        read_result = registry.execute("read_file", {"path": "notes.txt"})
        cleaned, no_parts = split_multimodal_parts(read_result)
        assert no_parts is None, no_parts
        assert cleaned == read_result
        print("[6] hasil tool biasa tetap text-only (backward compatible) OK")

        # 7) END-TO-END orchestrator NYATA: LLM memanggil view_image, lalu
        #    provider MELIHAT gambar pada pesan user multimodal.
        from agent_ai.core.executor import ToolExecutor
        from agent_ai.core.orchestrator import AgentOrchestrator

        class ScriptedCapturingProvider(OpenAICompatibleProvider):
            name = "scripted-capture"

            def __init__(self, script: List[Dict[str, Any]]) -> None:
                self.config = SimpleNamespace(model="m", context_window=0)
                self.script = list(script)
                self.calls = 0
                self.seen: List[Any] = []

            def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
                self.seen.append(list(messages or []))
                idx = self.calls
                self.calls += 1
                raw = self.script[idx] if idx < len(self.script) else _final_turn()
                return GenerateResult(text="", model="m", provider=self.name, raw=raw)

        script = [
            _tool_turn([_tool_call("c1", "view_image", {"path": "./xxx.png"})]),
            _final_turn("gambar berisi kotak hijau."),
        ]
        provider2 = ScriptedCapturingProvider(script)
        events: List[Dict[str, Any]] = []
        orchestrator = AgentOrchestrator(
            provider=provider2,
            executor=ToolExecutor(registry=build_registry(root=root)),
            options=GenerateOptions(model="m"),
            system_prompt="Kamu agent.",
            use_continuous_loop=True,
            event_sink=lambda et, payload: events.append({"type": et, **payload}),
        )
        outcome = orchestrator.run("deskripsikan image ./xxx.png tersebut")
        assert outcome.success, outcome

        assert len(provider2.seen) >= 2, provider2.seen
        second = provider2.seen[1]
        image_user_msgs = [
            m
            for m in second
            if m.get("role") == "user" and m.get("parts")
        ]
        assert image_user_msgs, "provider harus MELIHAT pesan user multimodal"
        seen_parts = image_user_msgs[-1]["parts"]
        assert seen_parts[0]["type"] == "image", seen_parts[0]
        assert base64.b64decode(seen_parts[0]["data"])[:8] == b"\x89PNG\r\n\x1a\n"

        # Base64 TIDAK bocor ke pesan role "tool" (yang harus text-only).
        tool_msgs = [m for m in second if m.get("role") == "tool"]
        assert tool_msgs, "harus ada pesan role tool"
        assert part["data"] not in json.dumps(tool_msgs), "base64 TIDAK boleh di tool message"

        # Riwayat tetap valid: assistant(tool_calls) -> tool -> user(multimodal).
        roles = [m.get("role") for m in second]
        idx_tool = roles.index("tool")
        assert roles[idx_tool + 1] == "user", roles
        assert any(
            e["type"] == "vision_image_attached" for e in events
        ), "event vision_image_attached harus diemit"
        print("[7] E2E: view_image -> pesan user multimodal terlihat provider OK")

        # 7b) Backward compatible: tanpa image, provider TIDAK melihat parts.
        provider3 = ScriptedCapturingProvider([_final_turn("halo")])
        orchestrator3 = AgentOrchestrator(
            provider=provider3,
            executor=ToolExecutor(registry=build_registry(root=root)),
            options=GenerateOptions(model="m"),
            system_prompt="Kamu agent.",
            use_continuous_loop=True,
        )
        outcome3 = orchestrator3.run("task teks saja tanpa gambar")
        assert outcome3.success, outcome3
        assert provider3.seen, provider3.seen
        assert not any(m.get("parts") for m in provider3.seen[0]), (
            "tanpa gambar tidak boleh ada parts"
        )
        print("[7b] Agent tanpa image tetap text-only (backward compatible) OK")

    print()
    print("[OK] tool Vision Agent `view_image` bekerja (workspace-bound, multimodal).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
