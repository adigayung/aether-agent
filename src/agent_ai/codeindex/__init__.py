"""Code Index AETHER.

Membangun representasi terstruktur sebuah project (file, symbol, import)
secara deterministik tanpa LLM/embeddings/vector DB/SQLite.

Layer ini terpisah dari filesystem.py dan Project Discovery, dan tidak
mengubah Project Intelligence.

    from agent_ai.codeindex import CodeIndexer

    index = CodeIndexer(root=Path(".")).build()
    index.find_symbols(name="add")
"""

from agent_ai.codeindex.index import CodeIndex
from agent_ai.codeindex.indexer import CodeIndexer
from agent_ai.codeindex.languages import Language, LanguageRegistry
from agent_ai.codeindex.models import FileEntry, Import, Symbol, SymbolKind
from agent_ai.codeindex.parsers.base import LanguageParser
from agent_ai.codeindex.relations import (
    ImportResolution,
    ImportResolver,
    Reference,
    ReferenceFinder,
    RepositoryGraph,
    RepositoryIntelligence,
)

__all__ = [
    "CodeIndex",
    "CodeIndexer",
    "Language",
    "LanguageRegistry",
    "LanguageParser",
    "FileEntry",
    "Import",
    "Symbol",
    "SymbolKind",
    "RepositoryIntelligence",
    "RepositoryGraph",
    "ImportResolver",
    "ImportResolution",
    "ReferenceFinder",
    "Reference",
]

