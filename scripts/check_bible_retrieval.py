"""Verifier: Context-Aware Project Bible Retrieval.

Menguji bahwa Project Bible TIDAK lagi dikirim UTUH ke LLM, tetapi dipilih
berdasarkan relevance terhadap task/pertanyaan, lalu dibatasi token budget.

Cakupan (offline, deterministik, tanpa jaringan):

    [1] Relevance retrieval      -> knowledge relevan dipilih, yang tidak
                                    berkaitan TIDAK ikut dikirim.
    [2] Task tidak berkaitan     -> tidak otomatis mengirim seluruh Bible.
    [3] Query lemah/kosong       -> fallback bekerja (context tidak kosong).
    [4] Fallback kategori        -> query tanpa kecocokan melebar ke kategori.
    [5] Token budget             -> Bible besar tetap <= budget.
    [6] Agent parity             -> jalur Agent memakai retrieval.
    [7] Continuous loop parity   -> retrieval dihitung SEKALI per task
                                    (tidak mengulang seluruh Bible per iterasi).
    [8] Consultant parity        -> Consultant memakai retrieval yang sama.
    [9] Bible tetap utuh         -> retrieval READ-ONLY (Bible tidak berubah).
    [10] Existing behavior       -> Bible kosong / brain None tetap berjalan.
    [11] Duck-typed brain        -> brain lama (hanya get_context) tetap bekerja
                                    (backward compatible).
    [12] Helper                  -> keyword/estimasi token deterministik.
    [13] Context Builder parity  -> tahap Context Builder memakai retrieval yang
                                    sama (kategori eksplisit tetap dihormati).
    [14] Entry raksasa           -> satu entry > budget dipotong (bukan dibuang).
    Observability: metadata retrieval diemit TANPA isi Bible (bagian dari
    check Agent parity).

Jalankan:
    python scripts/check_bible_retrieval.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

DUMMY_ROOT = PROJECT_ROOT / "dummy_test"
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from agent_ai.core.models import AgentStatus  # noqa: E402
from agent_ai.core.orchestrator import AgentOrchestrator  # noqa: E402
from agent_ai.core.response import FinishReason, LLMAction, LLMResponse  # noqa: E402
from agent_ai.projects.brain import ProjectBrain  # noqa: E402
from agent_ai.projects.intelligence import ProjectIntelligence  # noqa: E402
from agent_ai.projects.learning import IntelligenceLearner  # noqa: E402
from agent_ai.projects.retrieval import (  # noqa: E402
    BibleRetriever,
    estimate_tokens,
    query_keywords,
)
from agent_ai.providers.base import BaseProvider, GenerateResult  # noqa: E402

#: Marker knowledge yang HARUS terpilih untuk query authentication.
AUTH_MARKER = "BUG-101 login Google OAuth gagal karena refresh token expired"
UNRELATED_MARKER = "DEPLOY_MARKER_XYZ deployment memakai rsync manual di server"
FACTS_MARKER = "FACT-1 project memakai Python 3.11 dan Django"
UI_MARKER = "UI-900 warna tombol login memakai violet accent AETHER"


# --------------------------------------------------------------------------- #
# Fake provider
# --------------------------------------------------------------------------- #
class RecordingProvider(BaseProvider):
    """Provider palsu: merekam messages yang benar-benar dikirim ke LLM."""

    name = "recording"

    def __init__(self, responses=None, budget_tokens=None) -> None:
        self._responses = list(responses or [])
        self.budget_tokens = budget_tokens
        self.calls = []

    def knowledge_budget_tokens(self):  # noqa: D102 - hook provider
        return self.budget_tokens

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):  # noqa: D102
        self.calls.append({"messages": messages, "tools": tools})
        item = self._responses.pop(0) if self._responses else "final: selesai"
        return GenerateResult(
            text=item if isinstance(item, str) else "",
            model="recording-model",
            provider=self.name,
            raw={"item": item},
        )

    def normalize_response(self, result):  # noqa: D102
        item = result.raw.get("item")
        if isinstance(item, dict) and "tool" in item:
            return LLMResponse(
                actions=[LLMAction(name=item["tool"], arguments=item.get("arguments", {}))],
                finish_reason=FinishReason.TOOL_CALLS,
                provider=self.name,
                model=result.model,
            )
        return LLMResponse(
            text=result.text or "",
            actions=[],
            finish_reason=FinishReason.STOP,
            provider=self.name,
            model=result.model,
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _write_bible(root: Path, entries) -> None:
    """Tulis fixture Bible memakai API existing (BibleStore via learner)."""
    intelligence = ProjectIntelligence.for_project(root)
    learner = IntelligenceLearner(intelligence)
    for category, content in entries:
        learner.add_verified(category, content, source="verifier")


def _knowledge_message(messages) -> str:
    """Ambil system message knowledge dari messages provider.

    Mendukung dua bentuk: objek Message (loop lama) dan dict (format provider).
    """
    chunks = []
    for message in messages or []:
        if isinstance(message, dict):
            role = message.get("role")
            content = message.get("content") or ""
        else:
            role = getattr(message, "role", None)
            content = getattr(message, "content", "") or ""
        if role != "system":
            continue
        if "# Project Intelligence" in content:
            chunks.append(content)
    return "\n".join(chunks)


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def check_relevance(root: Path) -> None:
    brain = ProjectBrain.for_project(root)
    retriever = BibleRetriever(brain)
    result = retriever.retrieve("fix authentication bug")
    assert result is not None
    text = result.text
    print(f"    total={result.total_entries} selected={result.selected_count} "
          f"level={result.level} tokens={result.estimated_tokens}/{result.budget_tokens} "
          f"categories={result.categories}")

    assert "Retrieved Project Bible context" in text, "context harus menyatakan retrieval"
    assert "HANYA subset Project Bible" in text, "context harus transparan (subset)"
    assert "BUG-101" in text, "knowledge authentication harus dipilih"
    assert "authentication" in text.lower(), "knowledge architecture authentication harus dipilih"
    assert UNRELATED_MARKER not in text, "knowledge tidak berkaitan tidak boleh dikirim"
    assert "FACT-1" not in text, "kategori inti tanpa kecocokan tidak boleh ikut (level entry)"
    assert result.estimated_tokens <= result.budget_tokens
    print("[1] Relevance retrieval OK -> authentication dipilih, knowledge lain tidak ikut")


def check_unrelated_task(root: Path) -> None:
    brain = ProjectBrain.for_project(root)
    full = brain.get_context().text
    result = BibleRetriever(brain).retrieve("ubah warna tombol login dengan warna baru")
    assert result is not None
    assert result.selected_count < result.total_entries, "tidak boleh mengirim seluruh Bible"
    assert len(result.text) < len(full), "context harus lebih kecil dari Bible utuh"
    assert UNRELATED_MARKER not in result.text
    assert UI_MARKER in result.text, "knowledge ui yang relevan harus dipilih"
    print(f"[2] Task tidak berkaitan OK -> {result.selected_count}/{result.total_entries} entry "
          f"({len(result.text)} char vs Bible utuh {len(full)} char)")


def check_weak_query(root: Path) -> None:
    brain = ProjectBrain.for_project(root)
    for query in ("", "halo", "?"):
        result = BibleRetriever(brain).retrieve(query)
        assert result is not None, f"retrieval harus tetap bekerja untuk query {query!r}"
        assert result.text.strip(), "context tidak boleh kosong"
        assert "# Project Intelligence" in result.text
        assert result.estimated_tokens <= result.budget_tokens
    weak = BibleRetriever(brain).retrieve("halo")
    assert weak.level in ("core", "category", "entry", "mixed"), weak.level
    kb = [entry.category for entry in weak.selected]
    assert "facts" in kb, f"fallback harus memuat kategori inti (facts): {kb}"
    print(f"[3] Query lemah/kosong OK -> fallback level={weak.level} kategori={weak.categories}")


def check_category_fallback(root: Path) -> None:
    brain = ProjectBrain.for_project(root)
    result = BibleRetriever(brain).retrieve("zzzqqq tidak ada kecocokan sama sekali")
    assert result is not None
    assert result.selected_count >= 1, "fallback harus menyediakan knowledge, bukan kosong"
    assert result.level in ("category", "core", "mixed"), result.level
    assert result.estimated_tokens <= result.budget_tokens
    print(f"[4] Fallback kategori OK -> level={result.level} selected={result.selected_count}")


def check_token_budget() -> None:
    budget_root = DUMMY_ROOT / "bible_retrieval_budget"
    shutil.rmtree(budget_root, ignore_errors=True)
    budget_root.mkdir(parents=True, exist_ok=True)
    try:
        entries = []
        for index in range(120):
            entries.append(
                (
                    "facts" if index % 2 else "learnings",
                    f"KNOW-{index:03d} pencatatan pengetahuan panjang nomor {index} "
                    + ("detail pengetahuan projektif " * 4),
                )
            )
        _write_bible(budget_root, entries)
        brain = ProjectBrain.for_project(budget_root)
        full = brain.get_context().text

        budget = 1000
        result = BibleRetriever(brain).retrieve("detail pengetahuan project", budget_tokens=budget)
        assert result is not None
        assert result.estimated_tokens <= budget, (result.estimated_tokens, budget)
        assert len(result.text) <= budget * 4, (len(result.text), budget * 4)
        assert result.truncated is True, "pemotongan budget harus ditandai"
        assert len(result.text) < len(full)
        print(f"[5] Token budget OK -> {len(full)} char ({estimate_tokens(full)} token) "
              f"-> {len(result.text)} char ({result.estimated_tokens} token) <= {budget} token")
    finally:
        shutil.rmtree(budget_root, ignore_errors=True)


def check_agent_parity(root: Path) -> None:
    brain = ProjectBrain.for_project(root)
    full = brain.get_context().text
    events = []
    provider = RecordingProvider(budget_tokens=None)
    orchestrator = AgentOrchestrator(
        provider=provider,
        brain=brain,
        event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )
    result = orchestrator.run("fix authentication bug")
    assert result.status == AgentStatus.DONE, result.status

    sent = _knowledge_message(provider.calls[0]["messages"])
    assert sent, "Agent harus menerima context Bible (retrieval)"
    assert "BUG-101" in sent, "Agent harus menerima knowledge relevan"
    assert UNRELATED_MARKER not in sent, "Agent tidak boleh menerima knowledge tidak relevan"
    assert len(sent) < len(full), "Agent tidak boleh menerima Bible utuh"
    assert "Catatan: ini HANYA subset Project Bible" in sent, "context harus transparan (subset)"

    retrieval_events = [payload for name, payload in events if name == "bible_retrieval"]
    assert len(retrieval_events) == 1, f"harus ada 1 event retrieval, dapat {len(retrieval_events)}"
    metadata = retrieval_events[0]
    assert "selected" in metadata and "budget" in metadata
    assert isinstance(metadata["budget"], int), metadata["budget"]
    assert isinstance(metadata["used"], int), metadata["used"]
    assert "BUG-101" not in str(metadata), "metadata log tidak boleh memuat isi Bible"
    assert "content" not in metadata
    print(f"[6] Agent parity OK -> context {len(sent)} char (Bible utuh {len(full)} char), "
          f"event retrieval={len(retrieval_events)}")


def check_continuous_loop_parity(root: Path) -> None:
    brain = ProjectBrain.for_project(root)
    events = []
    provider = RecordingProvider(
        responses=[
            {"tool": "read_file", "arguments": {"path": "tidak-ada.py"}},
            "final: selesai",
        ]
    )
    orchestrator = AgentOrchestrator(
        provider=provider,
        brain=brain,
        use_continuous_loop=True,
        event_sink=lambda event_type, payload: events.append((event_type, payload)),
    )
    result = orchestrator.run_continuous_loop("fix authentication bug", max_steps=6)
    assert result.status == AgentStatus.DONE, (result.status, result.error)

    # Retrieval dihitung SEKALI; knowledge tidak di-inject ulang per iterasi.
    for call in provider.calls:
        knowledge = _knowledge_message(call["messages"])
        assert knowledge.count("# Project Intelligence") == 1, "knowledge ter-inject lebih dari sekali"
    last_sent = _knowledge_message(provider.calls[-1]["messages"])
    assert "BUG-101" in last_sent and UNRELATED_MARKER not in last_sent
    retrieval_events = [payload for name, payload in events if name == "bible_retrieval"]
    assert len(retrieval_events) == 1, f"retrieval harus sekali per task, dapat {len(retrieval_events)}"
    print(f"[7] Continuous loop parity OK -> {len(provider.calls)} iterasi, "
          f"retrieval event={len(retrieval_events)} (sekali)")


def check_consultant_parity(root: Path) -> None:
    from agent_ai.consultant.service import ConsultantService

    provider = RecordingProvider()
    result = ConsultantService().consult(
        "fix authentication bug", provider=provider, root=str(root), mode="quick"
    )
    assert result.status == "done", (result.status, result.error)
    sent = _knowledge_message(provider.calls[0]["messages"])
    assert sent, "Consultant harus menerima context Bible (retrieval)"
    assert "BUG-101" in sent, "Consultant harus menerima knowledge relevan"
    assert UNRELATED_MARKER not in sent, "Consultant tidak boleh menerima knowledge tidak relevan"
    print(f"[8] Consultant parity OK -> context retrieval {len(sent)} char")


def check_bible_untouched(root: Path) -> None:
    facts = root / ".aether" / "bible" / "facts.md"
    before = facts.read_text(encoding="utf-8")
    brain = ProjectBrain.for_project(root)
    BibleRetriever(brain).retrieve("fix authentication bug")
    after = facts.read_text(encoding="utf-8")
    assert before == after, "retrieval TIDAK boleh mengubah Bible"
    files_before = sorted(p.name for p in (root / ".aether" / "bible").glob("*.md"))
    BibleRetriever(brain).retrieve("deployment server")
    files_after = sorted(p.name for p in (root / ".aether" / "bible").glob("*.md"))
    assert files_before == files_after, "retrieval tidak boleh menambah/menghapus file Bible"
    print("[9] Bible tetap utuh OK -> retrieval READ-ONLY")


def check_empty_bible() -> None:
    empty_root = DUMMY_ROOT / "bible_retrieval_empty"
    shutil.rmtree(empty_root, ignore_errors=True)
    empty_root.mkdir(parents=True, exist_ok=True)
    try:
        brain = ProjectBrain.for_project(empty_root)
        result = BibleRetriever(brain).retrieve("fix authentication bug")
        assert result is not None
        assert result.selected_count == 0
        assert result.level == "empty"
        assert "# Project Intelligence" in result.text, "header tetap ada (kompatibel verifier lama)"

        provider = RecordingProvider()
        outcome = AgentOrchestrator(provider=provider, brain=brain).run("task tanpa bible")
        assert outcome.status == AgentStatus.DONE, outcome.status
        sent = _knowledge_message(provider.calls[0]["messages"])
        assert "# Project Intelligence" in sent

        # Brain None: tidak ada context knowledge sama sekali, task tetap jalan.
        provider2 = RecordingProvider()
        outcome2 = AgentOrchestrator(provider=provider2).run("task tanpa brain")
        assert outcome2.status == AgentStatus.DONE, outcome2.status
        assert _knowledge_message(provider2.calls[0]["messages"]) == ""
        print("[10] Existing behavior OK -> Bible kosong & brain None tetap berjalan")
    finally:
        shutil.rmtree(empty_root, ignore_errors=True)


def check_duck_typed_brain() -> None:
    """Brain lama (hanya get_context) -> jalur lama dipakai (backward compatible)."""

    class FakeBrain:
        def __init__(self, text):
            self._text = text
            self.calls = 0

        def get_context(self, *args, **kwargs):
            self.calls += 1

            class _Ctx:
                pass

            ctx = _Ctx()
            ctx.text = self._text
            return ctx

        def learn(self, observations):
            return None

    long_text = "# Project Intelligence\n" + "\n".join(
        f"- baris pengetahuan {index:04d}" for index in range(500)
    )
    brain = FakeBrain(long_text)
    provider = RecordingProvider(budget_tokens=500)
    AgentOrchestrator(provider=provider, brain=brain).run("tanya")
    sent = _knowledge_message(provider.calls[0]["messages"])
    assert sent, "jalur lama harus tetap mengirim context"
    assert len(sent) <= 500 * 4 + 200, "pemotongan anggaran provider harus tetap berlaku"
    assert brain.calls == 1
    print("[11] Duck-typed brain OK -> jalur lama (get_context + potong per baris) tetap bekerja")


def check_context_builder_parity(root: Path) -> None:
    """Tahap Context Builder juga memakai retrieval yang sama (bukan Bible utuh)."""
    from agent_ai.codeindex.indexer import CodeIndexer
    from agent_ai.contextbuilder.builder import ContextBuilder
    from agent_ai.contextbuilder.models import ContextRequest

    brain = ProjectBrain.for_project(root)
    full = brain.get_context().text
    index = CodeIndexer(root=root).build()
    builder = ContextBuilder(index=index, root=root, brain=brain)
    result = builder.build(ContextRequest(task="fix authentication bug"))
    assert result.brain, "Context Builder harus menyertakan knowledge"
    assert "BUG-101" in result.brain, "Context Builder harus menerima knowledge relevan"
    assert UNRELATED_MARKER not in result.brain, "Context Builder tidak boleh menerima knowledge tidak relevan"
    assert len(result.brain) < len(full), "Context Builder tidak boleh menerima Bible utuh"

    # Kategori eksplisit dari pemanggil tetap dihormati (filter lama).
    filtered = builder.build(
        ContextRequest(task="fix authentication bug", intelligence_categories=["known_bugs"])
    )
    assert "BUG-101" in filtered.intelligence
    assert "Layer authentication" not in filtered.intelligence
    print(f"[13] Context Builder parity OK -> brain {len(result.brain)} char "
          f"(Bible utuh {len(full)} char), filter kategori eksplisit tetap bekerja")


def check_single_huge_entry() -> None:
    """Satu entry raksasa: dipotong agar muat budget, bukan dibuang."""
    root = DUMMY_ROOT / "bible_retrieval_huge"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        _write_bible(root, [("facts", "HUGE-1 " + ("pengetahuan penting project " * 3000))])
        brain = ProjectBrain.for_project(root)
        budget = 500
        result = BibleRetriever(brain).retrieve("pengetahuan penting project", budget_tokens=budget)
        assert result is not None
        assert result.estimated_tokens <= budget, (result.estimated_tokens, budget)
        assert result.selected_count == 1, "entry paling relevan harus dipertahankan"
        assert "HUGE-1" in result.text, "awal entry relevan harus tetap ada"
        assert result.truncated is True
        print(f"[14] Entry raksasa OK -> dipotong ({len(result.text)} char, "
              f"{result.estimated_tokens} token) <= {budget} token")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_helpers() -> None:
    keywords = query_keywords("Perbaiki bug authentication di GatewayService")
    assert "bug" in keywords and "authentication" in keywords
    assert "di" not in keywords, "stopword harus dibuang"
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 40) == 10
    print(f"[12] Helper OK -> keywords={keywords} tokens(40 char)={estimate_tokens('a' * 40)}")


def main() -> int:
    print("=== Verifikasi Context-Aware Project Bible Retrieval ===")
    fixture = DUMMY_ROOT / "bible_retrieval"
    shutil.rmtree(fixture, ignore_errors=True)
    fixture.mkdir(parents=True, exist_ok=True)
    try:
        entries = [
            ("known_bugs", AUTH_MARKER),
            ("known_bugs", "BUG-202 form login mengabaikan error HTTP 401"),
            ("architecture", "Layer authentication memakai modul auth/service.py"),
            ("decisions", "OAuth memakai PKCE untuk flow mobile"),
            ("ui", UI_MARKER),
            ("learnings", UNRELATED_MARKER),
            ("facts", FACTS_MARKER),
            ("conventions", "CONV-1 gunakan type hints pada modul baru"),
            ("known_gaps", "GAP-1 belum ada rate limiting pada endpoint publik"),
            ("problems", "PROB-1 build frontend butuh Node.js terpasang"),
        ]
        # Filler: knowledge tidak berkaitan supaya "Bible utuh" jelas lebih besar
        # dari subset hasil retrieval (kata kuncinya sengaja tidak overlap).
        for index in range(30):
            category = "learnings" if index % 2 else "problems"
            entries.append(
                (
                    category,
                    f"ARSIP-{index:03d} catatan arsip internal nomor {index} mengenai "
                    "prosedur dokumentasi tertulis tim dan tata kelola berkas",
                )
            )
        _write_bible(fixture, entries)

        check_relevance(fixture)
        check_unrelated_task(fixture)
        check_weak_query(fixture)
        check_category_fallback(fixture)
        check_token_budget()
        check_agent_parity(fixture)
        check_continuous_loop_parity(fixture)
        check_consultant_parity(fixture)
        check_bible_untouched(fixture)
        check_empty_bible()
        check_duck_typed_brain()
        check_helpers()
        check_context_builder_parity(fixture)
        check_single_huge_entry()
        print()
        print("[OK] Project Bible retrieval berbasis relevance + token budget terverifikasi.")
        return 0
    finally:
        shutil.rmtree(fixture, ignore_errors=True)
        if DUMMY_ROOT.exists() and not any(DUMMY_ROOT.iterdir()):
            try:
                DUMMY_ROOT.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
