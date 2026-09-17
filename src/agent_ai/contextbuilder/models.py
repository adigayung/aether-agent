"""Model untuk Context Builder.

Representasi request + hasil context terstruktur (deterministik, plain data).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ContextRequest:
    """Permintaan pembangunan context.

    Attributes:
        task: task/request utama dari user.
        max_files: batas jumlah file relevan.
        max_bytes: batas total bytes source yang disertakan.
        include_intelligence: sertakan Project Intelligence/Bible.
        include_brain: sertakan Brain context bila tersedia.
        intelligence_categories: kategori intelligence opsional.
    """

    task: str
    max_files: int = 8
    max_bytes: int = 60_000
    include_intelligence: bool = True
    include_brain: bool = True
    intelligence_categories: Optional[List[str]] = None


@dataclass
class RelevantFile:
    """File relevan yang dipilih beserta alasan + source (bila disertakan).

    Attributes:
        path: path file relatif terhadap project root.
        language: bahasa file.
        score: skor relevansi (deterministik).
        reason: alasan singkat pemilihan.
        source: isi source (None bila tidak disertakan karena limit).
        truncated: True bila source dipotong karena limit bytes.
    """

    path: str
    language: str
    score: int
    reason: str
    source: Optional[str] = None
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "language": self.language,
            "score": self.score,
            "reason": self.reason,
            "truncated": self.truncated,
            "included": self.source is not None,
        }


@dataclass
class ContextResult:
    """Context terstruktur hasil Context Builder.

    Attributes:
        task: task/request.
        intelligence: teks Project Intelligence/Bible (bila disertakan).
        brain: teks Brain context (bila disertakan).
        files: daftar file relevan (dengan source bila disertakan).
        symbols: symbol relevan (file + line).
        metadata: info batas/limit + statistik.
    """

    task: str
    intelligence: str = ""
    brain: str = ""
    files: List[RelevantFile] = field(default_factory=list)
    symbols: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "intelligence": self.intelligence,
            "brain": self.brain,
            "files": [f.to_dict() for f in self.files],
            "symbols": self.symbols,
            "metadata": self.metadata,
        }

    def to_text(self) -> str:
        """Render context menjadi teks terstruktur untuk LLM (provider-agnostic)."""
        lines: List[str] = ["# Task", self.task]

        if self.intelligence:
            lines.append("\n# Project Rules / Intelligence")
            lines.append(self.intelligence)

        if self.brain:
            lines.append("\n# Brain Context")
            lines.append(self.brain)

        lines.append("\n# Relevant Symbols")
        if self.symbols:
            for sym in self.symbols:
                parent = f"{sym['parent']}." if sym.get("parent") else ""
                lines.append(
                    f"- {sym['kind']} {parent}{sym['name']} @ {sym['file']}:{sym['line']}"
                )
        else:
            lines.append("(tidak ada symbol relevan)")

        lines.append("\n# Relevant Files")
        if self.files:
            for f in self.files:
                lines.append(f"\n## {f.path} ({f.language})")
                if f.source is not None:
                    lines.append("```")
                    lines.append(f.source)
                    lines.append("```")
                else:
                    lines.append("(source tidak disertakan: melebihi limit)")
        else:
            lines.append("(tidak ada file relevan)")

        lines.append("\n# Metadata")
        for key, value in self.metadata.items():
            lines.append(f"- {key}: {value}")

        return "\n".join(lines)
