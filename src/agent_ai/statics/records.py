"""Pembacaan & normalisasi record sumber statistik (per-project, read-only).

Sumber:
    * Agent Task Log  : ``<root>/.aether/log/<task_id>.log`` (JSONL) — dibaca
      lewat :class:`TaskLogReader` yang sudah ada.
    * Consultant usage: ``<root>/.aether/consultant/usage.jsonl`` (JSONL) —
      dibaca lewat :class:`ConsultantUsageStore` (best-effort, toleran baris
      rusak).

Kontrak kelengkapan usage (mengikuti TASK 2):
    * ``actual``      : input DAN output tersedia (total andal).
    * ``partial``     : hanya sebagian komponen tersedia.
    * ``unavailable`` : tidak ada komponen yang dilaporkan provider.

ATURAN AKURASI:
    * Komponen yang hilang = ``None`` (BUKAN 0).
    * Satu task dihitung SATU kali (bukan per event/per request LLM).
    * ``rounds``/``tool_calls`` dihitung dari event task log milik task ini.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from agent_ai.consultant.telemetry import (
    PROJECT_USAGE_RELPATH,
    ConsultantUsageStore,
)
from agent_ai.core.response import (
    USAGE_STATUS_ACTUAL,
    USAGE_STATUS_PARTIAL,
    USAGE_STATUS_UNAVAILABLE,
    response_usage,
)
from agent_ai.projects.aether_store import TaskLogReader

# --------------------------------------------------------------------------- #
# Konstanta status task (dari Task Log lifecycle).
# --------------------------------------------------------------------------- #
AGENT_TASK_STATUSES = ("completed", "failed", "cancelled", "incomplete")

TASK_USAGE_ACTUAL = USAGE_STATUS_ACTUAL
TASK_USAGE_PARTIAL = USAGE_STATUS_PARTIAL
TASK_USAGE_UNAVAILABLE = USAGE_STATUS_UNAVAILABLE

#: Event provider_response yang BERHASIL (tanpa `error`) menandai satu request
#: LLM sukses/round. Event error tidak dihitung sebagai round sukses.
_EVENT_PROVIDER_RESPONSE = "provider_response"
#: Event provider_request menandai SATU pemanggilan LLM logis yang benar-benar
#: dimulai (diemit TEPAT SEKALI per round, SEBELUM retry). Ini sumber yang benar
#: untuk `llm_requests`: pada jalur continuous loop satu request logis dapat
#: menghasilkan BEBERAPA `provider_response` (attempt gagal + final), sehingga
#: menghitung respons sukses saja akan meng-under-count request yang gagal.
_EVENT_PROVIDER_REQUEST = "provider_request"
_EVENT_TOOL_CALLED = "tool_called"
#: Event retry provider-level (telemetry `_generate_with_retry`). Dihitung HANYA
#: sebagai observability; retry TIDAK dihitung sebagai request baru (attempt
#: masih bagian dari satu `provider_request` logis).
_EVENT_PROVIDER_RETRY = "provider_retry"
_EVENT_PROVIDER_RETRY_EXHAUSTED = "provider_retry_exhausted"
#: Event retry provider yang AKHIRNYA berhasil. Membedakan retry yang pulih
#: dari retry yang berujung exhaustion. Observability; bukan request baru.
_EVENT_PROVIDER_RETRY_SUCCEEDED = "provider_retry_succeeded"
#: Event pindah provider (fallback) — memuat provider/model asal & tujuan.
_EVENT_PROVIDER_FALLBACK = "provider_fallback"
#: Event respons provider TERPOTONG (`finish_reason: length`). Ini menandai
#: bahwa truncation TERJADI — BUKAN bukti bahwa truncation berhasil dipulihkan.
_EVENT_PROVIDER_RESPONSE_TRUNCATED = "provider_response_truncated"
#: Event abort karena batas truncation recovery tercapai. Menandai truncation
#: yang TIDAK berhasil dipulihkan (task dihentikan gagal oleh infrastruktur).
_EVENT_LOOP_TRUNCATION_ABORT = "loop_truncation_abort"
#: Event hasil tool (sukses/gagal) — sumber tool failure rate yang FAKTUAL.
_EVENT_TOOL_COMPLETED = "tool_completed"
#: Event pengulangan retrieval resource yang sama (read_file/search_code/...).
_EVENT_RETRIEVAL_REPEAT = "retrieval_repeat"
#: Event verification/completion gate (bila benar-benar tercatat).
_EVENT_VERIFICATION_STRATEGY_APPLIED = "verification_strategy_applied"
_EVENT_VERIFICATION_GATE_PASSED = "verification_gate_passed"
_EVENT_VERIFICATION_NUDGE = "verification_nudge"


# --------------------------------------------------------------------------- #
# Model data.
# --------------------------------------------------------------------------- #
@dataclass
class TokenSum:
    """Akumulasi token yang hanya menjumlahkan angka yang DIKETAHUI.

    ``input_tokens``/``output_tokens``/``total_tokens`` bernilai ``None`` selama
    BELUM ada satu pun kontribusi untuk komponen tersebut (bukan 0), sehingga
    konsumen dapat membedakan "nol nyata" vs "tidak ada data".
    """

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    input_count: int = 0
    output_count: int = 0
    total_count: int = 0

    def add(self, *, input_tokens: Optional[int], output_tokens: Optional[int], total_tokens: Optional[int]) -> None:
        if input_tokens is not None:
            self.input_count += 1
            self.input_tokens = (self.input_tokens or 0) + int(input_tokens)
        if output_tokens is not None:
            self.output_count += 1
            self.output_tokens = (self.output_tokens or 0) + int(output_tokens)
        if total_tokens is not None:
            self.total_count += 1
            self.total_tokens = (self.total_tokens or 0) + int(total_tokens)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "input_known": self.input_count,
            "output_known": self.output_count,
            "total_known": self.total_count,
        }


@dataclass
class AgentTaskStats:
    """Statistik satu task Agent (dihitung sekali per task)."""

    task_id: str
    project_id: str
    prompt: str = ""
    status: str = "incomplete"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    providers: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    #: Jumlah REQUEST LLM yang benar-benar direkam (event `provider_request`).
    #: Bila log legacy tidak punya event request, nilainya = jumlah respons
    #: sukses (`llm_rounds`) agar tetap kompatibel tanpa menghitung ganda.
    llm_requests: int = 0
    #: Jumlah RESPONS sukses (event `provider_response` tanpa `error`). Satu
    #: request logis yang berhasil = satu round.
    llm_rounds: int = 0
    #: Jumlah respons GAGAL (event `provider_response` DENGAN `error`). Pada
    #: jalur continuous loop ini adalah attempt provider yang gagal — termasuk
    #: attempt sebelum retry. Observability; TIDAK diklaim sebagai request baru.
    llm_failed_responses: int = 0
    #: Jumlah attempt retry provider-level yang direkam (`provider_retry`).
    #: HANYA diisi bila event retry memang tercatat; TIDAK dikarang.
    provider_retries: int = 0
    #: Berapa kali seluruh attempt provider untuk satu request HABIS
    #: (`provider_retry_exhausted`). Observability; TIDAK diklaim sebagai request.
    provider_retry_exhausted: int = 0
    #: Berapa kali AETHER berpindah provider (`provider_fallback`). Satu entri
    #: memuat provider asal & tujuan; disimpan di ``provider_fallback_pairs``.
    provider_fallbacks: int = 0
    #: Pasangan (from_provider -> to_provider) tiap fallback yang tercatat.
    provider_fallback_pairs: List[Tuple[str, str]] = field(default_factory=list)
    #: Retry provider yang AKHIRNYA berhasil (`provider_retry_succeeded`).
    #: Berpasangan dengan ``provider_retries`` untuk menghitung recovery rate.
    provider_retry_succeeded: int = 0
    #: Jumlah event truncation yang TERCATAT (`provider_response_truncated`).
    #: Ini menghitung BERAPA KALI respons terpotong terjadi — bukan berapa kali
    #: berhasil dipulihkan. Event ini boleh muncul walau task akhirnya gagal.
    truncation_events: int = 0
    #: Jumlah abort karena batas truncation recovery tercapai
    #: (`loop_truncation_abort`). Menandai truncation yang TIDAK pulih.
    truncation_aborts: int = 0
    #: Jumlah truncation yang BENAR-BENAR dipulihkan (truncation events yang
    #: tidak berujung abort). BUKAN sekadar jumlah event truncation: respons
    #: terpotong yang akhirnya menggagalkan task TIDAK dihitung sebagai pulih.
    truncation_recoveries: int = 0
    #: True bila log memuat minimal satu event `provider_request` (sumber
    #: `llm_requests` yang eksplisit). False -> log legacy (kompatibilitas).
    requests_from_events: bool = False
    tool_calls: int = 0
    #: Pemanggilan tool per NAMA tool (`tool_called`). Sumber breakdown tool.
    tool_calls_by_tool: Dict[str, int] = field(default_factory=dict)
    #: Tool yang dilaporkan BERHASIL / GAGAL (`tool_completed.success`).
    #: Sumber tool failure rate yang FAKTUAL (bukan asumsi).
    tool_success: int = 0
    tool_failed: int = 0
    #: Pengulangan retrieval resource yang sama (`retrieval_repeat`). Menandai
    #: indikasi pembacaan/pencarian ulang; bukan error.
    retrieval_repeats: int = 0
    #: Event verification/completion gate yang tercatat (bila ada).
    verification_checks: int = 0
    verification_gate_passed: int = 0
    verification_nudges: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    usage_status: str = TASK_USAGE_UNAVAILABLE

    def day_key(self) -> Optional[str]:
        """Tanggal (UTC, ``YYYY-MM-DD``) untuk pengelompokan harian."""
        return _day_key_from_ts(self.finished_at or self.started_at)

    def provider_label(self) -> str:
        if not self.providers:
            return ""
        return self.providers[0] if len(self.providers) == 1 else "mixed"

    def model_label(self) -> str:
        if not self.models:
            return ""
        return self.models[0] if len(self.models) == 1 else "mixed"

    def to_dict(self, *, include_prompt: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "providers": list(self.providers),
            "models": list(self.models),
            "llm_requests": self.llm_requests,
            "llm_rounds": self.llm_rounds,
            "llm_failed_responses": self.llm_failed_responses,
            "provider_retries": self.provider_retries,
            "provider_retry_succeeded": self.provider_retry_succeeded,
            "provider_retry_exhausted": self.provider_retry_exhausted,
            "provider_fallbacks": self.provider_fallbacks,
            "provider_fallback_pairs": [list(p) for p in self.provider_fallback_pairs],
            "truncation_events": self.truncation_events,
            "truncation_aborts": self.truncation_aborts,
            "truncation_recoveries": self.truncation_recoveries,
            "requests_from_events": self.requests_from_events,
            "tool_calls": self.tool_calls,
            "tool_calls_by_tool": dict(self.tool_calls_by_tool),
            "tool_success": self.tool_success,
            "tool_failed": self.tool_failed,
            "retrieval_repeats": self.retrieval_repeats,
            "verification_checks": self.verification_checks,
            "verification_gate_passed": self.verification_gate_passed,
            "verification_nudges": self.verification_nudges,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "usage_status": self.usage_status,
        }
        if include_prompt:
            data["prompt"] = self.prompt
        return data


@dataclass
class ConsultantUsageStats:
    """Agregasi usage Consultant (per project; bukan task Agent)."""

    project_id: str
    sessions: int = 0
    turns: int = 0
    responses: int = 0
    usage_actual: int = 0
    usage_partial: int = 0
    usage_unavailable: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    providers: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    #: Record mentah yang dipakai untuk breakdown harian provider/model.
    records: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "sessions": self.sessions,
            "turns": self.turns,
            "responses": self.responses,
            "usage_actual": self.usage_actual,
            "usage_partial": self.usage_partial,
            "usage_unavailable": self.usage_unavailable,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "providers": list(self.providers),
            "models": list(self.models),
        }


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #
def _day_key_from_ts(value: Optional[str]) -> Optional[str]:
    """Tanggal UTC ``YYYY-MM-DD`` dari timestamp ISO-8601 AETHER.

    Semua timestamp AETHER ditulis dalam UTC (``_now_iso`` memakai
    ``timezone.utc``). Pengelompokan tanggal memakai aturan timezone yang
    KONSISTEN: ambil komponen UTC apa adanya (tanpa konversi lokal) sehingga
    tidak ada data yang terhitung dua kali / terlewat.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < 10:
        return None
    return text[:10]


def _iso_to_epoch(value: Optional[str]) -> Optional[float]:
    """Epoch detik dari timestamp ISO-8601 (toleran; None bila tidak valid)."""
    if not value or not isinstance(value, str):
        return None
    from datetime import datetime, timezone

    text = value.strip()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _usage_status_from_components(
    *, input_tokens: Optional[int], output_tokens: Optional[int], total_tokens: Optional[int]
) -> str:
    """Klasifikasi kelengkapan usage dari komponen yang diketahui."""
    has_input = input_tokens is not None
    has_output = output_tokens is not None
    has_total = total_tokens is not None
    if not (has_input or has_output or has_total):
        return TASK_USAGE_UNAVAILABLE
    if has_input and has_output:
        return TASK_USAGE_ACTUAL
    return TASK_USAGE_PARTIAL


def _unique(items: List[str]) -> List[str]:
    seen: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen


# --------------------------------------------------------------------------- #
# Agent task log reader.
# --------------------------------------------------------------------------- #
def read_agent_task_stats(root: str) -> List[AgentTaskStats]:
    """Baca SEMUA task Agent project-local menjadi daftar :class:`AgentTaskStats`.

    Isolasi: HANYA membaca ``<root>/.aether/log/*.log`` (satu project). Setiap
    file log = satu task (dihitung SEKALI, berapa pun jumlah request LLM-nya).

    Args:
        root: root project yang SUDAH tervalidasi.

    Returns:
        Daftar AgentTaskStats (urutan sesuai daftar log task).
    """
    from pathlib import Path

    from agent_ai.projects.aether_store import AetherProjectStore

    try:
        store = AetherProjectStore(root)
        log_paths = store.list_task_logs()
    except Exception:  # noqa: BLE001 - root rusak -> tanpa data (bukan fallback project lain)
        return []

    results: List[AgentTaskStats] = []
    for log_path in log_paths:
        task_id = Path(log_path).stem
        try:
            stats = _task_stats_from_log(store, task_id)
        except Exception:  # noqa: BLE001 - satu task rusak tidak menggagalkan semuanya
            continue
        if stats is not None:
            results.append(stats)
    return results


def _task_stats_from_log(store: Any, task_id: str) -> Optional[AgentTaskStats]:
    """Bangun :class:`AgentTaskStats` dari satu file Task Log.

    Menggabungkan:
        * lifecycle (status, first/last timestamp, prompt) dari
          :meth:`TaskLogReader.get_task_info`,
        * rounds/tool_calls/token dari event task log itu sendiri.

    Returns:
        AgentTaskStats, atau None bila log tidak menghasilkan info apa pun.
    """
    reader = TaskLogReader(store, task_id=task_id)
    events = reader.load_events()
    if not events:
        return None
    info = reader.get_task_info() or {}

    tokens = TokenSum()
    providers: List[str] = []
    models: List[str] = []
    request_count = 0
    llm_requests = 0
    llm_rounds = 0
    llm_failed_responses = 0
    provider_retries = 0
    provider_retry_succeeded = 0
    provider_retry_exhausted = 0
    provider_fallbacks = 0
    provider_fallback_pairs: List[Tuple[str, str]] = []
    truncation_events = 0
    truncation_aborts = 0
    truncation_recoveries = 0
    tool_calls = 0
    tool_calls_by_tool: Dict[str, int] = {}
    tool_success = 0
    tool_failed = 0
    retrieval_repeats = 0
    verification_checks = 0
    verification_gate_passed = 0
    verification_nudges = 0

    for ev in events:
        event_type = ev.get("event", "")
        data = ev.get("data", {}) or {}
        if event_type == _EVENT_PROVIDER_RESPONSE:
            # provider_response DENGAN `error` = attempt provider yang GAGAL
            # (bukan round sukses, bukan request baru). Dicatat terpisah agar
            # jumlah request/gagal/retry dapat dibedakan — tanpa mengarang.
            if "error" in data:
                llm_failed_responses += 1
                continue
            llm_rounds += 1
            provider = str(data.get("provider", "") or "")
            model = str(data.get("model", "") or "")
            if provider:
                providers.append(provider)
            if model:
                models.append(model)
            usage = response_usage(data)
            if usage:
                tokens.add(
                    input_tokens=usage.get("prompt"),
                    output_tokens=usage.get("completion"),
                    total_tokens=usage.get("total"),
                )
        elif event_type == _EVENT_PROVIDER_REQUEST:
            # Satu `provider_request` = satu pemanggilan LLM logis (diemit
            # sekali per round SEBELUM retry). Sumber request yang benar.
            request_count += 1
        elif event_type == _EVENT_PROVIDER_RETRY:
            provider_retries += 1
        elif event_type == _EVENT_PROVIDER_RETRY_SUCCEEDED:
            provider_retry_succeeded += 1
        elif event_type == _EVENT_PROVIDER_RETRY_EXHAUSTED:
            provider_retry_exhausted += 1
        elif event_type == _EVENT_PROVIDER_FALLBACK:
            provider_fallbacks += 1
            from_provider = str(data.get("from_provider", "") or "")
            to_provider = str(data.get("to_provider", "") or "")
            provider_fallback_pairs.append((from_provider, to_provider))
        elif event_type == _EVENT_PROVIDER_RESPONSE_TRUNCATED:
            # Respons TERPOTONG terjadi. Ini menandai truncation, BUKAN bukti
            # bahwa truncation berhasil dipulihkan: task bisa berakhir abort.
            truncation_events += 1
        elif event_type == _EVENT_LOOP_TRUNCATION_ABORT:
            # Truncation TIDAK berhasil dipulihkan pada batas recovery.
            truncation_aborts += 1
        elif event_type == _EVENT_TOOL_CALLED:
            tool_calls += 1
            tool_name = str(data.get("tool", "") or "") or "unknown"
            tool_calls_by_tool[tool_name] = tool_calls_by_tool.get(tool_name, 0) + 1
        elif event_type == _EVENT_TOOL_COMPLETED:
            # Hasil AKTUAL tool (success flag diset oleh executor). Kegagalan
            # dihitung dari sini, bukan diasumsikan.
            if data.get("success") is False:
                tool_failed += 1
            else:
                tool_success += 1
        elif event_type == _EVENT_RETRIEVAL_REPEAT:
            retrieval_repeats += 1
        elif event_type == _EVENT_VERIFICATION_STRATEGY_APPLIED:
            verification_checks += 1
        elif event_type == _EVENT_VERIFICATION_GATE_PASSED:
            verification_gate_passed += 1
        elif event_type == _EVENT_VERIFICATION_NUDGE:
            verification_nudges += 1

    # `llm_requests` = jumlah request yang BENAR-BENAR direkam (event
    # `provider_request`) bila log memuatnya. Log LEGACY tidak mencatat event
    # request -> jatuh ke jumlah respons sukses (rounds) agar tetap kompatibel
    # dan TIDAK menghitung ganda (respons gagal TIDAK ikut terhitung).
    requests_from_events = request_count > 0
    llm_requests = request_count if requests_from_events else llm_rounds

    started_at = info.get("first_timestamp")
    finished_at = info.get("last_timestamp")
    duration = None
    start_epoch = _iso_to_epoch(started_at)
    end_epoch = _iso_to_epoch(finished_at)
    if start_epoch is not None and end_epoch is not None and end_epoch >= start_epoch:
        duration = round(end_epoch - start_epoch, 3)

    status = str(info.get("status", "incomplete") or "incomplete")
    usage_status = _usage_status_from_components(
        input_tokens=tokens.input_tokens,
        output_tokens=tokens.output_tokens,
        total_tokens=tokens.total_tokens,
    )

    # Truncation yang BENAR-BENAR pulih = event truncation dikurangi abort.
    # (Setiap abort didahului minimal satu event truncation; pengurangan
    # dibatasi >= 0 agar data historis tak-konsisten tidak menghasilkan negatif.)
    truncation_recoveries = max(0, truncation_events - truncation_aborts)

    return AgentTaskStats(
        task_id=task_id,
        project_id="",
        prompt=str(info.get("task", "") or ""),
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration,
        providers=_unique(providers),
        models=_unique(models),
        llm_requests=llm_requests,
        llm_rounds=llm_rounds,
        llm_failed_responses=llm_failed_responses,
        provider_retries=provider_retries,
        provider_retry_succeeded=provider_retry_succeeded,
        provider_retry_exhausted=provider_retry_exhausted,
        provider_fallbacks=provider_fallbacks,
        provider_fallback_pairs=provider_fallback_pairs,
        truncation_events=truncation_events,
        truncation_aborts=truncation_aborts,
        truncation_recoveries=truncation_recoveries,
        requests_from_events=requests_from_events,
        tool_calls=tool_calls,
        tool_calls_by_tool=tool_calls_by_tool,
        tool_success=tool_success,
        tool_failed=tool_failed,
        retrieval_repeats=retrieval_repeats,
        verification_checks=verification_checks,
        verification_gate_passed=verification_gate_passed,
        verification_nudges=verification_nudges,
        input_tokens=tokens.input_tokens,
        output_tokens=tokens.output_tokens,
        total_tokens=tokens.total_tokens,
        usage_status=usage_status,
    )


# --------------------------------------------------------------------------- #
# Consultant usage reader.
# --------------------------------------------------------------------------- #
def read_consultant_usage_stats(root: str, project_id: str) -> ConsultantUsageStats:
    """Baca & agregasikan usage Consultant project-local.

    Sumber: ``<root>/.aether/consultant/usage.jsonl`` (TASK 2). Record dengan
    identitas ``(project_id, session_id, turn_id, response_index)`` yang SAMA
    di-dedupe (tidak dihitung ganda), meskipun file ditulis ulang.

    Args:
        root: root project yang SUDAH tervalidasi.
        project_id: id project (untuk label & filter record).

    Returns:
        :class:`ConsultantUsageStats`.
    """
    from pathlib import Path

    path = Path(root).joinpath(*PROJECT_USAGE_RELPATH)
    store = ConsultantUsageStore(str(path))
    records = store.read_all()

    stats = ConsultantUsageStats(project_id=project_id)
    tokens = TokenSum()
    seen: set = set()
    sessions: set = set()
    turns: set = set()
    providers: List[str] = []
    models: List[str] = []

    for obj in records:
        if not isinstance(obj, dict):
            continue
        ident = (
            obj.get("project_id", ""),
            obj.get("session_id", ""),
            obj.get("turn_id", ""),
            obj.get("response_index"),
        )
        if ident in seen:
            continue
        seen.add(ident)

        # Isolasi ketat: HANYA record milik project ini yang dihitung.
        rec_project = obj.get("project_id", "")
        if project_id and rec_project and rec_project != project_id:
            continue

        stats.responses += 1
        session_id = obj.get("session_id", "")
        turn_id = obj.get("turn_id", "")
        if session_id:
            sessions.add(session_id)
        if turn_id:
            turns.add((session_id, turn_id))

        provider = str(obj.get("provider", "") or "")
        model = str(obj.get("model", "") or "")
        if provider:
            providers.append(provider)
        if model:
            models.append(model)

        input_tokens = _as_int(obj.get("input_tokens"))
        output_tokens = _as_int(obj.get("output_tokens"))
        total_tokens = _as_int(obj.get("total_tokens"))
        tokens.add(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
        status = str(obj.get("usage_status", "") or "")
        if status == TASK_USAGE_ACTUAL:
            stats.usage_actual += 1
        elif status == TASK_USAGE_PARTIAL:
            stats.usage_partial += 1
        else:
            stats.usage_unavailable += 1
        stats.records.append(obj)

    stats.sessions = len(sessions)
    stats.turns = len(turns)
    stats.input_tokens = tokens.input_tokens
    stats.output_tokens = tokens.output_tokens
    stats.total_tokens = tokens.total_tokens
    stats.providers = _unique(providers)
    stats.models = _unique(models)
    return stats


def _as_int(value: Any) -> Optional[int]:
    """int valid (>= 0) atau None (bukan 0)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer():
        result = int(value)
        return result if result >= 0 else None
    return None


# --------------------------------------------------------------------------- #
# Filter helpers (dipakai aggregate.py).
# --------------------------------------------------------------------------- #
def aggregate_agent_tasks(tasks: List[AgentTaskStats]) -> Tuple[TokenSum, Dict[str, int]]:
    """Akumulasi token + hitungan status dari daftar task Agent."""
    tokens = TokenSum()
    status_counts: Dict[str, int] = {}
    for task in tasks:
        tokens.add(
            input_tokens=task.input_tokens,
            output_tokens=task.output_tokens,
            total_tokens=task.total_tokens,
        )
        status_counts[task.status] = status_counts.get(task.status, 0) + 1
    return tokens, status_counts


def aggregate_consultant_usage(stats: ConsultantUsageStats) -> TokenSum:
    """Akumulasi token dari record Consultant yang sudah dinormalisasi."""
    tokens = TokenSum()
    tokens.add(
        input_tokens=stats.input_tokens,
        output_tokens=stats.output_tokens,
        total_tokens=stats.total_tokens,
    )
    return tokens
