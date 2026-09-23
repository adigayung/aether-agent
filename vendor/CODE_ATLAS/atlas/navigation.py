"""Deterministic relevance and navigation index for CODE ATLAS.

Provides a runtime navigation layer that can locate relevant files,
modules, and symbols from a query string, expand nearby relationships,
detect entry points, and estimate token usage — all using Python
standard library only, with no LLM, embeddings, or external services.

All output is deterministic for the same project + query.
"""

from __future__ import annotations

import ast
import os
from collections import defaultdict
from math import ceil
from typing import Iterable


def _name_from_node(node: ast.AST) -> str | None:
    """Extract a string constant or name identifier from an AST node."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        obj = _name_from_node(node.value)
        if obj is not None:
            return f"{obj}.{node.attr}"
    return None


def _is_main_check(test: ast.AST) -> bool:
    """Check if an AST test expression is ``__name__ == '__main__'`` or similar."""
    if isinstance(test, ast.Compare):
        if len(test.ops) == 1 and isinstance(test.ops[0], (ast.Eq, ast.Is)):
            left = _name_from_node(test.left)
            right = _name_from_node(test.comparators[0]) if test.comparators else None
            return (left == "__name__" and right == "__main__") or (
                right == "__name__" and left == "__main__"
            )
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        return any(_is_main_check(v) for v in test.values)
    return False


def estimate_tokens(text: str) -> int:
    """Estimate token count from character count.

    This is a rough approximation using ``ceil(len(text) / 4)`` and is
    **not** a model-specific tokenizer. Always label the result as
    *estimated tokens*.
    """
    return ceil(len(text) / 4)


# ---------------------------------------------------------------------------
# NavigationIndex  (public API)
# ---------------------------------------------------------------------------

class NavigationIndex:
    """Runtime navigation index built from existing CODE ATLAS data.

    Typical usage::

        idx = NavigationIndex(modules, symbols, relationships, discovery)
        result = idx.find("AgentRuntime")
        context = idx.expand_context("app.core.runtime.AgentRuntime.run", depth=1)
        entrypoints = idx.get_entrypoints()

    All results are deterministic (sorted, deduplicated).
    """

    def __init__(
        self,
        modules: dict[str, dict],
        symbols: dict[str, dict],
        relationships: dict,
        discovery,
    ) -> None:
        self.modules = modules
        self.symbols = symbols
        self.relationships = relationships
        self.discovery = discovery

        # Build file → module lookup
        self._file_to_module: dict[str, str] = {}
        for mod_name, mod_info in modules.items():
            self._file_to_module[mod_info["file"]] = mod_name

        # Build full qualified names (with module prefix) from short names
        # e.g. "Runtime" -> "app.core.runtime.Runtime"
        self._short_to_full: dict[str, str] = {}
        # Reverse: full -> short
        self._full_to_short: dict[str, str] = {}
        self._build_full_qualified_names()

        # Internal indexes:
        #   _full_info: full qualified name -> symbol info dict
        #   _name_to_qualified: short name -> [full qualified names]
        #   _module_to_qualified: module name -> [full qualified names]
        #   _file_to_qualified: file path -> [full qualified names]
        self._full_info: dict[str, dict] = {}
        self._name_to_qualified: dict[str, list[str]] = defaultdict(list)
        self._module_to_qualified: dict[str, list[str]] = defaultdict(list)
        self._file_to_qualified: dict[str, list[str]] = defaultdict(list)

        # Reverse relationship maps (computed at runtime, not stored in JSON)
        self._imported_by: dict[str, list[str]] = defaultdict(list)
        self._inherited_by: dict[str, list[str]] = defaultdict(list)
        self._called_by: dict[str, list[str]] = defaultdict(list)

        self._build_indexes()
        self._build_reverse_relationships()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_full_qualified_names(self) -> None:
        """Build mapping from short qualified names to full module-prefixed names.

        For each symbol like "Runtime.run" in file "app/core/runtime.py",
        the full name becomes "app.core.runtime.Runtime.run".
        """
        for short_name, sym_info in self.symbols.items():
            mod_name = self._file_to_module.get(sym_info["file"], "")
            if mod_name:
                full_name = f"{mod_name}.{short_name}"
            else:
                full_name = short_name
            self._short_to_full[short_name] = full_name
            self._full_to_short[full_name] = short_name

    def _try_full(self, name: str) -> str | None:
        """If *name* could be a full qualified name, return it if found.

        Otherwise, try to look up the name in the short->full map and
        return the full form.
        """
        # Is it already a full name?
        if name in self._full_info:
            return name
        # Is it a short name?
        if name in self._short_to_full:
            return self._short_to_full[name]
        # Try qualified pattern: maybe module.symbol
        return None

    # ------------------------------------------------------------------
    # Internal index builders
    # ------------------------------------------------------------------

    def _build_indexes(self) -> None:
        for short_name, sym_info in self.symbols.items():
            full_name = self._short_to_full.get(short_name, short_name)
            self._full_info[full_name] = dict(sym_info)  # copy

            # Index by short (last component of qualified name)
            short_last = short_name.split(".")[-1]
            self._name_to_qualified[short_last].append(full_name)

            # Index by file
            file_path = sym_info["file"]
            self._file_to_qualified[file_path].append(full_name)

            # Index by module
            mod_name = self._file_to_module.get(file_path, "")
            if mod_name:
                self._module_to_qualified[mod_name].append(full_name)

        # Sort deterministically
        for key in self._name_to_qualified:
            self._name_to_qualified[key].sort()
        for key in self._module_to_qualified:
            self._module_to_qualified[key].sort()
        for key in self._file_to_qualified:
            self._file_to_qualified[key].sort()

    def _build_reverse_relationships(self) -> None:
        for entry in self.relationships.get("imports", []):
            source, target, _local = entry
            self._imported_by[target].append(source)

        for entry in self.relationships.get("inherits", []):
            child, parent, _local = entry
            self._inherited_by[parent].append(child)

        for entry in self.relationships.get("calls", []):
            caller, target, _local = entry
            self._called_by[target].append(caller)

        for key in self._imported_by:
            self._imported_by[key].sort()
        for key in self._inherited_by:
            self._inherited_by[key].sort()
        for key in self._called_by:
            self._called_by[key].sort()

    # ------------------------------------------------------------------
    # Query matching with deterministic relevance ordering
    # ------------------------------------------------------------------

    def find(
        self,
        query: str,
        max_results: int | None = None,
    ) -> dict:
        """Find relevant symbols and files matching *query*.

        Priority (highest → lowest):

        1. Exact qualified symbol name  (``app.core.runtime.Runtime``)
        2. Exact short symbol name      (``Runtime``)
        3. Exact module name            (``app.core.runtime``)
        4. Exact filename               (``app/core/runtime.py``)
        5. Case-insensitive exact match
        6. Path / name substring match

        Returns a dict with keys ``matches`` (list of qualified symbol
        names) and ``files`` (list of relative file paths).
        """
        query_lower = query.lower()

        exact_qualified: list[str] = []
        exact_symbol: list[str] = []
        exact_module: list[str] = []
        exact_file: list[str] = []
        ci_exact: list[str] = []
        substring: list[str] = []

        seen_symbols: set[str] = set()

        # --- Priority 1: exact full qualified symbol -----------------------
        if query in self._full_info:
            exact_qualified.append(query)
            seen_symbols.add(query)
        # Also check with module prefix added if query is a short name
        full_via_short = self._short_to_full.get(query)
        if full_via_short and full_via_short not in seen_symbols:
            exact_qualified.append(full_via_short)
            seen_symbols.add(full_via_short)

        # --- Priority 2: exact short symbol name --------------------------
        if query in self._name_to_qualified:
            for qn in self._name_to_qualified[query]:
                if qn not in seen_symbols:
                    exact_symbol.append(qn)
                    seen_symbols.add(qn)

        # --- Priority 3: exact module name --------------------------------
        if query in self._module_to_qualified:
            for qn in self._module_to_qualified[query]:
                if qn not in seen_symbols:
                    exact_module.append(qn)
                    seen_symbols.add(qn)

        # --- Priority 4: exact filename -----------------------------------
        if query in self._file_to_qualified:
            for qn in self._file_to_qualified[query]:
                if qn not in seen_symbols:
                    exact_file.append(qn)
                    seen_symbols.add(qn)

        # --- Priority 5: case-insensitive exact ---------------------------
        for full_name in self._full_info:
            if full_name not in seen_symbols and full_name.lower() == query_lower:
                ci_exact.append(full_name)
                seen_symbols.add(full_name)

        for short_last, qualified_list in self._name_to_qualified.items():
            if short_last.lower() == query_lower:
                for qn in qualified_list:
                    if qn not in seen_symbols:
                        ci_exact.append(qn)
                        seen_symbols.add(qn)

        for mod_name in self._module_to_qualified:
            if mod_name.lower() == query_lower:
                for qn in self._module_to_qualified[mod_name]:
                    if qn not in seen_symbols:
                        ci_exact.append(qn)
                        seen_symbols.add(qn)

        for file_path in self._file_to_qualified:
            if file_path.lower() == query_lower:
                for qn in self._file_to_qualified[file_path]:
                    if qn not in seen_symbols:
                        ci_exact.append(qn)
                        seen_symbols.add(qn)

        # --- Priority 6: substring match ----------------------------------
        for full_name in self._full_info:
            if full_name not in seen_symbols and query_lower in full_name.lower():
                substring.append(full_name)
                seen_symbols.add(full_name)

        for short_last, qualified_list in self._name_to_qualified.items():
            if query_lower in short_last.lower():
                for qn in qualified_list:
                    if qn not in seen_symbols:
                        substring.append(qn)
                        seen_symbols.add(qn)

        for mod_name in self._module_to_qualified:
            if query_lower in mod_name.lower():
                for qn in self._module_to_qualified[mod_name]:
                    if qn not in seen_symbols:
                        substring.append(qn)
                        seen_symbols.add(qn)

        for file_path in self._file_to_qualified:
            if query_lower in file_path.lower():
                for qn in self._file_to_qualified[file_path]:
                    if qn not in seen_symbols:
                        substring.append(qn)
                        seen_symbols.add(qn)

        matches = (
            exact_qualified
            + exact_symbol
            + exact_module
            + exact_file
            + ci_exact
            + substring
        )

        if max_results is not None:
            matches = matches[:max_results]

        # Collect unique files in order of match appearance
        files: list[str] = []
        seen_files: set[str] = set()
        for qn in matches:
            full_info = self._full_info.get(qn)
            if full_info:
                f = full_info["file"]
                if f not in seen_files:
                    files.append(f)
                    seen_files.add(f)

        return {"matches": matches, "files": files}

    # ------------------------------------------------------------------
    # Relationship expansion
    # ------------------------------------------------------------------

    def expand_context(
        self,
        qualified_name: str,
        depth: int = 1,
    ) -> dict:
        """Expand context around *qualified_name* using existing relationships.

        *depth* controls traversal levels (0 = root only).
        Cycles are prevented with a visited set.

        Returns a dict with keys ``root``, ``related`` (list of
        qualified names), and ``files`` (list of relative file paths).
        """
        # Resolve to full qualified name if it's a short name
        full_root = self._try_full(qualified_name)
        if full_root is None or full_root not in self._full_info:
            return {
                "root": qualified_name,
                "related": [],
                "files": [],
            }

        related: list[str] = []
        visited: set[str] = {full_root}
        frontier: set[str] = {full_root}

        # Also include the parent module of the root as a virtual node,
        # since relationships like imports are module-level.
        root_parts = full_root.split(".")
        if len(root_parts) >= 2:
            parent_module = ".".join(root_parts[:-1])
            # Only add if it's an actual module, not a class/method
            if parent_module in self.modules:
                visited.add(parent_module)

        for _ in range(depth):
            next_frontier: set[str] = set()
            for node in frontier:
                # Determine the parent module of this node (for module-level
                # relationships like imports).
                node_parts = node.split(".")
                parent_module = ""
                # Check progressively shorter prefixes to find actual module
                for i in range(len(node_parts), 0, -1):
                    candidate = ".".join(node_parts[:i])
                    if candidate in self.modules:
                        parent_module = candidate
                        break

                # Walk relationships: match against both the node itself
                # and its parent module (for module-level relationships).
                nodes_to_check = {node}
                if parent_module:
                    nodes_to_check.add(parent_module)

                for entry in self.relationships.get("imports", []):
                    src, tgt, _local = entry
                    for n in nodes_to_check:
                        if src == n and tgt not in visited:
                            visited.add(tgt)
                            related.append(tgt)
                            next_frontier.add(tgt)
                        elif tgt == n and src not in visited:
                            visited.add(src)
                            related.append(src)
                            next_frontier.add(src)

                for entry in self.relationships.get("inherits", []):
                    child, parent, _local = entry
                    for n in nodes_to_check:
                        if child == n and parent not in visited:
                            visited.add(parent)
                            related.append(parent)
                            next_frontier.add(parent)
                        elif parent == n and child not in visited:
                            visited.add(child)
                            related.append(child)
                            next_frontier.add(child)

                for entry in self.relationships.get("calls", []):
                    caller, target, _local = entry
                    for n in nodes_to_check:
                        if caller == n and target not in visited:
                            visited.add(target)
                            related.append(target)
                            next_frontier.add(target)
                        elif target == n and caller not in visited:
                            visited.add(caller)
                            related.append(caller)
                            next_frontier.add(caller)

            frontier = next_frontier

        # Collect files
        files: list[str] = []
        seen_files: set[str] = set()

        root_info = self._full_info.get(full_root)
        if root_info:
            f = root_info["file"]
            files.append(f)
            seen_files.add(f)

        for qn in related:
            full_qn = qn
            sym_info = self._full_info.get(full_qn)
            # Also check if qn is a module (no dot at end or just module name)
            if sym_info is None:
                continue
            f = sym_info["file"]
            if f not in seen_files:
                files.append(f)
                seen_files.add(f)

        related.sort()
        files.sort()
        return {"root": full_root, "related": related, "files": files}

    # ------------------------------------------------------------------
    # Entry point detection
    # ------------------------------------------------------------------

    def get_entrypoints(self) -> list[str]:
        """Detect modules containing ``if __name__ == '__main__'`` blocks.

        Returns a sorted list of module names, or an empty list if none
        are found.
        """
        entrypoints: list[str] = []

        # Build file → module mapping (already in self._file_to_module)
        for rel_path, mod_name in sorted(self._file_to_module.items(),
                                         key=lambda x: x[0]):
            abs_path = os.path.join(self.discovery.root, rel_path)
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
                if isinstance(node, ast.If) and _is_main_check(node.test):
                    entrypoints.append(mod_name)
                    break

        entrypoints.sort()
        return entrypoints