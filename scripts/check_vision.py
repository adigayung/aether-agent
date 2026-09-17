"""Verifikasi Vision / Multimodal Input (#46).

Deterministik, tanpa API cloud. Fixture kecil di
J:\\Agent_Ai\\dummy_test\\vision_fixture dan dibersihkan setelah test.

Menguji:
    1. ImageInput valid
    2. MIME detection
    3. resize mempertahankan aspect ratio
    4. compression menghasilkan payload valid
    5. maximum bytes dihormati bila memungkinkan
    6. PNG/JPEG/WebP
    7. readability-oriented policy
    8. invalid image ditolak secara terstruktur
    9. no unsafe path handling
   10. vision capability requirement
   11. routing menolak model tanpa VISION/MULTIMODAL
   12. provider abstraction tetap bersih
   13. tidak ada provider-specific Vision engine
   14. no duplicate routing/tool/executor
   15. architecture boundary bersih

Jalankan:
    python scripts/check_vision.py
"""

from __future__ import annotations

import io
import shutil
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
from agent_ai.routing import ModelRouter, RoutingRegistry, RoutingRequest  # noqa: E402
from agent_ai.tools.base import ToolValidationError  # noqa: E402
from agent_ai.vision import (  # noqa: E402
    ImageFormat,
    ImageInputLoader,
    ImagePreprocessor,
    InvalidImageError,
    UnsupportedImageFormatError,
    VisionConfig,
    VisionPolicy,
    VisionRequest,
)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "vision_fixture"


def _make_image(path: Path, size, fmt: str, color=(120, 80, 200), text=False) -> None:
    """Buat gambar uji dengan Pillow (deterministik)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, color)
    if text:
        draw = ImageDraw.Draw(img)
        for i in range(0, size[1], 20):
            draw.text((5, i), "CODE 0123456789 def foo(): return 42", fill=(255, 255, 255))
    img.save(path, format=fmt)


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    _make_image(FIXTURE / "big.png", (3000, 1500), "PNG")
    _make_image(FIXTURE / "photo.jpg", (2400, 1600), "JPEG")
    _make_image(FIXTURE / "shot.webp", (2000, 1000), "WEBP")
    _make_image(FIXTURE / "text.png", (1600, 900), "PNG", text=True)
    (FIXTURE / "not_image.txt").write_text("ini bukan gambar", encoding="utf-8")
    (FIXTURE / "empty.png").write_bytes(b"")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def main() -> int:
    print("=== Verifikasi Vision / Multimodal Input (#46) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    loader = ImageInputLoader(root=PROJECT_ROOT)
    pre = ImagePreprocessor(VisionConfig())
    policy = VisionPolicy(VisionConfig())

    # 1) ImageInput valid.
    img = loader.load("dummy_test/vision_fixture/big.png")
    assert img.data and img.mime_type == "image/png", img.mime_type
    assert img.filename == "big.png", img.filename
    assert img.format == ImageFormat.PNG, img.format
    print(f"[1] ImageInput valid OK -> {img.filename} ({len(img.data)} bytes)")

    # 2) MIME detection (dari magic bytes, tanpa Pillow).
    assert ImageInputLoader.detect_mime((FIXTURE / "big.png").read_bytes()) == "image/png"
    assert ImageInputLoader.detect_mime((FIXTURE / "photo.jpg").read_bytes()) == "image/jpeg"
    assert ImageInputLoader.detect_mime((FIXTURE / "shot.webp").read_bytes()) == "image/webp"
    assert ImageInputLoader.detect_mime(b"bukan gambar") is None
    print("[2] MIME detection OK -> png/jpeg/webp terdeteksi, non-image None")

    # 3) resize mempertahankan aspect ratio.
    proc = pre.process(img.data, mime_type="image/png")
    orig = proc.original_metadata
    new = proc.metadata
    assert proc.resized, "gambar besar harus di-resize"
    assert max(new.width, new.height) <= 1568, (new.width, new.height)
    # Aspect ratio dipertahankan (toleransi pembulatan).
    assert abs(orig.aspect_ratio - new.aspect_ratio) < 0.02, (orig.aspect_ratio, new.aspect_ratio)
    print(f"[3] resize mempertahankan aspect ratio OK -> {orig.width}x{orig.height} -> {new.width}x{new.height}")

    # 4) compression menghasilkan payload valid.
    assert proc.payload.get("type") == "image", proc.payload.get("type")
    assert proc.payload.get("encoding") == "base64"
    import base64
    decoded = base64.b64decode(proc.payload["data"])
    assert decoded == proc.data, "payload base64 harus decode ke data yang sama"
    # Payload dapat dibuka kembali sebagai gambar valid.
    from PIL import Image
    with Image.open(io.BytesIO(decoded)) as reopened:
        assert reopened.width == new.width and reopened.height == new.height
    print(f"[4] compression payload valid OK -> {proc.size_bytes} bytes, mime={proc.mime_type}")

    # 5) maximum bytes dihormati bila memungkinkan.
    small_cfg = VisionConfig(max_bytes=60_000, jpeg_quality=85)
    pre_small = ImagePreprocessor(small_cfg)
    big_jpg = loader.load("dummy_test/vision_fixture/photo.jpg")
    proc_small = pre_small.process(big_jpg.data, mime_type="image/jpeg")
    assert proc_small.size_bytes <= small_cfg.max_bytes, (proc_small.size_bytes, small_cfg.max_bytes)
    print(f"[5] maximum bytes dihormati OK -> {proc_small.size_bytes} <= {small_cfg.max_bytes}")

    # 6) PNG/JPEG/WebP.
    for name, mime, fmt in (
        ("big.png", "image/png", ImageFormat.PNG),
        ("photo.jpg", "image/jpeg", ImageFormat.JPEG),
        ("shot.webp", "image/webp", ImageFormat.WEBP),
    ):
        src = loader.load(f"dummy_test/vision_fixture/{name}")
        out = pre.process(src.data, mime_type=mime)
        assert out.metadata.format == fmt, (name, out.metadata.format)
        assert out.size_bytes > 0
    print("[6] PNG/JPEG/WebP OK -> ketiga format diproses")

    # 7) readability-oriented policy (gambar ber-teks tidak di-resize ekstrem).
    text_img = loader.load("dummy_test/vision_fixture/text.png")
    text_img.metadata["has_text"] = True
    assert policy.is_readability(text_img) is True
    proc_text = pre.process(text_img.data, mime_type="image/png", readability=True)
    # Readability: dimensi maksimum lebih besar (2048) daripada default (1568).
    assert max(proc_text.metadata.width, proc_text.metadata.height) <= 2048
    # Gambar 1600x900 < 2048 -> TIDAK di-resize (readability diprioritaskan).
    assert proc_text.resized is False, "gambar ber-teks < readability_max_dimension tidak boleh di-resize"
    meta = policy.preprocessing_metadata(text_img)
    assert meta["readability"] is True and meta["max_dimension"] == 2048
    print(f"[7] readability-oriented policy OK -> resized={proc_text.resized}, max_dim={meta['max_dimension']}")

    # 8) invalid image ditolak secara terstruktur.
    try:
        loader.load("dummy_test/vision_fixture/not_image.txt")
        raise AssertionError("harus menolak non-image")
    except UnsupportedImageFormatError:
        pass
    try:
        loader.load("dummy_test/vision_fixture/empty.png")
        raise AssertionError("harus menolak file kosong")
    except InvalidImageError:
        pass
    try:
        pre.process(b"bukan gambar sama sekali", mime_type="image/png")
        raise AssertionError("harus menolak bytes bukan gambar")
    except InvalidImageError:
        pass
    print("[8] invalid image ditolak terstruktur OK -> UnsupportedImageFormatError/InvalidImageError")

    # 9) no unsafe path handling (path traversal ditolak).
    try:
        loader.load("../../etc/passwd")
        raise AssertionError("harus menolak path traversal")
    except ToolValidationError:
        pass
    try:
        loader.load("dummy_test/../../outside.png")
        raise AssertionError("harus menolak path keluar workspace")
    except ToolValidationError:
        pass
    print("[9] no unsafe path handling OK -> path traversal ditolak")

    # 10) vision capability requirement.
    req = VisionRequest(prompt="Apa isi gambar ini?", images=[img])
    assert ModelCapability.VISION in req.required_capabilities
    assert ModelCapability.MULTIMODAL in req.required_capabilities
    assert policy.required_capabilities() == req.required_capabilities
    problems = policy.validate_request(req)
    assert problems == [], problems
    # Request tanpa gambar -> masalah terstruktur.
    assert policy.validate_request(VisionRequest(prompt="x", images=[])) != []
    print(f"[10] vision capability requirement OK -> {sorted(c.value for c in req.required_capabilities)}")

    # 11) routing menolak model tanpa VISION/MULTIMODAL.
    cap_reg = ModelCapabilityRegistry()
    cap_reg.register(ModelCapabilities(
        provider="text_only",
        model="text-model",
        capabilities=frozenset({ModelCapability.TOOL_CALLING}),
        context_window=128000,
    ))
    cap_reg.register(ModelCapabilities(
        provider="vision_provider",
        model="vision-model",
        capabilities=frozenset({ModelCapability.VISION, ModelCapability.MULTIMODAL}),
        context_window=128000,
    ))
    router = ModelRouter(RoutingRegistry(capability_registry=cap_reg))
    routing_req = RoutingRequest(
        task=req.prompt,
        required_capabilities=policy.required_capabilities(),
    )
    decision = router.route(routing_req)
    assert not decision.failed, decision.rationale
    assert decision.selected.provider == "vision_provider", decision.selected.provider
    # Model text-only harus dieliminasi.
    eliminated = [c for c in decision.candidates if not c.eligible]
    assert any(c.provider == "text_only" for c in eliminated), "text-only harus dieliminasi"
    # Bila hanya ada model text-only -> routing gagal (tidak memilih).
    cap_reg2 = ModelCapabilityRegistry()
    cap_reg2.register(ModelCapabilities(
        provider="text_only", model="text-model",
        capabilities=frozenset({ModelCapability.TOOL_CALLING}), context_window=128000,
    ))
    router2 = ModelRouter(RoutingRegistry(capability_registry=cap_reg2))
    decision2 = router2.route(RoutingRequest(task="x", required_capabilities=policy.required_capabilities()))
    assert decision2.failed is True and decision2.selected is None, decision2.selected
    print(f"[11] routing menolak model tanpa VISION/MULTIMODAL OK -> {decision.selected.key}")

    # 12) provider abstraction tetap bersih.
    from agent_ai.providers.base import BaseProvider
    assert hasattr(BaseProvider, "generate") and hasattr(BaseProvider, "is_available")
    vision_dir = SRC_DIR / "agent_ai" / "vision"
    for p in vision_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        for name in ("openrouter", "deepseek", "ollama", "openai_compatible"):
            assert f"import {name}" not in text, f"{p.name} tidak boleh impor provider konkret"
        assert ".generate(" not in text, f"{p.name} tidak boleh memanggil provider.generate"
    print("[12] provider abstraction tetap bersih OK")

    # 13) tidak ada provider-specific Vision engine.
    for p in vision_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8").lower()
        assert "deepseek" not in text, f"{p.name} tidak boleh hardcode DeepSeek"
        assert "if provider ==" not in text, f"{p.name} tidak boleh punya cabang khusus provider"
        # Tidak ada OCR / image-to-text / enhancement.
        for bad in ("pytesseract", "easyocr", "ocr(", "image_to_text", "enhance("):
            assert bad not in text, f"{p.name} tidak boleh melakukan OCR/enhancement: {bad}"
    print("[13] tidak ada provider-specific Vision engine OK")

    # 14) no duplicate routing/tool/executor.
    for p in vision_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "class ModelRouter" not in text, f"{p.name} tidak boleh membuat routing engine kedua"
        assert "class RoutingRegistry" not in text, f"{p.name} tidak boleh membuat routing registry kedua"
        assert "class ToolExecutor" not in text, f"{p.name} tidak boleh membuat executor kedua"
        assert "class AgentRuntime" not in text, f"{p.name} tidak boleh membuat runtime kedua"
        assert "import subprocess" not in text, f"{p.name} tidak boleh menjalankan command"
    print("[14] no duplicate routing/tool/executor OK")

    # 15) architecture boundary bersih.
    #     - core TIDAK boleh impor vision.
    core_dir = SRC_DIR / "agent_ai" / "core"
    for p in core_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "agent_ai.vision" not in text, f"core/{p.name} tidak boleh impor vision"
    #     - vision tidak boleh impor core/runtime (boundary).
    for p in vision_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "agent_ai.core" not in text, f"{p.name} tidak boleh impor core"
        assert "agent_ai.runtime" not in text, f"{p.name} tidak boleh impor runtime"
        for bad in ("import requests", "import urllib", "import socket"):
            assert bad not in text, f"{p.name} tidak boleh melakukan network: {bad}"
    #     - vision memakai boundary helper yang sudah ada (bukan mekanisme kedua).
    input_src = (vision_dir / "input.py").read_text(encoding="utf-8")
    assert "_resolve_within_root" in input_src, "vision harus memakai boundary helper existing"
    print("[15] architecture boundary bersih OK")

    print()
    print("[OK] Vision / Multimodal Input bekerja (provider-agnostic, readability-aware).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
