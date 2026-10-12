"""TASK 2 — Persistensi telemetry & akurasi usage.

Cakupan (tanpa provider LLM eksternal; semua fake/lokal):

    A. Normalisasi `response_usage`:
       1. usage LENGKAP -> input/output/total benar (OpenAI-compatible + Ollama).
       2. usage PARSIAL -> TIDAK ada total/komponen menyesatkan.
       3. usage TIDAK tersedia -> None (BUKAN 0).
       4. format OpenAI-compatible & Ollama native tetap didukung.
    B. `usage_detail` / `usage_status`: status actual/partial/unavailable +
       sumber angka eksplisit.
    C. Persistensi usage Consultant:
       5. metadata usage tersimpan & dapat dibaca kembali (project-local).
       6. record terisolasi di project yang benar (dua project bergantian).
    D. Isolasi kegagalan: telemetry gagal menulis TIDAK menggagalkan konsultasi.
    E. Tidak ada penghitungan token ganda (satu record per respons provider).

Jalankan:
    set PYTHONPATH=src;web\\django_app && python -m pytest tests/test_usage_telemetry.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import pytest  # noqa: E402

from agent_ai.consultant.service import ConsultantService  # noqa: E402
from agent_ai.consultant.telemetry import (  # noqa: E402
    ConsultantUsageStore,
    project_usage_path,
)
from agent_ai.core.response import (  # noqa: E402
    USAGE_STATUS_ACTUAL,
    USAGE_STATUS_PARTIAL,
    USAGE_STATUS_UNAVAILABLE,
    response_usage,
    usage_detail,
    usage_status,
)
from agent_ai.providers.base import (  # noqa: E402
    BaseProvider,
    GenerateOptions,
    GenerateResult,
)


# --------------------------------------------------------------------------- #
# A. Normalisasi usage
# --------------------------------------------------------------------------- #
def test_a1_complete_usage_openai() -> None:
    raw = {"usage": {"prompt_tokens": 100, "completion_tokens": 50}}
    assert response_usage(raw) == {"prompt": 100, "completion": 50, "total": 150}
    detail = usage_detail(raw)
    assert (detail["prompt"], detail["completion"], detail["total"]) == (100, 50, 150)
    assert detail["status"] == USAGE_STATUS_ACTUAL
    assert detail["source"] == "openai_usage"


def test_a1_complete_usage_provider_total_wins() -> None:
    raw = {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 999}}
    assert response_usage(raw) == {"prompt": 100, "completion": 50, "total": 999}
    detail = usage_detail(raw)
    assert detail["total"] == 999
    assert detail["status"] == USAGE_STATUS_ACTUAL


def test_a4_ollama_native_still_supported() -> None:
    raw = {"prompt_eval_count": 20, "eval_count": 12}
    assert response_usage(raw) == {"prompt": 20, "completion": 12, "total": 32}
    detail = usage_detail(raw)
    assert detail["status"] == USAGE_STATUS_ACTUAL
    assert detail["source"] == "ollama_native"


def test_a2_partial_usage_does_not_fabricate_total() -> None:
    """Hanya input: total TIDAK boleh disimpulkan (bukan input+0)."""
    only_prompt = response_usage({"usage": {"prompt_tokens": 7}})
    assert only_prompt == {"prompt": 7}
    assert "total" not in only_prompt
    assert usage_status(only_prompt) == USAGE_STATUS_PARTIAL

    only_completion = response_usage({"usage": {"completion_tokens": 9}})
    assert only_completion == {"completion": 9}
    assert "total" not in only_completion

    only_total = response_usage({"usage": {"total_tokens": 42}})
    assert only_total == {"total": 42}
    assert usage_status(only_total) == USAGE_STATUS_PARTIAL

    # Ollama partial (hanya eval_count).
    part_ollama = response_usage({"eval_count": 5})
    assert part_ollama == {"completion": 5}
    assert "total" not in part_ollama


def test_a3_unavailable_usage_is_none_not_zero() -> None:
    assert response_usage({}) is None
    assert response_usage({"usage": {}}) is None
    assert response_usage(None) is None
    assert usage_status(None) == USAGE_STATUS_UNAVAILABLE

    detail = usage_detail({"usage": {}})
    assert detail["prompt"] is None and detail["completion"] is None and detail["total"] is None
    assert detail["status"] == USAGE_STATUS_UNAVAILABLE


def test_a5_invalid_values_treated_as_unavailable() -> None:
    # Nilai negatif / bool / string bukan angka valid -> tidak dipakai.
    assert response_usage({"usage": {"prompt_tokens": -1}}) is None
    assert response_usage({"usage": {"prompt_tokens": True}}) is None
    assert response_usage({"usage": {"prompt_tokens": "100"}}) is None


# --------------------------------------------------------------------------- #
# C. Persistensi usage Consultant
# --------------------------------------------------------------------------- #
class UsageProvider(BaseProvider):
    """Provider fake: satu respons final dengan usage pada `raw` (seperti nyata)."""

    name = "usage-fake"

    def __init__(self, usage: Optional[Dict[str, Any]] = None, model: str = "fake-model") -> None:
        self._usage = usage
        self._model = model
        self.calls = 0

    def generate(
        self,
        prompt: Optional[str] = None,
        messages: Optional[Any] = None,
        options: Optional[GenerateOptions] = None,
        tools: Optional[Any] = None,
        tool_choice: Optional[Any] = None,
    ) -> GenerateResult:
        self.calls += 1
        raw: Dict[str, Any] = {}
        if self._usage is not None:
            raw["usage"] = dict(self._usage)
        return GenerateResult(
            text="Jawaban final Consultant.",
            model=self._model,
            provider=self.name,
            raw=raw,
        )

    def is_available(self) -> bool:
        return True


class _Scope:
    """Project scope minimal: memetakan project_id -> root (isolasi)."""

    def __init__(self, roots: Dict[str, str], active: Optional[str] = None) -> None:
        self._roots = dict(roots)
        self._active = active or (next(iter(roots)) if roots else None)

    def active_project_id(self) -> Optional[str]:
        return self._active

    def project_root(self, project_id: str) -> Optional[str]:
        return self._roots.get(project_id)

    def registered_project_ids(self) -> List[str]:
        return list(self._roots.keys())


def _read_usage(project_root: Path) -> List[Dict[str, Any]]:
    return ConsultantUsageStore(project_usage_path(str(project_root))).read_all()


def test_c5_consultant_usage_persisted_and_readable(tmp_path: Path) -> None:
    root = tmp_path / "projA"
    root.mkdir()
    svc = ConsultantService(
        project_scope=_Scope({"p-1": str(root)}),
    )
    provider = UsageProvider(usage={"prompt_tokens": 120, "completion_tokens": 30})

    result = svc.consult("Halo", provider=provider, project_id="p-1")
    assert result.status == "done"

    records = _read_usage(root)
    assert len(records) == 1, records
    rec = records[0]
    assert rec["activity"] == "consultant"
    assert rec["project_id"] == "p-1"
    assert rec["session_id"] == result.session_id
    assert rec["turn_id"]
    assert rec["response_index"] == 0
    assert rec["provider"] == "usage-fake"
    assert rec["model"] == "fake-model"
    assert rec["input_tokens"] == 120
    assert rec["output_tokens"] == 30
    assert rec["total_tokens"] == 150
    assert rec["usage_status"] == USAGE_STATUS_ACTUAL
    assert rec["usage_source"] == "provider_response.usage"
    assert rec["timestamp"] > 0
    # TIDAK ada teks prompt/respons yang disimpan.
    assert "Halo" not in str(rec)
    assert "Jawaban final" not in str(rec)


def test_c6_consultant_usage_isolated_per_project(tmp_path: Path) -> None:
    root_a = tmp_path / "projA"
    root_b = tmp_path / "projB"
    root_a.mkdir()
    root_b.mkdir()
    svc = ConsultantService(project_scope=_Scope({"p-a": str(root_a), "p-b": str(root_b)}))

    svc.consult("A", provider=UsageProvider(usage={"prompt_tokens": 10, "completion_tokens": 1}), project_id="p-a")
    svc.consult("B", provider=UsageProvider(usage={"prompt_tokens": 20, "completion_tokens": 2}), project_id="p-b")
    svc.consult("A2", provider=UsageProvider(usage={"prompt_tokens": 30, "completion_tokens": 3}), project_id="p-a")

    a_records = _read_usage(root_a)
    b_records = _read_usage(root_b)
    assert len(a_records) == 2
    assert len(b_records) == 1
    # Isolasi keras: tidak ada pencampuran lintas-project.
    assert all(r["project_id"] == "p-a" for r in a_records)
    assert all(r["project_id"] == "p-b" for r in b_records)
    assert {r["input_tokens"] for r in a_records} == {10, 30}
    assert b_records[0]["input_tokens"] == 20


def test_c_no_unavailable_usage_recorded_as_zero(tmp_path: Path) -> None:
    """Provider tanpa usage -> record status unavailable, BUKAN 0."""
    root = tmp_path / "proj"
    root.mkdir()
    svc = ConsultantService(project_scope=_Scope({"p-1": str(root)}))
    svc.consult("Halo", provider=UsageProvider(usage=None), project_id="p-1")

    records = _read_usage(root)
    assert len(records) == 1
    rec = records[0]
    assert rec["usage_status"] == USAGE_STATUS_UNAVAILABLE
    assert rec["input_tokens"] is None
    assert rec["output_tokens"] is None
    assert rec["total_tokens"] is None
    assert rec["usage_source"] == "unavailable"


def test_c_partial_usage_recorded_as_partial(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    svc = ConsultantService(project_scope=_Scope({"p-1": str(root)}))
    svc.consult(
        "Halo",
        provider=UsageProvider(usage={"prompt_tokens": 11}),
        project_id="p-1",
    )
    rec = _read_usage(root)[0]
    assert rec["usage_status"] == USAGE_STATUS_PARTIAL
    assert rec["input_tokens"] == 11
    assert rec["output_tokens"] is None
    assert rec["total_tokens"] is None


# --------------------------------------------------------------------------- #
# D. Isolasi kegagalan telemetry
# --------------------------------------------------------------------------- #
def test_d_telemetry_failure_does_not_break_consult(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    svc = ConsultantService(project_scope=_Scope({"p-1": str(root)}))

    def _boom(self, record):  # noqa: ANN001
        raise RuntimeError("disk penuh")

    # Kegagalan append harus ditelan di dalam jalur konsultasi (dibungkus
    # try/except pada _record_provider_usage). Simulasi store yang melempar.
    import agent_ai.consultant.service as svc_mod

    class _ExplodingStore(svc_mod.ConsultantUsageStore):  # type: ignore[misc]
        def append(self, record):  # noqa: ANN001
            raise RuntimeError("disk penuh")

    monkeypatch.setattr(svc_mod, "ConsultantUsageStore", _ExplodingStore)

    result = svc.consult(
        "Halo",
        provider=UsageProvider(usage={"prompt_tokens": 5, "completion_tokens": 5}),
        project_id="p-1",
    )
    # Percakapan tetap sukses meski telemetry gagal.
    assert result.status == "done"
    assert result.reply


def test_d_store_append_is_best_effort_on_bad_path(tmp_path: Path) -> None:
    # Path tidak valid (parent adalah file) -> append mengembalikan False, tidak raise.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    store = ConsultantUsageStore(str(blocker / "sub" / "usage.jsonl"))
    from agent_ai.consultant.telemetry import make_usage_record

    rec = make_usage_record(
        project_id="p",
        session_id="s",
        turn_id="t",
        response_index=0,
        usage={"prompt": 1, "completion": 2, "total": 3},
    )
    # Tidak boleh melempar; best-effort.
    store.append(rec)


# --------------------------------------------------------------------------- #
# E. Tidak ada penghitungan ganda
# --------------------------------------------------------------------------- #
class MultiResponseProvider(BaseProvider):
    """Provider fake: N respons (usage per respons) lalu final tanpa usage."""

    name = "multi-fake"

    def __init__(self, usages: List[Dict[str, Any]]) -> None:
        self._usages = list(usages)
        self.calls = 0

    def generate(self, prompt=None, messages=None, options=None, tools=None, tool_choice=None):
        self.calls += 1
        if self._usages:
            usage = self._usages.pop(0)
            return GenerateResult(
                text="",
                model="m",
                provider=self.name,
                raw={"usage": usage},
            )
        return GenerateResult(text="Final.", model="m", provider=self.name, raw={})

    def is_available(self) -> bool:
        return True

    def normalize_response(self, result):  # noqa: ANN001
        # Paksa 2 putaran pertama menjadi tool-call agar loop mengulang, lalu final.
        from agent_ai.core.response import FinishReason, LLMAction, LLMResponse

        if result.raw.get("usage"):
            return LLMResponse(
                actions=[LLMAction(name="list_files", arguments={"path": "."})],
                finish_reason=FinishReason.TOOL_CALLS,
                raw=result.raw,
                provider=result.provider,
                model=result.model,
            )
        return LLMResponse(
            text="Final.", finish_reason=FinishReason.STOP, raw=result.raw,
            provider=result.provider, model=result.model,
        )


def test_e_one_record_per_provider_response_no_double_count(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    svc = ConsultantService(project_scope=_Scope({"p-1": str(root)}))
    provider = MultiResponseProvider(
        usages=[
            {"prompt_tokens": 10, "completion_tokens": 1},
            {"prompt_tokens": 20, "completion_tokens": 2},
        ]
    )
    result = svc.consult("Halo", provider=provider, project_id="p-1")
    assert result.status == "done"

    records = _read_usage(root)
    # Satu record per respons provider: 2 respons ber-usage + 1 respons final
    # tanpa usage. TIDAK ada respons ganda yang dihitung dua kali.
    assert len(records) == 3, records
    # Semua record satu giliran (turn_id sama) -> identitas giliran memadai.
    assert len({r["turn_id"] for r in records}) == 1
    # response_index unik & berurutan -> anti duplikasi.
    assert sorted(r["response_index"] for r in records) == [0, 1, 2]

    # Total token dijumlahkan SEKALI per respons (tidak ada double count).
    with_usage = [r for r in records if r["usage_status"] == USAGE_STATUS_ACTUAL]
    assert len(with_usage) == 2
    assert sum(r["total_tokens"] for r in with_usage) == (10 + 1) + (20 + 2)
    # Respons final tanpa usage ditandai unavailable, bukan 0.
    final = [r for r in records if r["response_index"] == 2][0]
    assert final["usage_status"] == USAGE_STATUS_UNAVAILABLE
    assert final["total_tokens"] is None


def test_e_store_dedups_same_identity(tmp_path: Path) -> None:
    from agent_ai.consultant.telemetry import make_usage_record

    path = str(tmp_path / "usage.jsonl")
    store = ConsultantUsageStore(path)
    rec = make_usage_record(
        project_id="p", session_id="s", turn_id="t", response_index=0,
        usage={"prompt": 1, "completion": 2, "total": 3},
    )
    assert store.append(rec) is True
    # Identitas sama -> tidak ditulis ulang.
    assert store.append(rec) is False
    assert len(ConsultantUsageStore(path).read_all()) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
