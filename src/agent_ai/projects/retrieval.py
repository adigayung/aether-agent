"""Project Bible Retrieval (relevance-based knowledge selection).

Modul ini memilih SEBAGIAN knowledge Project Bible yang paling relevan dengan
task/pertanyaan, lalu memotongnya sesuai token budget. Tujuannya: Bible TIDAK
lagi dikirim UTUH ke LLM secara default.

Alur:

    Task / Question
        -> BibleRetriever.retrieve(query, budget_tokens=...)
             - baca entri Bible (READ-ONLY, lewat ProjectBrain) 
             - skor relevance per kategori + per entri (exact/keyword)
             - ranking (relevance + kepentingan kategori + confidence)
             - fallback bila hasil terlalu sedikit (kategori terkait -> inti)
             - hormati token budget sebagai batas AKHIR
        -> RetrievalResult.text  (dipakai sebagai system message ke LLM)

Prinsip:
    - READ-ONLY: tidak mengubah, menghapus, atau memindahkan knowledge Bible.
    - Bible tetap sumber kebenaran knowledge yang LENGKAP; retrieval hanya
      memilih subset untuk SATU request (bukan memory ringkas).
    - Deterministik: input sama -> output sama (tanpa random, tanpa waktu).
    - Tanpa dependency baru: tanpa embedding/vector DB/DBMS/tokenizer eksternal.
    - Provider-agnostic: hanya menghasilkan teks biasa.
    - Bila sumber knowledge tidak menyediakan akses terstruktur (mis. objek
      brain duck-typed yang hanya punya `get_context()`), `retrieve()`
      mengembalikan None supaya pemanggil memakai jalur lama (backward
      compatible).

Contoh:

    from agent_ai.projects.retrieval import BibleRetriever

    retriever = BibleRetriever(brain)
    result = retriever.retrieve("perbaiki bug login OAuth", budget_tokens=8000)
    text = result.text
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent_ai.projects.models import BIBLE_CATEGORIES

#: Estimasi kasar karakter per token (heuristik sederhana, tanpa tokenizer).
CHARS_PER_TOKEN = 4

#: Anggaran token DEFAULT untuk konteks Bible bila provider tidak melaporkan
#: context window-nya (mis. provider cloud). Ini BATAS AKHIR, bukan mekanisme
#: relevance: relevance ditentukan lebih dulu, budget hanya memotong sisanya.
DEFAULT_BUDGET_TOKENS = 8000

#: Jumlah entry minimal sebelum retrieval melebar ke kategori lain (fallback).
MIN_SELECTED_ENTRIES = 3

#: Kategori yang menurut panduan Bible (`index.md`) paling sering dibutuhkan.
CORE_CATEGORIES = ("facts", "architecture", "conventions")

#: Bobot kepentingan kategori (relevance awal & urutan fallback).
CATEGORY_IMPORTANCE = {
    "facts": 2.6,
    "architecture": 2.6,
    "known_bugs": 2.5,
    "known_gaps": 2.2,
    "conventions": 2.2,
    "problems": 2.1,
    "decisions": 1.9,
    "learnings": 1.8,
    "ui": 1.7,
}
_DEFAULT_IMPORTANCE = 1.0

#: Penanda urutan kanonik kategori Bible (deterministik).
_CATEGORY_ORDER: Dict[str, int] = {name: index for index, name in enumerate(BIBLE_CATEGORIES)}

#: Kata sambung/filler yang diabaikan saat mengekstrak kata kunci query.
#: Kata kerja spesifik ("fix", "add", "auth") SENGAJA tidak diabaikan.
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "into", "are", "was",
        "were", "been", "have", "has", "had", "will", "would", "should", "could",
        "can", "not", "but", "you", "your", "our", "its", "their", "there", "here",
        "what", "which", "when", "where", "how", "why", "who", "all", "any", "some",
        "please", "using", "used", "use",
        "dan", "yang", "untuk", "dengan", "pada", "dari", "ke", "di", "ini", "itu",
        "agar", "supaya", "tolong", "coba", "jalankan", "pastikan", "tidak", "adalah",
        "akan", "sudah", "bisa", "saya", "kamu", "anda", "apakah", "apa", "bagaimana",
        "mengapa", "dimana", "jika", "bila", "serta", "atau", "juga", "masih", "harus",
        "dapat", "ada", "tentang", "hanya", "saja", "secara", "oleh", "tersebut",
        "sebuah", "suatu", "para", "kami", "kita", "mereka", "dia",
    }
)

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

#: Batas jumlah kata kunci yang dipertimbangkan (anti query raksasa).
_MAX_KEYWORDS = 24

#: Batas karakter query yang disimpan di metadata (bukan isi Bible).
_MAX_QUERY_PREVIEW = 160


def _category_order(category: str) -> int:
    """Urutan kanonik kategori (kategori tak dikenal diletakkan di akhir)."""
    return _CATEGORY_ORDER.get(category, len(_CATEGORY_ORDER) + 1)


def query_keywords(query: Optional[str], limit: int = _MAX_KEYWORDS) -> List[str]:
    """Ekstrak kata kunci dari query (deterministik, urut kemunculan).

    Args:
        query: teks task/pertanyaan user.
        limit: batas jumlah kata kunci.

    Returns:
        Daftar kata kunci lowercase (min 3 karakter, tanpa stopword, unik).
    """
    seen: set = set()
    keywords: List[str] = []
    for match in _WORD_RE.finditer(query or ""):
        word = match.group(0).lower()
        if word in _STOPWORDS or word in seen:
            continue
        seen.add(word)
        keywords.append(word)
        if len(keywords) >= max(1, int(limit)):
            break
    return keywords


def estimate_tokens(text: str, chars_per_token: int = CHARS_PER_TOKEN) -> int:
    """Estimasi jumlah token dari panjang teks (heuristik sederhana)."""
    if not text:
        return 0
    per_token = max(1, int(chars_per_token))
    return int(math.ceil(len(text) / per_token))


def _content_text(value: Any) -> str:
    """Ubah content entry (str/dict/list/angka) menjadi teks deterministik."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


@dataclass
class RetrievedEntry:
    """Satu entry Bible yang terpilih (read-only salinan teks)."""

    category: str
    content: str
    score: float = 0.0
    prior: float = 0.0
    matched_terms: List[str] = field(default_factory=list)
    position: int = 0
    #: Asal pemilihan: "entry" (relevance) atau "category"/"core" (fallback).
    source: str = "entry"


@dataclass
class RetrievalResult:
    """Hasil retrieval Bible (teks terpilih + metadata, tanpa isi Bible penuh)."""

    query: str
    text: str
    categories: List[str] = field(default_factory=list)
    selected: List[RetrievedEntry] = field(default_factory=list)
    total_entries: int = 0
    estimated_tokens: int = 0
    budget_tokens: int = DEFAULT_BUDGET_TOKENS
    level: str = "empty"
    truncated: bool = False

    @property
    def selected_count(self) -> int:
        return len(self.selected)

    def to_metadata(self) -> Dict[str, Any]:
        """Metadata ringkas untuk logging/observability (TANPA isi Bible).

        Catatan: nama key SENGAJA tidak memuat kata "token" karena
        `sanitize_payload` (observability existing) meredaksi key sensitif
        berdasarkan substring — key seperti "budget_tokens" akan berubah
        menjadi "[redacted]" sehingga angka anggaran tidak terbaca di log.
        """
        return {
            "query": (self.query or "")[:_MAX_QUERY_PREVIEW],
            "level": self.level,
            "total_entries": self.total_entries,
            "selected": self.selected_count,
            "categories": list(self.categories),
            "budget": self.budget_tokens,
            "used": self.estimated_tokens,
            "used_chars": len(self.text),
            "unit": "tokens",
            "truncated": self.truncated,
        }


class BibleRetriever:
    """Relevance-based retriever untuk Project Bible (read-only).

    Args:
        brain: ProjectBrain (atau objek apa pun yang menyediakan
            `.intelligence.read_category(category)` atau `.read(categories)`).
        categories: batas kategori yang dibaca (None = semua kategori backend).
        max_tokens: override anggaran token default (bila pemanggil tidak
            memberikan `budget_tokens`).
        chars_per_token: estimasi karakter per token.
    """

    def __init__(
        self,
        brain: Any,
        *,
        categories: Optional[Sequence[str]] = None,
        max_tokens: Optional[int] = None,
        chars_per_token: int = CHARS_PER_TOKEN,
    ) -> None:
        self.brain = brain
        self.categories = list(categories) if categories else None
        self.max_tokens = int(max_tokens) if max_tokens else None
        self.chars_per_token = max(1, int(chars_per_token))

    # ------------------------------------------------------------------ #
    # Reading (structured access only)
    # ------------------------------------------------------------------ #
    def _category_names(self) -> List[str]:
        if self.categories:
            return list(self.categories)
        intelligence = getattr(self.brain, "intelligence", None)
        available = getattr(intelligence, "categories", None)
        if available:
            return [str(name) for name in available]
        return list(BIBLE_CATEGORIES)

    def read_entries(self) -> Optional[Dict[str, List[Tuple[str, float]]]]:
        """Baca seluruh entry Bible per kategori (read-only).

        Returns:
            Dict {kategori: [(teks_content, confidence), ...]} atau None bila
            sumber tidak menyediakan akses terstruktur (perlu fallback lama).
        """
        categories = self._category_names()

        intelligence = getattr(self.brain, "intelligence", None)
        read_category = getattr(intelligence, "read_category", None)
        if callable(read_category):
            data: Dict[str, List[Tuple[str, float]]] = {}
            for category in categories:
                items: List[Tuple[str, float]] = []
                try:
                    entries = read_category(category)
                except Exception:  # noqa: BLE001 - kategori rusak dilewati
                    entries = []
                for entry in entries or []:
                    text = _content_text(getattr(entry, "content", entry))
                    if not text.strip():
                        continue
                    confidence = getattr(entry, "confidence", 1.0)
                    try:
                        score = float(confidence)
                    except (TypeError, ValueError):
                        score = 1.0
                    items.append((text, min(1.0, max(0.0, score))))
                data[str(category)] = items
            return data

        reader = getattr(self.brain, "read", None)
        if callable(reader):
            try:
                raw = reader(self.categories) if self.categories else reader()
            except TypeError:
                raw = reader()
            except Exception:  # noqa: BLE001 - sumber gagal -> fallback lama
                return None
            if not isinstance(raw, dict):
                return None
            data = {}
            for category, values in raw.items():
                items = []
                for value in values or []:
                    text = _content_text(value)
                    if text.strip():
                        items.append((text, 1.0))
                data[str(category)] = items
            return data

        return None

    # ------------------------------------------------------------------ #
    # Scoring
    # ------------------------------------------------------------------ #
    @staticmethod
    def _entry_score(
        content: str,
        category: str,
        keywords: Sequence[str],
        confidence: float,
    ) -> Tuple[float, float, List[str]]:
        """Hitung relevance satu entry.

        Returns:
            (relevance, prior, matched_terms) — `relevance` dari kemunculan
            kata kunci, `prior` dari kepentingan kategori & confidence.
        """
        lowered = content.lower()
        matched: List[str] = []
        relevance = 0.0
        for keyword in keywords:
            occurrences = lowered.count(keyword)
            if not occurrences:
                continue
            matched.append(keyword)
            # Term frequency dibatasi 3 agar satu kata tidak mendominasi.
            relevance += 1.0 + 0.5 * (min(occurrences, 3) - 1)
        lowered_category = category.lower()
        category_hits = [kw for kw in keywords if kw in lowered_category]
        relevance += 2.0 * len(category_hits)
        importance = CATEGORY_IMPORTANCE.get(category, _DEFAULT_IMPORTANCE)
        prior = importance * 0.35 * max(0.0, min(1.0, confidence))
        return relevance, prior, matched

    # ------------------------------------------------------------------ #
    # Selection
    # ------------------------------------------------------------------ #
    def _fallback_order(
        self,
        scored: Sequence[RetrievedEntry],
        categories: Sequence[str],
        keywords: Sequence[str],
        taken: Sequence[RetrievedEntry],
    ) -> List[RetrievedEntry]:
        """Entry tambahan saat hasil relevance terlalu sedikit.

        Urutan: kategori INTI lebih dulu, lalu kategori dengan skor tertinggi
        (kepentingan + kecocokan nama kategori), entry menurut urutan file.
        """
        seen = {(entry.category, entry.position) for entry in taken}

        def category_score(category: str) -> float:
            base = CATEGORY_IMPORTANCE.get(category, _DEFAULT_IMPORTANCE)
            bonus = sum(1.0 for kw in keywords if kw in category.lower())
            return base + bonus

        ordered_categories = sorted(
            categories,
            key=lambda cat: (
                0 if cat in CORE_CATEGORIES else 1,
                -category_score(cat),
                _category_order(cat),
                cat,
            ),
        )
        by_category: Dict[str, List[RetrievedEntry]] = {}
        for entry in scored:
            by_category.setdefault(entry.category, []).append(entry)

        out: List[RetrievedEntry] = []
        for category in ordered_categories:
            for entry in sorted(by_category.get(category, []), key=lambda e: e.position):
                key = (entry.category, entry.position)
                if key in seen:
                    continue
                seen.add(key)
                source = "core" if category in CORE_CATEGORIES else "category"
                out.append(replace(entry, source=source))
        return out

    def _select(
        self,
        scored: Sequence[RetrievedEntry],
        categories: Sequence[str],
        keywords: Sequence[str],
    ) -> Tuple[List[RetrievedEntry], str]:
        """Pilih & urutkan entry: relevance dulu, fallback bila perlu."""
        matched = [entry for entry in scored if entry.matched_terms]
        matched.sort(
            key=lambda e: (-(e.score + e.prior), _category_order(e.category), e.position)
        )

        # Query lemah (0-1 kata kunci): pakai kategori inti lebih dulu supaya
        # pertanyaan umum tetap mendapat knowledge pokok project.
        if len(keywords) <= 1:
            ordered = self._fallback_order(scored, categories, keywords, [])
            ordered = [e for e in ordered if e.matched_terms] + [
                e for e in ordered if not e.matched_terms
            ]
            return ordered, ("core" if ordered else "empty")

        if matched:
            if len(matched) >= MIN_SELECTED_ENTRIES:
                return matched, "entry"
            extra = self._fallback_order(scored, categories, keywords, matched)
            needed = MIN_SELECTED_ENTRIES - len(matched)
            return matched + extra[:needed], ("mixed" if extra[:needed] else "entry")

        ordered = self._fallback_order(scored, categories, keywords, [])
        return ordered, ("category" if ordered else "empty")

    # ------------------------------------------------------------------ #
    # Rendering + budget
    # ------------------------------------------------------------------ #
    @staticmethod
    def _group(entries: Sequence[RetrievedEntry]) -> List[Tuple[str, List[RetrievedEntry]]]:
        """Kelompokkan entry per kategori (urutan kanonik) untuk rendering."""
        groups: Dict[str, List[RetrievedEntry]] = {}
        for entry in entries:
            groups.setdefault(entry.category, []).append(entry)
        return sorted(groups.items(), key=lambda item: (_category_order(item[0]), item[0]))

    @staticmethod
    def _render(
        entries: Sequence[RetrievedEntry],
        total_entries: int,
    ) -> str:
        """Render teks context final (subset Bible, transparan ke LLM)."""
        lines: List[str] = [
            "# Project Intelligence",
            "",
            "<!-- AETHER Bible retrieval: hanya knowledge yang RELEVAN disertakan. -->",
            "Retrieved Project Bible context (relevance-based).",
        ]
        if entries:
            sections = ", ".join(category for category, _ in BibleRetriever._group(entries))
            lines.append(f"Relevant sections: {sections}")
            lines.append(
                f"Selected {len(entries)} of {total_entries} Bible entries for this task."
            )
        else:
            lines.append(
                "Project Bible belum memiliki knowledge yang dapat diambil "
                "untuk task ini."
            )
        for category, items in BibleRetriever._group(entries):
            lines.append("")
            lines.append(f"## {category}")
            for entry in items:
                lines.append(f"- {entry.content.replace(chr(10), ' ')}")
        lines.append("")
        lines.append(
            "Catatan: ini HANYA subset Project Bible yang paling relevan, bukan "
            "seluruh Bible. Knowledge lain tetap tersimpan dan dapat diambil "
            "melalui tool pencarian/navigasi bila diperlukan."
        )
        return "\n".join(lines)

    def _fit_to_budget(
        self,
        ordered: Sequence[RetrievedEntry],
        total_entries: int,
        budget_tokens: int,
    ) -> Tuple[List[RetrievedEntry], bool, str]:
        """Potong daftar terpilih agar estimasi token <= budget.

        Pemotongan dilakukan dari entry dengan ranking TERENDAH, sehingga
        knowledge paling relevan selalu dipertahankan. Bila TERSISA satu entry
        dan masih melebihi budget, isi entry itu dipotong (bukan dibuang),
        supaya knowledge paling relevan tidak hilang total.

        Returns:
            (entries, truncated, text)
        """
        selected = list(ordered)
        truncated = False
        text = self._render(selected, total_entries)
        while len(selected) > 1 and estimate_tokens(text, self.chars_per_token) > budget_tokens:
            selected.pop()
            truncated = True
            text = self._render(selected, total_entries)

        if selected and estimate_tokens(text, self.chars_per_token) > budget_tokens:
            original = selected[0]
            content = original.content
            while content:
                overflow = estimate_tokens(text, self.chars_per_token) - budget_tokens
                if overflow <= 0:
                    break
                cut = len(content) - overflow * self.chars_per_token - 16
                if cut >= len(content):
                    cut = len(content) - 1
                if cut <= 0:
                    break
                content = content[:cut].rstrip()
                selected = [replace(original, content=content + " …")]
                text = self._render(selected, total_entries)
                truncated = True
            if estimate_tokens(text, self.chars_per_token) > budget_tokens:
                # Bahkan header/entry minimal tidak muat -> kosongkan knowledge.
                selected = []
                text = self._render([], total_entries)
                truncated = True
        return selected, truncated, text

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def retrieve(
        self,
        query: Optional[str] = None,
        *,
        budget_tokens: Optional[int] = None,
    ) -> Optional[RetrievalResult]:
        """Ambil knowledge Bible yang relevan dengan `query`.

        Args:
            query: task/pertanyaan user (dipakai untuk relevance).
            budget_tokens: batas token konteks (dari provider bila tersedia).
                Bila None/<=0, dipakai `self.max_tokens` atau
                `DEFAULT_BUDGET_TOKENS`.

        Returns:
            RetrievalResult, atau None bila sumber knowledge tidak menyediakan
            akses terstruktur (pemanggil harus memakai jalur lama).
        """
        data = self.read_entries()
        if data is None:
            return None

        raw_query = str(query or "")
        keywords = query_keywords(raw_query)

        scored: List[RetrievedEntry] = []
        total_entries = 0
        for category, items in data.items():
            for position, (content, confidence) in enumerate(items):
                relevance, prior, matched = self._entry_score(
                    content, category, keywords, confidence
                )
                total_entries += 1
                scored.append(
                    RetrievedEntry(
                        category=category,
                        content=content,
                        score=relevance,
                        prior=prior,
                        matched_terms=matched,
                        position=position,
                    )
                )

        if budget_tokens and int(budget_tokens) > 0:
            budget = int(budget_tokens)
        elif self.max_tokens:
            budget = self.max_tokens
        else:
            budget = DEFAULT_BUDGET_TOKENS

        ordered, level = self._select(scored, list(data.keys()), keywords)
        selected, truncated, text = self._fit_to_budget(ordered, total_entries, budget)

        if not selected:
            level = "empty"

        categories = [category for category, _ in self._group(selected)]
        return RetrievalResult(
            query=raw_query,
            text=text,
            categories=categories,
            selected=selected,
            total_entries=total_entries,
            estimated_tokens=estimate_tokens(text, self.chars_per_token),
            budget_tokens=budget,
            level=level,
            truncated=truncated,
        )


def retrieve_bible_context(
    brain: Any,
    query: Optional[str] = None,
    *,
    token_budget: Optional[int] = None,
    categories: Optional[Sequence[str]] = None,
) -> Optional[RetrievalResult]:
    """Helper: retrieval Bible memakai ProjectBrain (read-only).

    Args:
        brain: ProjectBrain (atau objek dengan akses terstruktur).
        query: task/pertanyaan user.
        token_budget: batas token konteks.
        categories: batas kategori (None = semua).

    Returns:
        RetrievalResult, atau None bila retrieval tidak tersedia.
    """
    return BibleRetriever(brain, categories=categories).retrieve(
        query, budget_tokens=token_budget
    )
