"""
Evidence collection and management for RIG.

Per blueprint section 15: every node and edge MUST have ≥1 evidence reference.
Evidence MAY come from:
- file + line range
- build-system call stack
- machine-readable build artifact
- machine-readable test artifact
- manifest/lockfile
- generated metadata

Evidence rules (section 15):
1. No fabricated line number
2. No fake file:1 placeholder
3. Evidence path MUST be repository-relative
4. External toolchain evidence != repository source evidence
5. Secret-bearing snippets MUST be omitted/redacted
6. Evidence identity MUST be deterministic
"""

from __future__ import annotations

import os
from typing import List, Optional

from rig.identity import evidence_id, normalize_path
from rig.models import Evidence, EvidenceLocatorKind


class EvidenceCollector:
    """Collects evidence references during extraction.

    Evidence can come from multiple sources and is deduplicated
    by deterministic evidence ID.
    """

    def __init__(self, repo_root: str):
        self._repo_root = os.path.abspath(repo_root)
        self._items: dict[str, Evidence] = {}

    def _make_relative(self, path: str) -> str:
        """Convert absolute path to repo-relative path."""
        abs_path = os.path.abspath(path)
        try:
            rel = os.path.relpath(abs_path, self._repo_root)
        except ValueError:
            # Different drive on Windows; use path as-is
            rel = abs_path
        return normalize_path(rel)

    def add_file_evidence(
        self,
        file_path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        text_snippet: Optional[str] = None,
        source_ref: Optional[str] = None,
    ) -> Evidence:
        """Add evidence pointing to a source file location."""
        rel_path = self._make_relative(file_path)

        # Validate: no fabricated line numbers (rule 1, 2)
        eid = evidence_id(rel_path, start_line, end_line)

        if eid in self._items:
            existing = self._items[eid]
            if text_snippet and existing.text_snippet is None:
                existing.text_snippet = text_snippet[:500]  # Keep bounded
            return existing

        ev = Evidence(
            id=eid,
            locator_kind=EvidenceLocatorKind.FILE,
            file_path=rel_path,
            start_line=start_line,
            end_line=end_line,
            text_snippet=text_snippet[:500] if text_snippet else None,
            source_ref=source_ref,
        )
        self._items[eid] = ev
        return ev

    def add_build_artifact_evidence(
        self,
        artifact_path: str,
        call_stack: Optional[str] = None,
        text_snippet: Optional[str] = None,
    ) -> Evidence:
        """Add evidence from a build artifact (CMake File API, etc.)."""
        rel_path = self._make_relative(artifact_path)
        eid = evidence_id(rel_path, call_stack=call_stack)

        if eid in self._items:
            existing = self._items[eid]
            if text_snippet and existing.text_snippet is None:
                existing.text_snippet = text_snippet[:500]
            return existing

        ev = Evidence(
            id=eid,
            locator_kind=EvidenceLocatorKind.BUILD_ARTIFACT,
            file_path=rel_path,
            call_stack=call_stack,
            text_snippet=text_snippet[:500] if text_snippet else None,
        )
        self._items[eid] = ev
        return ev

    def add_manifest_evidence(
        self,
        manifest_path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        text_snippet: Optional[str] = None,
    ) -> Evidence:
        """Add evidence from a manifest/lockfile."""
        rel_path = self._make_relative(manifest_path)
        eid = evidence_id(rel_path, start_line, end_line)

        if eid in self._items:
            existing = self._items[eid]
            if text_snippet and existing.text_snippet is None:
                existing.text_snippet = text_snippet[:500]
            return existing

        ev = Evidence(
            id=eid,
            locator_kind=EvidenceLocatorKind.MANIFEST,
            file_path=rel_path,
            start_line=start_line,
            end_line=end_line,
            text_snippet=text_snippet[:500] if text_snippet else None,
        )
        self._items[eid] = ev
        return ev

    def add_lockfile_evidence(
        self,
        lockfile_path: str,
        entry_key: Optional[str] = None,
        text_snippet: Optional[str] = None,
    ) -> Evidence:
        """Add evidence from a lockfile entry."""
        rel_path = self._make_relative(lockfile_path)
        eid = evidence_id(rel_path, call_stack=entry_key)

        if eid in self._items:
            existing = self._items[eid]
            if text_snippet and existing.text_snippet is None:
                existing.text_snippet = text_snippet[:500]
            return existing

        ev = Evidence(
            id=eid,
            locator_kind=EvidenceLocatorKind.LOCKFILE,
            file_path=rel_path,
            call_stack=entry_key,
            text_snippet=text_snippet[:500] if text_snippet else None,
        )
        self._items[eid] = ev
        return ev

    def add_call_stack_evidence(
        self,
        file_path: str,
        call_stack: str,
        text_snippet: Optional[str] = None,
    ) -> Evidence:
        """Add evidence from a build-system call stack (e.g., CMake backtrace)."""
        rel_path = self._make_relative(file_path)
        eid = evidence_id(rel_path, call_stack=call_stack)

        if eid in self._items:
            existing = self._items[eid]
            if text_snippet and existing.text_snippet is None:
                existing.text_snippet = text_snippet[:500]
            return existing

        ev = Evidence(
            id=eid,
            locator_kind=EvidenceLocatorKind.CALL_STACK,
            file_path=rel_path,
            call_stack=call_stack,
            text_snippet=text_snippet[:500] if text_snippet else None,
        )
        self._items[eid] = ev
        return ev

    def get_all(self) -> List[Evidence]:
        """Return all collected evidence, sorted by ID for determinism."""
        return sorted(self._items.values(), key=lambda e: e.id)

    def get(self, evidence_id: str) -> Optional[Evidence]:
        return self._items.get(evidence_id)