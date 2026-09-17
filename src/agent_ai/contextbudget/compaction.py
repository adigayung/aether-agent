"""Context Compaction: ringkas context saat mendekati budget.

Mempertahankan informasi penting:
    - task
    - relevant files (path + alasan, tanpa source penuh bila perlu)
    - symbols
    - changes
    - errors/observations penting
    - dependency information

TIDAK menyimpan/menghasilkan chain-of-thought. Deterministik.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent_ai.contextbudget.budget import TokenEstimator


@dataclass
class CompactContext:
    """Context ringkas hasil compaction.

    Attributes:
        task: task/request.
        files: daftar file (path + alasan + symbol), tanpa source penuh.
        symbols: symbol penting.
        changes: ringkasan perubahan (path + jenis).
        observations: error/observasi penting.
        dependencies: informasi dependency (path -> deps).
        dropped_files: file yang di-drop karena budget.
        metadata: info tambahan.
    """

    task: str = ""
    files: List[Dict[str, Any]] = field(default_factory=list)
    symbols: List[Dict[str, Any]] = field(default_factory=list)
    changes: List[Dict[str, Any]] = field(default_factory=list)
    observations: List[str] = field(default_factory=list)
    dependencies: Dict[str, List[str]] = field(default_factory=dict)
    dropped_files: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "files": self.files,
            "symbols": self.symbols,
            "changes": self.changes,
            "observations": self.observations,
            "dependencies": self.dependencies,
            "dropped_files": self.dropped_files,
            "metadata": self.metadata,
        }

    def to_text(self) -> str:
        """Render context ringkas menjadi teks (provider-agnostic)."""
        lines: List[str] = ["# Task", self.task or "(kosong)"]

        lines.append("\n# Relevant Files")
        if self.files:
            for f in self.files:
                lines.append(f"- {f.get('path')} ({f.get('reason', 'relevan')})")
        else:
            lines.append("(tidak ada)")

        lines.append("\n# Symbols")
        if self.symbols:
            for s in self.symbols:
                parent = f"{s['parent']}." if s.get("parent") else ""
                lines.append(f"- {s.get('kind')} {parent}{s.get('name')} @ {s.get('file')}:{s.get('line')}")
        else:
            lines.append("(tidak ada)")

        if self.changes:
            lines.append("\n# Changes")
            for c in self.changes:
                lines.append(f"- {c.get('change_type')} {c.get('path')}")

        if self.dependencies:
            lines.append("\n# Dependencies")
            for path, deps in self.dependencies.items():
                lines.append(f"- {path} -> {', '.join(deps) if deps else '(none)'}")

        if self.observations:
            lines.append("\n# Observations")
            for obs in self.observations:
                lines.append(f"- {obs}")

        return "\n".join(lines)


class ContextCompactor:
    """Menghasilkan context ringkas dari context penuh.

    Args:
        estimator: TokenEstimator opsional.
    """

    def __init__(self, estimator: Optional[TokenEstimator] = None) -> None:
        self.estimator = estimator or TokenEstimator()

    def compact(
        self,
        task: str,
        files: List[Any],
        symbols: Optional[List[Dict[str, Any]]] = None,
        changes: Optional[List[Any]] = None,
        observations: Optional[List[str]] = None,
        dependencies: Optional[Dict[str, List[str]]] = None,
        max_tokens: Optional[int] = None,
    ) -> CompactContext:
        """Bangun CompactContext (tanpa source penuh).

        Args:
            task: task/request.
            files: daftar file (objek dengan atribut path/reason/symbols, atau dict).
            symbols: symbol penting.
            changes: daftar perubahan (objek dengan path/change_type, atau dict).
            observations: error/observasi penting.
            dependencies: informasi dependency.
            max_tokens: bila diisi, drop file sampai estimasi <= max_tokens.
        """
        compact_files: List[Dict[str, Any]] = []
        for f in files or []:
            compact_files.append(self._file_summary(f))

        compact_changes: List[Dict[str, Any]] = []
        for c in changes or []:
            compact_changes.append(self._change_summary(c))

        result = CompactContext(
            task=task,
            files=compact_files,
            symbols=list(symbols or []),
            changes=compact_changes,
            observations=list(observations or []),
            dependencies=dict(dependencies or {}),
        )

        # Drop file bila melebihi max_tokens (deterministik: dari belakang).
        if max_tokens is not None:
            while result.files and self.estimator.estimate(result.to_text()) > max_tokens:
                dropped = result.files.pop()
                result.dropped_files.append(dropped.get("path", ""))

        result.metadata["estimated_tokens"] = self.estimator.estimate(result.to_text())
        result.metadata["files_kept"] = len(result.files)
        result.metadata["files_dropped"] = len(result.dropped_files)
        return result

    @staticmethod
    def _file_summary(f: Any) -> Dict[str, Any]:
        if isinstance(f, dict):
            return {
                "path": f.get("path"),
                "reason": f.get("reason", "relevan"),
                "symbols": f.get("symbols", []),
            }
        return {
            "path": getattr(f, "path", None),
            "reason": getattr(f, "reason", "relevan"),
            "symbols": getattr(f, "symbols", []),
        }

    @staticmethod
    def _change_summary(c: Any) -> Dict[str, Any]:
        if isinstance(c, dict):
            return {"path": c.get("path"), "change_type": c.get("change_type")}
        change_type = getattr(c, "change_type", None)
        return {
            "path": getattr(c, "path", None),
            "change_type": getattr(change_type, "value", change_type),
        }
