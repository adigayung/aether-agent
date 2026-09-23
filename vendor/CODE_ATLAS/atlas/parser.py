"""Deterministic Python AST structure parser for CODE ATLAS.

Task 2 scope: parse .py files, extract module names, symbols
(classes, functions, methods) with source locations.
No relationship resolution, no call graphs, no inheritance trees.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class ParseDiagnostic:
    """Record of a file that could not be parsed."""

    file: str
    error: str


def _resolve_module_name(file_path: str, project_name: str) -> str:
    """Build a deterministic Python module name from a relative file path.

    Rules:
      - Remove the ``.py`` or ``.pyi`` extension.
      - Strip a trailing ``/__init__`` to reflect the package name.
      - A bare root-level ``__init__`` uses the project directory name.
      - Replace ``/`` with ``.``.
    """
    stem = file_path

    if stem.endswith(".py"):
        stem = stem[:-3]
    elif stem.endswith(".pyi"):
        stem = stem[:-4]
    else:
        # Not a recognised Python extension; return as-is.
        return stem.replace("/", ".")

    # Package indicator
    if stem.endswith("/__init__"):
        stem = stem[:-9]  # strip the "/__init__" segment

    # Root-level __init__ (stem is now empty)
    if not stem:
        return project_name

    return stem.replace("/", ".")


def _extract_symbols(
    file_path: str,
    tree: ast.AST,
) -> dict[str, dict]:
    """Walk an AST and extract every class, function, and method symbol.

    Returns a dict keyed by fully-qualified name (e.g. ``Foo.bar``)
    with basic metadata (kind, file, start_line, end_line, methods).
    """
    symbols: dict[str, dict] = {}

    def _visit(
        body: list[ast.stmt],
        qual_prefix: str = "",
        inside_class: bool = False,
    ) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                qual = (
                    f"{qual_prefix}.{node.name}" if qual_prefix else node.name
                )
                methods: list[str] = []
                for child in node.body:
                    if isinstance(child, ast.FunctionDef):
                        methods.append(child.name)

                symbols[qual] = {
                    "kind": "class",
                    "file": file_path,
                    "start_line": node.lineno,
                    "end_line": node.end_lineno,
                    "methods": methods,
                }
                _visit(node.body, qual, inside_class=True)

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = (
                    f"{qual_prefix}.{node.name}" if qual_prefix else node.name
                )
                kind = "method" if inside_class else "function"
                symbols[qual] = {
                    "kind": kind,
                    "file": file_path,
                    "start_line": node.lineno,
                    "end_line": node.end_lineno,
                }
                # Recurse for nested functions / classes inside functions.
                _visit(node.body, qual, inside_class=False)

    _visit(tree.body)
    return symbols


def parse_project(
    project_root: str,
    py_files: Iterable[str],
    project_name: str,
) -> tuple[dict[str, dict], dict[str, dict], list[ParseDiagnostic]]:
    """Parse every ``.py`` file and return module map, symbol map, and errors.

    Args:
        project_root: Absolute, normalized root path.
        py_files: Relative (``/``-separated) paths of ``.py`` files.
        project_name: Display name of the project (used for root-level ``__init__``).

    Returns:
        A tuple ``(modules, symbols, diagnostics)``:
        - ``modules``: ``{module_name: {"file": path}}``
        - ``symbols``: ``{qualified_name: {kind, file, start_line, end_line, ...}}``
        - ``diagnostics``: list of ``ParseDiagnostic`` for files that failed.
    """
    modules: dict[str, dict] = {}
    symbols: dict[str, dict] = {}
    diagnostics: list[ParseDiagnostic] = []

    py_files_sorted = sorted(py_files)

    for rel_path in py_files_sorted:
        mod_name = _resolve_module_name(rel_path, project_name)
        modules[mod_name] = {"file": rel_path}

        abs_path = os.path.join(project_root, rel_path)

        try:
            with open(abs_path, "r", encoding="utf-8") as fh:
                source = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            diagnostics.append(
                ParseDiagnostic(file=rel_path, error=str(type(exc).__name__))
            )
            continue

        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            diagnostics.append(
                ParseDiagnostic(file=rel_path, error="syntax_error")
            )
            continue

        file_symbols = _extract_symbols(rel_path, tree)
        symbols.update(file_symbols)

    return modules, symbols, diagnostics