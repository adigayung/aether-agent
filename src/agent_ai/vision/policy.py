"""VisionPolicy: policy vision (preprocessing mode + routing requirement).

Provider-agnostic, deterministik. Policy TIDAK melakukan preprocessing atau
routing; ia hanya memutuskan:
    - apakah gambar perlu mode readability (ber-teks/UI/code),
    - requirement capability untuk routing (VISION/MULTIMODAL),
    - metadata/policy agar provider adapter dapat menentukan mode/detail.

Tidak ada branching provider-specific. Provider adapter yang menerjemahkan
payload ke format API masing-masing.
"""

from __future__ import annotations

from typing import FrozenSet, List, Optional

from agent_ai.capabilities.models import ModelCapability
from agent_ai.vision.models import ImageInput, VisionConfig, VisionRequest

#: Capability yang WAJIB untuk vision request.
VISION_REQUIRED_CAPABILITIES: FrozenSet[ModelCapability] = frozenset({
    ModelCapability.VISION,
    ModelCapability.MULTIMODAL,
})


class VisionPolicy:
    """Policy vision (preprocessing mode + routing requirement).

    Args:
        config: VisionConfig (bounded). Default: VisionConfig().
    """

    def __init__(self, config: Optional[VisionConfig] = None) -> None:
        self.config = config or VisionConfig()

    # ------------------------------------------------------------------ #
    # Preprocessing mode
    # ------------------------------------------------------------------ #
    def is_readability(self, image: ImageInput) -> bool:
        """True bila gambar sebaiknya diproses dengan mode readability.

        Mode readability dipakai bila:
            - readability_mode aktif di config, DAN
            - gambar ditandai ber-teks/UI/code (metadata "readability" atau
              "has_text"), ATAU resolusi tinggi (kemungkinan screenshot/dokumen).
        """
        if not self.config.readability_mode:
            return False
        meta = image.metadata or {}
        if meta.get("readability") or meta.get("has_text"):
            return True
        # Heuristik deterministik: gambar besar cenderung screenshot/dokumen.
        size = meta.get("size_bytes") or (len(image.data) if image.data else 0)
        return size >= 500_000

    def preprocessing_metadata(self, image: ImageInput) -> dict:
        """Metadata/policy agar provider adapter dapat menentukan mode/detail.

        Provider-agnostic: TIDAK meng-hardcode perilaku provider tertentu.
        """
        return {
            "readability": self.is_readability(image),
            "max_dimension": (
                self.config.readability_max_dimension
                if self.is_readability(image)
                else self.config.max_dimension
            ),
            "jpeg_quality": self.config.jpeg_quality,
            "max_bytes": self.config.max_bytes,
            "preserve_alpha": self.config.preserve_alpha,
        }

    # ------------------------------------------------------------------ #
    # Routing requirement
    # ------------------------------------------------------------------ #
    def required_capabilities(self) -> FrozenSet[ModelCapability]:
        """Capability WAJIB untuk vision request (VISION + MULTIMODAL)."""
        return VISION_REQUIRED_CAPABILITIES

    def routing_requirements(self, request: VisionRequest) -> dict:
        """Requirement untuk Model Routing (tanpa membuat routing engine kedua).

        Returns:
            dict berisi required_capabilities + metadata (untuk diteruskan ke
            RoutingRequest yang sudah ada).
        """
        required = request.required_capabilities or VISION_REQUIRED_CAPABILITIES
        return {
            "required_capabilities": required,
            "metadata": {
                "vision": True,
                "image_count": len(request.images),
            },
        }

    def validate_request(self, request: VisionRequest) -> List[str]:
        """Validasi vision request (deterministik).

        Returns:
            Daftar masalah (kosong bila valid).
        """
        problems: List[str] = []
        if not request.prompt or not request.prompt.strip():
            problems.append("prompt kosong")
        if not request.images:
            problems.append("tidak ada image input")
        for idx, img in enumerate(request.images):
            if img.data is None and not img.path:
                problems.append(f"image[{idx}] tidak punya data/path")
        return problems
