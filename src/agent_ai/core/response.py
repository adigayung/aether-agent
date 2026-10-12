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

    # Total: pakai angka provider bila ada; BILA TIDAK ADA, hitung HANYA ketika
    # prompt DAN completion keduanya dilaporkan provider (angka aktual, bukan
    # estimasi tokenizer). Bila hanya salah satu yang tersedia, `total` TIDAK
    # diisi: komponen yang hilang TIDAK boleh disimpulkan sebagai 0.
    if "total" not in usage and "prompt" in usage and "completion" in usage:
        usage["total"] = usage["prompt"] + usage["completion"]

    return usage


#: Status ketersediaan usage (kontrak telemetry TASK 3).
USAGE_STATUS_ACTUAL = "actual"          # input + output tersedia (total andal)
USAGE_STATUS_PARTIAL = "partial"        # sebagian komponen tersedia
USAGE_STATUS_UNAVAILABLE = "unavailable"  # provider tidak melaporkan usage

#: Sumber angka usage yang EKSPLISIT (bukan asumsi dari nama provider).
USAGE_SOURCE_OPENAI = "openai_usage"     # usage.prompt_tokens/..._tokens
USAGE_SOURCE_CANONICAL = "aether_usage"  # usage.prompt/completion/total
USAGE_SOURCE_OLLAMA = "ollama_native"    # prompt_eval_count/eval_count
USAGE_SOURCE_UNAVAILABLE = "unavailable"


def _usage_value(raw: Any, *, nested: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Ekstrak komponen usage mentah + sumber eksplisit dari payload provider.

    Mengembalikan dict ``{"prompt", "completion", "total", "source"}`` dengan
    nilai ``Optional[int]`` (None = komponen TIDAK dilaporkan) dan ``source``
    string yang menandai format provider asal angka (bukan asumsi nama provider).
    """
    result: Dict[str, Any] = {
        "prompt": None,
        "completion": None,
        "total": None,
        "source": USAGE_SOURCE_UNAVAILABLE,
    }
    if nested is None:
        # Ollama native (top-level) — sumber angka adalah respons provider.
        prompt = _usage_int(raw.get("prompt_eval_count"))
        completion = _usage_int(raw.get("eval_count"))
        if prompt is not None or completion is not None:
            result["prompt"] = prompt
            result["completion"] = completion
            result["source"] = USAGE_SOURCE_OLLAMA
        return result

    prompt = _usage_int(nested.get("prompt_tokens"))
    completion = _usage_int(nested.get("completion_tokens"))
    total = _usage_int(nested.get("total_tokens"))
    openai_keys = any(
        nested.get(k) is not None
        for k in ("prompt_tokens", "completion_tokens", "total_tokens")
    )
    if prompt is None:
        prompt = _usage_int(nested.get("prompt"))
    if completion is None:
        completion = _usage_int(nested.get("completion"))
    if total is None:
        total = _usage_int(nested.get("total"))
    if prompt is None and completion is None and total is None:
        return result
    result["prompt"] = prompt
    result["completion"] = completion
    result["total"] = total
    result["source"] = USAGE_SOURCE_OPENAI if openai_keys else USAGE_SOURCE_CANONICAL
    return result


def usage_detail(raw: Any) -> Dict[str, Any]:
    """Metadata usage terperinci + status + sumber untuk KONTRAK telemetry.

    Berbeda dari :func:`response_usage` (yang mengembalikan subset
    ``{"prompt","completion","total"}`` untuk kompatibilitas pemanggil lama),
    fungsi ini mengembalikan SEMUA komponen termasuk yang TIDAK tersedia
    (``None``) supaya konsumen statistik dapat membedakan nilai valid, parsial,
    dan tidak tersedia — tanpa menyimpulkan komponen yang hilang sebagai 0.

    Returns:
        dict dengan kunci:
            prompt, completion, total : Optional[int]
                None = komponen TIDAK dilaporkan provider (BUKAN 0).
            status : str
                ``actual``      -> input + output tersedia (total andal),
                ``partial``     -> hanya sebagian komponen tersedia,
                ``unavailable`` -> provider tidak melaporkan usage apa pun.
            source : str
                sumber angka eksplisit (``openai_usage`` / ``aether_usage`` /
                ``ollama_native`` / ``unavailable``).
    """
    if not isinstance(raw, dict):
        return {
            "prompt": None,
            "completion": None,
            "total": None,
            "status": USAGE_STATUS_UNAVAILABLE,
            "source": USAGE_SOURCE_UNAVAILABLE,
        }

    nested = raw.get("usage")
    values = _usage_value(raw, nested=nested if isinstance(nested, dict) else None)
    prompt = values["prompt"]
    completion = values["completion"]
    total = values["total"]
    source = values["source"]

    if prompt is None and completion is None and total is None:
        status = USAGE_STATUS_UNAVAILABLE
    elif prompt is not None and completion is not None:
        status = USAGE_STATUS_ACTUAL
    else:
        status = USAGE_STATUS_PARTIAL

    # Total: gunakan angka provider; bila tidak ada tetapi input+output tersedia,
    # turunkan dari keduanya (angka aktual). Jangan mengisi bila salah satu hilang.
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion

    return {
        "prompt": prompt,
        "completion": completion,
        "total": total,
        "status": status,
        "source": source,
    }


def usage_status(usage: Optional[Dict[str, Any]]) -> str:
    """Klasifikasi status dari hasil :func:`response_usage` (kompatibilitas).

    Args:
        usage: hasil ``response_usage`` (subset ``{"prompt","completion","total"}``)
            atau ``None``.

    Returns:
        ``actual`` bila input+output tersedia, ``partial`` bila hanya sebagian,
        ``unavailable`` bila kosong.
    """
    if not usage:
        return USAGE_STATUS_UNAVAILABLE
    has_prompt = usage.get("prompt") is not None
    has_completion = usage.get("completion") is not None
    if has_prompt and has_completion:
        return USAGE_STATUS_ACTUAL
    return USAGE_STATUS_PARTIAL


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
        truncated: True bila response provider terpotong
            (finish_reason="length"): output model TIDAK lengkap, entah karena
            tool-call yang argumennya tidak lengkap DIBUANG (bukan file parsial)
            maupun karena hanya teks yang terpotong tanpa tool-call yang dibuang.
            Dipakai caller untuk memperlakukan kondisi ini sebagai recoverable
            (bounded), bukan sebagai jawaban final.
        incomplete_tool_calls: jumlah tool-call yang dibuang karena argumennya
            terpotong (JSON tidak lengkap). 0 bila tidak ada. Rincian
            observability — BUKAN penentu apakah `truncated` bernilai True.
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
