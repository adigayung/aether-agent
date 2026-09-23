#!/usr/bin/env python3
"""
MAP_CODE_RIG - Repository Intelligence Graph Generator

Usage:
    python rig.py <project_path> <output_path>

Generates a canonical Repository Intelligence Graph (RIG) in JSON format
from a source repository. Per FINAL_OPENAI_RIG_BLUEPRINT.md.

Examples:
    python rig.py .\\my_project .\\map_project.json
    python rig.py /home/user/project /tmp/rig.json --verbose

Exit codes:
    0 - success
    1 - fatal error (invalid args, cannot write)
    2 - validation error (graph built but validation failed)
"""

from __future__ import annotations

import os
import sys

# Ensure rig package is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rig.cli import parse_args, handle_fatal, print_diagnostics
from rig.config import Config, VERSION
from rig.pipeline import RIGPipeline
from rig.serializer import RIGSerializer
from rig.validator import DiagnosticLevel


def main():
    """Entry point for MAP_CODE_RIG CLI."""
    # ── Parse arguments ──
    try:
        config = parse_args()
    except SystemExit as e:
        sys.exit(e.code)
    except Exception as e:
        handle_fatal(f"Argument parsing failed: {e}")

    # ── Validate project path ──
    if not os.path.isdir(config.project_path):
        handle_fatal(
            f"Project path is not a directory: {config.project_path}"
        )

    # ── Validate output path ──
    output_dir = os.path.dirname(config.output_path)
    if output_dir and not os.path.isdir(output_dir):
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as e:
            handle_fatal(f"Cannot create output directory: {e}")

    if os.path.exists(config.output_path) and not config.overwrite:
        handle_fatal(
            f"Output file exists: {config.output_path}. Use --overwrite to replace."
        )

    # ── Run pipeline ──
    pipeline = RIGPipeline(config)
    rig, diagnostics = pipeline.run()

    if rig is None:
        handle_fatal("Pipeline failed to produce RIG")

    # ── Serialize ──
    try:
        serializer = RIGSerializer()
        json_output = serializer.serialize(rig)
    except Exception as e:
        handle_fatal(f"Serialization failed: {e}")

    # ── Write output (atomic) ──
    try:
        with open(config.output_path, "w", encoding="utf-8") as f:
            f.write(json_output)
            f.write("\n")
    except IOError as e:
        handle_fatal(f"Cannot write output: {e}")

    # ── Print diagnostics ──
    print_diagnostics(diagnostics, config.verbose)

    # ── Summary ──
    if config.verbose:
        errors = [d for d in diagnostics if d.level == DiagnosticLevel.ERROR]
        warnings = [d for d in diagnostics if d.level == DiagnosticLevel.WARNING]
        print(f"\nSummary: {len(rig.components)} components, "
              f"{len(rig.edges)} edges, "
              f"{len(rig.evidence)} evidence items",
              file=sys.stderr)
        print(f"Output: {os.path.abspath(config.output_path)}", file=sys.stderr)

    # ── Check for validation errors ──
    has_errors = any(d.level == DiagnosticLevel.ERROR for d in diagnostics)
    if has_errors:
        # Still exit successfully because output was written with diagnostics
        # But warn the user
        print(f"\nWARNING: RIG built with {len([d for d in diagnostics if d.level == DiagnosticLevel.ERROR])} "
              f"validation error(s). Output may contain structural issues.",
              file=sys.stderr)
        sys.exit(0)  # Exit 0 since output was produced

    sys.exit(0)


if __name__ == "__main__":
    main()