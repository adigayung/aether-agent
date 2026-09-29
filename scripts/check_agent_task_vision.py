"""Verifikasi Vision/Multimodal pada jalur AGENT TASK (deterministik, tanpa API key).

Membuktikan rantai ADDITIVE (reuse mekanisme vision Consultant):

    attachment -> normalization -> image parts -> jalur Agent -> provider image_url

    1. Provider: content part image -> image_url data URL (payload OpenAI-
       compatible). Pesan text-only TIDAK berubah.
    2. Helper vision bersama (`build_image_parts`) — dipakai bersama Consultant:
       base64 valid -> image part; None/kosong -> None; gambar invalid ditolak.
    3. Gateway `create_task` menerima `images`: dinormalisasi (batas SAMA dengan
       Consultant: maks 8, JPEG/PNG/WebP, batas byte) lalu disimpan sebagai
       attachment task. base64 TIDAK muncul di dict task (anti bocor ke
       API/log/activity). Image parts hasil gateway -> provider image_url.
    4. Jalur Agent: `create_task -> _run_task_inner -> TaskExecutor` menerima
       `user_parts` (image parts).
    5. Validasi: `images` bukan array / gambar invalid / terlalu banyak ->
       ValidationError.
    6. Backward compatible: create_task tanpa images -> user_parts None.
    7. Runtime/orchestrator NYATA (TaskExecutor + provider capture):
       AgentOrchestrator menerima `user_parts` dan provider MELIHAT image part
       pada pesan user.
    8. Frontend: api.js `createTask` mengirim `images`; TaskComposer.vue memuat
       jalur attach image (format mengikuti Consultant).

Jalankan:
    python scripts/check_agent_task_vision.py
"""

from __future__ import annotations

import base64
import io
import json
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
    print("=== Verifikasi Vision/Multimodal Agent Task ===")
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

    # 1) Provider: content part image -> image_url data URL; text-only tetap.
    payload_text = _provider_payload(provider, [Message(role="user", content="halo")])
    assert payload_text["messages"][0]["content"] == "halo"
    assert "parts" not in payload_text["messages"][0], payload_text["messages"][0]
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
    print("[1] provider: image part -> image_url data URL OK")

    # 2) Helper vision bersama (dipakai Consultant + Agent Task).
    from agent_ai.vision.parts import build_image_parts
    from agent_ai.vision.models import VisionError

    parts = build_image_parts([{"data": b64, "mime_type": "image/png"}])
    assert parts and parts[0]["type"] == "image", parts
    assert parts[0]["mime_type"] == "image/png", parts[0]
    assert base64.b64decode(parts[0]["data"])[:8] == b"\x89PNG\r\n\x1a\n"
    assert build_image_parts(None) is None
    assert build_image_parts([]) is None
    bad = None
    try:
        build_image_parts(
            [{"data": base64.b64encode(b"nope").decode("ascii"), "mime_type": "image/png"}]
        )
    except Exception as exc:  # noqa: BLE001
        bad = exc
    assert bad is not None and isinstance(bad, (VisionError, ValueError)), bad
    print("[2] helper vision bersama (build_image_parts) OK")

    # Django setup (GatewayService butuh settings untuk ProjectStore).
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ["DJANGO_ALLOWED_HOSTS"] = "testserver,127.0.0.1,localhost"
    import django

    django.setup()
    import api.services as services_mod
    from api.services import ValidationError
    from agent_ai.core.cancel import CancellationToken
    from agent_ai.task.preparation import TaskPreparation

    class FakeTaskExecutor:
        """TaskExecutor palsu: menangkap kwargs (termasuk `user_parts`)."""

        def __init__(self):
            self.calls = []

        def run(self, prepared, **kwargs):
            self.calls.append(kwargs)
            on_status = kwargs.get("on_status")
            if on_status is not None:
                on_status("completed", "done", None)
            return {"status": "completed", "result": "done", "error": None, "iterations": 1}

    gw = services_mod.GatewayService(auto_execute=False, task_executor=FakeTaskExecutor())

    # 3) create_task: images -> attachment tersimpan (tanpa base64 di respons).
    record = gw.create_task(
        "lihat gambar ini", images=[{"data": b64, "mime_type": "image/png"}]
    )
    tid = record["task_id"]
    assert gw._task_attachments.get(tid), "attachment harus tersimpan per-task"
    stored = gw._task_attachments[tid]
    assert stored[0]["type"] == "image" and stored[0]["mime_type"] == "image/png", stored
    # base64 TIDAK boleh bocor ke dict task (API list/history/activity/log).
    assert "images" not in record and "attachments" not in record, record
    assert b64 not in json.dumps(record), "base64 TIDAK boleh ada di respons task"
    # image parts hasil gateway -> provider image_url content.
    payload_gw = _provider_payload(
        provider,
        [Message(role="user", content="q", parts=stored)],
    )
    gw_blocks = payload_gw["messages"][0]["content"]
    assert gw_blocks[1]["type"] == "image_url", gw_blocks[1]
    assert gw_blocks[1]["image_url"]["url"].startswith("data:image/png;base64,"), gw_blocks[1]
    print("[3] create_task simpan attachment (tanpa base64) + image_url OK")

    # 4) Jalur Agent: _run_task_inner -> TaskExecutor menerima user_parts.
    gw._run_task_inner(tid, CancellationToken())
    call = gw.task_executor.calls[-1]
    up = call.get("user_parts")
    assert up and up[0]["type"] == "image", up
    assert up[0]["mime_type"] == "image/png", up
    print("[4] jalur Agent meneruskan user_parts ke TaskExecutor OK")

    # 5) Validasi: bukan array / gambar invalid / terlalu banyak -> ValidationError.
    try:
        gw.create_task("x", images="bukan-array")
        raise AssertionError("harus menolak images bukan array")
    except ValidationError:
        pass
    try:
        gw.create_task(
            "x",
            images=[{"data": base64.b64encode(b"nope").decode("ascii"), "mime_type": "image/png"}],
        )
        raise AssertionError("harus menolak gambar invalid")
    except ValidationError:
        pass
    try:
        gw.create_task("x", images=[{"data": b64, "mime_type": "image/png"}] * 9)
        raise AssertionError("harus menolak lebih dari 8 gambar")
    except ValidationError:
        pass
    print("[5] validasi images (array/format/jumlah) OK")

    # 6) Backward compatible: tanpa images -> user_parts None (perilaku lama).
    record2 = gw.create_task("tanpa gambar")
    tid2 = record2["task_id"]
    gw._run_task_inner(tid2, CancellationToken())
    call2 = gw.task_executor.calls[-1]
    assert not call2.get("user_parts"), call2.get("user_parts")
    print("[6] create_task tanpa images -> tanpa user_parts OK")

    # 7) Runtime/orchestrator NYATA: AgentOrchestrator menerima user_parts dan
    #    provider MELIHAT image part pada pesan user.
    from agent_ai.session.store import InMemorySessionStore
    from api.execution import TaskExecutor
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
    tx = TaskExecutor(InMemorySessionStore(), provider_factory=lambda: cap)
    prepared = TaskPreparation().prepare("gambar ini berisi apa?", task_id="t-vision-1")
    summary = tx.run(
        prepared,
        session_id="s-vision-1",
        task_id="t-vision-1",
        user_parts=[img_part],
    )
    assert summary["status"] == "completed", summary
    assert cap.seen is not None
    user_msgs = [m for m in cap.seen if m.get("role") == "user"]
    assert user_msgs and user_msgs[-1].get("parts") == [img_part], user_msgs[-1]
    print("[7] TaskExecutor -> AgentRuntime -> AgentOrchestrator menerima user_parts OK")

    # 7b) Tanpa user_parts -> pesan user TIDAK membawa parts (backward compatible).
    cap2 = CaptureProvider()
    tx2 = TaskExecutor(InMemorySessionStore(), provider_factory=lambda: cap2)
    prepared2 = TaskPreparation().prepare("teks saja", task_id="t-vision-2")
    tx2.run(prepared2, session_id="s-vision-2", task_id="t-vision-2")
    user_msgs2 = [m for m in cap2.seen if m.get("role") == "user"]
    assert "parts" not in user_msgs2[-1], user_msgs2[-1]
    print("[7b] jalur Agent tanpa gambar tetap text-only OK")

    # 8) Frontend: api.js + TaskComposer.vue memuat jalur attach image.
    api_js = (PROJECT_ROOT / "web" / "frontend" / "src" / "api.js").read_text(encoding="utf-8")
    assert "function createTask(" in api_js, "createTask harus ada di api.js"
    assert "body.images = images" in api_js, "api.js harus mengirim field 'images'"
    composer = (
        PROJECT_ROOT
        / "web"
        / "frontend"
        / "src"
        / "components"
        / "TaskComposer.vue"
    ).read_text(encoding="utf-8")
    for needle in ("onFilesPicked", "attachments", "attach-btn", "removeAttachment", "images:"):
        assert needle in composer, f"TaskComposer.vue harus memuat: {needle}"
    assert "image/jpeg,image/png,image/webp" in composer, "accept harus jpeg/png/webp"
    print("[8] frontend Agent Task attach image OK -> api.js + TaskComposer.vue")

    print()
    print("[OK] Vision/Multimodal Agent Task bekerja (additive, backward compatible).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
