"""Relationship resolution for CODE ATLAS.

Task 3 scope: detect imports, inheritance, function/method calls,
and module-qualified calls from Python AST. Distinguish local project
symbols from external/unresolved ones.
"""

from __future__ import annotations

import ast
import os
from typing import Iterable


# Python builtins that should never be treated as project symbols.
_BUILTINS: frozenset[str] = frozenset({
    "str", "int", "float", "bool", "list", "dict", "tuple", "set",
    "frozenset", "bytes", "bytearray", "object", "type", "Exception",
    "BaseException", "ValueError", "TypeError", "KeyError", "IndexError",
    "RuntimeError", "StopIteration", "OSError", "IOError", "FileNotFoundError",
    "NotADirectoryError", "PermissionError", "True", "False", "None",
    "property", "staticmethod", "classmethod", "super", "isinstance",
    "issubclass", "hasattr", "getattr", "setattr", "delattr",
    "len", "range", "map", "filter", "zip", "enumerate", "sorted",
    "reversed", "iter", "next", "open", "print", "input",
    "abs", "all", "any", "bin", "chr", "complex", "divmod",
    "eval", "exec", "format", "globals", "hash", "hex", "id",
    "locals", "max", "min", "oct", "ord", "pow", "repr",
    "round", "sum", "vars", "__import__",
    "callable", "classmethod", "staticmethod", "property",
    "__name__", "__file__", "__doc__",
})


def _resolve_relative(
    from_module: str,
    level: int,
    tail: str,
) -> str | None:
    """Resolve a relative import to an absolute module name.

    Args:
        from_module: Absolute module name doing the import.
        level: Number of dots (1 for ``.foo``, 2 for ``..foo``).
        tail: Dotted name after the dots, e.g. ``"foo.Bar"`` or ``""``.

    Returns:
        Absolute module name, or ``None`` if resolution is impossible.
    """
    parts = from_module.split(".")
    if level > len(parts):
        return None  # beyond package root

    base = ".".join(parts[:-level]) if level > 0 else ""

    if tail:
        return f"{base}.{tail}" if base else tail
    return base


def _name_from_ast(node: ast.AST) -> str | None:
    """Extract a dotted name from an AST node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        obj = _name_from_ast(node.value)
        if obj is not None:
            return f"{obj}.{node.attr}"
    if isinstance(node, ast.Subscript):
        return _name_from_ast(node.value)
    if isinstance(node, ast.Call):
        return _name_from_ast(node.func)
    return None


def _new_import(
    imports: list[list],
    from_mod: str,
    to_name: str,
    local: bool,
) -> None:
    entry = [from_mod, to_name, local]
    if entry not in imports:
        imports.append(entry)


def _new_inherit(
    inherits: list[list],
    child: str,
    parent: str,
    local: bool,
) -> None:
    entry = [child, parent, local]
    if entry not in inherits:
        inherits.append(entry)


def _new_call(
    calls: list[list],
    caller: str,
    target: str,
    local: bool,
) -> None:
    if not caller:
        return
    entry = [caller, target, local]
    if entry not in calls:
        calls.append(entry)


def _new_unresolved(
    unresolved: list[list],
    caller: str,
    name: str,
) -> None:
    if not caller:
        return
    entry = [caller, name]
    if entry not in unresolved:
        unresolved.append(entry)


def _dedup_and_sort(items: list[list]) -> list[list]:
    seen: set = set()
    result: list[list] = []
    for item in items:
        key = tuple(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    result.sort()
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_relationships(
    project_root: str,
    py_files: Iterable[str],
    modules: dict[str, dict],
    symbols: dict[str, dict],
    project_name: str,
    file_to_module: dict[str, str] | None = None,
) -> dict:
    """Build relationship maps from project Python files.

    Returns a dict with keys:
        ``imports``, ``inherits``, ``calls``, ``unresolved``
    each holding a sorted, deduplicated list of relationship entries.
    """
    imports: list[list] = []
    inherits: list[list] = []
    calls: list[list] = []
    unresolved: list[list] = []

    # ---- Indexes -----------------------------------------------------------
    if file_to_module is None:
        file_to_module = {}
        for mod_name, mod_info in modules.items():
            file_to_module[mod_info["file"]] = mod_name

    module_to_file: dict[str, str] = {}
    for mod_name, mod_info in modules.items():
        module_to_file[mod_name] = mod_info["file"]

    # Symbols per file (short names like "Runtime.run")
    syms_by_file: dict[str, list[str]] = {}
    for qual_name, sym_info in symbols.items():
        f = sym_info["file"]
        syms_by_file.setdefault(f, []).append(qual_name)

    # Build mapping: short symbol name -> fully qualified name with module prefix
    # e.g. "Runtime.run" -> "app.core.runtime.Runtime.run"
    short_to_full: dict[str, str] = {}
    for qual_name, sym_info in symbols.items():
        f = sym_info["file"]
        mod = file_to_module.get(f, "")
        if mod:
            full = f"{mod}.{qual_name}" if qual_name else mod
            short_to_full[qual_name] = full
            # Also map each component: "Runtime.run" -> full, "Runtime" -> full prefix
            # For just the class/function name
            first = qual_name.split(".")[0]
            if first not in short_to_full:
                short_to_full[first] = f"{mod}.{first}"

    all_modules: set[str] = set(modules.keys())
    all_symbols: set[str] = set(symbols.keys())
    # Fully-qualified names (with module prefix) for lookup
    all_full_names: set[str] = set(short_to_full.values())

    # Import aliases per file: file -> {alias_name: target_qualified}
    import_aliases: dict[str, dict[str, str]] = {}

    sorted_py = sorted(py_files)

    # ---- PASS 1: collect import aliases ------------------------------------
    for rel_path in sorted_py:
        abs_path = os.path.join(project_root, rel_path)
        cur_mod = file_to_module.get(rel_path, "")
        if not cur_mod:
            continue

        try:
            with open(abs_path, "r", encoding="utf-8") as fh:
                source = fh.read()
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue

        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            continue

        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = alias.name
                    name = alias.asname or target.split(".")[0]
                    aliases[name] = target
            elif isinstance(node, ast.ImportFrom):
                level = node.level
                module_base = node.module or ""
                if level > 0:
                    resolved = _resolve_relative(cur_mod, level, module_base)
                    if resolved is None:
                        continue
                    module_base = resolved
                for alias in node.names:
                    name = alias.asname or alias.name
                    target = f"{module_base}.{alias.name}"
                    aliases[name] = target
        if aliases:
            import_aliases[rel_path] = aliases

    # ---- PASS 2: extract imports -------------------------------------------
    for rel_path in sorted_py:
        abs_path = os.path.join(project_root, rel_path)
        cur_mod = file_to_module.get(rel_path, "")
        if not cur_mod:
            continue

        try:
            with open(abs_path, "r", encoding="utf-8") as fh:
                source = fh.read()
        except (OSError, UnicodeDecodeError):
            continue

        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target_name = alias.name
                    is_local = target_name in all_modules
                    _new_import(imports, cur_mod, target_name, is_local)

            elif isinstance(node, ast.ImportFrom):
                level = node.level
                module_base = node.module or ""

                if level > 0:
                    resolved = _resolve_relative(cur_mod, level, module_base)
                    if resolved is None:
                        for alias in node.names:
                            _new_unresolved(
                                unresolved, cur_mod,
                                f"(relative) {'.' * level}{module_base}.{alias.name}",
                            )
                        continue
                    module_base = resolved

                is_local_module = module_base in all_modules

                for alias in node.names:
                    qualified = f"{module_base}.{alias.name}"
                    is_local = (
                        is_local_module
                        or qualified in all_symbols
                        or qualified in all_modules
                    )
                    _new_import(imports, cur_mod, qualified, is_local)

    # ---- PASS 3: inheritance -----------------------------------------------
    for rel_path in sorted_py:
        abs_path = os.path.join(project_root, rel_path)
        cur_mod = file_to_module.get(rel_path, "")
        if not cur_mod:
            continue

        try:
            with open(abs_path, "r", encoding="utf-8") as fh:
                source = fh.read()
        except (OSError, UnicodeDecodeError):
            continue

        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            continue

        file_syms = syms_by_file.get(rel_path, [])
        file_aliases = import_aliases.get(rel_path, {})

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            child_short = _find_short_qual(node.name, file_syms)
            if child_short is None:
                continue

            # Fully qualify the child
            child_full = short_to_full.get(child_short, child_short)

            for base in node.bases:
                parent_name = _name_from_ast(base)
                if parent_name is None:
                    continue

                # Skip builtins
                if parent_name in _BUILTINS:
                    continue
                top = parent_name.split(".")[0]
                if top in _BUILTINS:
                    continue

                # Try direct match in all_symbols (short names)
                if parent_name in all_symbols:
                    parent_full = short_to_full.get(parent_name, parent_name)
                    _new_inherit(inherits, child_full, parent_full, True)
                    continue

                # Try alias expansion
                if parent_name in file_aliases:
                    expanded = file_aliases[parent_name]
                    if expanded in all_symbols:
                        parent_full = short_to_full.get(expanded, expanded)
                        _new_inherit(inherits, child_full, parent_full, True)
                        continue
                    _new_inherit(
                        inherits, child_full, expanded,
                        expanded in all_symbols or expanded.split(".")[0] in all_modules,
                    )
                    continue

                # Try matching the last component to a symbol in this file
                last = parent_name.split(".")[-1]
                resolved = False
                for sym in file_syms:
                    if sym.endswith("." + last) or sym == last:
                        parent_full = short_to_full.get(sym, sym)
                        _new_inherit(inherits, child_full, parent_full, True)
                        resolved = True
                        break
                if not resolved:
                    # Also try matching across all symbols
                    for sym in all_symbols:
                        if sym == last or sym.endswith("." + last):
                            parent_full = short_to_full.get(sym, sym)
                            _new_inherit(inherits, child_full, parent_full, True)
                            resolved = True
                            break
                if not resolved:
                    _new_inherit(inherits, child_full, parent_name, False)

    # ---- PASS 4: calls -----------------------------------------------------
    for rel_path in sorted_py:
        abs_path = os.path.join(project_root, rel_path)
        cur_mod = file_to_module.get(rel_path, "")
        if not cur_mod:
            continue

        try:
            with open(abs_path, "r", encoding="utf-8") as fh:
                source = fh.read()
        except (OSError, UnicodeDecodeError):
            continue

        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            continue

        file_syms = syms_by_file.get(rel_path, [])
        file_aliases = import_aliases.get(rel_path, {})

        # Walk all function/method bodies with module-prefixed qualifier
        def _walk_scope(
            body: list[ast.stmt],
            qual: str = "",  # fully qualified current scope (module-prefixed)
        ) -> None:
            for stmt in body:
                _process_stmt_calls(
                    stmt, qual, file_syms, file_aliases,
                    all_modules, all_symbols, all_full_names, short_to_full, calls, unresolved,
                )
                # Recurse into nested function/class scopes
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nested = f"{qual}.{stmt.name}" if qual else stmt.name
                    _walk_scope(stmt.body, nested)
                elif isinstance(stmt, ast.ClassDef):
                    for child in stmt.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            nested = f"{qual}.{stmt.name}.{child.name}" if qual else f"{stmt.name}.{child.name}"
                            _walk_scope(child.body, nested)

        # Start walk with module name as top-level qualifier
        _walk_scope(tree.body, qual=cur_mod)

    # ---- Finalise ----------------------------------------------------------
    imports = _dedup_and_sort(imports)
    inherits = _dedup_and_sort(inherits)
    calls = _dedup_and_sort(calls)
    unresolved = _dedup_and_sort(unresolved)

    return {
        "imports": imports,
        "inherits": inherits,
        "calls": calls,
        "unresolved": unresolved,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_short_qual(
    name: str,
    file_syms: list[str],
) -> str | None:
    """Find the short qualified name for a simple name in a file's symbols."""
    candidates = [s for s in file_syms if s.endswith("." + name) or s == name]
    if not candidates:
        return None
    candidates.sort(key=lambda s: len(s.split(".")))
    return candidates[0]


def _process_stmt_calls(
    stmt: ast.stmt,
    qual: str,  # fully qualified caller (module-prefixed)
    file_syms: list[str],
    file_aliases: dict[str, str],
    all_modules: set[str],
    all_symbols: set[str],
    all_full_names: set[str],
    short_to_full: dict[str, str],
    calls: list[list],
    unresolved: list[list],
) -> None:
    """Find call nodes inside *stmt* (not nested inside deeper functions/classes)."""
    for node in ast.walk(stmt):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        # ---- Simple name: foo() --------------------------------------------
        if isinstance(func, ast.Name):
            name = func.id
            if name in _BUILTINS or name.split(".")[0] in _BUILTINS:
                continue

            # Check alias expansion first
            if name in file_aliases:
                target = file_aliases[name]
                is_local = (
                    target in all_symbols
                    or target in all_modules
                    or target.split(".")[0] in all_modules
                )
                _new_call(calls, qual, target, is_local)
                continue

            # Check if name matches a symbol in this file (short names)
            found = False
            for sym in file_syms:
                if sym == name or sym.endswith("." + name):
                    full_target = short_to_full.get(sym, sym)
                    _new_call(calls, qual, full_target, True)
                    found = True
                    break
            if not found:
                # Also check across all symbols
                for sym in all_symbols:
                    if sym == name or sym.endswith("." + name):
                        full_target = short_to_full.get(sym, sym)
                        _new_call(calls, qual, full_target, True)
                        found = True
                        break
            if not found:
                _new_unresolved(unresolved, qual, name)
            continue

        # ---- Attribute: obj.attr() or self.method() ------------------------
        if isinstance(func, ast.Attribute):
            attr_name = func.attr

            # self.method() pattern
            if isinstance(func.value, ast.Name) and func.value.id == "self":
                # Resolve method in current class
                parts = qual.split(".")
                if len(parts) >= 2:
                    class_qual = ".".join(parts[:-1])
                    # Check short name in symbols of this file first
                    for sym in file_syms:
                        if sym.endswith("." + attr_name):
                            full_target = short_to_full.get(sym, sym)
                            _new_call(calls, qual, full_target, True)
                            break
                    else:
                        method_qual = f"{class_qual}.{attr_name}"
                        if method_qual in all_symbols:
                            full_target = short_to_full.get(method_qual, method_qual)
                            _new_call(calls, qual, full_target, True)
                        else:
                            _new_unresolved(unresolved, qual, f"self.{attr_name}")
                else:
                    _new_unresolved(unresolved, qual, f"self.{attr_name}")
                continue

            # Module-qualified or object-qualified: obj.method()
            obj_name = _name_from_ast(func.value)
            if obj_name is None:
                full = _name_from_ast(func)
                if full:
                    _new_unresolved(unresolved, qual, full)
                continue

            qualified = f"{obj_name}.{attr_name}"

            # Check if it's a known symbol (short or full)
            if qualified in all_symbols or qualified in all_full_names:
                full_target = short_to_full.get(qualified, qualified)
                _new_call(calls, qual, full_target, True)
                continue

            # Check if obj_name is a known module
            if obj_name in all_modules:
                candidate = f"{obj_name}.{attr_name}"
                if candidate in all_symbols or candidate in all_full_names:
                    full_target = short_to_full.get(candidate, candidate)
                    _new_call(calls, qual, full_target, True)
                    continue
                _new_unresolved(unresolved, qual, qualified)
                continue

            # Check alias
            if obj_name in file_aliases:
                expanded = file_aliases[obj_name]
                candidate = f"{expanded}.{attr_name}"
                is_local = (
                    candidate in all_symbols
                    or expanded in all_modules
                    or expanded.split(".")[0] in all_modules
                )
                _new_call(calls, qual, candidate, is_local)
                continue

            _new_unresolved(unresolved, qual, qualified)
            continue

        # ---- Complex callable (skip) ---------------------------------------
        full = _name_from_ast(func)
        if full:
            _new_unresolved(unresolved, qual, full)