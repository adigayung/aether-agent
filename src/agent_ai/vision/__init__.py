"""Vision / Multimodal Input Subsystem AETHER (#46).

Menambahkan kemampuan AETHER menerima input gambar secara provider-agnostic,
dengan preprocessing gambar yang efisien sebelum dikirim ke vision-capable model.

    from agent_ai.vision import (
        ImageInputLoader,
        ImagePreprocessor,
        VisionPolicy,
        ImageInput,
        ProcessedImage,
        VisionRequest,
        VisionConfig,
    )

Vision memakai subsystem yang sudah ada:
    - capabilities/  -> ModelCapability.VISION / MULTIMODAL (source of truth)
    - routing/       -> RoutingRequest (requirement capability, bukan engine baru)
    - providers/     -> payload provider-agnostic (diterjemahkan provider adapter)
    - config/        -> VisionConfig

Vision TIDAK membuat provider Vision khusus, TIDAK melakukan OCR/image-to-text/
enhancement, TIDAK membuat routing engine kedua, dan TIDAK mengubah provider lama.
"""

from agent_ai.vision.input import ImageInputLoader
from agent_ai.vision.models import (
    FORMAT_TO_MIME,
    MIME_TO_FORMAT,
    ImageFormat,
    ImageInput,
    ImageMetadata,
    InvalidImageError,
    ProcessedImage,
    UnsupportedImageFormatError,
    VisionConfig,
    VisionError,
    VisionRequest,
)
from agent_ai.vision.policy import VISION_REQUIRED_CAPABILITIES, VisionPolicy
from agent_ai.vision.preprocessing import ImagePreprocessor

__all__ = [
    "ImageInputLoader",
    "ImagePreprocessor",
    "VisionPolicy",
    "ImageInput",
    "ImageMetadata",
    "ProcessedImage",
    "VisionRequest",
    "VisionConfig",
    "ImageFormat",
    "VisionError",
    "InvalidImageError",
    "UnsupportedImageFormatError",
    "MIME_TO_FORMAT",
    "FORMAT_TO_MIME",
    "VISION_REQUIRED_CAPABILITIES",
]
