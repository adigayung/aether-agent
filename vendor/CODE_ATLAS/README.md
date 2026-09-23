# CODE ATLAS

**Compact, deterministic project map for LLM-powered code navigation.**

CODE ATLAS is a standalone Python tool that produces a compact JSON map of
any Python codebase — files, modules, symbols, and their relationships
(imports, inheritance, calls, entry points) — using **only the Python standard
library**. No external dependencies, no LLM, no embeddings, no API server.

The map is designed to be fed directly into an LLM context window: it tells
the model *where* things are, *what* they are, and *what they connect to*,
without storing source code. Actual file content is read from the filesystem
when needed.

---

## Requirements

- **Python ≥ 3.10** (uses `ast`, `pathlib`, `os`, `json`, and other stdlib modules)
- No third-party packages, no pip install required
- Works on Windows, macOS, and Linux

## Quick Start

```bash
# Clone or download the repository
git clone https://github.com/your-name/code-atlas.git
cd code-atlas

# Run against any Python project
python atlas.py /path/to/your-project output.json
```

### Try it on itself

```bash
python atlas.py . code-atlas-map.json
```

This produces a map of CODE ATLAS's own source with ~10 modules, 270+ symbols,
and 590+ relationships — all from ~89 KB of JSON (~22K estimated tokens).

### Example output

```json
{
  "version": 1,
  "project": { "name": "my-project", "language": "python" },
  "files": [ "src/main.py", "src/utils.py" ],
  "modules": { "main": { "file": "src/main.py" }, "utils": { "file": "src/utils.py" } },
  "symbols": {
    "main.hello":         { "kind": "function", "file": "src/main.py", "start_line": 1, "end_line": 3 },
    "main.MyClass":       { "kind": "class",    "file": "src/main.py", "start_line": 5, "end_line": 20, "methods": ["run"] },
    "main.MyClass.run":   { "kind": "method",   "file": "src/main.py", "start_line": 8, "end_line": 12 }
  },
  "imports":    [ ["main", "utils.helper", true] ],
  "inherits":   [ ["main.MyClass", "Exception", false] ],
  "calls":      [ ["main.MyClass.run", "utils.helper", true] ],
  "entrypoints": [ "main" ]
}
```

The CLI also prints a concise summary:

```
CODE ATLAS

Project: code-atlas
Files: 8
Modules: 6
Symbols: 274
Relationships: 594

JSON: 89828 bytes
Estimated tokens: 22457

Output: code-atlas-map.json
```

> **Token estimation**: `ceil(characters / 4)` — a rough approximation, not an
> exact tokeniser. Labelled as "estimated tokens" in all output.

---

## Pipeline Architecture

```
Project root
    │
    ▼
┌─────────────────┐
│  1. Discovery   │  Walk filesystem, ignore build/venv/cache,
│                 │  normalise paths to relative '/' form, sort.
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  2. Parse       │  ast.parse each .py file, extract symbols
│                 │  (classes, functions, methods) with source
│                 │  locations. One invalid file ≠ total failure.
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  3. Resolve     │  Four-pass resolver: (i) import aliases,
│                 │  (ii) imports, (iii) inheritance, (iv) calls.
│                 │  Static only — no type inference, no dynamic
│                 │  analysis. "Unknown better than wrong."
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  4. Build Map   │  Serialise to compact JSON: sorted, deduplicated,
│                 │  no timestamps, no IDs, no AST dumps.
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  5. Navigate    │  Runtime NavigationIndex — deterministic query
│                 │  matching, context expansion, reverse relationship
│                 │  lookup. No data duplicated; indexes computed
│                 │  on the fly from the map.
└─────────────────┘
```

---

## Output Schema

| Field | Type | Description |
|---|---|---|
| `version` | `int` | Schema version (currently `1`) |
| `project` | `object` | `{ name: str, language: str }` |
| `files` | `list[str]` | Sorted relative file paths (always `/` separators) |
| `modules` | `dict` | `{ module_name: { file: path } }` |
| `symbols` | `dict` | `{ qualified_name: { kind, file, start_line, end_line, methods? } }` |
| `imports` | `list[[str,str,bool]]` | `[from_module, to_name, is_local]` |
| `inherits` | `list[[str,str,bool]]` | `[child, parent, is_local]` |
| `calls` | `list[[str,str,bool]]` | `[caller, target, is_local]` |
| `entrypoints` | `list[str]` | Modules with `if __name__ == '__main__':` |

- All relationships are stored as compact arrays (no nested objects).
- Empty arrays (`[]`) mean "there is none of this kind of relationship."
- Boolean `is_local` is `true` when target is inside the project workspace.

### Symbol kinds

| Kind | Description | Additional fields |
|---|---|---|
| `class` | A class definition | `methods` (list of method names) |
| `function` | A module-level function | — |
| `method` | A class method | — |

### Qualified names

- **Identity is the qualified name** (e.g. `package.module.ClassName.method`).
- No numeric IDs, no UUIDs, no memory addresses.
- Sorted deterministically — byte-for-byte identical output on the same project.

---

## Navigation API

The `NavigationIndex` computes indexes and reverse relationships at runtime
without modifying the stored JSON map.

```python
from atlas.navigation import NavigationIndex, estimate_tokens

nav = NavigationIndex(modules, symbols, imports, inherits, calls, files)

# Find symbols / files matching a query
result = nav.find("AgentRuntime")
# → {"matches": [...], "files": [...]}

# Expand relationships around a symbol (depth-limited, cycle-safe)
context = nav.expand_context("app.core.AgentRuntime.run", depth=1)
# → {"root": "...", "related": [...], "files": [...]}

# Get all entry point modules
entrypoints = nav.get_entrypoints()
# → ["app.cli", ...]

# Estimate token count
estimate_tokens(json_string)  # → int
```

### Matching priority

1. Exact qualified symbol name (`app.core.AgentRuntime`)
2. Exact short symbol name (`AgentRuntime`)
3. Exact module name (`app.core`)
4. Exact filename (`app/core/runtime.py`)
5. Case-insensitive exact match
6. Substring match (path or name)

All results are **deterministic** — same project + same query = same output.

---

## Key Design Decisions

- **No source-code duplication**: the map stores only metadata (names,
  locations, connections). Actual source is read from the filesystem.
- **Deterministic**: sorted, deduplicated, byte-for-byte identical output
  across runs. No timestamps, random IDs, or filesystem traversal order.
- **Compact**: minimal JSON formatting, no pretty-print, no numeric IDs.
  Arrays over repeated objects.
- **Workspace-boundary aware**: only files under the project root are
  mapped. External imports (e.g. `typing`, `fastapi`) are marked non-local
  but never resolved to project symbols.
- **Unknown better than wrong**: dynamic calls (`getattr`, `factory()`,
  `eval()`) produce no fabricated targets. If it can't be proven statically,
  it is not recorded.
- **Cycle-safe**: relationship expansion uses a visited set and a configurable
  depth limit (default 1).
- **Parse-failure robust**: one invalid `.py` file does not crash the entire
  project; a diagnostic is recorded and other files are processed normally.
- **Module naming**: derived deterministically from package structure.
  Normalised relative path used as fallback when structure is ambiguous.
- **Inheritance**: built-in types (`str`, `Exception`, `object`) are not
  recorded in the map.
- **Constructor calls**: `MyClass()` is recorded as a call to `MyClass`.

### Ignored directories

`.git`, `.venv`, `venv`, `env`, `ENV`, `node_modules`, `__pycache__`,
`.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.nox`, `build`,
`dist`, `.eggs`

---

## What CODE ATLAS Does Not Do

CODE ATLAS is a **navigation map**, not a source-code replacement or a
general-purpose analysis framework:

| ❌ Not included | Reason |
|---|---|
| LLM, embeddings, vector DB, semantic search | Out of scope — the map is *consumed* by LLMs, not enhanced by them |
| External dependencies / API server | Standalone tool, zero install friction |
| Source code in the map | Only metadata; read from filesystem when needed |
| AST dumps, bytecode, decorator nodes, local variables | Keeps the map compact and focused on navigation |
| Runtime tracing / dynamic analysis | Static analysis only — no `__file__` / `sys.path` resolving |
| Type inference | Fully static; `getattr`, `eval`, factory patterns produce no targets |
| Git intelligence, IDE integration | Single-purpose CLI tool |
| SQLite, caching, persistent databases | Stateless — run, produce JSON, exit |
| Non-Python files | `.py` only; other languages are silently ignored |

---

## Development

### Running tests

```bash
python -m pytest tests/
```

All 152 tests from Tasks 1–6 pass in under 1 second:

```
tests/test_discovery.py ......
tests/test_parser.py ...........
tests/test_resolver.py ......................
tests/test_navigation.py ..................................
```

Tests use only the Python standard library — no pytest required if you prefer
`unittest` directly:

```bash
python -m unittest discover tests
```

### Project structure

```
code-atlas/
├── atlas.py              # CLI entry point
├── atlas/
│   ├── __init__.py       # Package marker
│   ├── config.py         # Ignored dirs / file extension constants
│   ├── discovery.py      # Filesystem walk, module naming
│   ├── parser.py         # ast.parse → symbol extraction
│   ├── resolver.py       # Relationship resolution (imports, inherits, calls)
│   └── navigation.py     # NavigationIndex (query, context, reverse)
├── tests/
│   ├── test_discovery.py
│   ├── test_parser.py
│   ├── test_resolver.py
│   └── test_navigation.py
├── .gitignore
└── README.md
```

---

## License

MIT — free to use, modify, and distribute.

---

## Contributing

Contributions are welcome! Please ensure:

- All existing tests pass.
- New tests are added for any new functionality.
- Output remains deterministic.
- No external dependencies are introduced.