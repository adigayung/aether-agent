"""CODE ATLAS — Compact LLM Project Navigation Map.

Standalone, Python-only, deterministic project mapper.
"""

from __future__ import annotations

import json
import os
import sys
from math import ceil

from atlas import __version__
from atlas.discovery import discover_project, DiscoveryResult
from atlas.config import DEFAULT_OUTPUT_NAME
from atlas.parser import parse_project, ParseDiagnostic
from atlas.resolver import resolve_relationships
from atlas.navigation import NavigationIndex

PROG = "atlas.py"


def _fail(message: str) -> int:
    """Print error to stderr and return non-zero exit code."""
    print(f"error: {message}", file=sys.stderr)
    return 1


def _print_usage() -> None:
    print(f"usage: {PROG} <project_path> <output_path>")
    print()
    print("positional arguments:")
    print("  project_path  path to the project directory to map")
    print("  output_path   path where the JSON atlas will be written")


def _make_output(
    discovery: DiscoveryResult,
    modules: dict[str, dict],
    symbols: dict[str, dict],
    diagnostics: list[ParseDiagnostic],
    relationships: dict,
    entrypoints: list[str] | None = None,
) -> dict:
    """Build the final output dictionary.

    Only includes fields that help LLM navigation.
    Parser diagnostics and unresolved references are excluded —
    they are low-value metadata that do not aid navigation.
    """
    output = discovery.to_dict()
    output["modules"] = modules
    output["symbols"] = symbols

    # Merge relationships — always present, even if empty
    output["imports"] = relationships.get("imports", [])
    output["inherits"] = relationships.get("inherits", [])
    output["calls"] = relationships.get("calls", [])

    if entrypoints is not None:
        output["entrypoints"] = entrypoints

    return output


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for CODE ATLAS."""
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv:
        _print_usage()
        return _fail("missing required arguments: project_path and output_path")

    if len(argv) < 2:
        _print_usage()
        return _fail("missing required argument: output_path")

    if len(argv) > 2:
        _print_usage()
        return _fail("too many arguments")

    project_path, output_path = argv[0], argv[1]

    try:
        # Step 1: Discover files
        discovery = discover_project(project_path)

        # Step 2: Filter .py files for parsing
        py_files = [f for f in discovery.files if f.endswith((".py", ".pyi"))]

        # Step 3: Parse and extract symbols
        modules, symbols, diagnostics = parse_project(
            discovery.root, py_files, discovery.name,
        )

        # Step 4: Resolve relationships
        relationships = resolve_relationships(
            discovery.root,
            py_files,
            modules,
            symbols,
            discovery.name,
        )

        # Step 5: Build final output
        nav = NavigationIndex(modules, symbols, relationships, discovery)
        entrypoints = nav.get_entrypoints()
        output = _make_output(
            discovery, modules, symbols, diagnostics, relationships,
            entrypoints=entrypoints,
        )

    except (FileNotFoundError, NotADirectoryError) as exc:
        return _fail(str(exc))

    # Serialize to compact JSON (no indent) for minimal token usage
    json_str = json.dumps(output, indent=None, sort_keys=False,
                          ensure_ascii=True, separators=(",", ":"))

    try:
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(json_str)
    except OSError as exc:
        return _fail(f"could not write output file '{output_path}': {exc}")

    # Print concise benchmark summary
    total_symbols = len(symbols)
    total_relationships = (
        len(output.get("imports", []))
        + len(output.get("inherits", []))
        + len(output.get("calls", []))
    )
    est_tokens = ceil(len(json_str) / 4)

    print("CODE ATLAS")
    print()
    print(f"Project: {discovery.name}")
    print(f"Files: {len(discovery.files)}")
    print(f"Modules: {len(modules)}")
    print(f"Symbols: {total_symbols}")
    print(f"Relationships: {total_relationships}")
    print()
    print(f"JSON: {len(json_str.encode('utf-8'))} bytes")
    print(f"Estimated tokens: {est_tokens}")
    print()
    print(f"Output: {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())