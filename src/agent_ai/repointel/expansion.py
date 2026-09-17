"""Dependency Expansion: perluasan dependency terbatas (bounded).

Dari file/symbol relevan, temukan dependency/import terkait tanpa menyebar ke
seluruh repository. Memakai `RepositoryIntelligence` (Code Index) yang sudah
ada — TIDAK membuat indexer kedua.

Deterministik: hasil diurutkan berdasarkan (depth, path).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set

from agent_ai.codeindex.index import CodeIndex
from agent_ai.codeindex.relations import RepositoryIntelligence
from agent_ai.repointel.models import DependencyNode


class DependencyExpander:
    """Perluasan dependency bounded di atas import graph Code Index.

    Args:
        index: CodeIndex yang sudah dibangun.
        intelligence: RepositoryIntelligence opsional (dibuat bila None).
    """

    def __init__(
        self,
        index: CodeIndex,
        intelligence: Optional[RepositoryIntelligence] = None,
    ) -> None:
        self.index = index
        self.intelligence = intelligence or RepositoryIntelligence(index)

    def expand(
        self,
        seeds: List[str],
        max_depth: int = 1,
        max_nodes: int = 50,
        direction: str = "forward",
    ) -> List[DependencyNode]:
        """Perluas dependency dari `seeds` secara bounded (BFS).

        Args:
            seeds: daftar file awal (path relatif).
            max_depth: kedalaman maksimum (0 = hanya seed).
            max_nodes: batas jumlah node hasil (selain seed).
            direction: "forward" (dependency), "backward" (dependent),
                atau "both".

        Returns:
            Daftar DependencyNode (urut depth lalu path), deterministik.
        """
        if max_depth < 0:
            max_depth = 0
        if max_nodes < 0:
            max_nodes = 0

        valid_seeds = [s for s in seeds if self.index.file(s) is not None]
        visited: Set[str] = set(valid_seeds)
        nodes: List[DependencyNode] = [
            DependencyNode(path=s, depth=0, direction="seed") for s in sorted(valid_seeds)
        ]

        frontier: List[str] = sorted(valid_seeds)
        depth = 0
        while frontier and depth < max_depth and len(nodes) - len(valid_seeds) < max_nodes:
            depth += 1
            next_frontier: List[str] = []
            for current in frontier:
                for neighbor in self._neighbors(current, direction):
                    if neighbor in visited:
                        continue
                    if len(nodes) - len(valid_seeds) >= max_nodes:
                        break
                    visited.add(neighbor)
                    nodes.append(
                        DependencyNode(
                            path=neighbor, depth=depth, direction=direction, via=current
                        )
                    )
                    next_frontier.append(neighbor)
            frontier = sorted(next_frontier)

        # Urut deterministik: depth, lalu path.
        nodes.sort(key=lambda n: (n.depth, n.path))
        return nodes

    def _neighbors(self, path: str, direction: str) -> List[str]:
        """Tetangga import graph untuk sebuah file (deterministik)."""
        result: List[str] = []
        if direction in ("forward", "both"):
            result.extend(self.intelligence.dependencies(path))
        if direction in ("backward", "both"):
            result.extend(self.intelligence.dependents(path))
        # Dedup + urut.
        return sorted(set(result))
