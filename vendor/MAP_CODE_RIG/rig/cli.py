"""
Command-line interface for MAP_CODE_RIG.

Per blueprint section 29:
Canonical command: python create_rig_json.py <project_path> <output_json>
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from rig.config import Config, VERSION
from rig.models import Diagnostic, DiagnosticLevel


def parse_args(argv: Optional[List[str]] = None) -> Config:
    """Parse command-line arguments into a Config object."""
    parser = argparse.ArgumentParser(
        prog="rig.py",
        description="MAP_CODE_RIG - Repository Intelligence Graph Generator",
        epilog=(
            "Generates a canonical Repository Intelligence Graph (RIG) "
            "from a source repository."
        ),
    )

    parser.add_argument(
        "project_path",
        type=str,
        help="Path to the repository/project to analyze",
    )

    parser.add_argument(
        "output_path",
        type=str,
        help="Path for the output JSON RIG file",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=False,
        help="Overwrite output file if it exists",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Enable verbose diagnostic output",
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )

    args = parser.parse_args(argv)

    config = Config(
        project_path=args.project_path,
        output_path=args.output_path,
        verbose=args.verbose,
        overwrite=args.overwrite,
    )

    return config


def handle_fatal(error_message: str, exit_code: int = 1) -> None:
    """Handle a fatal error - print to stderr and exit."""
    print(f"ERROR: {error_message}", file=sys.stderr)
    sys.exit(exit_code)


def print_diagnostics(diagnostics: List[Diagnostic], verbose: bool) -> None:
    """Print diagnostics to stderr."""
    if not diagnostics and not verbose:
        return

    for diag in sorted(diagnostics, key=lambda d: (d.level, d.message)):
        level_tag = f"[{diag.level.upper()}]" if diag.level else "[INFO]"
        msg = f"{level_tag} {diag.message}"
        if diag.source:
            msg += f" (source: {diag.source})"
        if diag.entity_id:
            msg += f" (entity: {diag.entity_id})"

        if diag.level == DiagnosticLevel.ERROR:
            print(msg, file=sys.stderr)
        elif verbose:
            print(msg, file=sys.stderr)