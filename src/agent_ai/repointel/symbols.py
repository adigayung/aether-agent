"""Symbol Relationship Intelligence.

Menghubungkan symbol dengan file, import, reference, dan dependency.
Membedakan minimal: definition, reference, import, dependency.

Memakai Code Index + RepositoryIntelligence yang sudah ada. Deterministik,
read-only, tanpa LLM/embeddings.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set

from agent_ai.codeindex.index import CodeIndex
from agent_ai.codeindex.relations import RepositoryIntelligence
from agent_ai.repointel.models import RelationKind, SymbolRelation


class SymbolRelationshipIndex:
    """Indeks relasi symbol (definition/reference/import/dependency).

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

    def relations(self, symbol: str) -> List[SymbolRelation]:
        """Semua relasi untuk sebuah symbol (deterministik).

        Menggabungkan:
            - definition: lokasi symbol didefinisikan.
            - reference: penggunaan symbol di file lain.
            - import: import yang menyebut symbol (bila nama cocok).
            - dependency: file yang mengimpor file definisi symbol.
        """
        if not symbol:
            return []
        relations: List[SymbolRelation] = []

        definitions = [s for s in self.index.find_symbols(name=symbol) if s.name == symbol]
        definition_files: Set[str] = set()
        for sym in definitions:
            definition_files.add(sym.file)
            relations.append(
                SymbolRelation(
                    symbol=symbol,
                    kind=RelationKind.DEFINITION,
                    file=sym.file,
                    line=sym.line,
                    detail={"kind": sym.kind.value, "parent": sym.parent},
                )
            )

        # reference (penggunaan di file lain).
        for ref in self.intelligence.references.find(symbol):
            relations.append(
                SymbolRelation(
                    symbol=symbol,
                    kind=RelationKind.REFERENCE,
                    file=ref.file,
                    line=ref.line,
                    detail={"text": ref.text},
                )
            )

        # import yang menyebut symbol (nama cocok di daftar names).
        for entry in self.index.files:
            for imp in entry.imports:
                if symbol in (imp.names or []):
                    relations.append(
                        SymbolRelation(
                            symbol=symbol,
                            kind=RelationKind.IMPORT,
                            file=entry.path,
                            line=imp.line,
                            target=imp.module,
                            detail={"module": imp.module},
                        )
                    )

        # dependency: file yang mengimpor file definisi symbol.
        for def_file in sorted(definition_files):
            for dependent in self.intelligence.dependents(def_file):
                relations.append(
                    SymbolRelation(
                        symbol=symbol,
                        kind=RelationKind.DEPENDENCY,
                        file=dependent,
                        target=def_file,
                        detail={"imports": def_file},
                    )
                )

        # Urut deterministik: kind, file, line.
        relations.sort(key=lambda r: (r.kind.value, r.file, r.line))
        return relations

    def by_kind(self, symbol: str, kind: RelationKind) -> List[SymbolRelation]:
        """Relasi symbol difilter berdasarkan jenis."""
        return [r for r in self.relations(symbol) if r.kind == kind]

    def summary(self, symbol: str) -> Dict[str, object]:
        """Ringkasan jumlah relasi per jenis (deterministik)."""
        rels = self.relations(symbol)
        counts: Dict[str, int] = {k.value: 0 for k in RelationKind}
        for r in rels:
            counts[r.kind.value] += 1
        return {"symbol": symbol, "counts": counts, "total": len(rels)}
