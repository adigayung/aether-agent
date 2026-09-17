"""Interface parser bahasa (adapter).

Setiap parser menerima isi file (teks) dan mengembalikan symbol + import
secara deterministik. Parser TIDAK menjalankan command project dan TIDAK
memakai LLM/embeddings.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Tuple

from agent_ai.codeindex.models import Import, Symbol


class LanguageParser(ABC):
    """Abstract base untuk parser bahasa."""

    #: Nama bahasa yang ditangani parser ini.
    language: str = "base"

    @abstractmethod
    def parse(self, path: str, source: str) -> Tuple[List[Symbol], List[Import]]:
        """Parse isi file menjadi symbol + import.

        Args:
            path: path file relatif terhadap project root (untuk lokasi symbol).
            source: isi file (teks).

        Returns:
            Tuple (symbols, imports).
        """
        raise NotImplementedError
