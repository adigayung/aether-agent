"""Verifikasi Model Routing (#44).

Deterministik, tanpa API cloud. Fixture kecil di
J:\\Agent_Ai\\dummy_test\\model_routing_fixture dan dibersihkan setelah test.

Menguji:
    1. simple task memilih kandidat yang sesuai
    2. complex task dapat memilih kandidat berbeda
    3. capability requirement dipatuhi
    4. context window diperiksa
    5. unavailable candidate dieliminasi
    6. deterministic result
    7. no candidate menghasilkan keputusan terstruktur
    8. tidak melakukan fallback
    9. tidak membuat capability registry baru
   10. provider abstraction tetap utuh
   11. architecture boundary tetap bersih

Jalankan:
    python scripts/check_model_routing.py
"""

from __future__ import annotations

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
from agent_ai.routing import (  # noqa: E402
    ModelRouter,
    RoutingConfig,
    RoutingRegistry,
    RoutingRequest,
    TaskComplexity,
)

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
FIXTURE = DUMMY_ROOT / "model_routing_fixture"


def setup_fixture() -> None:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    (FIXTURE / "note.txt").write_text("routing fixture\n", encoding="utf-8")


def teardown_fixture() -> None:
    shutil.rmtree(FIXTURE, ignore_errors=True)


def _build_registry() -> ModelCapabilityRegistry:
    """Capability registry fixture (memakai registry yang sudah ada)."""
    reg = ModelCapabilityRegistry()
    # Model ringan (lokal) - cocok untuk task sederhana.
    reg.register(ModelCapabilities(
        provider="ollama",
        model="qwen2.5-coder:7b",
        capabilities=frozenset({ModelCapability.TOOL_CALLING, ModelCapability.STREAMING}),
        context_window=32768,
    ))
    # Model kuat dengan reasoning + context besar - cocok task kompleks.
    reg.register(ModelCapabilities(
        provider="openrouter",
        model="anthropic/claude-3.5",
        capabilities=frozenset({
            ModelCapability.TOOL_CALLING,
            ModelCapability.REASONING,
            ModelCapability.VISION,
            ModelCapability.MULTIMODAL,
            ModelCapability.STRUCTURED_OUTPUT,
        }),
        context_window=200000,
    ))
    # Model menengah.
    reg.register(ModelCapabilities(
        provider="deepseek",
        model="deepseek-chat",
        capabilities=frozenset({ModelCapability.TOOL_CALLING, ModelCapability.STRUCTURED_OUTPUT}),
        context_window=64000,
    ))
    return reg


def main() -> int:
    print("=== Verifikasi Model Routing (#44) ===")
    setup_fixture()
    try:
        return _run()
    finally:
        teardown_fixture()


def _run() -> int:
    cap_reg = _build_registry()
    routing_reg = RoutingRegistry(capability_registry=cap_reg)
    router = ModelRouter(routing_reg, config=RoutingConfig(prefer_default_provider=True))

    # 1) simple task memilih kandidat yang sesuai (model ringan).
    simple_req = RoutingRequest(
        task="rename variabel typo",
        preferred_provider="ollama",
    )
    dec_simple = router.route(simple_req)
    assert not dec_simple.failed, dec_simple.rationale
    assert dec_simple.complexity == TaskComplexity.SIMPLE, dec_simple.complexity
    assert dec_simple.selected.provider == "ollama", dec_simple.selected.provider
    print(f"[1] simple task memilih kandidat sesuai OK -> {dec_simple.provider}:{dec_simple.model}")

    # 2) complex task dapat memilih kandidat berbeda (reasoning + context besar).
    complex_req = RoutingRequest(
        task="refactor arsitektur dan optimasi performance end-to-end",
        required_capabilities=frozenset({ModelCapability.REASONING}),
    )
    dec_complex = router.route(complex_req)
    assert not dec_complex.failed, dec_complex.rationale
    assert dec_complex.complexity == TaskComplexity.COMPLEX, dec_complex.complexity
    assert dec_complex.selected.provider == "openrouter", dec_complex.selected.provider
    assert dec_complex.selected.key != dec_simple.selected.key, "complex harus bisa beda dari simple"
    print(f"[2] complex task memilih kandidat berbeda OK -> {dec_complex.provider}:{dec_complex.model}")

    # 3) capability requirement dipatuhi (vision wajib -> hanya openrouter).
    vision_req = RoutingRequest(
        task="analisis gambar screenshot",
        required_capabilities=frozenset({ModelCapability.VISION}),
    )
    dec_vision = router.route(vision_req)
    assert not dec_vision.failed, dec_vision.rationale
    assert dec_vision.selected.provider == "openrouter", dec_vision.selected.provider
    # Kandidat tanpa vision harus dieliminasi.
    eliminated = [c for c in dec_vision.candidates if not c.eligible]
    assert any(c.provider == "ollama" for c in eliminated), "ollama harus dieliminasi (tanpa vision)"
    print(f"[3] capability requirement dipatuhi OK -> {dec_vision.provider}:{dec_vision.model}")

    # 4) context window diperiksa (butuh 100k -> ollama/deepseek dieliminasi).
    ctx_req = RoutingRequest(
        task="baca seluruh repository",
        context_tokens=100000,
    )
    dec_ctx = router.route(ctx_req)
    assert not dec_ctx.failed, dec_ctx.rationale
    assert dec_ctx.selected.context_window >= 100000, dec_ctx.selected.context_window
    assert dec_ctx.selected.provider == "openrouter", dec_ctx.selected.provider
    eliminated_ctx = [c for c in dec_ctx.candidates if not c.eligible]
    assert {c.provider for c in eliminated_ctx} >= {"ollama", "deepseek"}, eliminated_ctx
    print(f"[4] context window diperiksa OK -> {dec_ctx.provider}:{dec_ctx.model}")

    # 5) unavailable candidate dieliminasi.
    def availability(provider: str) -> bool:
        return provider.lower() != "openrouter"  # openrouter dianggap tidak tersedia

    routing_reg_unavail = RoutingRegistry(
        capability_registry=cap_reg,
        availability=availability,
    )
    router_unavail = ModelRouter(routing_reg_unavail, config=RoutingConfig())
    dec_unavail = router_unavail.route(RoutingRequest(task="task biasa"))
    assert not dec_unavail.failed, dec_unavail.rationale
    assert dec_unavail.selected.provider != "openrouter", dec_unavail.selected.provider
    unavail = [c for c in dec_unavail.candidates if c.provider == "openrouter"]
    assert unavail and unavail[0].eligible is False, "openrouter harus dieliminasi (unavailable)"
    print(f"[5] unavailable candidate dieliminasi OK -> {dec_unavail.provider}:{dec_unavail.model}")

    # 6) deterministic result (input sama -> keputusan sama).
    req_a = RoutingRequest(task="refactor arsitektur", required_capabilities=frozenset({ModelCapability.REASONING}))
    req_b = RoutingRequest(task="refactor arsitektur", required_capabilities=frozenset({ModelCapability.REASONING}))
    dec_a = router.route(req_a)
    dec_b = router.route(req_b)
    assert dec_a.selected.key == dec_b.selected.key, (dec_a.selected.key, dec_b.selected.key)
    assert dec_a.selected.score == dec_b.selected.score
    assert dec_a.rationale == dec_b.rationale
    print(f"[6] deterministic result OK -> {dec_a.selected.key} score={dec_a.selected.score}")

    # 7) no candidate menghasilkan keputusan terstruktur (bukan exception).
    impossible = RoutingRequest(
        task="task mustahil",
        required_capabilities=frozenset({ModelCapability.VISION}),
        context_tokens=10_000_000,  # melebihi semua kandidat
    )
    dec_none = router.route(impossible)
    assert dec_none.failed is True, "harus failed"
    assert dec_none.selected is None, dec_none.selected
    assert dec_none.rationale, "harus ada rationale terstruktur"
    assert dec_none.to_dict()["failed"] is True
    print(f"[7] no candidate -> keputusan terstruktur OK -> rationale='{dec_none.rationale}'")

    # 8) tidak melakukan fallback (gagal tetap gagal, tidak pindah provider).
    #    Bila requirement tidak terpenuhi, selected harus None (bukan kandidat lain).
    assert dec_none.selected is None
    #    Router tidak memilih provider lain yang tidak memenuhi requirement.
    assert all(not c.eligible for c in dec_none.candidates), "semua kandidat harus tidak eligible"
    print("[8] tidak melakukan fallback OK")

    # 9) tidak membuat capability registry baru.
    routing_dir = SRC_DIR / "agent_ai" / "routing"
    files = {p.name for p in routing_dir.glob("*.py")}
    assert files == {"__init__.py", "models.py", "classifier.py", "router.py", "registry.py"}, files
    for p in routing_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "class ModelCapabilityRegistry" not in text, f"{p.name} tidak boleh membuat capability registry baru"
        assert "class ModelCapabilities" not in text, f"{p.name} tidak boleh mendefinisikan ModelCapabilities"
    # RoutingRegistry harus MEMBUNGKUS registry yang ada.
    reg_src = (routing_dir / "registry.py").read_text(encoding="utf-8")
    assert "ModelCapabilityRegistry" in reg_src, "RoutingRegistry harus memakai ModelCapabilityRegistry yang ada"
    print("[9] tidak membuat capability registry baru OK")

    # 10) provider abstraction tetap utuh (routing tidak mengubah provider).
    #     Routing tidak mengimpor provider konkret & tidak memanggil generate().
    for p in routing_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        for name in ("openrouter", "deepseek", "ollama", "openai_compatible"):
            assert f"import {name}" not in text, f"{p.name} tidak boleh impor provider konkret"
        assert ".generate(" not in text, f"{p.name} tidak boleh memanggil provider.generate"
    # Provider abstraction (BaseProvider) tetap tersedia & tidak berubah.
    from agent_ai.providers.base import BaseProvider
    assert hasattr(BaseProvider, "generate") and hasattr(BaseProvider, "is_available")
    print("[10] provider abstraction tetap utuh OK")

    # 11) architecture boundary tetap bersih.
    #     - core TIDAK boleh impor routing (Agent Core tetap provider-agnostic).
    core_dir = SRC_DIR / "agent_ai" / "core"
    for p in core_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "agent_ai.routing" not in text, f"core/{p.name} tidak boleh impor routing"
    #     - routing tidak boleh impor core/runtime (boundary).
    for p in routing_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "agent_ai.core" not in text, f"{p.name} tidak boleh impor core"
        assert "agent_ai.runtime" not in text, f"{p.name} tidak boleh impor runtime"
    #     - routing tidak melakukan network/exec.
    for p in routing_dir.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        for bad in ("import requests", "import urllib", "import socket", "import subprocess", "os.system("):
            assert bad not in text, f"{p.name} tidak boleh melakukan network/exec: {bad}"
    print("[11] architecture boundary tetap bersih OK")

    print()
    print("[OK] Model Routing bekerja (deterministik, capability-aware, no fallback).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
