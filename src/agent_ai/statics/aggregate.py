"""Agregasi statistik per-project (TASK 3) —— membangun kontrak respons JSON.

Fungsi utama :func:`build_statistics` menerima parameter yang SUDAH
divalidasi/di-resolve oleh gateway (root project + project_id) dan menghasilkan
kontrak respons lengkap:

    {
      "project_id": str,
      "generated_at": iso,
      "range": {"from": ..., "to": ..., "group_by": ..., "activity": ...},
      "filters": {"provider": ..., "model": ..., "status": ...},
      "totals": {...},          # overview
      "daily": [...],           # statistik harian (hari kosong tetap ada)
      "breakdowns": {"provider": [...], "model": [...]},
      "tasks": {"items": [...], "total": N, "limit": ..., "offset": ...,
                "sort": ..., "order": ...},
      "consultant": {...},      # ringkasan usage Consultant (activity != agent)
      "data_quality": {...},    # kelengkapan data (coverage usage)
      "trends": {...},          # TASK 6: periode ini vs periode sebelumnya
      "anomalies": {...},       # TASK 6: indikator statistik (median/MAD)
      "cost": {"available": False, "reason": "..."}  # TANPA angka biaya
    }

ATURAN AKURASI (ditegakkan di sini):
    * Hanya metrik dengan angka BENAR-BENAR diketahui yang dijumlahkan.
    * Metrik tanpa satu pun nilai -> ``None`` (BUKAN 0).
    * ``data_quality`` menjelaskan coverage usage (actual/partial/unavailable).
    * ``success_rate`` memakai denominator yang jelas (task terminal), running &
      cancelled TIDAK dianggap failed.
    * TIDAK ada angka biaya (sumber harga valid belum tersedia).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from agent_ai.statics.records import (
    AGENT_TASK_STATUSES,
    TASK_USAGE_ACTUAL,
    TASK_USAGE_PARTIAL,
    TASK_USAGE_UNAVAILABLE,
    AgentTaskStats,
    ConsultantUsageStats,
    TokenSum,
    _as_int,
    read_agent_task_stats,
    read_consultant_usage_stats,
)

#: Nilai valid parameter.
GROUP_BY_VALUES = ("day", "provider", "model", "task")
ACTIVITY_VALUES = ("all", "agent", "consultant")
STATUS_VALUES = ("completed", "failed", "cancelled", "incomplete", "running")

#: Status yang dianggap terminal (untuk denominator success rate).
#: Running/incomplete TIDAK terminal -> tidak dianggap failed.
_TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def _parse_date(value: Optional[str], *, field: str) -> Optional[date]:
    """Parse tanggal ``YYYY-MM-DD`` (atau ISO-8601 penuh) -> date UTC.

    Raises:
        ValueError: bila format tidak valid.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError(f"Parameter '{field}' harus tanggal (YYYY-MM-DD): {value!r}") from exc


def _normalize_range(
    from_value: Optional[str], to_value: Optional[str]
) -> Tuple[Optional[date], Optional[date]]:
    """Normalisasi rentang tanggal inklusif + validasi urutan.

    Raises:
        ValueError: bila format salah atau from > to.
    """
    start = _parse_date(from_value, field="from")
    end = _parse_date(to_value, field="to")
    if start is not None and end is not None and start > end:
        raise ValueError("Parameter 'from' tidak boleh lebih besar dari 'to'.")
    return start, end


def _in_range(day_key: Optional[str], start: Optional[date], end: Optional[date]) -> bool:
    """True bila ``day_key`` (YYYY-MM-DD, UTC) berada dalam rentang inklusif.

    Batas ``from``/``to`` inklusif dan tidak ada data yang terhitung dua kali:
    satu tanggal dibandingkan dengan dua batas secara eksklusif-inclusive yang
    sama untuk semua jalur (Task Log & Consultant).
    """
    if day_key is None:
        return False
    try:
        day = date.fromisoformat(day_key)
    except ValueError:
        return False
    if start is not None and day < start:
        return False
    if end is not None and day > end:
        return False
    return True


def _day_range(start: Optional[date], end: Optional[date]) -> List[str]:
    """Daftar tanggal (``YYYY-MM-DD``) inklusif antara start & end.

    Bila salah satu batas None, daftar hari diisi dari data yang tersedia oleh
    pemanggil (lihat :func:`build_daily`). Di sini diasumsikan keduanya ada.
    """
    if start is None or end is None:
        return []
    days: List[str] = []
    cursor = start
    while cursor <= end:
        days.append(cursor.isoformat())
        cursor = cursor + timedelta(days=1)
    return days


# --------------------------------------------------------------------------- #
# Filter helpers.
# --------------------------------------------------------------------------- #
def _task_matches(task: AgentTaskStats, provider: Optional[str], model: Optional[str], status: Optional[str]) -> bool:
    if status and task.status != status:
        return False
    if provider and provider not in task.providers:
        return False
    if model and model not in task.models:
        return False
    return True


def _consultant_record_matches(
    record: Dict[str, Any], provider: Optional[str], model: Optional[str], status: Optional[str]
) -> bool:
    # Consultant record tidak punya status task seperti Agent; filter status
    # hanya berlaku untuk task Agent.
    if status:
        return False
    if provider and str(record.get("provider", "") or "") != provider:
        return False
    if model and str(record.get("model", "") or "") != model:
        return False
    return True


# --------------------------------------------------------------------------- #
# Overview.
# --------------------------------------------------------------------------- #
def _build_totals(
    tasks: List[AgentTaskStats],
    consultant: ConsultantUsageStats,
    *,
    activity: str,
) -> Dict[str, Any]:
    """Bangun blok ``totals`` (overview).

    ``total_tasks`` HANYA menghitung task Agent (bukan sesi Consultant). Sesi
    Consultant disajikan terpisah di blok ``consultant`` / ``totals.consultant``.
    """
    status_counts = {name: 0 for name in AGENT_TASK_STATUSES}
    for task in tasks:
        key = task.status if task.status in status_counts else "incomplete"
        status_counts[key] = status_counts.get(key, 0) + 1

    total_tasks = len(tasks) if activity in ("all", "agent") else 0

    llm_requests = sum(t.llm_requests for t in tasks) if activity in ("all", "agent") else 0
    llm_rounds = sum(t.llm_rounds for t in tasks) if activity in ("all", "agent") else 0
    llm_failed_responses = (
        sum(t.llm_failed_responses for t in tasks) if activity in ("all", "agent") else 0
    )
    provider_retries = (
        sum(t.provider_retries for t in tasks) if activity in ("all", "agent") else 0
    )
    provider_retry_exhausted = (
        sum(t.provider_retry_exhausted for t in tasks) if activity in ("all", "agent") else 0
    )
    provider_fallbacks = (
        sum(t.provider_fallbacks for t in tasks) if activity in ("all", "agent") else 0
    )
    provider_retry_succeeded = (
        sum(t.provider_retry_succeeded for t in tasks) if activity in ("all", "agent") else 0
    )
    truncation_recoveries = (
        sum(t.truncation_recoveries for t in tasks) if activity in ("all", "agent") else 0
    )
    truncation_events = (
        sum(t.truncation_events for t in tasks) if activity in ("all", "agent") else 0
    )
    truncation_aborts = (
        sum(t.truncation_aborts for t in tasks) if activity in ("all", "agent") else 0
    )
    retrieval_repeats = (
        sum(t.retrieval_repeats for t in tasks) if activity in ("all", "agent") else 0
    )
    tool_calls = sum(t.tool_calls for t in tasks) if activity in ("all", "agent") else 0
    tool_success = sum(t.tool_success for t in tasks) if activity in ("all", "agent") else 0
    tool_failed = sum(t.tool_failed for t in tasks) if activity in ("all", "agent") else 0
    verification_checks = (
        sum(t.verification_checks for t in tasks) if activity in ("all", "agent") else 0
    )
    verification_gate_passed = (
        sum(t.verification_gate_passed for t in tasks) if activity in ("all", "agent") else 0
    )
    verification_nudges = (
        sum(t.verification_nudges for t in tasks) if activity in ("all", "agent") else 0
    )

    # Durasi: hanya dari task yang punya durasi valid (started & finished).
    durations = (
        [t.duration_seconds for t in tasks if t.duration_seconds is not None]
        if activity in ("all", "agent")
        else []
    )
    total_duration = sum(durations) if durations else None
    avg_duration = round(total_duration / len(durations), 3) if durations else None

    tokens = TokenSum()
    if activity in ("all", "agent"):
        for task in tasks:
            tokens.add(
                input_tokens=task.input_tokens,
                output_tokens=task.output_tokens,
                total_tokens=task.total_tokens,
            )
    if activity in ("all", "consultant"):
        tokens.add(
            input_tokens=consultant.input_tokens,
            output_tokens=consultant.output_tokens,
            total_tokens=consultant.total_tokens,
        )

    # Kelengkapan usage per task Agent.
    u_actual = sum(1 for t in tasks if t.usage_status == TASK_USAGE_ACTUAL)
    u_partial = sum(1 for t in tasks if t.usage_status == TASK_USAGE_PARTIAL)
    u_unavailable = sum(1 for t in tasks if t.usage_status == TASK_USAGE_UNAVAILABLE)

    # Success rate: denominator = task TERMINAL (completed+failed+cancelled).
    # Running/incomplete & cancelled TIDAK dianggap failed.
    completed = status_counts.get("completed", 0)
    failed = status_counts.get("failed", 0)
    cancelled = status_counts.get("cancelled", 0)
    incomplete = status_counts.get("incomplete", 0)
    terminal = sum(status_counts.get(s, 0) for s in _TERMINAL_STATUSES)
    success_rate = round(completed / terminal, 4) if terminal else None

    return {
        "total_tasks": total_tasks,
        "tasks_by_status": {
            "completed": completed if activity in ("all", "agent") else 0,
            "failed": failed if activity in ("all", "agent") else 0,
            "cancelled": cancelled if activity in ("all", "agent") else 0,
            "incomplete": incomplete if activity in ("all", "agent") else 0,
        },
        "success_rate": success_rate if activity in ("all", "agent") else None,
        "success_rate_denominator": terminal if activity in ("all", "agent") else 0,
        "tokens": {
            "input_tokens": tokens.input_tokens,
            "output_tokens": tokens.output_tokens,
            "total_tokens": tokens.total_tokens,
        },
        "llm_requests": llm_requests,
        "llm_rounds": llm_rounds,
        "llm_failed_responses": llm_failed_responses,
        "provider_retries": provider_retries,
        "provider_retry_succeeded": provider_retry_succeeded,
        "provider_retry_exhausted": provider_retry_exhausted,
        "provider_fallbacks": provider_fallbacks,
        "truncation_events": truncation_events,
        "truncation_aborts": truncation_aborts,
        "truncation_recoveries": truncation_recoveries,
        "retrieval_repeats": retrieval_repeats,
        "tool_calls": tool_calls,
        "tool_success": tool_success,
        "tool_failed": tool_failed,
        "verification_checks": verification_checks,
        "verification_gate_passed": verification_gate_passed,
        "verification_nudges": verification_nudges,
        "duration": {
            "total_seconds": total_duration,
            "average_seconds": avg_duration,
            "measured_tasks": len(durations),
        },
        "usage_completeness": {
            "actual": u_actual,
            "partial": u_partial,
            "unavailable": u_unavailable,
        },
        "consultant": consultant.to_dict() if activity in ("all", "consultant") else None,
    }


# --------------------------------------------------------------------------- #
# Daily.
# --------------------------------------------------------------------------- #
def _build_daily(
    tasks: List[AgentTaskStats],
    consultant_records: List[Dict[str, Any]],
    *,
    start: Optional[date],
    end: Optional[date],
    activity: str,
) -> List[Dict[str, Any]]:
    """Statistik harian.

    Hari tanpa aktivitas TETAP disertakan (nilai token ``None``, hitungan 0)
    ketika rentang eksplisit diberikan, sehingga grafik frontend dapat
    menampilkan rentang waktu secara benar.
    """
    buckets: Dict[str, Dict[str, Any]] = {}

    def _bucket(day: str) -> Dict[str, Any]:
        if day not in buckets:
            buckets[day] = {
                "date": day,
                "tasks": 0,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "tokens": {"input_tokens": None, "output_tokens": None, "total_tokens": None},
                "llm_requests": 0,
                "tool_calls": 0,
                "consultant_responses": 0,
                "_tokens": TokenSum(),
            }
        return buckets[day]

    if activity in ("all", "agent"):
        for task in tasks:
            day = task.day_key()
            if day is None:
                continue
            bucket = _bucket(day)
            bucket["tasks"] += 1
            status = task.status if task.status in ("completed", "failed", "cancelled") else None
            if status:
                bucket[status] += 1
            bucket["llm_requests"] += task.llm_requests
            bucket["tool_calls"] += task.tool_calls
            bucket["_tokens"].add(
                input_tokens=task.input_tokens,
                output_tokens=task.output_tokens,
                total_tokens=task.total_tokens,
            )

    if activity in ("all", "consultant"):
        for record in consultant_records:
            ts = record.get("timestamp")
            day = _day_key_from_epoch(ts)
            if day is None:
                continue
            bucket = _bucket(day)
            bucket["consultant_responses"] += 1
            bucket["_tokens"].add(
                input_tokens=_as_int(record.get("input_tokens")),
                output_tokens=_as_int(record.get("output_tokens")),
                total_tokens=_as_int(record.get("total_tokens")),
            )

    # Isi hari kosong di dalam rentang eksplisit.
    if start is not None and end is not None:
        for day in _day_range(start, end):
            _bucket(day)

    result: List[Dict[str, Any]] = []
    for day in sorted(buckets.keys()):
        bucket = buckets[day]
        token_sum: TokenSum = bucket.pop("_tokens")
        bucket["tokens"] = {
            "input_tokens": token_sum.input_tokens,
            "output_tokens": token_sum.output_tokens,
            "total_tokens": token_sum.total_tokens,
        }
        result.append(bucket)
    return result


def _day_key_from_epoch(value: Any) -> Optional[str]:
    """Tanggal UTC ``YYYY-MM-DD`` dari epoch detik (untuk record Consultant)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.date().isoformat()


# --------------------------------------------------------------------------- #
# Breakdowns provider/model.
# --------------------------------------------------------------------------- #
def _empty_breakdown_bucket(key: str) -> Dict[str, Any]:
    return {
        "key": key,
        "requests": 0,
        "tokens": {"input_tokens": None, "output_tokens": None, "total_tokens": None},
        "usage_actual": 0,
        "usage_partial": 0,
        "usage_unavailable": 0,
        "_tokens": TokenSum(),
        "_tasks": set(),
    }


def _task_breakdown_key(task: AgentTaskStats, dimension: str) -> str:
    if dimension == "provider":
        return task.provider_label() or "unknown"
    return task.model_label() or "unknown"


def _build_breakdowns(
    tasks: List[AgentTaskStats],
    consultant_records: List[Dict[str, Any]],
    *,
    dimension: str,
    activity: str,
) -> List[Dict[str, Any]]:
    """Agregasi per provider ATAU per model.

    Untuk task Agent, satu task memberi kontribusi ke SATU kunci (provider/model
    label). Jika task melibatkan lebih dari satu provider/model (mis. fallback),
    label menjadi ``mixed`` agar tidak membingungkan.
    """
    buckets: Dict[str, Dict[str, Any]] = {}

    def _bucket(key: str) -> Dict[str, Any]:
        if key not in buckets:
            buckets[key] = _empty_breakdown_bucket(key)
        return buckets[key]

    if activity in ("all", "agent"):
        for task in tasks:
            key = _task_breakdown_key(task, dimension)
            bucket = _bucket(key)
            bucket["requests"] += task.llm_requests
            bucket["_tasks"].add(task.task_id)
            if task.usage_status == TASK_USAGE_ACTUAL:
                bucket["usage_actual"] += 1
            elif task.usage_status == TASK_USAGE_PARTIAL:
                bucket["usage_partial"] += 1
            else:
                bucket["usage_unavailable"] += 1
            bucket["_tokens"].add(
                input_tokens=task.input_tokens,
                output_tokens=task.output_tokens,
                total_tokens=task.total_tokens,
            )

    if activity in ("all", "consultant"):
        for record in consultant_records:
            key = (
                str(record.get("provider", "") or "") or "unknown"
                if dimension == "provider"
                else str(record.get("model", "") or "") or "unknown"
            )
            bucket = _bucket(key)
            bucket["requests"] += 1
            status = str(record.get("usage_status", "") or "")
            if status == TASK_USAGE_ACTUAL:
                bucket["usage_actual"] += 1
            elif status == TASK_USAGE_PARTIAL:
                bucket["usage_partial"] += 1
            else:
                bucket["usage_unavailable"] += 1
            bucket["_tokens"].add(
                input_tokens=_as_int(record.get("input_tokens")),
                output_tokens=_as_int(record.get("output_tokens")),
                total_tokens=_as_int(record.get("total_tokens")),
            )

    result: List[Dict[str, Any]] = []
    for key in sorted(buckets.keys()):
        bucket = buckets[key]
        token_sum: TokenSum = bucket.pop("_tokens")
        task_ids = bucket.pop("_tasks")
        bucket["tasks"] = len(task_ids)
        bucket["tokens"] = {
            "input_tokens": token_sum.input_tokens,
            "output_tokens": token_sum.output_tokens,
            "total_tokens": token_sum.total_tokens,
        }
        result.append(bucket)
    return result


# --------------------------------------------------------------------------- #
# Task detail list (pagination + sorting).
# --------------------------------------------------------------------------- #
def _task_sort_key(task: AgentTaskStats, sort: str) -> Any:
    if sort == "total_tokens":
        return task.total_tokens if task.total_tokens is not None else -1
    if sort == "duration":
        return task.duration_seconds if task.duration_seconds is not None else -1.0
    if sort == "status":
        return task.status or ""
    if sort == "task_id":
        return task.task_id or ""
    # default: terbaru
    return task.finished_at or task.started_at or ""


def _build_tasks_block(
    tasks: List[AgentTaskStats],
    *,
    sort: str,
    order: str,
    limit: int,
    offset: int,
) -> Dict[str, Any]:
    """Blok detail task dengan sorting & pagination.

    Sorting default: ``total_tokens`` descending sehingga task paling boros
    token tampil lebih dulu (tanpa perlu membaca ulang log per baris — semua
    metrik sudah ter-normalisasi di :class:`AgentTaskStats`).
    """
    reverse = order != "asc"
    ordered = sorted(tasks, key=lambda t: _task_sort_key(t, sort), reverse=reverse)
    total = len(ordered)
    window = ordered[offset : offset + limit] if limit > 0 else []
    return {
        "items": [t.to_dict(include_prompt=True) for t in window],
        "total": total,
        "limit": limit,
        "offset": offset,
        "sort": sort,
        "order": order,
    }


# --------------------------------------------------------------------------- #
# Data quality.
# --------------------------------------------------------------------------- #
def _build_data_quality(
    tasks: List[AgentTaskStats],
    consultant: ConsultantUsageStats,
    *,
    activity: str,
) -> Dict[str, Any]:
    """Ringkasan kelengkapan data & catatan keterbatasan (anti misleading)."""
    task_actual = sum(1 for t in tasks if t.usage_status == TASK_USAGE_ACTUAL)
    task_partial = sum(1 for t in tasks if t.usage_status == TASK_USAGE_PARTIAL)
    task_unavailable = sum(1 for t in tasks if t.usage_status == TASK_USAGE_UNAVAILABLE)

    notes: List[str] = []
    if task_partial:
        notes.append(
            f"{task_partial} task Agent melaporkan usage PARSIAL (hanya sebagian komponen token)."
        )
    if task_unavailable:
        notes.append(
            f"{task_unavailable} task Agent TIDAK melaporkan usage token (provider tidak mengirim usage)."
        )
    if activity in ("all", "consultant") and consultant.usage_unavailable:
        notes.append(
            f"{consultant.usage_unavailable} respons Consultant TIDAK melaporkan usage token."
        )
    notes.append(
        "Model dicatat dari event/log saat tersedia; nilai 'unknown' berarti tidak dilaporkan."
    )

    return {
        "usage_completeness": {
            "agent_tasks": {
                "actual": task_actual,
                "partial": task_partial,
                "unavailable": task_unavailable,
                "total": len(tasks),
            },
            "consultant_responses": {
                "actual": consultant.usage_actual,
                "partial": consultant.usage_partial,
                "unavailable": consultant.usage_unavailable,
                "total": consultant.responses,
            },
        },
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# Efficiency & reliability analytics (TASK 5).
# --------------------------------------------------------------------------- #
def _build_efficiency(
    tasks: List[AgentTaskStats],
    *,
    activity: str,
) -> Dict[str, Any]:
    """Blok ``efficiency`` — penggunaan sumber daya per task (Agent only).

    Definisi & denominator EKSPLISIT (anti misleading):
        * ``tasks``            = jumlah task Agent yang dihitung.
        * ``llm_requests``     = SUM ``provider_request`` (request LLM logis).
        * ``llm_rounds``       = SUM respons provider SUKSES.
        * ``llm_failed_responses`` = SUM respons provider GAGAL (attempt).
        * ``tool_calls``       = SUM ``tool_called``.
        * ``tool_success``/``tool_failed`` = dari ``tool_completed.success``.
        * ``tool_failure_rate`` denominator = ``tool_success + tool_failed``
          (HANYA tool yang hasilnya tercatat), None bila tidak ada hasil.
        * ``tokens_per_task``  memakai denominator ``token_measured_tasks``
          (task yang PUNYA minimal satu komponen token), bukan semua task.
        * ``retrieval_repeats`` = SUM ``retrieval_repeat``.
    Consultant tidak diikutkan: metrik ini khusus task Agent.
    """
    included = activity in ("all", "agent")
    tasks_in = tasks if included else []

    # Token: hanya task yang punya minimal satu komponen diketahui.
    token_measured = [
        t
        for t in tasks_in
        if t.total_tokens is not None or t.input_tokens is not None or t.output_tokens is not None
    ]
    total_tokens = sum(t.total_tokens or 0 for t in token_measured) if token_measured else None
    input_tokens = (
        sum(t.input_tokens or 0 for t in token_measured if t.input_tokens is not None)
        if any(t.input_tokens is not None for t in token_measured)
        else None
    )
    output_tokens = (
        sum(t.output_tokens or 0 for t in token_measured if t.output_tokens is not None)
        if any(t.output_tokens is not None for t in token_measured)
        else None
    )
    # Rata-rata total token per task yang punya datum total token (bukan semua).
    total_token_tasks = [t for t in tasks_in if t.total_tokens is not None]
    avg_total = (
        round(sum(t.total_tokens for t in total_token_tasks) / len(total_token_tasks), 1)
        if total_token_tasks
        else None
    )
    avg_input = (
        round(
            sum(t.input_tokens for t in tasks_in if t.input_tokens is not None)
            / len([t for t in tasks_in if t.input_tokens is not None]),
            1,
        )
        if any(t.input_tokens is not None for t in tasks_in)
        else None
    )
    avg_output = (
        round(
            sum(t.output_tokens for t in tasks_in if t.output_tokens is not None)
            / len([t for t in tasks_in if t.output_tokens is not None]),
            1,
        )
        if any(t.output_tokens is not None for t in tasks_in)
        else None
    )

    tool_success = sum(t.tool_success for t in tasks_in)
    tool_failed = sum(t.tool_failed for t in tasks_in)
    tool_outcomes = tool_success + tool_failed
    tool_failure_rate = round(tool_failed / tool_outcomes, 4) if tool_outcomes else None

    # Number of tool calls whose outcome is RECORDED vs total calls.
    tool_calls = sum(t.tool_calls for t in tasks_in)

    return {
        "available": bool(included and tasks_in),
        "tasks": len(tasks_in),
        "llm_requests": sum(t.llm_requests for t in tasks_in) if included else 0,
        "llm_rounds": sum(t.llm_rounds for t in tasks_in) if included else 0,
        "llm_failed_responses": (
            sum(t.llm_failed_responses for t in tasks_in) if included else 0
        ),
        "tool_calls": tool_calls,
        "tool_success": tool_success,
        "tool_failed": tool_failed,
        "tool_failure_rate": tool_failure_rate,
        "tool_failure_rate_denominator": tool_outcomes,
        "retrieval_repeats": sum(t.retrieval_repeats for t in tasks_in) if included else 0,
        "tokens": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        },
        "tokens_per_task": {
            "total_per_task": avg_total,
            "input_per_task": avg_input,
            "output_per_task": avg_output,
            # Denominator EKSPLISIT untuk rata-rata total token per task.
            "total_denominator": len(total_token_tasks),
            "measured_tasks": len(token_measured),
        },
        # Context compaction: TIDAK ada event yang tercatat saat ini -> None.
        "context_compaction": {
            "available": False,
            "events": None,
            "reason": (
                "Tidak ada event compaction yang tercatat pada Task Log; "
                "tidak dihitung agar tidak menampilkan angka menyesatkan."
            ),
        },
    }


def _build_reliability(
    tasks: List[AgentTaskStats],
    *,
    activity: str,
) -> Dict[str, Any]:
    """Blok ``reliability`` — retry/fallback/kegagalan yang NYATA tercatat.

    Semua hitungan berasal dari event historis Task Log:
        * ``provider_retries``            = ``provider_retry``.
        * ``provider_retry_succeeded``    = ``provider_retry_succeeded``.
        * ``provider_retry_exhausted``    = ``provider_retry_exhausted``.
        * ``provider_fallbacks``          = ``provider_fallback`` (+ pasangan
          provider asal -> tujuan di ``fallback_transitions``).
        * ``truncation_events``           = ``provider_response_truncated`` —
          truncation YANG TERJADI (bukan bukti pemulihan sukses).
        * ``truncation_aborts``           = ``loop_truncation_abort`` —
          truncation yang TIDAK berhasil dipulihkan (task dihentikan gagal).
        * ``truncation_recoveries``       = truncation yang BENAR-BENAR pulih
          (events - aborts), BUKAN sekadar jumlah event truncation.
        * ``failed_responses``            = respons provider GAGAL.
        * ``tasks_failed``/``tasks_cancelled`` = status terminal task Agent.
        * ``max_steps_aborts``            = None (tidak ada event yang
          merekam abort karena batas max_steps).
        * verification/completion gate    = ``verification_*`` (bila ada).
    """
    included = activity in ("all", "agent")
    tasks_in = tasks if included else []

    retries = sum(t.provider_retries for t in tasks_in) if included else 0
    retry_succeeded = sum(t.provider_retry_succeeded for t in tasks_in) if included else 0
    retry_exhausted = sum(t.provider_retry_exhausted for t in tasks_in) if included else 0

    # Transisi fallback per pasangan (from -> to), tanpa menghitung ganda.
    transitions: Dict[Tuple[str, str], int] = {}
    for t in tasks_in:
        for pair in t.provider_fallback_pairs:
            transitions[pair] = transitions.get(pair, 0) + 1
    fallback_transitions = [
        {"from_provider": a or "unknown", "to_provider": b or "unknown", "count": c}
        for (a, b), c in sorted(transitions.items())
    ]

    failed_tasks = sum(1 for t in tasks_in if t.status == "failed")
    cancelled_tasks = sum(1 for t in tasks_in if t.status == "cancelled")

    return {
        "available": bool(included and tasks_in),
        "provider_retries": retries,
        "provider_retry_succeeded": retry_succeeded,
        "provider_retry_exhausted": retry_exhausted,
        "provider_fallbacks": sum(t.provider_fallbacks for t in tasks_in) if included else 0,
        "fallback_transitions": fallback_transitions,
        "failed_responses": (
            sum(t.llm_failed_responses for t in tasks_in) if included else 0
        ),
        "successful_responses": (
            sum(t.llm_rounds for t in tasks_in) if included else 0
        ),
        "truncation_events": (
            sum(t.truncation_events for t in tasks_in) if included else 0
        ),
        "truncation_aborts": (
            sum(t.truncation_aborts for t in tasks_in) if included else 0
        ),
        "truncation_recoveries": (
            sum(t.truncation_recoveries for t in tasks_in) if included else 0
        ),
        "tasks_failed": failed_tasks,
        "tasks_cancelled": cancelled_tasks,
        "verification": {
            "checks": sum(t.verification_checks for t in tasks_in) if included else 0,
            "gate_passed": (
                sum(t.verification_gate_passed for t in tasks_in) if included else 0
            ),
            "nudges": sum(t.verification_nudges for t in tasks_in) if included else 0,
        },
        # Abort karena batas max_steps: tidak ada event yang merekamnya.
        "max_steps_aborts": {
            "available": False,
            "count": None,
            "reason": (
                "Task Log belum merekam event abort karena batas max_steps; "
                "ditandai gap (bukan nol)."
            ),
        },
    }


def _build_tool_usage(
    tasks: List[AgentTaskStats],
    *,
    activity: str,
) -> List[Dict[str, Any]]:
    """Breakdown pemanggilan tool per NAMA tool (Agent only).

    Sumber: event ``tool_called``. Satu task memberi kontribusi ke setiap tool
    yang benar-benar dipanggil. Diurutkan menurun berdasarkan jumlah panggilan.
    """
    if activity not in ("all", "agent"):
        return []
    counts: Dict[str, int] = {}
    for task in tasks:
        for name, n in task.tool_calls_by_tool.items():
            counts[name] = counts.get(name, 0) + n
    rows = [{"tool": name, "calls": n} for name, n in counts.items()]
    rows.sort(key=lambda r: (-r["calls"], r["tool"]))
    return rows


def _build_attention(
    tasks: List[AgentTaskStats],
    *,
    activity: str,
    limit: int = 10,
) -> Dict[str, Any]:
    """Blok ``attention`` — task yang menonjol dari data AKTUAL.

    Kriteria (berdasarkan angka yang tercatat, tanpa ambang sintetis):
        * token tertinggi (total token diketahui),
        * durasi terpanjang (durasi valid),
        * kegagalan reliabilitas (failed/cancelled atau ada retry exhausted).
    Setiap item menyertakan ``task_id`` agar dapat ditelusuri via History.
    """
    if activity not in ("all", "agent"):
        return {"available": False, "high_tokens": [], "long_running": [], "reliability_flags": []}

    def _item(t: AgentTaskStats) -> Dict[str, Any]:
        return {
            "task_id": t.task_id,
            "status": t.status,
            "total_tokens": t.total_tokens,
            "duration_seconds": t.duration_seconds,
            "provider_fallbacks": t.provider_fallbacks,
            "provider_retry_exhausted": t.provider_retry_exhausted,
        }

    high_tokens = sorted(
        [t for t in tasks if t.total_tokens is not None],
        key=lambda t: t.total_tokens,
        reverse=True,
    )[:limit]
    long_running = sorted(
        [t for t in tasks if t.duration_seconds is not None],
        key=lambda t: t.duration_seconds,
        reverse=True,
    )[:limit]
    reliability_flags = [
        t for t in tasks if t.status in ("failed", "cancelled") or t.provider_retry_exhausted
    ]
    reliability_flags.sort(
        key=lambda t: (t.status not in ("failed", "cancelled"), t.provider_retry_exhausted),
        reverse=True,
    )

    return {
        "available": bool(tasks),
        "high_tokens": [_item(t) for t in high_tokens],
        "long_running": [_item(t) for t in long_running],
        "reliability_flags": [_item(t) for t in reliability_flags[:limit]],
    }


# --------------------------------------------------------------------------- #
# Public entry point.
# --------------------------------------------------------------------------- #
def _aggregate_consultant_records(
    project_id: str, records: List[Dict[str, Any]]
) -> ConsultantUsageStats:
    """Agregasi record Consultant yang SUDAH difilter (periode sebelumnya).

    Dipakai TASK 6 untuk membangun pembanding tren. Dedupe identitas
    ``(project_id, session_id, turn_id, response_index)`` sama seperti pembacaan
    normal, sehingga record tidak dihitung ganda saat file ditulis ulang.
    """
    stats = ConsultantUsageStats(project_id=project_id)
    seen: set = set()
    sessions: set = set()
    turns: set = set()
    providers: List[str] = []
    models: List[str] = []
    tokens = TokenSum()
    for record in records:
        ident = (
            record.get("project_id", ""),
            record.get("session_id", ""),
            record.get("turn_id", ""),
            record.get("response_index"),
        )
        if ident in seen:
            continue
        seen.add(ident)

        stats.responses += 1
        session_id = record.get("session_id", "")
        turn_id = record.get("turn_id", "")
        if session_id:
            sessions.add(session_id)
        if turn_id:
            turns.add((session_id, turn_id))

        provider = str(record.get("provider", "") or "")
        model = str(record.get("model", "") or "")
        if provider:
            providers.append(provider)
        if model:
            models.append(model)

        tokens.add(
            input_tokens=_as_int(record.get("input_tokens")),
            output_tokens=_as_int(record.get("output_tokens")),
            total_tokens=_as_int(record.get("total_tokens")),
        )
        status = str(record.get("usage_status", "") or "")
        if status == TASK_USAGE_ACTUAL:
            stats.usage_actual += 1
        elif status == TASK_USAGE_PARTIAL:
            stats.usage_partial += 1
        else:
            stats.usage_unavailable += 1
        stats.records.append(record)

    stats.sessions = len(sessions)
    stats.turns = len(turns)
    stats.input_tokens = tokens.input_tokens
    stats.output_tokens = tokens.output_tokens
    stats.total_tokens = tokens.total_tokens
    stats.providers = sorted(set(providers))
    stats.models = sorted(set(models))
    return stats


def _empty_totals(activity: str) -> Dict[str, Any]:
    """Blok ``totals`` kosong (periode pembanding tanpa data).

    Dipakai ketika tidak ada task Consultant pada periode sebelumnya sehingga
    ``_build_totals`` tetap bisa dijalankan tanpa membaca ulang sumber. Semua
    metrik bernilai ``None``/0 sesuai aturan "tidak tersedia" kontrak.
    """
    return _build_totals([], ConsultantUsageStats(project_id=""), activity=activity)


def build_statistics(
    *,
    project_id: str,
    root: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    group_by: str = "day",
    activity: str = "all",
    provider: Optional[str] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
    sort: str = "total_tokens",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """Bangun kontrak statistik lengkap untuk SATU project.

    Args:
        project_id: id project (sudah divalidasi gateway).
        root: root project (sudah divalidasi gateway) — batas isolasi.
        from_date/to_date: rentang tanggal inklusif ``YYYY-MM-DD`` (opsional).
        group_by: ``day`` | ``provider`` | ``model`` | ``task``.
        activity: ``all`` | ``agent`` | ``consultant``.
        provider/model/status: filter opsional.
        sort/order/limit/offset: kontrol daftar detail task.

    Returns:
        Dict kontrak respons (lihat docstring modul).

    Raises:
        ValueError: bila parameter tidak valid.
    """
    if group_by not in GROUP_BY_VALUES:
        raise ValueError(f"group_by harus salah satu dari {list(GROUP_BY_VALUES)}.")
    if activity not in ACTIVITY_VALUES:
        raise ValueError(f"activity harus salah satu dari {list(ACTIVITY_VALUES)}.")
    if status and status not in STATUS_VALUES:
        raise ValueError(f"status harus salah satu dari {list(STATUS_VALUES)}.")
    if sort not in ("total_tokens", "duration", "status", "task_id", "time"):
        raise ValueError("sort harus salah satu dari ['total_tokens','duration','status','task_id','time'].")
    if order not in ("asc", "desc"):
        raise ValueError("order harus 'asc' atau 'desc'.")
    if limit < 0:
        raise ValueError("limit tidak boleh negatif.")
    if offset < 0:
        raise ValueError("offset tidak boleh negatif.")

    start, end = _normalize_range(from_date, to_date)

    # TASK 6: periode pembanding = rentang sebelumnya dengan panjang hari SAMA.
    from agent_ai.statics.analytics import previous_range

    prev_start, prev_end = previous_range(start, end)

    # ---- Baca sumber (SATU root project; tidak ada scan global). ---------- #
    all_tasks = read_agent_task_stats(root)
    consultant = read_consultant_usage_stats(root, project_id)

    # ---- Filter tanggal + provider/model/status. ------------------------- #
    tasks: List[AgentTaskStats] = []
    for task in all_tasks:
        if not _in_range(task.day_key(), start, end):
            continue
        if not _task_matches(task, provider, model, status):
            continue
        # Label project pada setiap task.
        task.project_id = project_id
        tasks.append(task)

    # Consultant records difilter sama (tanggal + provider/model).
    consultant_records: List[Dict[str, Any]] = []
    for record in consultant.records:
        day = _day_key_from_epoch(record.get("timestamp"))
        if not _in_range(day, start, end):
            continue
        if not _consultant_record_matches(record, provider, model, status):
            continue
        consultant_records.append(record)

    # Consultant hanya relevan untuk activity all/consultant.
    if activity == "agent":
        consultant_records = []

    # ---- Bangun blok-blok kontrak. --------------------------------------- #
    totals = _build_totals(tasks, consultant, activity=activity)
    daily = _build_daily(tasks, consultant_records, start=start, end=end, activity=activity)

    if group_by == "provider":
        breakdowns = {"provider": _build_breakdowns(tasks, consultant_records, dimension="provider", activity=activity)}
    elif group_by == "model":
        breakdowns = {"model": _build_breakdowns(tasks, consultant_records, dimension="model", activity=activity)}
    else:
        breakdowns = {
            "provider": _build_breakdowns(tasks, consultant_records, dimension="provider", activity=activity),
            "model": _build_breakdowns(tasks, consultant_records, dimension="model", activity=activity),
        }

    tasks_block = _build_tasks_block(tasks, sort=sort, order=order, limit=limit, offset=offset)
    data_quality = _build_data_quality(tasks, consultant, activity=activity)

    # TASK 5: analitik efisiensi & reliability (dari event yang SUDAH tercatat).
    efficiency = _build_efficiency(tasks, activity=activity)
    reliability = _build_reliability(tasks, activity=activity)
    tool_usage = _build_tool_usage(tasks, activity=activity)
    attention = _build_attention(tasks, activity=activity)

    # TASK 6: perbandingan tren (periode sebelumnya, rentang SETARA).
    from agent_ai.statics.analytics import build_trends

    range_available = start is not None and end is not None
    prev_tasks: List[AgentTaskStats] = []
    prev_totals: Optional[Dict[str, Any]] = None
    if range_available:
        # Baca sekali lagi dari sumber project-LOCAL yang sama (isolasi tetap:
        # root yang sudah tervalidasi; tidak ada agregasi lintas-project).
        for task in all_tasks:
            if not _in_range(task.day_key(), prev_start, prev_end):
                continue
            if not _task_matches(task, provider, model, status):
                continue
            task.project_id = project_id
            prev_tasks.append(task)
        prev_consultant = ConsultantUsageStats(project_id=project_id)
        if activity in ("all", "consultant"):
            prev_records = [
                record
                for record in consultant.records
                if _in_range(_day_key_from_epoch(record.get("timestamp")), prev_start, prev_end)
                and _consultant_record_matches(record, provider, model, status)
            ]
            prev_consultant = _aggregate_consultant_records(project_id, prev_records)
        prev_totals = _build_totals(prev_tasks, prev_consultant, activity=activity)

    trends = build_trends(
        current_tasks=tasks,
        previous_tasks=prev_tasks,
        current_totals=totals,
        previous_totals=prev_totals or _empty_totals(activity),
        available=bool(range_available),
        range_from=from_date,
        range_to=to_date,
        previous_from=prev_start,
        previous_to=prev_end,
    )

    # TASK 6: indikator anomali sederhana (median/MAD; data historis project ini).
    from agent_ai.statics.analytics import build_anomalies

    tool_outcomes = efficiency["tool_failure_rate_denominator"]
    tool_failure_now = efficiency["tool_failure_rate"]
    tool_failure_prev: Optional[float] = None
    if range_available:
        prev_success = sum(t.tool_success for t in prev_tasks)
        prev_failed = sum(t.tool_failed for t in prev_tasks)
        prev_outcomes = prev_success + prev_failed
        tool_failure_prev = round(prev_failed / prev_outcomes, 4) if prev_outcomes else None
    anomalies = build_anomalies(
        tasks=tasks,
        daily=daily,
        # Indikator kegagalan memakai tingkat kegagalan TOOL (hasil tercatat),
        # sejalan dengan blok efficiency; task failed sudah terlihat di trend.
        failure_rate_now=tool_failure_now if tool_outcomes else None,
        failure_rate_prev=tool_failure_prev,
    )

    # Range efektif yang dilaporkan (echo parameter asli, bukan hasil parsing).
    return {
        "project_id": project_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "range": {
            "from": from_date,
            "to": to_date,
            "timezone": "UTC",
            "group_by": group_by,
            "activity": activity,
        },
        "filters": {
            "provider": provider,
            "model": model,
            "status": status,
        },
        "totals": totals,
        "daily": daily,
        "breakdowns": breakdowns,
        "tasks": tasks_block,
        # Blok analitik baru (TASK 5) — hanya Agent; Consultant punya blok sendiri.
        "efficiency": efficiency,
        "reliability": reliability,
        "tool_usage": tool_usage,
        "attention": attention,
        # Blok analitik lanjutan (TASK 6) — additive, definisi metrik konsisten
        # dengan blok existing di atas.
        "trends": trends,
        "anomalies": anomalies,
        "consultant": consultant.to_dict() if activity in ("all", "consultant") else None,
        "data_quality": data_quality,
        "cost": {
            "available": False,
            "reason": (
                "Sumber harga model yang valid belum tersedia; biaya tidak dihitung "
                "untuk menghindari angka menyesatkan."
            ),
        },
    }
