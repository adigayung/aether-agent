"""Relevant Dependency Query: facade query dependency yang bounded.

Menyediakan API untuk mendapatkan file/symbol yang berhubungan dengan target
tertentu. Hasil deterministik dan bounded (max_files/max_depth).

Memakai Code Index + RepositoryIntelligence + DependencyExpander +
SymbolRelationshipIndex. TIDAK membuat indexer kedua.
"""

from __future__ import annotations

from typing import List, Optional

from agent_ai.codeindex.index import CodeIndex
from agent_ai.codeindex.relations import RepositoryIntelligence
from agent_ai.repointel.expansion import DependencyExpander
from agent_ai.repointel.models import DependencyQueryResult
from agent_ai.repointel.symbols import SymbolRelationshipIndex


class RelevantDependencyQuery:
    """Facade query dependency relevan (deterministik, bounded).

    Args:
        index: CodeIndex yang sudah dibangun.
        intelligence: RepositoryIntelligence opsional.
    """

    def __init__(
        self,
        index: CodeIndex,
        intelligence: Optional[RepositoryIntelligence] = None,
    ) -> None:
        self.index = index
        self.intelligence = intelligence or RepositoryIntelligence(index)
        self.expander = DependencyExpander(index, self.intelligence)
        self.symbols = SymbolRelationshipIndex(index, self.intelligence)

    # ------------------------------------------------------------------ #
    # File query
    # ------------------------------------------------------------------ #
    def for_file(
        self,
        path: str,
        max_depth: int = 1,
        max_files: int = 30,
        direction: str = "both",
    ) -> DependencyQueryResult:
        """File/symbol terkait untuk sebuah file target.

        Args:
            path: file target (path relatif).
            max_depth: kedalaman expansion.
            max_files: batas jumlah file hasil.
            direction: "forward"/"backward"/"both".

        Returns:
            DependencyQueryResult (bounded, deterministik).
        """
        if self.index.file(path) is None:
            return DependencyQueryResult(
                target=path,
                target_type="file",
                metadata={"error": "file tidak ditemukan di index"},
            )

        nodes = self.expander.expand(
            [path], max_depth=max_depth, max_nodes=max_files, direction=direction
        )
        truncated = len(nodes) > max_files
        nodes = nodes[:max_files]

        files = [n.path for n in nodes]
        dependencies = self.intelligence.dependencies(path)
        dependents = self.intelligence.dependents(path)

        # Symbol yang didefinisikan di file target.
        entry = self.index.file(path)
        symbols = [
            {"name": s.name, "kind": s.kind.value, "line": s.line, "parent": s.parent}
            for s in (entry.symbols if entry else [])
        ]

        return DependencyQueryResult(
            target=path,
            target_type="file",
            files=files,
            symbols=symbols,
            dependencies=dependencies,
            dependents=dependents,
            truncated=truncated,
            metadata={
                "max_depth": max_depth,
                "max_files": max_files,
                "direction": direction,
                "node_count": len(nodes),
            },
        )

    # ------------------------------------------------------------------ #
    # Symbol query
    # ------------------------------------------------------------------ #
    def for_symbol(
        self,
        symbol: str,
        max_files: int = 30,
    ) -> DependencyQueryResult:
        """File/symbol terkait untuk sebuah symbol target.

        Menggabungkan relasi symbol (definition/reference/import/dependency)
        dan file terkait (bounded).
        """
        relations = self.symbols.relations(symbol)

        # Kumpulkan file terkait dari relasi (deterministik).
        related_files = sorted({r.file for r in relations})
        truncated = len(related_files) > max_files
        related_files = related_files[:max_files]

        definitions = [
            {"file": r.file, "line": r.line, "detail": r.detail}
            for r in relations
            if r.kind.value == "definition"
        ]

        return DependencyQueryResult(
            target=symbol,
            target_type="symbol",
            files=related_files,
            symbols=definitions,
            relations=relations,
            truncated=truncated,
            metadata={
                "max_files": max_files,
                "relation_count": len(relations),
                "summary": self.symbols.summary(symbol),
            },
        )
