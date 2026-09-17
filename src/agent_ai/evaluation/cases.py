"""Registry evaluation case.

Mendaftarkan evaluation case berdasarkan id, mengambilnya kembali, dan
mendaftarkannya. TIDAK mengandung logic agent (hanya data case).

    from agent_ai.evaluation import EvaluationCaseRegistry, EvaluationCase

    registry = EvaluationCaseRegistry()
    registry.register(EvaluationCase(id="case_1", ...))
    cases = registry.list()
"""

from __future__ import annotations

from typing import Dict, List

from agent_ai.evaluation.models import EvaluationCase


class EvaluationCaseRegistry:
    """Kumpulan evaluation case yang terdaftar, diakses lewat id unik."""

    def __init__(self) -> None:
        self._cases: Dict[str, EvaluationCase] = {}

    def register(self, case: EvaluationCase) -> None:
        """Daftarkan sebuah case berdasarkan atribut `id`.

        Raises:
            ValueError: bila id case kosong.
        """
        case_id = getattr(case, "id", None)
        if not case_id:
            raise ValueError("EvaluationCase harus punya atribut 'id' yang unik.")
        self._cases[case_id] = case

    def get(self, case_id: str) -> EvaluationCase:
        """Ambil case berdasarkan id.

        Raises:
            KeyError: bila id case belum terdaftar.
        """
        if case_id not in self._cases:
            available = ", ".join(sorted(self._cases)) or "(kosong)"
            raise KeyError(f"Evaluation case '{case_id}' tidak terdaftar. Tersedia: {available}")
        return self._cases[case_id]

    def has(self, case_id: str) -> bool:
        """Cek apakah case terdaftar."""
        return case_id in self._cases

    def list(self) -> List[EvaluationCase]:
        """Daftar semua case (urut berdasarkan id)."""
        return [self._cases[key] for key in sorted(self._cases)]

    def ids(self) -> List[str]:
        """Daftar id case yang terdaftar."""
        return sorted(self._cases)

    def __len__(self) -> int:
        return len(self._cases)


# ---------------------------------------------------------------------------
# Registry global (opsional). Case didaftarkan oleh pemanggil.
# ---------------------------------------------------------------------------
registry = EvaluationCaseRegistry()
