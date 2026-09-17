"""Intelligent Retrieval: pemilihan file relevan + dependency expansion bounded.

Memakai RepositoryIntelligenceV2 (repointel) untuk:
    - memilih file relevan (symbol + path match)
    - memperluas dependency secara bounded
    - menggunakan symbol relationship
    - menghindari full repository dump

TIDAK membuat retrieval engine kedua: hanya mengorkestrasi Code Index +
RepositoryIntelligenceV2. Deterministik.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from agent_ai.codeindex.index import CodeIndex
from agent_ai.contextbudget.profiles import Budget
from agent_ai.repointel import RepositoryIntelligenceV2

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

#: Stopwords minimal (filler murni) untuk ekstraksi keyword.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with",
    "please", "make", "perbaiki", "tambahkan", "buat",
    "dan", "yang", "di", "ke", "pada", "agar", "supaya", "pastikan", "berhasil",
    "jalankan", "coba", "tolong", "ini", "itu", "dengan", "untuk",
}


@dataclass
class RetrievedFile:
    """File hasil retrieval beserta skor + alasan.

    Attributes:
        path: path relatif.
        language: bahasa file.
        score: skor relevansi (deterministik).
        reason: alasan pemilihan.
        symbols: symbol relevan di file ini (name/line).
        depth: kedalaman dependency (0 = seed).
    """

    path: str
    language: str
    score: float
    reason: str
    symbols: List[Dict[str, Any]] = field(default_factory=list)
    depth: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "language": self.language,
            "score": self.score,
            "reason": self.reason,
            "symbols": self.symbols,
            "depth": self.depth,
        }


class IntelligentRetriever:
    """Retrieval relevan berbasis RepositoryIntelligenceV2 (bounded).

    Args:
        index: CodeIndex yang sudah dibangun.
        intelligence: RepositoryIntelligenceV2 opsional.
    """

    def __init__(
        self,
        index: CodeIndex,
        intelligence: Optional[RepositoryIntelligenceV2] = None,
    ) -> None:
        self.index = index
        self.intelligence = intelligence or RepositoryIntelligenceV2(index)

    # ------------------------------------------------------------------ #
    # Keyword extraction
    # ------------------------------------------------------------------ #
    @staticmethod
    def keywords(task: str) -> List[str]:
        """Ekstrak keyword dari task (deterministik, urut kemunculan)."""
        seen: Set[str] = set()
        result: List[str] = []
        for match in _WORD_RE.finditer(task or ""):
            word = match.group(0)
            lower = word.lower()
            if lower in _STOPWORDS or lower in seen:
                continue
            seen.add(lower)
            result.append(word)
        return result

    # ------------------------------------------------------------------ #
    # Retrieval
    # ------------------------------------------------------------------ #
    def retrieve(self, task: str, budget: Budget) -> List[RetrievedFile]:
        """Pilih file relevan + expand dependency (bounded, deterministik).

        Args:
            task: task/request user.
            budget: Budget dari retrieval profile.

        Returns:
            Daftar RetrievedFile (urut skor desc, path asc), dibatasi budget.
        """
        keywords = self.keywords(task)
        scores: Dict[str, float] = {}
        reasons: Dict[str, str] = {}
        symbols_by_file: Dict[str, List[Dict[str, Any]]] = {}

        # 1) Skor dari symbol yang cocok.
        for keyword in keywords:
            for symbol in self.index.find_symbols(name=keyword):
                scores[symbol.file] = scores.get(symbol.file, 0.0) + 3.0
                reasons.setdefault(symbol.file, f"symbol '{symbol.name}'")
                symbols_by_file.setdefault(symbol.file, []).append(
                    {"name": symbol.name, "kind": symbol.kind.value, "line": symbol.line}
                )

        # 2) Skor tambahan bila nama file mengandung keyword.
        for entry in self.index.files:
            name = entry.path.lower()
            for keyword in keywords:
                if keyword.lower() in name:
                    scores[entry.path] = scores.get(entry.path, 0.0) + 1.0
                    reasons.setdefault(entry.path, f"path cocok '{keyword}'")

        # 3) Filter relevance threshold.
        threshold = budget.relevance_threshold
        seeds = {p: s for p, s in scores.items() if s >= threshold}

        # 4) Dependency expansion bounded (forward + backward sesuai depth).
        expanded: Dict[str, int] = {p: 0 for p in seeds}
        if budget.max_depth > 0 and seeds and budget.max_nodes > 0:
            nodes = self.intelligence.expand(
                sorted(seeds),
                max_depth=budget.max_depth,
                max_nodes=budget.max_nodes,
                direction="both",
            )
            for node in nodes:
                if node.path in expanded:
                    continue
                expanded[node.path] = node.depth
                reasons.setdefault(node.path, f"dependency (depth {node.depth})")

        # 5) Susun hasil: seed dulu (skor), lalu dependency (skor menurun).
        results: List[RetrievedFile] = []
        for path, score in sorted(seeds.items(), key=lambda kv: (-kv[1], kv[0])):
            entry = self.index.file(path)
            results.append(
                RetrievedFile(
                    path=path,
                    language=entry.language if entry else "unknown",
                    score=score,
                    reason=reasons.get(path, "relevan"),
                    symbols=symbols_by_file.get(path, []),
                    depth=0,
                )
            )
        for path, depth in sorted(expanded.items(), key=lambda kv: (kv[1], kv[0])):
            if path in seeds:
                continue
            entry = self.index.file(path)
            results.append(
                RetrievedFile(
                    path=path,
                    language=entry.language if entry else "unknown",
                    score=max(1.0 - depth * 0.1, 0.0),
                    reason=reasons.get(path, "dependency"),
                    symbols=symbols_by_file.get(path, []),
                    depth=depth,
                )
            )

        # 6) Batasi jumlah file sesuai budget.
        return results[: budget.max_files]
