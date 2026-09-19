"""Verifikasi Vision/Multimodal AETHER Consultant (deterministik, tanpa API key).

Menguji jalur ADDITIVE image dari chat Consultant ke provider:

    1. Provider layer: Message dengan image content part -> OpenAI payload
       memakai image_url data URL; pesan text-only TIDAK berubah.
    2. Core: ChatMessage/ConversationHistory membawa parts (content blocks).
    3. Orchestrator: run_continuous_loop meneruskan user_parts ke pesan user.
    4. ConsultantService: images (base64) diproses modul vision existing dan
       dilampirkan sebagai part pada pesan user ke provider.
    5. Backward compatible: consult tanpa images tidak mengirim parts.
    6. Gateway: field images diteruskan; validasi gambar tidak valid -> error.
    7. Frontend: api.js + ConsultantChat.vue memuat jalur attach image.

Jalankan:
    python scripts/check_consultant_vision.py
"""

from __future__ import annotations

import base64
import io
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
DJANGO_APP_DIR = PROJECT_ROOT / "web" / "django_app"
for p in (str(SRC_DIR), str(DJANGO_APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _png_bytes(size=(64, 48), color=(10, 200, 90)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _provider_payload(provider, messages):
    """Panggil _build_payload internal provider (tanpa network)."""
    return provider._build_payload(None, messages, None, None, None)


def main() -> int:
    print("=== Verifikasi Vision/Multimodal Consultant ===")
    from agent_ai.providers.base import Message
    from agent_ai.providers.openai_compatible import OpenAICompatibleProvider
    from agent_ai.config.settings import OpenAIConfig

    provider = OpenAICompatibleProvider(
        config=OpenAIConfig(api_key="x", base_url="http://localhost", model="m")
    )

    png = _png_bytes()
    b64 = base64.b64encode(png).decode("ascii")
    img_part = {
        "type": "image",
        "mime_type": "image/png",
        "encoding": "base64",
        "data": b64,
    }

    # 1) Provider: text-only TIDAK berubah.
    payload_text = _provider_payload(
        provider, [Message(role="user", content="halo")]
    )
    assert payload_text["messages"][0]["content"] == "halo"
    assert "parts" not in payload_text["messages"][0], payload_text["messages"][0]
    print("[1] text-only payload tidak berubah OK")

    # 1b) Provider: pesan image -> content blocks image_url data URL.
    payload_img = _provider_payload(
        provider,
        [Message(role="user", content="apa isi gambar ini?", parts=[img_part])],
    )
    blocks = payload_img["messages"][0]["content"]
    assert isinstance(blocks, list), blocks
    assert blocks[0] == {"type": "text", "text": "apa isi gambar ini?"}, blocks[0]
    assert blocks[1]["type"] == "image_url", blocks[1]
    url = blocks[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,"), url[:40]
    assert url.split(",", 1)[1] == b64
    assert "parts" not in payload_img["messages"][0]
    print("[1b] image content-part -> image_url data URL OK")

    # 1c) Part non-image diabaikan, teks tetap ada.
    payload_mixed = _provider_payload(
        provider,
        [Message(role="user", content="x", parts=[{"type": "audio", "data": "z"}, img_part])],
    )
    mixed_blocks = payload_mixed["messages"][0]["content"]
    assert len(mixed_blocks) == 2, mixed_blocks  # text + 1 image
    print("[1c] part tak didukung diabaikan OK")

    # 2) Core: ChatMessage/ChatMessage provider dict membawa parts.
    from agent_ai.core.types import ChatMessage
    from agent_ai.core.history import ConversationHistory

    msg = ChatMessage(role="user", content="t", parts=[img_part])
    pd = msg.to_provider_dict()
    assert pd["parts"] == [img_part], pd
    assert pd["content"] == "t"

    hist = ConversationHistory()
    hist.append_system_message("sys")
    hist.append_user_message("q", parts=[img_part])
    provider_msgs = hist.to_provider_format()
    assert provider_msgs[-1]["parts"] == [img_part], provider_msgs[-1]
    print("[2] ChatMessage/ConversationHistory membawa parts OK")

    # 3) Orchestrator: user_parts diteruskan ke pesan user awal.
    from agent_ai.core.orchestrator import AgentOrchestrator
    from agent_ai.core.response import FinishReason, LLMResponse
    from agent_ai.providers.base import BaseProvider, GenerateResult

    class CaptureProvider(BaseProvider):
        name = "capture"

        def __init__(self):
            self.seen = None

        def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
            self.seen = messages
            return GenerateResult(text="", model="m", provider="capture")

        def normalize_response(self, result):
            return LLMResponse(text="final", finish_reason=FinishReason.STOP)

    cap = CaptureProvider()
    orch = AgentOrchestrator(provider=cap, use_tools=False)
    res = orch.run_continuous_loop("lihat ini", user_parts=[img_part], max_steps=3)
    assert res.status.value == "completed" or res.result == "final", res
    assert cap.seen is not None
    user_msgs = [m for m in cap.seen if m.get("role") == "user"]
    assert user_msgs and user_msgs[-1].get("parts") == [img_part], user_msgs[-1]
    print("[3] run_continuous_loop meneruskan user_parts OK")

    # 4) ConsultantService: images diproses modul vision + dikirim ke provider.
    from agent_ai.consultant.service import ConsultantService

    cap2 = CaptureProvider()
    service = ConsultantService()
    result = service.consult(
        "gambar ini berisi apa?",
        provider=cap2,
        mode="quick",
        images=[{"data": b64, "mime_type": "image/png", "filename": "shot.png"}],
    )
    assert result.status == "done", result.error
    user_msgs2 = [m for m in cap2.seen if m.get("role") == "user"]
    assert user_msgs2, cap2.seen
    parts = user_msgs2[-1].get("parts")
    assert parts and parts[0]["type"] == "image", parts
    assert parts[0]["mime_type"] == "image/png", parts
    # Payload vision provider-agnostic -> base64 valid & decodable.
    decoded = base64.b64decode(parts[0]["data"])
    assert decoded[:8] == b"\x89PNG\r\n\x1a\n", "payload harus PNG valid"
    print("[4] ConsultantService memproses image + lampirkan ke pesan user OK")

    # 5) Backward compatible: tanpa images -> TIDAK ada parts.
    cap3 = CaptureProvider()
    service.consult("tanpa gambar", provider=cap3, mode="quick")
    user_msgs3 = [m for m in cap3.seen if m.get("role") == "user"]
    assert user_msgs3 and "parts" not in user_msgs3[-1], user_msgs3[-1]
    print("[5] consult tanpa image tetap text-only OK")

    # 5b) Gambar tidak valid -> error jelas (bukan silent-fail).
    from agent_ai.vision.models import VisionError

    raised = None
    try:
        service.consult(
            "x",
            provider=CaptureProvider(),
            mode="quick",
            images=[{"data": base64.b64encode(b"bukan gambar").decode("ascii"),
                     "mime_type": "image/png"}],
        )
    except Exception as exc:  # noqa: BLE001
        raised = exc
    assert raised is not None and isinstance(raised, (VisionError, ValueError)), raised
    print(f"[5b] gambar tidak valid ditolak jelas OK -> {type(raised).__name__}")

    # 6) Gateway: images diteruskan; gambar tidak valid -> ValidationError.
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
    import django

    django.setup()
    import api.services as services_mod
    from api.services import ValidationError

    gw = services_mod.GatewayService(auto_execute=False, consultant_service=ConsultantService())
    gw_cap = CaptureProvider()
    gw._build_consultant_provider = lambda *a, **k: gw_cap  # type: ignore[assignment]
    out = gw.consult(
        message="apa ini?",
        root=str(PROJECT_ROOT),
        images=[{"data": b64, "mime_type": "image/png"}],
    )
    assert out["status"] == "done", out
    gw_user = [m for m in gw_cap.seen if m.get("role") == "user"]
    assert gw_user[-1].get("parts"), "gateway harus meneruskan image ke provider"
    print("[6] Gateway meneruskan images OK")

    # 6b) Validasi: images bukan array / gambar invalid -> ValidationError (400).
    try:
        gw.consult(message="x", images="bukan-array")
        raise AssertionError("harus menolak images bukan array")
    except ValidationError:
        pass
    try:
        gw.consult(
            message="x",
            images=[{"data": base64.b64encode(b"nope").decode("ascii"), "mime_type": "image/png"}],
        )
        raise AssertionError("harus menolak gambar invalid")
    except ValidationError:
        pass
    # 6c) Tanpa images -> perilaku lama (tidak ada parts).
    gw_cap2 = CaptureProvider()
    gw._build_consultant_provider = lambda *a, **k: gw_cap2  # type: ignore[assignment]
    gw.consult(message="text saja", root=str(PROJECT_ROOT))
    gw_user2 = [m for m in gw_cap2.seen if m.get("role") == "user"]
    assert "parts" not in gw_user2[-1], gw_user2[-1]
    print("[6b/6c] validasi images + backward compatible OK")

    # 7) Frontend: jalur attach image ada di api.js + ConsultantChat.vue.
    api_js = (PROJECT_ROOT / "web" / "frontend" / "src" / "api.js").read_text(encoding="utf-8")
    assert "body.images = images" in api_js, "api.js harus mengirim field 'images'"
    chat_vue = (
        PROJECT_ROOT / "web" / "frontend" / "src" / "components" / "ConsultantChat.vue"
    ).read_text(encoding="utf-8")
    for needle in ("onFilesPicked", "attachments", "attach-btn", "removeAttachment", "images:"):
        assert needle in chat_vue, f"ConsultantChat.vue harus memuat: {needle}"
    assert "image/jpeg,image/png,image/webp" in chat_vue, "accept harus jpeg/png/webp"
    print("[7] frontend attach image OK -> api.js + ConsultantChat.vue")

    print()
    print("[OK] Vision/Multimodal Consultant bekerja (additive, backward compatible).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
