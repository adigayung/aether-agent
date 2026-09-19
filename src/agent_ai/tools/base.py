"""Interface/abstract base untuk semua tool.

Tool adalah unit kemampuan yang bisa dipanggil Agent Core melalui interface
yang konsisten. Setiap tool mendefinisikan:
    - name: nama unik tool.
    - description: deskripsi singkat (untuk LLM/planner nantinya).
    - input_schema: definisi input (JSON-schema-like) untuk validasi/dokumentasi.
    - execute(**arguments): menjalankan tool dan mengembalikan hasil.

Tahap ini hanya fondasi: belum ada tool konkret, tool calling dari LLM,
planner, memory, Git, database, atau command execution.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


# ---------------------------------------------------------------------------
# Exception hierarchy untuk tool
# ---------------------------------------------------------------------------
class ToolError(Exception):
    """Base exception untuk semua error tool."""


class ToolNotFoundError(ToolError):
    """Tool tidak terdaftar di registry."""


class ToolValidationError(ToolError):
    """Argumen yang diberikan tidak sesuai input_schema tool."""


class ToolExecutionError(ToolError):
    """Tool gagal saat dieksekusi (error asli disimpan di __cause__)."""


class BaseTool(ABC):
    """Abstract base class untuk semua tool.

    Kontrak utama:
        - name: nama unik tool (wajib di-override).
        - description: deskripsi singkat.
        - input_schema: definisi input (JSON-schema-like).
        - execute(**arguments): logika tool.
    """

    #: Nama unik tool. Wajib di-override oleh subclass.
    name: str = "base"

    #: Deskripsi singkat tool.
    description: str = ""

    #: Definisi input (JSON-schema-like). Contoh:
    #: {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    input_schema: Dict[str, Any] = {"type": "object", "properties": {}}

    # ------------------------------------------------------------------ #
    # Windows tool-use contract (diterapkan oleh semua tool).
    # ------------------------------------------------------------------ #
    # Pada Windows:
    #   - Source code search → gunakan search_code, bukan find/grep.
    #   - File reading      → gunakan read_file, bukan cat/type.
    #   - Directory listing  → gunakan list_files, bukan ls/dir.
    #   - Terminal execution → gunakan run_command dengan command
    #     yang kompatibel dengan Windows (native executable atau
    #     CMD builtins). JANGAN gunakan command Unix/Linux.
    #   - Working directory  → gunakan parameter cwd pada run_command,
    #     bukan cd di dalam command string.

    @abstractmethod
    def execute(self, **arguments: Any) -> Any:
        """Jalankan tool dengan argumen yang diberikan.

        Args:
            **arguments: argumen sesuai `input_schema`.

        Returns:
            Hasil eksekusi tool (bebas, sesuai tool).

        Raises:
            ToolValidationError: bila argumen tidak valid.
            ToolExecutionError: bila eksekusi gagal.
        """
        raise NotImplementedError

    def validate(self, arguments: Dict[str, Any]) -> None:
        """Validasi argumen terhadap `input_schema` (validasi minimal).

        Hanya memeriksa field `required`. Override bila butuh validasi lebih ketat.

        Raises:
            ToolValidationError: bila field wajib tidak ada.
        """
        required = self.input_schema.get("required", []) if self.input_schema else []
        missing = [key for key in required if key not in arguments]
        if missing:
            raise ToolValidationError(
                f"Tool '{self.name}' kekurangan argumen wajib: {', '.join(missing)}"
            )

    def to_spec(self) -> Dict[str, Any]:
        """Representasi tool untuk dokumentasi/LLM (name, description, schema)."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def __repr__(self) -> str:  # pragma: no cover - bantuan debug
        return f"<{self.__class__.__name__} name={self.name!r}>"
