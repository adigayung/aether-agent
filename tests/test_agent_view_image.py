"""Regresi: tool Vision Agent `view_image` (image lokal -> input multimodal).

Use case yang dijaga:
    "deskripsikan image ./xxx.png tersebut, tulis deskripsi dalam gambar.txt"

Membuktikan Agent dapat MENGIRIM gambar lokal ke model sebagai input
multimodal lewat mekanisme Vision yang sudah ada (ImageInputLoader +
ImagePreprocessor + provider multimodal), TANPA workaround OCR/ASCII/PIL-stat/
Playwright, dan tetap:

  * workspace-bound (path di luar workspace ditolak);
  * menolak file non-image;
  * backward compatible (task tanpa image tetap text-only).

Verifier lengkap: ``scripts/check_agent_view_image.py``.
"""

from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from agent_ai.config.settings import OpenAIConfig  # noqa: E402
from agent_ai.core.executor import ToolExecutor  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.providers.base import GenerateOptions, GenerateResult, Message  # noqa: E402
from agent_ai.providers.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from agent_ai.tools.base import (  # noqa: E402
    ToolError,
    MULTIMODAL_PARTS_KEY,
    split_multimodal_parts,
)
from agent_ai.tools.registry import build_registry  # noqa: E402

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_bytes(size=(32, 24), color=(5, 5, 200)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "xxx.png").write_bytes(_png_bytes())
    (root / "notes.txt").write_text("bukan gambar", encoding="utf-8")
    return root


# --------------------------------------------------------------------------- #
# 1) Tool terdaftar & menghasilkan image part
# --------------------------------------------------------------------------- #
def test_view_image_registered_on_agent_registry(tmp_path: Path) -> None:
    reg = build_registry(root=tmp_path)
    assert "view_image" in reg.list()
    spec = reg.get("view_image").to_spec()
    assert spec["input_schema"]["required"] == ["path"]


def test_view_image_returns_multimodal_part(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    reg = build_registry(root=root)
    result = reg.execute("view_image", {"path": "./xxx.png"})
    assert result["ok"] is True
    assert result["path"] == "xxx.png"
    assert result["mime_type"] == "image/png"
    parts = result[MULTIMODAL_PARTS_KEY]
    assert len(parts) == 1 and parts[0]["type"] == "image"
    assert parts[0]["encoding"] == "base64"
    assert base64.b64decode(parts[0]["data"]).startswith(PNG_MAGIC)


def test_split_multimodal_parts_strips_key() -> None:
    cleaned, parts = split_multimodal_parts(
        {"path": "a.png", MULTIMODAL_PARTS_KEY: [{"type": "image", "data": "x"}]}
    )
    assert MULTIMODAL_PARTS_KEY not in cleaned
    assert parts == [{"type": "image", "data": "x"}]
    # Output non-dict / tanpa part: tidak berubah.
    assert split_multimodal_parts("text") == ("text", None)
    assert split_multimodal_parts({"a": 1}) == ({"a": 1}, None)


# --------------------------------------------------------------------------- #
# 2) Image part -> provider multimodal
# --------------------------------------------------------------------------- #
def test_image_part_becomes_provider_image_url() -> None:
    part = {
        "type": "image",
        "mime_type": "image/png",
        "encoding": "base64",
        "data": base64.b64encode(_png_bytes()).decode("ascii"),
    }
    provider = OpenAICompatibleProvider(
        config=OpenAIConfig(api_key="x", base_url="http://localhost", model="m")
    )
    payload = provider._build_payload(
        prompt=None,
        messages=[Message(role="user", content="isi?", parts=[part])],
        options=None,
        tools=None,
    )
    blocks = payload["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "isi?"}
    assert blocks[1]["type"] == "image_url"
    assert blocks[1]["image_url"]["url"] == f"data:image/png;base64,{part['data']}"


# --------------------------------------------------------------------------- #
# 3) Penolakan: non-image & path di luar workspace
# --------------------------------------------------------------------------- #
def test_view_image_rejects_non_image(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    reg = build_registry(root=root)
    with pytest.raises(ToolError):
        reg.execute("view_image", {"path": "notes.txt"})


def test_view_image_rejects_outside_workspace(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(_png_bytes())
    reg = build_registry(root=root)
    for bad in (str(outside / "secret.png"), "../outside/secret.png"):
        with pytest.raises(ToolError):
            reg.execute("view_image", {"path": bad})


# --------------------------------------------------------------------------- #
# 4) End-to-end orchestrator
# --------------------------------------------------------------------------- #
def _tool_turn(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "choices": [
            {"message": {"content": "", "tool_calls": calls}, "finish_reason": "tool_calls"}
        ]
    }


def _final_turn(text: str = "selesai") -> Dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class _CapturingProvider(OpenAICompatibleProvider):
    name = "capturing"

    def __init__(self, script: List[Dict[str, Any]]) -> None:
        self.config = SimpleNamespace(model="m", context_window=0)
        self.script = list(script)
        self.calls = 0
        self.seen: List[List[Dict[str, Any]]] = []

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        self.seen.append([dict(m) for m in (messages or [])])
        idx = self.calls
        self.calls += 1
        raw = self.script[idx] if idx < len(self.script) else _final_turn()
        return GenerateResult(text="", model="m", provider=self.name, raw=raw)


def test_agent_view_image_reaches_provider_as_multimodal(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    call = {
        "id": "c1",
        "type": "function",
        "function": {"name": "view_image", "arguments": json.dumps({"path": "./xxx.png"})},
    }
    provider = _CapturingProvider([_tool_turn([call]), _final_turn("kotak biru")])
    events: List[Dict[str, Any]] = []
    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=build_registry(root=root)),
        options=GenerateOptions(model="m"),
        system_prompt="Kamu agent.",
        use_continuous_loop=True,
        event_sink=lambda et, payload: events.append({"type": et, **payload}),
    )
    outcome = orchestrator.run("deskripsikan image ./xxx.png tersebut")
    assert outcome.success

    second = provider.seen[1]
    multimodal = [m for m in second if m.get("role") == "user" and m.get("parts")]
    assert multimodal, "provider harus melihat pesan user multimodal"
    part = multimodal[-1]["parts"][0]
    assert part["type"] == "image"
    assert base64.b64decode(part["data"]).startswith(PNG_MAGIC)

    # Base64 tidak bocor ke pesan role "tool" (kontrak tool calling).
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert tool_msgs
    assert part["data"] not in json.dumps(tool_msgs)

    # Riwayat valid: assistant(tool_calls) -> tool -> user(multimodal).
    roles = [m.get("role") for m in second]
    assert roles[roles.index("tool") + 1] == "user"
    assert any(e["type"] == "vision_image_attached" for e in events)


def test_agent_without_image_is_backward_compatible(tmp_path: Path) -> None:
    provider = _CapturingProvider([_final_turn("halo")])
    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=build_registry(root=tmp_path)),
        options=GenerateOptions(model="m"),
        system_prompt="Kamu agent.",
        use_continuous_loop=True,
    )
    assert orchestrator.run("task teks saja").success
    assert not any(m.get("parts") for m in provider.seen[0])


def test_regular_tool_result_stays_text_only(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    call = {
        "id": "c9",
        "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps({"path": "notes.txt"})},
    }
    provider = _CapturingProvider([_tool_turn([call]), _final_turn("ok")])
    orchestrator = AgentOrchestrator(
        provider=provider,
        executor=ToolExecutor(registry=build_registry(root=root)),
        options=GenerateOptions(model="m"),
        system_prompt="Kamu agent.",
        use_continuous_loop=True,
    )
    assert orchestrator.run("baca notes.txt").success
    second = provider.seen[1]
    assert not any(m.get("role") == "user" and m.get("parts") for m in second)
