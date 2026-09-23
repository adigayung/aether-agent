"""
Python source-code mapping extractor for RIG.

Uses Python standard library `ast` as the parser to extract:
- source files
- modules
- classes
- functions
- methods
- imports / from-imports
- static invocation/call relationships
- inheritance
- containment (module→class→method, module→function)

All entities and relationships have deterministic IDs and evidence.

Design principles:
- Uses existing RIG infrastructure (GraphBuilder, EvidenceCollector, identity)
- No external dependencies beyond standard library
- Parse failure on one file does not stop the whole extraction
- Unresolved references are marked as such, not fabricated
"""

from __future__ import annotations

import ast
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from rig.evidence import EvidenceCollector
from rig.extractor import ExtractorPlugin, ExtractorResult
from rig.identity import (
    code_class_id, code_class_identity_key,
    code_file_id, code_file_identity_key,
    code_function_id, code_function_identity_key,
    code_module_id, code_module_identity_key,
    code_symbol_id, code_symbol_identity_key,
    edge_id, normalize_path,
)
from rig.models import (
    CodeClass, CodeFile, CodeFunction, CodeModule, CodeSymbol,
    Diagnostic, Edge, EdgeType, Evidence,
    UnresolvedReference, UnresolvedKind,
)


# ── Internal helpers ──────────────────────────────────────────────────────

KNOWN_PYTHON_SOURCE_DIRS = {"src", "lib", "source", "scripts", "tests", "test", ""}


def _is_python_file(filename: str) -> bool:
    """Check if filename is a Python source file (not __init__ or .pyc)."""
    return filename.endswith(".py") and not filename.endswith(".pyc")


def _module_name_from_path(rel_path: str) -> str:
    """Convert a relative file path to a Python module name.

    e.g., 'src/foo/bar.py' -> 'src.foo.bar'
          'src/foo/__init__.py' -> 'src.foo'
    """
    no_ext = rel_path.replace(".py", "")
    parts = no_ext.replace("\\", "/").split("/")
    # Remove __init__ suffix for package identity
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        # __init__.py at root -> use parent dir name or "root"
        parent_dir = os.path.dirname(rel_path.replace("\\", "/"))
        return parent_dir.replace("/", ".") if parent_dir else "__init__"
    return ".".join(parts)


# ── AST Visitor ───────────────────────────────────────────────────────────

class _CodeEntityVisitor(ast.NodeVisitor):
    """AST visitor that collects code entities and relationships from a Python file.

    This is a pure extraction visitor: it builds lists of entities
    and relationships from the AST without modifying any external state.
    """

    def __init__(self, rel_path: str, abs_path: str, module_name: str,
                 evidence_collector: EvidenceCollector):
        self.rel_path = rel_path
        self.abs_path = abs_path
        self.file_path = abs_path  # absolute path for file operations
        self.rel_path = rel_path  # repo-relative path for identity keys
        self.module_name = module_name
        self.evidence_collector = evidence_collector

        # Output collections
        self.code_modules: List[CodeModule] = []
        self.code_classes: List[CodeClass] = []
        self.code_functions: List[CodeFunction] = []
        self.code_symbols: List[CodeSymbol] = []
        self.edges: List[Edge] = []
        self.unresolved_refs: List[UnresolvedReference] = []
        self.diagnostics: List[Diagnostic] = []

        # Local tracking
        self._module_level_funcs: Set[str] = set()
        self._module_level_classes: Set[str] = set()
        self._current_class: Optional[str] = None
        self._imported_names: Dict[str, str] = {}  # local_name -> qualified_name
        self._imported_modules: Set[str] = set()
        self._all_symbols: Dict[str, str] = {}  # name -> node_id for resolution

        # Create module entity
        self._create_module()

    def _make_evidence(self, node: ast.AST) -> Evidence:
        """Create evidence for an AST node."""
        line_start = getattr(node, "lineno", 1)
        line_end = getattr(node, "end_lineno", line_start)
        return self.evidence_collector.add_file_evidence(
            self.abs_path,
            start_line=line_start,
            end_line=line_end,
        )

    def _make_edge(self, edge_type: str, source: str, target: str,
                   role: Optional[str] = None,
                   evidence: Optional[Evidence] = None) -> Optional[Edge]:
        """Create a deterministic edge with evidence."""
        eid = edge_id(edge_type, source, target, role)
        ev_ids = [evidence.id] if evidence else []
        if not ev_ids:
            return None
        return Edge(
            id=eid,
            type=edge_type,
            source=source,
            target=target,
            role=role,
            evidence_ids=ev_ids,
            origin_plugin="python_code",
        )

    def _create_module(self):
        """Create the module entity for this file."""
        mid = code_module_id(self.rel_path)
        ev = self.evidence_collector.add_file_evidence(
            self.abs_path, start_line=1, end_line=1
        )
        is_pkg = os.path.basename(self.rel_path) == "__init__.py"
        mod = CodeModule(
            id=mid,
            name=self.module_name,
            identity_key=code_module_identity_key(self.rel_path),
            evidence_ids=[ev.id],
            file_path=self.rel_path,
            is_package=is_pkg,
        )
        self.code_modules.append(mod)
        self._module_id = mid
        self._all_symbols[self.module_name] = mid

    def _add_function(self, name: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        """Add a function/method entity from the AST."""
        line_start = node.lineno
        line_end = getattr(node, "end_lineno", line_start)
        ev = self._make_evidence(node)
        parent_class = self._current_class or ""
        is_method = bool(self._current_class)

        fid = code_function_id(self.rel_path, name, parent_class)
        key = code_function_identity_key(self.rel_path, name, parent_class)

        func = CodeFunction(
            id=fid,
            name=name,
            identity_key=key,
            evidence_ids=[ev.id],
            file_path=self.rel_path,
            line_start=line_start,
            line_end=line_end,
            is_method=is_method,
            parent_class_id=(
                code_class_id(self.rel_path, self._current_class)
                if self._current_class else None
            ),
        )
        self.code_functions.append(func)

        if is_method:
            # Contains: class -> method
            class_id = code_class_id(self.rel_path, self._current_class)
            edge = self._make_edge(
                EdgeType.CONTAINS, class_id, fid, evidence=ev
            )
            if edge:
                self.edges.append(edge)
        else:
            # Contains: module -> function
            edge = self._make_edge(
                EdgeType.CONTAINS, self._module_id, fid, evidence=ev
            )
            if edge:
                self.edges.append(edge)
            self._module_level_funcs.add(name)

        # Track for resolution
        qualified = f"{self.module_name}.{name}"
        if parent_class:
            qualified = f"{self.module_name}.{parent_class}.{name}"
        self._all_symbols[name] = fid
        self._all_symbols[qualified] = fid

        return fid

    def _add_class(self, name: str, node: ast.ClassDef) -> str:
        """Add a class entity from the AST."""
        line_start = node.lineno
        line_end = getattr(node, "end_lineno", line_start)
        ev = self._make_evidence(node)

        cid = code_class_id(self.rel_path, name)
        key = code_class_identity_key(self.rel_path, name)

        # Extract base classes (static only)
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(f"{self._attr_to_dotted(base)}")

        cls = CodeClass(
            id=cid,
            name=name,
            identity_key=key,
            evidence_ids=[ev.id],
            file_path=self.rel_path,
            line_start=line_start,
            line_end=line_end,
            bases=bases,
        )
        self.code_classes.append(cls)
        self._module_level_classes.add(name)

        # Contains: module -> class
        edge = self._make_edge(
            EdgeType.CONTAINS, self._module_id, cid, evidence=ev
        )
        if edge:
            self.edges.append(edge)

        # Inheritance edges (static bases only)
        for base_name in bases:
            target_id = self._resolve_name(base_name)
            if target_id:
                inherit_edge = self._make_edge(
                    EdgeType.INHERITS, cid, target_id, evidence=ev
                )
                if inherit_edge:
                    self.edges.append(inherit_edge)
            else:
                # Unresolved base
                ur_id = f"unresolved:{hash(f'{cid}|{base_name}')}"
                ur = UnresolvedReference(
                    id=ur_id,
                    source_entity_id=cid,
                    reference_text=base_name,
                    reference_kind=UnresolvedKind.SYMBOL,
                    reason=f"Base class '{base_name}' not found in local scope or imports",
                    evidence_ids=[ev.id],
                )
                self.unresolved_refs.append(ur)

        # Track
        qualified = f"{self.module_name}.{name}"
        self._all_symbols[name] = cid
        self._all_symbols[qualified] = cid

        return cid

    def _attr_to_dotted(self, attr_node: ast.Attribute) -> str:
        """Convert an ast.Attribute chain to a dotted string."""
        parts = []
        current = attr_node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        elif isinstance(current, ast.Call):
            # Dynamic, can't resolve
            return ""
        return ".".join(reversed(parts))

    def _resolve_name(self, name: str) -> Optional[str]:
        """Resolve a name to a known entity ID.

        Handles:
        - Local symbols (classes, functions in same file)
        - Imported names
        - Module-qualified references
        """
        if name in self._all_symbols:
            return self._all_symbols[name]
        if name in self._imported_names:
            qualified = self._imported_names[name]
            if qualified in self._all_symbols:
                return self._all_symbols[qualified]
        return None

    def _add_import_symbol(self, alias_name: str, qualified_name: str,
                           node: ast.AST) -> CodeSymbol:
        """Add a symbol entity for an import."""
        ev = self._make_evidence(node)
        line = getattr(node, "lineno", 1)
        sid = code_symbol_id(self.rel_path, alias_name, "import", line)
        key = code_symbol_identity_key(self.rel_path, alias_name, "import", line)

        sym = CodeSymbol(
            id=sid,
            name=alias_name,
            identity_key=key,
            evidence_ids=[ev.id],
            file_path=self.rel_path,
            line_start=line,
            line_end=getattr(node, "end_lineno", line),
            symbol_type="import",
        )
        self.code_symbols.append(sym)
        self._all_symbols[alias_name] = sid
        return sid

    # ── Visitor methods ──

    def visit_Import(self, node: ast.Import):
        """Handle: import foo.bar"""
        ev = self._make_evidence(node)
        for alias in node.names:
            as_name = alias.asname or alias.name
            # Register import name
            self._imported_names[as_name] = alias.name
            self._imported_modules.add(alias.name.split(".")[0])

            # Create symbol
            sym_id = self._add_import_symbol(as_name, alias.name, node)

            # Edge: module -> imports -> (target)
            # Try to resolve the import target
            target_mod_name = alias.name
            target_mod_path = target_mod_name.replace(".", "/") + ".py"
            target_id = self._all_symbols.get(target_mod_name)
            if target_id:
                import_edge = self._make_edge(
                    EdgeType.IMPORTS, self._module_id, target_id, evidence=ev
                )
                if import_edge:
                    self.edges.append(import_edge)
            else:
                # Could not resolve; create unresolved reference
                ur_id = f"unresolved:{hash(f'{self._module_id}|import|{target_mod_name}')}"
                ur = UnresolvedReference(
                    id=ur_id,
                    source_entity_id=self._module_id,
                    reference_text=target_mod_name,
                    reference_kind=UnresolvedKind.SYMBOL,
                    reason=f"Import target '{target_mod_name}' not in local graph",
                    evidence_ids=[ev.id],
                )
                self.unresolved_refs.append(ur)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        """Handle: from foo.bar import baz"""
        ev = self._make_evidence(node)
        module = node.module or ""
        for alias in node.names:
            as_name = alias.asname or alias.name
            qualified_name = f"{module}.{alias.name}" if module else alias.name
            self._imported_names[as_name] = qualified_name

            # Create symbol for the imported name
            sym_id = self._add_import_symbol(as_name, qualified_name, node)

            # Edge: module -> imports -> (qualified target)
            target_id = self._all_symbols.get(qualified_name)
            if target_id:
                import_edge = self._make_edge(
                    EdgeType.IMPORTS, self._module_id, target_id, evidence=ev
                )
                if import_edge:
                    self.edges.append(import_edge)
            else:
                # Unresolved from-import
                ur_id = f"unresolved:{hash(f'{self._module_id}|from-import|{qualified_name}')}"
                ur = UnresolvedReference(
                    id=ur_id,
                    source_entity_id=self._module_id,
                    reference_text=qualified_name,
                    reference_kind=UnresolvedKind.SYMBOL,
                    reason=f"From-import target '{qualified_name}' not in local graph",
                    evidence_ids=[ev.id],
                )
                self.unresolved_refs.append(ur)

    def visit_ClassDef(self, node: ast.ClassDef):
        """Handle class definition and recurse into methods."""
        # Save context
        prev_class = self._current_class
        cid = self._add_class(node.name, node)
        self._current_class = node.name

        # Visit body for methods
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._add_function(item.name, item)
            elif isinstance(item, ast.ClassDef):
                # Nested class (rare but possible)
                self.visit_ClassDef(item)

        # Restore context
        self._current_class = prev_class

    def visit_FunctionDef(self, node: ast.FunctionDef):
        """Handle module-level function definition."""
        # Only process module-level functions here
        # (methods are handled in visit_ClassDef)
        if self._current_class is None:
            self._add_function(node.name, node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        """Handle async module-level function definition."""
        if self._current_class is None:
            self._add_function(node.name, node)

    def visit_Call(self, node: ast.Call):
        """Handle function/method calls for static resolution.

        Only resolves:
        - Direct names: foo()
        - Attribute calls: obj.method()
        where the target can be statically determined.
        """
        ev = self._make_evidence(node)
        line = getattr(node, "lineno", 1)

        call_name = None
        if isinstance(node.func, ast.Name):
            call_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            # obj.method() -> try to resolve obj's type
            # Only resolve simple cases: self.method(), ClassName.method()
            if isinstance(node.func.value, ast.Name):
                obj_name = node.func.value.id
                method_name = node.func.attr
                # Check if obj_name is a class in this file
                if obj_name in self._module_level_classes:
                    call_name = f"{obj_name}.{method_name}"
                elif obj_name == "self" and self._current_class:
                    # self.method() inside a class
                    call_name = f"{self._current_class}.{method_name}"
                else:
                    # Could be an instance or imported; try qualified
                    call_name = f"{obj_name}.{method_name}"

        if call_name:
            target_id = self._resolve_name(call_name)
            if target_id:
                # Create invocation edge from parent function/method
                # Find the enclosing function
                parent_func_id = self._find_enclosing_function()
                if parent_func_id:
                    inv_edge = self._make_edge(
                        EdgeType.INVOKES, parent_func_id, target_id,
                        evidence=ev,
                    )
                    if inv_edge:
                        self.edges.append(inv_edge)
            # If not resolved, leave as unresolved (no fabricated edge)
            # Unresolved calls are normal in Python, we don't create
            # unresolved references for every call to avoid noise

        # Continue traversal
        self.generic_visit(node)

    def _find_enclosing_function(self) -> Optional[str]:
        """Find the enclosing function/method for the current context.

        This is approximate; for a more accurate approach we would
        need a context stack during visitor traversal.
        """
        # Return the last function added if it matches the current file
        if self.code_functions:
            return self.code_functions[-1].id
        return None


# ── Main Extractor Plugin ─────────────────────────────────────────────────

class PythonCodeExtractor(ExtractorPlugin):
    """Python source-code mapping extractor.

    Uses Python standard library `ast` to extract code structure:
    - Files, modules, classes, functions, methods, symbols
    - Containment, imports, inheritance, invocation relationships

    Priority: AST parsing > UNKNOWN
    Parse failures are handled per-file (graceful degradation).
    """

    @property
    def name(self) -> str:
        return "python_code"

    def get_source_priority(self) -> str:
        return "AST parsing > UNKNOWN"

    def detect(self, repo_root: str, discovery_result: Any) -> bool:
        """Always active for Python detection; we check for .py files."""
        # Check if there are any Python files in the repo
        python_markers = discovery_result.build_system_markers.get("python", [])
        if python_markers:
            return True
        # Also check common source directories
        for src_dir in ["src", "lib", "tests", "test", ""]:
            full_dir = os.path.join(repo_root, src_dir)
            if os.path.isdir(full_dir):
                for entry in os.listdir(full_dir):
                    if entry.endswith(".py"):
                        return True
        return False

    def extract(
        self,
        repo_root: str,
        discovery_result: Any,
        evidence_collector: EvidenceCollector,
        config: Any,
    ) -> ExtractorResult:
        result = ExtractorResult()

        # Find all Python files
        py_files = self._find_python_files(repo_root)
        visited_module_paths: Set[str] = set()

        # Sort for determinism
        py_files.sort()

        if not py_files:
            result.diagnostics.append(Diagnostic(
                level="info",
                message="Python code extractor: no .py files found",
                source="python_code",
            ))
            return result

        # Process each Python file
        for rel_path in py_files:
            abs_path = os.path.join(repo_root, rel_path)
            module_name = _module_name_from_path(rel_path)

            # Skip if already processed
            if module_name in visited_module_paths:
                continue
            visited_module_paths.add(module_name)

            # Create CodeFile entity
            code_file_ev = evidence_collector.add_file_evidence(
                abs_path, start_line=1
            )
            cf_id = code_file_id(rel_path)
            cf_key = code_file_identity_key(rel_path)

            # Parse the Python file
            code, parse_success, parse_error = self._parse_file(abs_path)

            if not parse_success:
                # Graceful degradation: record parse failure and continue
                code_file = CodeFile(
                    id=cf_id,
                    name=os.path.basename(rel_path),
                    identity_key=cf_key,
                    evidence_ids=[code_file_ev.id],
                    file_path=normalize_path(rel_path),
                    language="python",
                    parse_success=False,
                    parse_error=parse_error,
                )
                result.code_files.append(code_file)
                result.diagnostics.append(Diagnostic(
                    level="warning",
                    message=f"Python parse failure: {rel_path}: {parse_error}",
                    source="python_code",
                    entity_id=cf_id,
                ))
                continue

            # Successful parse: extract entities
            code_file = CodeFile(
                id=cf_id,
                name=os.path.basename(rel_path),
                identity_key=cf_key,
                evidence_ids=[code_file_ev.id],
                file_path=normalize_path(rel_path),
                language="python",
                parse_success=True,
            )
            result.code_files.append(code_file)

            # AST-based extraction
            visitor = _CodeEntityVisitor(
                rel_path=normalize_path(rel_path),
                abs_path=abs_path,
                module_name=module_name,
                evidence_collector=evidence_collector,
            )

            try:
                visitor.visit(code)
            except Exception as e:
                result.diagnostics.append(Diagnostic(
                    level="warning",
                    message=f"AST visitor error in {rel_path}: {e}",
                    source="python_code",
                    entity_id=cf_id,
                ))
                continue

            # Collect results
            result.code_modules.extend(visitor.code_modules)
            result.code_classes.extend(visitor.code_classes)
            result.code_functions.extend(visitor.code_functions)
            result.code_symbols.extend(visitor.code_symbols)
            result.edges.extend(visitor.edges)
            result.unresolved_references.extend(visitor.unresolved_refs)
            result.diagnostics.extend(visitor.diagnostics)

        result.diagnostics.append(Diagnostic(
            level="info",
            message=f"Python code extractor: {len(py_files)} files, "
                    f"{len(result.code_modules)} modules, "
                    f"{len(result.code_classes)} classes, "
                    f"{len(result.code_functions)} functions, "
                    f"{len(result.edges)} code edges",
            source="python_code",
        ))

        return result

    def _find_python_files(self, repo_root: str) -> List[str]:
        """Find all Python (.py) files in the repository.

        Excludes: .git, __pycache__, venv, node_modules, etc.
        Uses os.walk with ignore dirs filtering.
        """
        from rig.config import DEFAULT_IGNORE_DIRS

        py_files = []
        ignore_dirs = DEFAULT_IGNORE_DIRS | {"__pycache__", ".mypy_cache",
                                               ".pytest_cache", ".ruff_cache",
                                               ".eggs", "*.egg-info"}
        ignore_dirs_lower = {d.lower() for d in ignore_dirs}

        for root, dirs, files in os.walk(repo_root):
            # Filter ignored directories
            dirs[:] = [
                d for d in dirs
                if d.lower() not in ignore_dirs_lower
                and not d.startswith(".")
            ]
            # Sort for determinism
            dirs.sort()

            for f in sorted(files):
                if _is_python_file(f):
                    abs_path = os.path.join(root, f)
                    rel_path = os.path.relpath(abs_path, repo_root)
                    py_files.append(normalize_path(rel_path))

        return py_files

    def _parse_file(self, abs_path: str) -> Tuple[Optional[ast.AST], bool, Optional[str]]:
        """Parse a Python file, returning (tree, success, error_message)."""
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
            tree = ast.parse(source, filename=abs_path)
            return tree, True, None
        except SyntaxError as e:
            return None, False, f"SyntaxError at line {e.lineno}: {e.msg}"
        except UnicodeDecodeError as e:
            return None, False, f"UnicodeDecodeError: {e}"
        except Exception as e:
            return None, False, str(e)