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


def _usage_int(value: Any) -> Optional[int]:
    """Angka token valid (int >= 0) atau None (bukan angka/kosong/negatif)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer():
        result = int(value)
        return result if result >= 0 else None
    return None


def response_usage(raw: Any) -> Optional[Dict[str, int]]:
    """Token usage AKTUAL dari payload mentah provider (provider-agnostic).

    Membaca HANYA angka usage yang SUDAH dilaporkan provider/API — TIDAK ada
    estimasi tokenizer lokal dan TIDAK ada counter token baru. Mendukung bentuk
    yang lazim:

        - OpenAI-compatible : ``usage.prompt_tokens`` / ``completion_tokens`` /
          ``total_tokens``,
        - Ollama native     : ``prompt_eval_count`` / ``eval_count`` (top-level),
        - kunci ternormalisasi AETHER : ``prompt`` / ``completion`` / ``total``.

    Args:
        raw: payload mentah response provider (dict) atau apa pun.

    Returns:
        dict dengan subset kunci ``{"prompt", "completion", "total"}`` (int),
        atau ``None`` bila provider tidak melaporkan usage apa pun. Total
        dilaporkan apa adanya bila tersedia; bila hanya prompt+completion yang
        ada, ``total`` diisi hasil penjumlahannya (dari angka provider, bukan
        estimasi).
    """
    if not isinstance(raw, dict):
        return None

    nested = raw.get("usage")
    usage: Dict[str, int] = {}

    # 1) Bentuk OpenAI-compatible (usage.*_tokens) dan/atau kunci kanonik.
    if isinstance(nested, dict):
        prompt = _usage_int(nested.get("prompt_tokens"))
        if prompt is None:
            prompt = _usage_int(nested.get("prompt"))
        completion = _usage_int(nested.get("completion_tokens"))
        if completion is None:
            completion = _usage_int(nested.get("completion"))
        total = _usage_int(nested.get("total_tokens"))
        if total is None:
            total = _usage_int(nested.get("total"))
        if prompt is not None:
            usage["prompt"] = prompt
        if completion is not None:
            usage["completion"] = completion
        if total is not None:
            usage["total"] = total

    # 2) Bentuk Ollama native (top-level prompt_eval_count / eval_count).
    if "prompt" not in usage:
        prompt_eval = _usage_int(raw.get("prompt_eval_count"))
        if prompt_eval is not None:
            usage["prompt"] = prompt_eval
    if "completion" not in usage:
        eval_count = _usage_int(raw.get("eval_count"))
        if eval_count is not None:
            usage["completion"] = eval_count

    if not usage:
        return None

    # Total: pakai angka provider bila ada; selain itu jumlahkan prompt+completion
    # yang memang dilaporkan provider (bukan estimasi baru).
    if "total" not in usage and ("prompt" in usage or "completion" in usage):
        usage["total"] = usage.get("prompt", 0) + usage.get("completion", 0)

    return usage


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
        truncated: True bila response provider terpotong (finish_reason=length)
            sehingga ada tool-call yang argumennya tidak lengkap dan DIBUANG
            (bukan file parsial). Dipakai caller untuk memperlakukan kondisi ini
            sebagai recoverable, bukan sebagai jawaban final.
        incomplete_tool_calls: jumlah tool-call yang dibuang karena argumennya
            terpotong (JSON tidak lengkap). 0 bila tidak ada.
    """

    text: str = ""
    actions: List[LLMAction] = field(default_factory=list)
    finish_reason: FinishReason = FinishReason.STOP
    raw: Any = None
    provider: str = ""
    model: str = ""
    truncated: bool = False
    incomplete_tool_calls: int = 0

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
