"""Normalized LLM Action/Response protocol.

Memisahkan format response provider (Ollama/OpenAI/DeepSeek/...) dari format
yang dipakai Agent Loop. Protocol ini provider-agnostic:

    LLMResponse
    ├── text
    ├── actions[]        (tool call / action)
    │   ├── type
    │   ├── name
    │   └── arguments    (terstruktur, bukan string)
    ├── finish_reason
    └── raw              (response mentah untuk audit; secret tidak dicetak)

Belum ada autonomous tool execution. Protocol ini hanya representasi data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ActionType(str, Enum):
    """Jenis action dalam sebuah LLMResponse."""

    TOOL_CALL = "tool_call"
    FINAL = "final"


class FinishReason(str, Enum):
    """Alasan berhentinya generation."""

    STOP = "stop"            # selesai normal (mungkin ada teks final)
    TOOL_CALLS = "tool_calls"  # model meminta tool call
    LENGTH = "length"        # terpotong karena batas token
    ERROR = "error"          # terjadi error
    UNKNOWN = "unknown"


@dataclass
class LLMAction:
    """Satu action/tool call dari model.

    Attributes:
        name: nama tool/action (mis. "read_file", "final_answer").
        arguments: argumen terstruktur (dict) — BUKAN string yang perlu di-regex.
        type: jenis action (tool_call/final).
        id: identifier unik action (opsional, dari provider bila ada).
    """

    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    type: ActionType = ActionType.TOOL_CALL
    id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type.value,
            "name": self.name,
            "arguments": self.arguments,
            "id": self.id,
        }


@dataclass
class LLMResponse:
    """Response model yang sudah dinormalisasi (provider-agnostic).

    Attributes:
        text: teks jawaban (bisa kosong bila hanya tool call).
        actions: daftar action/tool call (bisa lebih dari satu).
        finish_reason: alasan berhenti.
        raw: response mentah provider untuk debugging/audit.
        provider: nama provider asal (opsional).
        model: nama model (opsional).
    """

    text: str = ""
    actions: List[LLMAction] = field(default_factory=list)
    finish_reason: FinishReason = FinishReason.STOP
    raw: Any = None
    provider: str = ""
    model: str = ""

    # ------------------------------------------------------------------ #
    # Convenience
    # ------------------------------------------------------------------ #
    @property
    def has_tool_calls(self) -> bool:
        """True bila ada minimal satu action bertipe tool_call."""
        return any(a.type == ActionType.TOOL_CALL for a in self.actions)

    @property
    def is_final(self) -> bool:
        """True bila response merepresentasikan state final (tanpa tool call)."""
        return not self.has_tool_calls

    def tool_calls(self) -> List[LLMAction]:
        """Daftar action bertipe tool_call."""
        return [a for a in self.actions if a.type == ActionType.TOOL_CALL]

    # ------------------------------------------------------------------ #
    # Serialization
    # ------------------------------------------------------------------ #
    def to_dict(self, include_raw: bool = False) -> Dict[str, Any]:
        """Representasi dict.

        Args:
            include_raw: sertakan `raw` (default False agar aman untuk log).
        """
        data: Dict[str, Any] = {
            "text": self.text,
            "actions": [a.to_dict() for a in self.actions],
            "finish_reason": self.finish_reason.value,
            "provider": self.provider,
            "model": self.model,
        }
        if include_raw:
            data["raw"] = self.raw
        return data

    def __repr__(self) -> str:  # pragma: no cover - bantuan debug
        return (
            f"<LLMResponse finish={self.finish_reason.value} "
            f"actions={len(self.actions)} text={self.text[:30]!r}>"
        )
