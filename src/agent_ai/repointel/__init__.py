"""Repository Intelligence v2 AETHER.

Meningkatkan pemahaman hubungan antar bagian project SEBELUM membaca source
code. Reuse Code Index (Python AST + generic parser) — TIDAK membuat indexer
kedua. Deterministik, read-only, rebuildable. Tanpa LLM/embeddings/vector
DB/SQLite.

    from agent_ai.repointel import RepositoryIntelligenceV2

    ri = RepositoryIntelligenceV2(index)
    ri.map()                              # RepositoryMap ringkas
    ri.query.for_file("app/main.py")      # dependency relevan (bounded)
    ri.query.for_symbol("helper")         # relasi symbol
    ri.expand(["app/main.py"], max_depth=2)

Komponen:
    - DependencyExpander        : perluasan dependency bounded
    - SymbolRelationshipIndex   : definition/reference/import/dependency
    - RepositoryMapBuilder      : repository map ringkas + entry points
    - RelevantDependencyQuery   : facade query dependency relevan
"""

from agent_ai.codeindex.index import CodeIndex
from agent_ai.codeindex.relations import RepositoryIntelligence
from agent_ai.repointel.expansion import DependencyExpander
from agent_ai.repointel.map import RepositoryMapBuilder
from agent_ai.repointel.models import (
    DependencyNode,
    DependencyQueryResult,
    EntryPoint,
    RelationKind,
    RepositoryMap,
    SymbolRelation,
)
from agent_ai.repointel.query import RelevantDependencyQuery
from agent_ai.repointel.symbols import SymbolRelationshipIndex


class RepositoryIntelligenceV2:
    """Facade utama Repository Intelligence v2.

    Args:
        index: CodeIndex yang sudah dibangun.
        intelligence: RepositoryIntelligence opsional (dibuat bila None).
    """

    def __init__(
        self,
        index: CodeIndex,
        intelligence: RepositoryIntelligence | None = None,
    ) -> None:
        self.index = index
        self.intelligence = intelligence or RepositoryIntelligence(index)
        self.expander = DependencyExpander(index, self.intelligence)
        self.symbols = SymbolRelationshipIndex(index, self.intelligence)
        self.map_builder = RepositoryMapBuilder(index, self.intelligence)
        self.query = RelevantDependencyQuery(index, self.intelligence)

    def map(self, include_relationships: bool = True) -> RepositoryMap:
        """Bangun RepositoryMap ringkas."""
        return self.map_builder.build(include_relationships=include_relationships)

    def expand(
        self,
        seeds: list,
        max_depth: int = 1,
        max_nodes: int = 50,
        direction: str = "forward",
    ) -> list:
        """Perluas dependency dari seeds (bounded)."""
        return self.expander.expand(
            seeds, max_depth=max_depth, max_nodes=max_nodes, direction=direction
        )

    def relations(self, symbol: str) -> list:
        """Relasi symbol (definition/reference/import/dependency)."""
        return self.symbols.relations(symbol)


__all__ = [
    "RepositoryIntelligenceV2",
    "DependencyExpander",
    "SymbolRelationshipIndex",
    "RepositoryMapBuilder",
    "RelevantDependencyQuery",
    "RepositoryMap",
    "DependencyNode",
    "DependencyQueryResult",
    "SymbolRelation",
    "RelationKind",
    "EntryPoint",
]
