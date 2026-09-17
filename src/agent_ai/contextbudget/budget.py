"""Context Budget: estimasi token + penegakan batas.

Estimasi token SEDERHANA (bukan tokenizer akurat): ~4 karakter per token.
Ini jelas sebagai ESTIMASI, bukan hitungan tokenizer model tertentu.
Tidak bergantung pada tokenizer library eksternal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent_ai.contextbudget.profiles import Budget

#: Rasio estimasi kasar: rata-rata ~4 karakter per token.
CHARS_PER_TOKEN = 4


class TokenEstimator:
    """Estimator token sederhana (deterministik, tanpa library eksternal).

    Catatan: ini ESTIMASI kasar (len(text)/4), bukan tokenizer akurat.
    """

    def __init__(self, chars_per_token: int = CHARS_PER_TOKEN) -> None:
        self.chars_per_token = max(int(chars_per_token), 1)

    def estimate(self, text: str) -> int:
        """Estimasi jumlah token untuk sebuah teks."""
        if not text:
            return 0
        return (len(text) + self.chars_per_token - 1) // self.chars_per_token

    def estimate_bytes(self, data: bytes) -> int:
        """Estimasi token dari bytes (decode aman)."""
        return self.estimate(data.decode("utf-8", errors="ignore"))


@dataclass
class BudgetUsage:
    """Pemakaian budget saat ini.

    Attributes:
        files: jumlah file yang disertakan.
        bytes_used: total bytes source.
        tokens_used: estimasi token.
        nodes_used: jumlah node dependency expansion.
    """

    files: int = 0
    bytes_used: int = 0
    tokens_used: int = 0
    nodes_used: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "files": self.files,
            "bytes_used": self.bytes_used,
            "tokens_used": self.tokens_used,
            "nodes_used": self.nodes_used,
        }


class ContextBudget:
    """Menegakkan batas context berdasarkan Budget.

    Args:
        budget: Budget (dari retrieval profile).
        estimator: TokenEstimator opsional.
    """

    def __init__(self, budget: Budget, estimator: Optional[TokenEstimator] = None) -> None:
        self.budget = budget
        self.estimator = estimator or TokenEstimator()
        self.usage = BudgetUsage()

    # ------------------------------------------------------------------ #
    # Checks
    # ------------------------------------------------------------------ #
    def can_add_file(self) -> bool:
        return self.usage.files < self.budget.max_files

    def can_add_bytes(self, nbytes: int) -> bool:
        return self.usage.bytes_used + nbytes <= self.budget.max_bytes

    def can_add_tokens(self, ntokens: int) -> bool:
        return self.usage.tokens_used + ntokens <= self.budget.max_tokens

    def remaining_bytes(self) -> int:
        return max(self.budget.max_bytes - self.usage.bytes_used, 0)

    def remaining_tokens(self) -> int:
        return max(self.budget.max_tokens - self.usage.tokens_used, 0)

    def remaining_files(self) -> int:
        return max(self.budget.max_files - self.usage.files, 0)

    def remaining_nodes(self) -> int:
        return max(self.budget.max_nodes - self.usage.nodes_used, 0)

    def is_exhausted(self) -> bool:
        """True bila budget file/bytes/token sudah habis."""
        return (
            self.usage.files >= self.budget.max_files
            or self.usage.bytes_used >= self.budget.max_bytes
            or self.usage.tokens_used >= self.budget.max_tokens
        )

    def near_limit(self, ratio: float = 0.9) -> bool:
        """True bila pemakaian mendekati batas (untuk pemicu compaction)."""
        if self.budget.max_tokens <= 0:
            return True
        return self.usage.tokens_used >= self.budget.max_tokens * ratio

    # ------------------------------------------------------------------ #
    # Accounting
    # ------------------------------------------------------------------ #
    def account(self, text: str, nbytes: Optional[int] = None) -> None:
        """Catat pemakaian untuk sebuah teks yang disertakan."""
        data_len = nbytes if nbytes is not None else len(text.encode("utf-8"))
        self.usage.files += 1
        self.usage.bytes_used += data_len
        self.usage.tokens_used += self.estimator.estimate(text)

    def account_nodes(self, count: int) -> None:
        self.usage.nodes_used += max(int(count), 0)

    def reset(self) -> None:
        self.usage = BudgetUsage()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "budget": self.budget.to_dict(),
            "usage": self.usage.to_dict(),
            "remaining": {
                "files": self.remaining_files(),
                "bytes": self.remaining_bytes(),
                "tokens": self.remaining_tokens(),
                "nodes": self.remaining_nodes(),
            },
        }
