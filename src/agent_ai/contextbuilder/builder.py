"""ContextBuilder: membangun context relevan untuk LLM.

Menggabungkan:
    - user task
    - Code Index (symbol/file relevan)
    - source files (hanya yang relevan)
    - Project Intelligence / Bible
    - Brain context (bila tersedia)

Prinsip:
    - Provider-agnostic.
    - Read-only terhadap source project.
    - Tidak menjalankan tools.
    - Tidak menyimpan state.
    - Deterministic (input sama -> output sama).
    - Batas jumlah file/bytes agar context tidak meledak.
    - Tidak ada embeddings/vector DB/SQLite/MCP/web/RAG.
    - Tidak melakukan full-project dump.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Set

from agent_ai.codeindex.index import CodeIndex
from agent_ai.codeindex.models import Symbol
from agent_ai.contextbuilder.models import ContextRequest, ContextResult, RelevantFile

# Token minimal yang diabaikan saat mengekstrak kata kunci dari task.
# Sengaja hanya kata sambung/filler murni; kata seperti "add"/"fix"/"test"
# TIDAK diabaikan karena bisa jadi nama symbol yang dicari.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with",
    "please", "make", "perbaiki", "tambahkan", "buat",
    "dan", "yang", "di", "ke", "pada", "agar", "supaya", "pastikan", "berhasil",
    "jalankan", "coba", "tolong", "ini", "itu", "dengan", "untuk",
}

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


class ContextBuilder:
    """Membangun context terstruktur dari task + Code Index + knowledge.

    Args:
        index: CodeIndex yang sudah dibangun (sumber struktur kode).
        root: project root (untuk membaca source file relevan).
        brain: ProjectBrain opsional (sumber Brain context).
    """

    def __init__(
        self,
        index: CodeIndex,
        root: Path,
        brain: Optional[object] = None,
        dependency_query: Optional[object] = None,
        retrieval: Optional[object] = None,
        budget: Optional[object] = None,
        partial_reader: Optional[object] = None,
        read_tracker: Optional[object] = None,
        compactor: Optional[object] = None,
    ) -> None:
        self.index = index
        self.root = Path(root).resolve()
        self.brain = brain
        # Repository Intelligence v2 (opsional): perluasan dependency bounded.
        self.dependency_query = dependency_query
        # Advanced Context / Token Budgeting (#40), semua opsional.
        self.retrieval = retrieval
        self.budget = budget
        self.partial_reader = partial_reader
        self.read_tracker = read_tracker
        self.compactor = compactor

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def build(self, request: ContextRequest) -> ContextResult:
        """Bangun context terstruktur untuk sebuah request (deterministik)."""
        keywords = self._keywords(request.task)

        # 1) Cari symbol relevan via Code Index.
        matched_symbols = self._match_symbols(keywords)

        # 2) Pilih file relevan (dedup, urut skor lalu path).
        selected = self._select_files(matched_symbols, keywords, request.max_files)

        # 2b) Perluasan dependency bounded (Repository Intelligence v2, opsional).
        expanded = self._expand_dependencies(selected, request.max_files)

        # 3) Ambil source hanya dari file relevan (batas bytes).
        files, used_bytes = self._extract_sources(expanded, request.max_bytes)

        # 4) Project Intelligence / Bible.
        intelligence = ""
        if request.include_intelligence and self.brain is not None:
            intelligence = self._read_intelligence(
                request.intelligence_categories, request.task
            )

        # 5) Brain context.
        brain_text = ""
        if request.include_brain and self.brain is not None:
            brain_text = self._read_brain(request.task)

        symbols_out = [
            {
                "name": s.name,
                "kind": s.kind.value,
                "file": s.file,
                "line": s.line,
                "parent": s.parent,
            }
            for s in matched_symbols
        ]

        metadata = {
            "keywords": keywords,
            "files_selected": len(files),
            "files_with_source": sum(1 for f in files if f.source is not None),
            "symbols_matched": len(matched_symbols),
            "bytes_used": used_bytes,
            "max_files": request.max_files,
            "max_bytes": request.max_bytes,
            "has_intelligence": bool(intelligence),
            "has_brain": bool(brain_text),
            "dependency_expansion": self.dependency_query is not None,
        }

        return ContextResult(
            task=request.task,
            intelligence=intelligence,
            brain=brain_text,
            files=files,
            symbols=symbols_out,
            metadata=metadata,
        )

    # ------------------------------------------------------------------ #
    # Advanced build (Advanced Context / Token Budgeting #40)
    # ------------------------------------------------------------------ #
    def build_advanced(
        self,
        request: ContextRequest,
        changes: Optional[List[object]] = None,
        observations: Optional[List[str]] = None,
    ) -> ContextResult:
        """Bangun context dengan retrieval profile + budget (opsional).

        Bila `retrieval`/`budget` tidak tersedia, fallback ke `build()` biasa
        (backward compatible). Fitur yang diaktifkan bergantung pada objek
        yang di-inject:
            - retrieval   : IntelligentRetriever (pemilihan + expansion bounded)
            - budget      : ContextBudget (batas files/bytes/tokens)
            - partial_reader : PartialReader (partial reading)
            - read_tracker   : ReadTracker (duplicate-read prevention)
            - compactor      : ContextCompactor (compaction saat mendekati budget)
        """
        if self.retrieval is None or self.budget is None:
            return self.build(request)

        budget = self.budget
        budget.reset()

        # 1) Retrieval relevan (bounded) via RepositoryIntelligenceV2.
        retrieved = self.retrieval.retrieve(request.task, budget.budget)

        # 2) Susun file relevan + partial reading + duplicate-read prevention.
        files: List[RelevantFile] = []
        for item in retrieved:
            if budget.is_exhausted():
                break
            relevant = RelevantFile(
                path=item.path,
                language=item.language,
                score=int(item.score),
                reason=item.reason,
            )
            self._fill_source(relevant, item, budget)
            files.append(relevant)

        # 3) Knowledge sources.
        intelligence = ""
        if request.include_intelligence and self.brain is not None:
            intelligence = self._read_intelligence(
                request.intelligence_categories, request.task
            )
        brain_text = ""
        if request.include_brain and self.brain is not None:
            brain_text = self._read_brain(request.task)

        symbols_out = [
            {"name": s.name, "kind": s.kind.value, "file": s.file, "line": s.line, "parent": s.parent}
            for s in self._match_symbols(self._keywords(request.task))
        ]

        metadata = {
            "keywords": self._keywords(request.task),
            "files_selected": len(files),
            "files_with_source": sum(1 for f in files if f.source is not None),
            "symbols_matched": len(symbols_out),
            "bytes_used": budget.usage.bytes_used,
            "tokens_estimated": budget.usage.tokens_used,
            "max_files": budget.budget.max_files,
            "max_bytes": budget.budget.max_bytes,
            "max_tokens": budget.budget.max_tokens,
            "profile": getattr(budget.budget, "max_files", None) is not None,
            "has_intelligence": bool(intelligence),
            "has_brain": bool(brain_text),
            "advanced": True,
            "partial_read": budget.budget.partial_read,
            "duplicate_read_prevention": budget.budget.duplicate_read_prevention,
        }

        result = ContextResult(
            task=request.task,
            intelligence=intelligence,
            brain=brain_text,
            files=files,
            symbols=symbols_out,
            metadata=metadata,
        )

        # 4) Compaction bila mendekati budget atau budget sudah habis.
        if self.compactor is not None and (budget.near_limit() or budget.is_exhausted()):
            compact = self.compactor.compact(
                task=request.task,
                files=files,
                symbols=symbols_out,
                changes=changes,
                observations=observations,
                max_tokens=budget.budget.max_tokens,
            )
            result.metadata["compacted"] = True
            result.metadata["compaction"] = compact.to_dict()
        else:
            result.metadata["compacted"] = False

        return result

    def _fill_source(self, relevant: RelevantFile, item: object, budget: object) -> None:
        """Isi source untuk satu file: dedup + partial read + budget."""
        path = relevant.path

        # Duplicate-read prevention: file tidak berubah -> jangan kirim ulang.
        if (
            self.read_tracker is not None
            and budget.budget.duplicate_read_prevention
            and self.read_tracker.is_unchanged(path)
        ):
            relevant.reason = f"{relevant.reason} (unchanged, tidak dikirim ulang)"
            return

        # Partial reading: baca bagian relevan (symbol/line range) bila ada.
        source: Optional[str] = None
        if (
            self.partial_reader is not None
            and budget.budget.partial_read
            and getattr(item, "symbols", None)
        ):
            symbol = item.symbols[0]
            partial = self.partial_reader.read_symbol(
                path,
                symbol_line=symbol.get("line", 1),
                limit=budget.budget.partial_read_limit,
            )
            if partial is not None:
                source = partial.content
                relevant.truncated = partial.truncated

        # Fallback: baca file penuh (dengan batas bytes).
        if source is None:
            try:
                source = (self.root / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return

        # Terapkan batas bytes/token.
        encoded = source.encode("utf-8")
        remaining = budget.remaining_bytes()
        if len(encoded) > remaining:
            source = encoded[:remaining].decode("utf-8", errors="ignore")
            relevant.truncated = True
        if not budget.can_add_tokens(budget.estimator.estimate(source)):
            return

        relevant.source = source
        budget.account(source)

        # Catat untuk duplicate-read prevention.
        if self.read_tracker is not None and budget.budget.duplicate_read_prevention:
            self.read_tracker.record(path, source, tokens=budget.estimator.estimate(source))

    # ------------------------------------------------------------------ #
    # Keyword extraction
    # ------------------------------------------------------------------ #
    @staticmethod
    def _keywords(task: str) -> List[str]:
        """Ekstrak kata kunci dari task (deterministik, urut kemunculan)."""
        seen: Set[str] = set()
        keywords: List[str] = []
        for match in _WORD_RE.finditer(task or ""):
            word = match.group(0)
            lower = word.lower()
            if lower in _STOPWORDS or lower in seen:
                continue
            seen.add(lower)
            keywords.append(word)
        return keywords

    # ------------------------------------------------------------------ #
    # Symbol matching
    # ------------------------------------------------------------------ #
    def _match_symbols(self, keywords: List[str]) -> List[Symbol]:
        """Cari symbol yang namanya cocok dengan keyword (deterministik)."""
        matched: List[Symbol] = []
        seen: Set[tuple] = set()
        for keyword in keywords:
            for symbol in self.index.find_symbols(name=keyword):
                key = (symbol.file, symbol.line, symbol.name)
                if key in seen:
                    continue
                seen.add(key)
                matched.append(symbol)
        # Urut deterministik: file, lalu line.
        matched.sort(key=lambda s: (s.file, s.line, s.name))
        return matched

    # ------------------------------------------------------------------ #
    # File selection
    # ------------------------------------------------------------------ #
    def _select_files(
        self,
        symbols: List[Symbol],
        keywords: List[str],
        max_files: int,
    ) -> List[RelevantFile]:
        """Pilih file relevan berdasarkan symbol + keyword (dedup, skor)."""
        scores: dict = {}
        reasons: dict = {}

        # Skor dari symbol yang cocok.
        for symbol in symbols:
            scores[symbol.file] = scores.get(symbol.file, 0) + 3
            reasons.setdefault(symbol.file, f"symbol '{symbol.name}'")

        # Skor tambahan bila nama file mengandung keyword.
        for entry in self.index.files:
            name = entry.path.lower()
            for keyword in keywords:
                if keyword.lower() in name:
                    scores[entry.path] = scores.get(entry.path, 0) + 1
                    reasons.setdefault(entry.path, f"path cocok '{keyword}'")

        # Urut: skor desc, lalu path asc (deterministik).
        ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))

        selected: List[RelevantFile] = []
        for path, score in ordered[:max_files]:
            entry = self.index.file(path)
            language = entry.language if entry else "unknown"
            selected.append(
                RelevantFile(
                    path=path,
                    language=language,
                    score=score,
                    reason=reasons.get(path, "relevan"),
                )
            )
        return selected

    # ------------------------------------------------------------------ #
    # Dependency expansion (Repository Intelligence v2)
    # ------------------------------------------------------------------ #
    def _expand_dependencies(
        self,
        selected: List[RelevantFile],
        max_files: int,
    ) -> List[RelevantFile]:
        """Perluas file terpilih dengan dependency relevan (bounded).

        Memakai `dependency_query` (RelevantDependencyQuery) bila tersedia.
        Hasil tetap dibatasi `max_files` dan deterministik. Bila tidak ada
        dependency_query, kembalikan `selected` apa adanya.
        """
        if self.dependency_query is None or not selected:
            return selected

        existing = {f.path for f in selected}
        additions: List[RelevantFile] = []
        for relevant in selected:
            if len(selected) + len(additions) >= max_files:
                break
            try:
                result = self.dependency_query.for_file(
                    relevant.path, max_depth=1, max_files=max_files
                )
            except Exception:  # noqa: BLE001 - expansion error tidak boleh crash
                continue
            for dep_path in getattr(result, "dependencies", []) or []:
                if dep_path in existing:
                    continue
                if len(selected) + len(additions) >= max_files:
                    break
                existing.add(dep_path)
                entry = self.index.file(dep_path)
                additions.append(
                    RelevantFile(
                        path=dep_path,
                        language=entry.language if entry else "unknown",
                        score=1,
                        reason=f"dependency dari '{relevant.path}'",
                    )
                )
        return selected + additions

    # ------------------------------------------------------------------ #
    # Source extraction
    # ------------------------------------------------------------------ #
    def _extract_sources(
        self,
        files: List[RelevantFile],
        max_bytes: int,
    ) -> tuple:
        """Baca source file relevan dengan batas total bytes (deterministik)."""
        used = 0
        for relevant in files:
            if used >= max_bytes:
                break
            path = self.root / relevant.path
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            encoded = source.encode("utf-8")
            remaining = max_bytes - used
            if len(encoded) > remaining:
                # Potong pada batas karakter yang aman.
                source = encoded[:remaining].decode("utf-8", errors="ignore")
                relevant.truncated = True
            relevant.source = source
            used += len(source.encode("utf-8"))
        return files, used

    # ------------------------------------------------------------------ #
    # Knowledge sources
    # ------------------------------------------------------------------ #
    def _read_intelligence(self, categories: Optional[List[str]], query: str = "") -> str:
        """Baca Project Intelligence/Bible via brain (read-only).

        Bila pemanggil TIDAK membatasi kategori, knowledge dipilih dengan
        retrieval berbasis relevance (komponen yang sama dipakai Agent &
        Consultant) supaya Bible tidak dikirim utuh. Kategori eksplisit dari
        pemanggil tetap dihormati apa adanya (filter category-filtered lama).
        """
        if categories:
            try:
                ctx = self.brain.get_context(categories=categories)
            except Exception:  # noqa: BLE001 - knowledge error tidak boleh crash
                return ""
            return getattr(ctx, "text", "") or ""
        return self._retrieve_knowledge(query)

    def _read_brain(self, query: str = "") -> str:
        """Baca Brain context (read-only) dengan retrieval relevance-based."""
        return self._retrieve_knowledge(query)

    def _retrieve_knowledge(self, query: str) -> str:
        """Knowledge Bible untuk context: retrieval dulu, fallback jalur lama.

        Retrieval memakai `BibleRetriever` (relevance + token budget). Bila
        sumber tidak menyediakan akses terstruktur (brain duck-typed), jalur
        lama `brain.get_context()` dipakai supaya tetap backward compatible.
        """
        try:
            from agent_ai.projects.retrieval import BibleRetriever

            result = BibleRetriever(self.brain).retrieve(query)
            if result is not None:
                return result.text
        except Exception:  # noqa: BLE001 - retrieval error -> fallback jalur lama
            pass
        try:
            ctx = self.brain.get_context()
        except Exception:  # noqa: BLE001 - knowledge error tidak boleh crash
            return ""
        return getattr(ctx, "text", "") or ""
