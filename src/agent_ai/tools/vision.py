"""Tool Vision Agent: baca image lokal workspace sebagai input multimodal.

Menambahkan SATU tool generik untuk Agent, ``view_image(path)``, yang membuat
Agent dapat MENGIRIM gambar lokal ke model sebagai input multimodal TANPA
workaround (OCR / ASCII-art / image statistics / script PIL / Playwright).

Rantai yang dipakai (SEMUA mekanisme yang sudah ada, tanpa subsystem baru):
    view_image(path)
      -> ImageInputLoader      (baca bytes + deteksi MIME + workspace boundary)
      -> ImagePreprocessor     (resize/kompresi bounded, format JPEG/PNG/WebP)
      -> payload provider-agnostic {"type": "image", ...}
      -> content part image    (diteruskan provider adapter -> image_url/Ollama
                                images) oleh orchestrator lewat pesan multimodal.

Prinsip:
    - Membaca bytes image LANGSUNG di Python (bukan OCR/ASCII/statistik).
    - Path dibatasi ke workspace root memakai helper boundary existing
      (``_resolve_within_root`` via ``ImageInputLoader``); path traversal /
      symlink keluar workspace DITOLAK.
    - Format & batas ukuran mengikuti sistem Vision existing (JPEG/PNG/WebP).
    - READ-ONLY: hanya membaca, tidak menulis/menghapus.

Tool ini TIDAK menyentuh jalur Vision Consultant yang sudah bekerja.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from agent_ai.tools.base import BaseTool, ToolExecutionError, ToolValidationError
from agent_ai.tools.filesystem import _DEFAULT_ROOT


class ViewImageTool(BaseTool):
    """Membaca file image lokal di workspace dan menyiapkannya sebagai input multimodal."""

    name = "view_image"
    description = (
        "Membaca file image lokal di workspace (JPEG/PNG/WebP) dan "
        "mengirimkannya ke model sebagai input gambar (multimodal). Gunakan "
        "saat task meminta mendeskripsikan/menganalisis ISI sebuah file "
        "gambar (mis. './xxx.png'). Path harus berada di dalam workspace."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path file image relatif terhadap project root "
                    "(mis. './xxx.png') atau absolut di dalam workspace."
                ),
            },
        },
        "required": ["path"],
    }

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root).resolve() if root is not None else _DEFAULT_ROOT

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _to_rel(self, path: Any) -> str:
        """Normalisasi path absolut di dalam workspace -> relatif posix."""
        text = str(path or "")
        try:
            candidate = Path(text)
            root_resolved = self.root.resolve()
            if candidate.is_absolute():
                resolved = candidate.resolve()
                if resolved == root_resolved or root_resolved in resolved.parents:
                    text = str(resolved.relative_to(root_resolved))
            else:
                while text.startswith("./") or text.startswith(".\\"):
                    text = text[2:]
        except Exception:  # noqa: BLE001 - normalisasi best-effort
            pass
        return text.replace("\\", "/").strip()

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def execute(self, **arguments: Any) -> Dict[str, Any]:
        """Baca image -> content part multimodal (provider-agnostic).

        Args:
            path: path file image di dalam workspace.

        Returns:
            dict metadata + kunci ``MULTIMODAL_PARTS_KEY`` berisi content part
            image (format internal AETHER). Orchestrator memisahkan part ini
            menjadi input multimodal, sedangkan sisanya menjadi teks hasil tool.

        Raises:
            ToolValidationError: path kosong / di luar workspace.
            ToolExecutionError: file tidak ada / bukan gambar / format tak didukung.
        """
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ToolValidationError(
                "Argumen 'path' wajib diisi dan berupa string path image."
            )
        path = path.strip()

        # Import lazy: modul vision hanya dimuat saat tool dipakai (Pillow).
        from agent_ai.tools.base import MULTIMODAL_PARTS_KEY
        from agent_ai.vision.input import ImageInputLoader
        from agent_ai.vision.models import VisionError
        from agent_ai.vision.preprocessing import ImagePreprocessor

        loader = ImageInputLoader(root=self.root)
        try:
            image_input = loader.load(path)
        except ToolValidationError:
            # Path di luar workspace: teruskan sebagai error validasi boundary.
            raise
        except VisionError as exc:
            raise ToolExecutionError(str(exc)) from exc

        preprocessor = ImagePreprocessor()
        try:
            processed = preprocessor.process(
                image_input.data,
                mime_type=image_input.mime_type,
                readability=True,
            )
        except VisionError as exc:
            raise ToolExecutionError(str(exc)) from exc

        metadata = processed.metadata.to_dict() if processed.metadata else {}
        return {
            "ok": True,
            "path": self._to_rel(image_input.path),
            "mime_type": processed.mime_type,
            "size_bytes": processed.size_bytes,
            "width": metadata.get("width"),
            "height": metadata.get("height"),
            "resized": processed.resized,
            "compressed": processed.compressed,
            "note": (
                "Gambar dibaca dari workspace dan dilampirkan sebagai input "
                "multimodal untuk model."
            ),
            MULTIMODAL_PARTS_KEY: [dict(processed.payload)],
        }