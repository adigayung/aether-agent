"""Parser Python (menggunakan modul `ast` standar, deterministik)."""

from __future__ import annotations

import ast
from typing import List, Tuple

from agent_ai.codeindex.models import Import, Symbol, SymbolKind
from agent_ai.codeindex.parsers.base import LanguageParser


class PythonParser(LanguageParser):
    """Parser Python berbasis AST."""

    language = "python"

    def parse(self, path: str, source: str) -> Tuple[List[Symbol], List[Import]]:
        symbols: List[Symbol] = []
        imports: List[Import] = []

        try:
            tree = ast.parse(source)
        except SyntaxError:
            # File tidak valid: kembalikan kosong (deterministik, tidak crash).
            return symbols, imports

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                symbols.append(
                    Symbol(
                        name=node.name,
                        kind=SymbolKind.CLASS,
                        file=path,
                        line=node.lineno,
                        signature=self._class_signature(node),
                    )
                )
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(
                            Symbol(
                                name=child.name,
                                kind=SymbolKind.METHOD,
                                file=path,
                                line=child.lineno,
                                parent=node.name,
                                signature=self._func_signature(child),
                            )
                        )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(
                    Symbol(
                        name=node.name,
                        kind=SymbolKind.FUNCTION,
                        file=path,
                        line=node.lineno,
                        signature=self._func_signature(node),
                    )
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(
                        Import(module=alias.name, file=path, line=node.lineno, names=[alias.name])
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [alias.name for alias in node.names]
                imports.append(
                    Import(
                        module=module,
                        file=path,
                        line=node.lineno,
                        names=names,
                        level=node.level or 0,
                    )
                )
        return symbols, imports

    @staticmethod
    def _func_signature(node) -> str:
        args = [a.arg for a in node.args.args]
        return f"{node.name}({', '.join(args)})"

    @staticmethod
    def _class_signature(node) -> str:
        bases = [ast.unparse(b) for b in node.bases] if node.bases else []
        return f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"

