"""TaskClassifier: klasifikasi kompleksitas task secara deterministik.

Provider-agnostic. TIDAK memakai LLM. Hanya berbasis sinyal teks sederhana
(panjang, kata kunci kompleksitas). Hasil: TaskComplexity.

Prinsip:
    - Deterministik (input sama -> output sama).
    - Tidak menyimpan chain-of-thought; hanya kategori + alasan singkat.
"""

from __future__ import annotations

from typing import List, Optional

from agent_ai.routing.models import RoutingConfig, TaskComplexity

# Kata kunci yang menandakan task kompleks (multi-langkah/arsitektural).
_COMPLEX_KEYWORDS = (
    "refactor",
    "arsitektur",
    "architecture",
    "migrasi",
    "migration",
    "integrasi",
    "integration",
    "optimasi",
    "optimize",
    "redesign",
    "multi",
    "end-to-end",
    "e2e",
    "distributed",
    "concurrency",
    "performance",
    "security",
    "audit",
)

# Kata kunci yang menandakan task sederhana.
_SIMPLE_KEYWORDS = (
    "rename",
    "typo",
    "ganti nama",
    "hapus",
    "delete",
    "tambah komentar",
    "comment",
    "print",
    "echo",
    "hello",
    "baca",
    "read",
    "list",
    "daftar",
)


class TaskClassifier:
    """Mengklasifikasi kompleksitas task (deterministik).

    Args:
        config: RoutingConfig (ambang panjang). Default: RoutingConfig().
    """

    def __init__(self, config: Optional[RoutingConfig] = None) -> None:
        self.config = config or RoutingConfig()

    def classify(self, task: str) -> TaskComplexity:
        """Klasifikasi kompleksitas task.

        Aturan (deterministik, urut):
            1. Ada kata kunci kompleks -> COMPLEX.
            2. Panjang task >= threshold_complex -> COMPLEX.
            3. Ada kata kunci sederhana DAN panjang < threshold_medium -> SIMPLE.
            4. Panjang task >= threshold_medium -> MEDIUM.
            5. Selain itu -> SIMPLE.
        """
        text = (task or "").strip().lower()
        length = len(text)

        has_complex = any(k in text for k in _COMPLEX_KEYWORDS)
        has_simple = any(k in text for k in _SIMPLE_KEYWORDS)

        if has_complex:
            return TaskComplexity.COMPLEX
        if length >= self.config.complexity_threshold_complex:
            return TaskComplexity.COMPLEX
        if has_simple and length < self.config.complexity_threshold_medium:
            return TaskComplexity.SIMPLE
        if length >= self.config.complexity_threshold_medium:
            return TaskComplexity.MEDIUM
        return TaskComplexity.SIMPLE

    def reasons(self, task: str) -> List[str]:
        """Alasan singkat klasifikasi (bukan chain-of-thought)."""
        text = (task or "").strip().lower()
        length = len(text)
        out: List[str] = [f"panjang_task={length}"]
        matched = [k for k in _COMPLEX_KEYWORDS if k in text]
        if matched:
            out.append(f"kata_kunci_kompleks={matched[:3]}")
        simple_matched = [k for k in _SIMPLE_KEYWORDS if k in text]
        if simple_matched:
            out.append(f"kata_kunci_sederhana={simple_matched[:3]}")
        return out
