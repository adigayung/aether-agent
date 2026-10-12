"""Analitik lanjutan Statics (TASK 6) — tren, anomali, dan ekspor.

Modul ini bersifat PURE (tanpa storage/cache baru, tanpa scan lintas-project)
dan hanya bekerja atas data yang SUDAH dibaca oleh
:func:`agent_ai.statics.aggregate.build_statistics` untuk SATU project root:

    * ``tasks``   -> daftar :class:`agent_ai.statics.records.AgentTaskStats`
                     (sudah difilter periode/provider/model/status).
    * ``daily``   -> bucket harian hasil agregasi existing.
    * ``totals``  -> blok overview hasil agregasi existing.

TIGA KAPABILITAS:

1. ``build_trends`` — perbandingan periode SEKARANG vs periode SEBELUMNYA
   dengan rentang yang SETARA (durasi hari identik). Definisi metrik mengikuti
   Statistics API existing (lihat :mod:`agent_ai.statics.aggregate`): success
   rate memakai denominator task terminal, token memakai ``actual``/``partial``
   dan ``None`` bila tidak diketahui. Persentase perubahan HANYA dihitung bila
   nilai dasar valid (> 0) dan pembanding tersedia; data nol dibedakan dari
   data tidak tersedia.

2. ``build_anomalies`` — indikator sederhana & transparan dari data historis
   project yang sedang dipilih. Metode: deviasi dari median (robust, tanpa
   model/AI/layanan eksternal). Setiap indikator menjelaskan ``reason`` dan
   ``basis`` (median/MAD/ambang), serta merujuk ``task_id`` atau tanggal.
   Bila data terlalu sedikit -> ``sufficient: false`` (BUKAN "tidak ada
   anomali"). Ini indikator STATISTIK, bukan penilaian keamanan.

3. ``serialize_export`` — payload ekspor CSV/JSON dengan metadata rentang,
   project, waktu pembuatan, dan keterangan kelengkapan data. Tidak memuat
   prompt, raw response LLM, API key, atau data sensitif lain. Untuk tabel
   task, pemanggil WAJIB memberi SELURUH task yang cocok filter (bukan hanya
   halaman pagination yang tampil).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent_ai.statics.records import AgentTaskStats

#: Jumlah minimal titik data agar pembanding statistik dianggap bermakna.
MIN_ANOMALY_SAMPLES = 4
#: Jumlah minimal hari (dengan data) untuk indikator lonjakan harian.
MIN_DAILY_SAMPLES = 5
#: Faktor deviasi (kelipatan MAD/median) yang dianggap menonjol.
_ANOMALY_FACTOR = 3.0
#: Faktor minimum relatif terhadap median bila MAD = 0 (distribusi sangat rapat).
_MIN_RELATIVE_FACTOR = 2.0


# --------------------------------------------------------------------------- #
# Helpers numerik.
# --------------------------------------------------------------------------- #
def _median(values: Sequence[float]) -> Optional[float]:
    """Median (robust terhadap outlier); ``None`` bila tidak ada nilai."""
    numbers = sorted(float(v) for v in values if v is not None)
    if not numbers:
        return None
    count = len(numbers)
    mid = count // 2
    if count % 2:
        return numbers[mid]
    return (numbers[mid - 1] + numbers[mid]) / 2.0


def _mad(values: Sequence[float], center: float) -> float:
    """Median Absolute Deviation dari ``center`` (0 bila tanpa sebaran)."""
    return float(_median([abs(float(v) - center) for v in values if v is not None]) or 0.0)


def _change(current: Optional[float], base: Optional[float]) -> Dict[str, Any]:
    """Perubahan absolut + persentase dengan aturan "tidak valid".

    Aturan:
        * ``current``/``base`` ``None`` -> tidak tersedia (``available: False``).
        * persentase HANYA bila ``base`` > 0 (dasar valid). Basis 0 -> tidak
          dihitung (``percent: None``, ``percent_available: False``) sehingga
          data nol tidak pernah disajikan sebagai 100%/∞.
    """
    if current is None or base is None:
        return {
            "available": False,
            "absolute": None,
            "percent": None,
            "percent_available": False,
            "reason": "Nilai periode pembanding tidak tersedia.",
        }
    absolute = float(current) - float(base)
    if base > 0:
        percent = round(absolute / float(base), 4)
        percent_available = True
        reason = None
    else:
        percent = None
        percent_available = False
        reason = (
            "Nilai dasar 0 (bukan angka valid untuk persentase); "
            "hanya perubahan absolut yang ditampilkan."
        )
    return {
        "available": True,
        "absolute": absolute,
        # Normalisasi tipe agar JSON konsisten (int bila utuh).
        "absolute_display": int(absolute) if float(absolute).is_integer() else round(absolute, 4),
        "percent": percent,
        "percent_available": percent_available,
        "base": float(base),
        "current": float(current),
        "reason": reason,
    }


def _task_tokens(task: AgentTaskStats) -> Optional[float]:
    """Total token task bila diketahui; fallback jumlah komponen yang ada."""
    if task.total_tokens is not None:
        return float(task.total_tokens)
    components = [v for v in (task.input_tokens, task.output_tokens) if v is not None]
    return float(sum(components)) if components else None


# --------------------------------------------------------------------------- #
# 1) Perbandingan tren (periode setara sebelumnya).
# --------------------------------------------------------------------------- #
def previous_range(
    start: Optional[date], end: Optional[date]
) -> Tuple[Optional[date], Optional[date]]:
    """Rentang pembanding: periode SEBELUM ``start`` dengan durasi hari identik.

    ``None`` bila rentang tidak lengkap (tanpa rentang eksplisit tidak ada
    "periode sebelumnya" yang bermakna, jadi pembanding ditandai tidak tersedia).
    """
    if start is None or end is None:
        return None, None
    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    return prev_start, prev_end


#: Metrik yang dibandingkan (label + unit) — definisi SAMA dengan Statistics API.
_TREND_METRICS: Tuple[Tuple[str, str, str], ...] = (
    ("total_tasks", "Tasks", "count"),
    ("llm_requests", "LLM requests", "count"),
    ("input_tokens", "Input tokens", "tokens"),
    ("output_tokens", "Output tokens", "tokens"),
    ("total_tokens", "Total tokens", "tokens"),
    ("completed", "Completed", "count"),
    ("failed", "Failed", "count"),
    ("cancelled", "Cancelled", "count"),
    ("success_rate", "Success rate", "rate"),
    ("tokens_per_task", "Avg tokens/task", "tokens"),
    ("avg_duration_seconds", "Avg duration/task", "seconds"),
)


def _metric_values(totals: Dict[str, Any], avg_duration: Optional[float]) -> Dict[str, Optional[float]]:
    """Ambil nilai metrik dari blok ``totals`` kontrak existing (tanpa hitung ulang)."""
    tokens = totals.get("tokens") or {}
    status_counts = totals.get("tasks_by_status") or {}
    return {
        "total_tasks": totals.get("total_tasks"),
        "llm_requests": totals.get("llm_requests"),
        "input_tokens": tokens.get("input_tokens"),
        "output_tokens": tokens.get("output_tokens"),
        "total_tokens": tokens.get("total_tokens"),
        "completed": status_counts.get("completed"),
        "failed": status_counts.get("failed"),
        "cancelled": status_counts.get("cancelled"),
        "success_rate": totals.get("success_rate"),
        "success_rate_denominator": totals.get("success_rate_denominator"),
        "tokens_per_task": None,
        "avg_duration_seconds": avg_duration,
    }


def _avg_duration(tasks: Sequence[AgentTaskStats]) -> Optional[float]:
    durations = [t.duration_seconds for t in tasks if t.duration_seconds is not None]
    return round(sum(durations) / len(durations), 3) if durations else None


def _avg_tokens_per_task(tasks: Sequence[AgentTaskStats]) -> Optional[float]:
    per_task = [_task_tokens(t) for t in tasks]
    present = [v for v in per_task if v is not None]
    return round(sum(present) / len(present), 1) if present else None


def build_trends(
    *,
    current_tasks: Sequence[AgentTaskStats],
    previous_tasks: Sequence[AgentTaskStats],
    current_totals: Dict[str, Any],
    previous_totals: Dict[str, Any],
    available: bool,
    range_from: Optional[str],
    range_to: Optional[str],
    previous_from: Optional[date],
    previous_to: Optional[date],
) -> Dict[str, Any]:
    """Blok ``trends`` — perbandingan periode sekarang vs periode sebelumnya.

    Args:
        current_tasks/previous_tasks: task yang sudah difilter untuk masing-masing
            periode (untuk rata-rata per-task & durasi).
        current_totals/previous_totals: blok ``totals`` hasil agregasi existing.
        available: False bila rentang eksplisit tidak diberikan (tidak ada
            periode pembanding) => semua ``change`` ditandai tidak tersedia.
        range_from/range_to/previous_from/previous_to: metadata rentang.
    """
    current = _metric_values(current_totals, _avg_duration(current_tasks))
    current["tokens_per_task"] = _avg_tokens_per_task(current_tasks)
    previous = _metric_values(previous_totals, _avg_duration(previous_tasks))
    previous["tokens_per_task"] = _avg_tokens_per_task(previous_tasks)

    metrics: Dict[str, Any] = {}
    for key, label, unit in _TREND_METRICS:
        if not available:
            change = {
                "available": False,
                "absolute": None,
                "percent": None,
                "percent_available": False,
                "reason": (
                    "Tidak ada rentang eksplisit; periode sebelumnya tidak dapat "
                    "ditentukan."
                ),
            }
        else:
            change = _change(current.get(key), previous.get(key))
        metrics[key] = {
            "label": label,
            "unit": unit,
            "current": current.get(key),
            "previous": previous.get(key),
            "change": change,
        }

    # Denominator success rate dibawa eksplisit agar pembaca tidak salah tafsir.
    metrics["success_rate"]["current_denominator"] = current.get("success_rate_denominator")
    metrics["success_rate"]["previous_denominator"] = previous.get("success_rate_denominator")

    if available:
        note = (
            "Perbandingan memakai periode sebelumnya dengan jumlah hari yang sama "
            f"({previous_from.isoformat()} → {previous_to.isoformat()})."
        )
    else:
        note = (
            "Perbandingan periode memerlukan rentang 'From' dan 'To' eksplisit; "
            "tanpa rentang, hanya nilai periode berjalan yang tersedia."
        )

    return {
        "available": bool(available),
        "range": {"from": range_from, "to": range_to},
        "previous_range": {
            "from": previous_from.isoformat() if previous_from else None,
            "to": previous_to.isoformat() if previous_to else None,
        },
        "equal_length": bool(available),
        "metrics": metrics,
        "note": note,
        # Metode: definisi sama dengan Statistics API (tanpa agregasi baru).
        "method": (
            "Metrik diambil dari blok totals yang sama dengan Statistics API; "
            "persentase hanya dihitung bila nilai dasar > 0."
        ),
    }


# --------------------------------------------------------------------------- #
# 2) Deteksi anomali (median/MAD — transparan, tanpa model).
# --------------------------------------------------------------------------- #
def _high_side_threshold(values: Sequence[float], median: float, mad: float) -> Tuple[float, str]:
    """Ambang "nilai tinggi" + penjelasan basisnya.

    MAD > 0 -> ``median + 3 * 1.4826 * MAD`` (skala robust setara sigma normal).
    MAD = 0 -> ``median * 2`` (distribusi sangat rapat / banyak nilai identik).
    """
    if mad > 0:
        scaled = mad * 1.4826
        return median + _ANOMALY_FACTOR * scaled, (
            f"ambang = median {median:g} + 3 × 1.4826 × MAD {mad:g}"
        )
    return median * _MIN_RELATIVE_FACTOR, (
        f"ambang = 2 × median {median:g} (MAD 0: nilai identik)"
    )


def build_anomalies(
    *,
    tasks: Sequence[AgentTaskStats],
    daily: Sequence[Dict[str, Any]],
    failure_rate_now: Optional[float],
    failure_rate_prev: Optional[float],
    limit: int = 10,
) -> Dict[str, Any]:
    """Blok ``anomalies`` — indikator statistik sederhana dari data historis.

    Indikator:
        * ``high_token_tasks`` — total token task jauh di atas pola project.
        * ``long_duration_tasks`` — durasi task jauh di atas pola project.
        * ``daily_token_spikes`` — lonjakan token harian.
        * ``failure_rate_increase`` — kenaikan tingkat kegagalan task/tool vs
          periode sebelumnya (bila pembanding tersedia).

    Semua indikator menyertakan ``reason`` + ``basis`` dan menandai
    ``sufficient`` bila data historis belum cukup untuk pembanding bermakna.
    """
    token_values = [v for v in (_task_tokens(t) for t in tasks) if v is not None]
    dur_values = [float(t.duration_seconds) for t in tasks if t.duration_seconds is not None]

    indicators: List[Dict[str, Any]] = []

    # --- (a) task dengan token jauh lebih tinggi dari pola project ----------
    token_median = _median(token_values)
    token_mad = _mad(token_values, token_median) if token_median is not None else 0.0
    token_sufficient = len(token_values) >= MIN_ANOMALY_SAMPLES
    if not token_sufficient:
        indicators.append(
            {
                "kind": "high_token_tasks",
                "available": False,
                "sufficient": False,
                "samples": len(token_values),
                "minimum_samples": MIN_ANOMALY_SAMPLES,
                "items": [],
                "reason": (
                    f"Data belum mencukupi: hanya {len(token_values)} task dengan token "
                    f"tercatat (minimal {MIN_ANOMALY_SAMPLES} untuk pembanding)."
                ),
            }
        )
    else:
        threshold, basis = _high_side_threshold(token_values, token_median, token_mad)
        rows = []
        for task in tasks:
            value = _task_tokens(task)
            if value is None or value <= threshold:
                continue
            rows.append(
                {
                    "task_id": task.task_id,
                    "date": task.day_key(),
                    "status": task.status,
                    "value": int(value),
                    "ratio_to_median": round(value / token_median, 2) if token_median else None,
                    "reason": (
                        f"Total token {int(value)} melebihi ambang {round(threshold)} "
                        f"(median project {token_median:g} dari {len(token_values)} task)."
                    ),
                }
            )
        rows.sort(key=lambda r: r["value"], reverse=True)
        indicators.append(
            {
                "kind": "high_token_tasks",
                "available": True,
                "sufficient": True,
                "samples": len(token_values),
                "minimum_samples": MIN_ANOMALY_SAMPLES,
                "median": token_median,
                "mad": token_mad,
                "threshold": round(threshold),
                "basis": basis,
                "items": rows[:limit],
                "truncated": len(rows) > limit,
            }
        )

    # --- (b) task dengan durasi tidak biasa panjang -------------------------
    dur_median = _median(dur_values)
    dur_mad = _mad(dur_values, dur_median) if dur_median is not None else 0.0
    dur_sufficient = len(dur_values) >= MIN_ANOMALY_SAMPLES
    if not dur_sufficient:
        indicators.append(
            {
                "kind": "long_duration_tasks",
                "available": False,
                "sufficient": False,
                "samples": len(dur_values),
                "minimum_samples": MIN_ANOMALY_SAMPLES,
                "items": [],
                "reason": (
                    f"Data belum mencukupi: hanya {len(dur_values)} task dengan durasi "
                    f"terukur (minimal {MIN_ANOMALY_SAMPLES} untuk pembanding)."
                ),
            }
        )
    else:
        threshold, basis = _high_side_threshold(dur_values, dur_median, dur_mad)
        rows = []
        for task in tasks:
            value = task.duration_seconds
            if value is None or value <= threshold:
                continue
            rows.append(
                {
                    "task_id": task.task_id,
                    "date": task.day_key(),
                    "status": task.status,
                    "value": round(value, 3),
                    "ratio_to_median": round(value / dur_median, 2) if dur_median else None,
                    "reason": (
                        f"Durasi {round(value, 1)}s melebihi ambang {round(threshold, 1)}s "
                        f"(median project {dur_median:g}s dari {len(dur_values)} task)."
                    ),
                }
            )
        rows.sort(key=lambda r: r["value"], reverse=True)
        indicators.append(
            {
                "kind": "long_duration_tasks",
                "available": True,
                "sufficient": True,
                "samples": len(dur_values),
                "minimum_samples": MIN_ANOMALY_SAMPLES,
                "median": dur_median,
                "mad": dur_mad,
                "threshold": round(threshold, 3),
                "basis": basis,
                "items": rows[:limit],
                "truncated": len(rows) > limit,
            }
        )

    # --- (c) lonjakan penggunaan token harian -----------------------------
    daily_tokens = [
        float(row.get("tokens", {}).get("total_tokens"))
        for row in daily
        if isinstance(row.get("tokens"), dict) and row["tokens"].get("total_tokens") is not None
    ]
    daily_sufficient = len(daily_tokens) >= MIN_DAILY_SAMPLES
    if not daily_sufficient:
        indicators.append(
            {
                "kind": "daily_token_spikes",
                "available": False,
                "sufficient": False,
                "samples": len(daily_tokens),
                "minimum_samples": MIN_DAILY_SAMPLES,
                "items": [],
                "reason": (
                    f"Data belum mencukupi: hanya {len(daily_tokens)} hari dengan token "
                    f"tercatat (minimal {MIN_DAILY_SAMPLES} hari untuk pembanding)."
                ),
            }
        )
    else:
        daily_median = _median(daily_tokens)
        daily_mad = _mad(daily_tokens, daily_median) if daily_median is not None else 0.0
        threshold, basis = _high_side_threshold(daily_tokens, daily_median, daily_mad)
        rows = []
        for row in daily:
            tokens_block = row.get("tokens") or {}
            value = tokens_block.get("total_tokens")
            if value is None or float(value) <= threshold:
                continue
            rows.append(
                {
                    "date": row.get("date"),
                    "value": int(value),
                    "tasks": row.get("tasks"),
                    "ratio_to_median": round(float(value) / daily_median, 2) if daily_median else None,
                    "reason": (
                        f"Token harian {int(value)} melebihi ambang {round(threshold)} "
                        f"(median harian {daily_median:g} dari {len(daily_tokens)} hari berdata)."
                    ),
                }
            )
        rows.sort(key=lambda r: r["value"], reverse=True)
        indicators.append(
            {
                "kind": "daily_token_spikes",
                "available": True,
                "sufficient": True,
                "samples": len(daily_tokens),
                "minimum_samples": MIN_DAILY_SAMPLES,
                "median": daily_median,
                "mad": daily_mad,
                "threshold": round(threshold),
                "basis": basis,
                "items": rows[:limit],
                "truncated": len(rows) > limit,
            }
        )

    # --- (d) kenaikan tingkat kegagalan ------------------------------------
    if failure_rate_now is None or failure_rate_prev is None:
        failure_item = {
            "kind": "failure_rate_increase",
            "available": False,
            "sufficient": False,
            "current": failure_rate_now,
            "previous": failure_rate_prev,
            "delta": None,
            "reason": (
                "Pembanding periode sebelumnya tidak tersedia (butuh rentang eksplisit "
                "dan minimal satu task dengan hasil tercatat di kedua periode)."
            ),
        }
    else:
        delta = round(failure_rate_now - failure_rate_prev, 4)
        failure_item = {
            "kind": "failure_rate_increase",
            "available": True,
            "sufficient": True,
            "current": failure_rate_now,
            "previous": failure_rate_prev,
            "delta": delta,
            "raised": delta > 0,
            "reason": (
                f"Tingkat kegagalan periode ini {failure_rate_now:.4f} vs "
                f"periode sebelumnya {failure_rate_prev:.4f} (delta {delta:+.4f})."
            ),
        }
    indicators.append(failure_item)

    flagged = [i for i in indicators if i.get("available") and (
        i.get("items") or i.get("raised")
    )]
    insufficient = [i for i in indicators if not i.get("sufficient")]

    return {
        "available": bool(tasks or daily),
        "method": (
            "Ambang 'nilai tinggi' = median + 3 × 1.4826 × MAD (robust), atau 2 × median "
            "bila MAD = 0. Ambang ini transparan dan tidak memakai model/layanan luar."
        ),
        "disclaimer": (
            "Indikator statistik untuk membantu penelusuran, BUKAN penilaian keamanan "
            "atau bukti aktivitas berbahaya."
        ),
        "sufficient": not insufficient,
        "insufficient": [
            {"kind": i["kind"], "samples": i.get("samples"), "reason": i.get("reason")}
            for i in insufficient
        ],
        "indicators": indicators,
        "flagged_count": len(flagged),
    }


# --------------------------------------------------------------------------- #
# 3) Ekspor CSV / JSON.
# --------------------------------------------------------------------------- #
#: Header tabel task pada ekspor (tanpa prompt / data sensitif).
TASK_EXPORT_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("task_id", "task_id"),
    ("status", "status"),
    ("started_at", "started_at"),
    ("finished_at", "finished_at"),
    ("duration_seconds", "duration_seconds"),
    ("providers", "providers"),
    ("models", "models"),
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("total_tokens", "total_tokens"),
    ("usage_status", "usage_status"),
    ("llm_requests", "llm_requests"),
    ("llm_rounds", "llm_rounds"),
    ("llm_failed_responses", "llm_failed_responses"),
    ("provider_retries", "provider_retries"),
    ("provider_retry_exhausted", "provider_retry_exhausted"),
    ("provider_fallbacks", "provider_fallbacks"),
    ("tool_calls", "tool_calls"),
    ("tool_success", "tool_success"),
    ("tool_failed", "tool_failed"),
    ("retrieval_repeats", "retrieval_repeats"),
)

TOKEN_COLUMNS = {"input_tokens", "output_tokens", "total_tokens"}


def _csv_cell(value: Any) -> str:
    """Format nilai untuk satu sel CSV dengan aturan konsisten.

    * ``None`` -> string kosong (BUKAN "0") sehingga kolom yang tidak diketahui
      tetap dapat dibedakan dari nilai nol nyata.
    * List -> digabung dengan ``;``.
    * Angka -> representasi desimal tanpa pemisah ribuan (aman untuk spreadsheet).
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ";".join(str(v) for v in value)
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _csv_row(cells: Sequence[Any]) -> str:
    """Satu baris CSV dengan escaping RFC 4180.

    Sel yang memuat koma, tanda kutip, CR/LF (atau spasi tepi) diapit tanda
    kutip ganda dan tanda kutip internal diduplikasi.
    """
    rendered = []
    for cell in cells:
        text = _csv_cell(cell)
        if any(ch in text for ch in (",", '"', "\n", "\r")) or text != text.strip():
            text = '"' + text.replace('"', '""') + '"'
        rendered.append(text)
    return ",".join(rendered)


def _metadata(
    *,
    project_id: str,
    from_date: Optional[str],
    to_date: Optional[str],
    group_by: str,
    activity: str,
    provider: Optional[str],
    model: Optional[str],
    status: Optional[str],
    generated_at: str,
    task_count: int,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Metadata ekspor (rentang, project, waktu, filter, kelengkapan)."""
    data: Dict[str, Any] = {
        "format_version": 1,
        "project_id": project_id,
        "range": {"from": from_date, "to": to_date, "timezone": "UTC"},
        "filters": {
            "group_by": group_by,
            "activity": activity,
            "provider": provider,
            "model": model,
            "status": status,
        },
        "generated_at": generated_at,
        "task_rows": task_count,
        "usage_completeness_legend": (
            "actual = usage token lengkap dilaporkan; partial = hanya sebagian komponen "
            "token dilaporkan; unavailable = usage tidak dilaporkan (sel dibiarkan kosong, "
            "BUKAN 0)."
        ),
        "excluded_fields": (
            "prompt, raw LLM response, API key, dan data sensitif lain tidak disertakan."
        ),
    }
    if extra:
        data.update(extra)
    return data


def _task_records(tasks: Sequence[AgentTaskStats]) -> List[Dict[str, Any]]:
    """Baris task untuk ekspor (SELURUH task yang cocok filter, tanpa prompt)."""
    return [task.to_dict(include_prompt=False) for task in tasks]


def build_csv_export(
    *,
    tasks: Sequence[AgentTaskStats],
    metadata: Dict[str, Any],
    trend_rows: Optional[Sequence[Dict[str, Any]]] = None,
) -> str:
    """Bangun CSV multi-bagian: metadata, tren (opsional), task, ringkasan.

    Setiap bagian diawali komentar ``# section = ...`` (diabaikan spreadsheet
    bila dibuka sebagai teks) dan dipisah baris kosong. Header selalu eksplisit.
    """
    lines: List[str] = []

    # --- Bagian metadata ---------------------------------------------------
    lines.append("# section = metadata")
    lines.append(_csv_row(["key", "value"]))
    for key, value in metadata.items():
        if isinstance(value, dict):
            lines.append(_csv_row([key, json.dumps(value, ensure_ascii=False, sort_keys=True)]))
        else:
            lines.append(_csv_row([key, value]))
    lines.append("")

    # --- Bagian tren -------------------------------------------------------
    if trend_rows:
        lines.append("# section = trends")
        lines.append(
            _csv_row(["metric", "unit", "current", "previous", "absolute_change", "percent_change"])
        )
        for row in trend_rows:
            lines.append(
                _csv_row(
                    [
                        row.get("key"),
                        row.get("unit"),
                        row.get("current"),
                        row.get("previous"),
                        (row.get("change") or {}).get("absolute"),
                        (row.get("change") or {}).get("percent"),
                    ]
                )
            )
        lines.append("")

    # --- Bagian task -------------------------------------------------------
    lines.append("# section = tasks")
    lines.append(_csv_row([header for _, header in TASK_EXPORT_COLUMNS]))
    for record in _task_records(tasks):
        lines.append(_csv_row([record.get(field) for field, _ in TASK_EXPORT_COLUMNS]))
    lines.append("")

    # --- Bagian ringkasan kelengkapan -------------------------------------
    completeness = metadata.get("usage_completeness_legend", "")
    lines.append("# section = data_quality")
    lines.append(_csv_row(["note", completeness]))

    return "\r\n".join(lines) + "\r\n"


def build_json_export(
    *,
    statistics: Dict[str, Any],
    tasks: Sequence[AgentTaskStats],
    trend_rows: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Bangun payload JSON ekspor yang mempertahankan struktur + kelengkapan.

    Struktur:
        ``export_metadata`` (rentang/project/waktu/filter),
        ``totals``, ``daily``, ``breakdowns``, ``trends`` (opsional),
        ``anomalies``, ``data_quality``, ``tasks`` (SELURUH record, tanpa prompt).

    ``trends`` mempertahankan blok terstruktur dari Statistics API (bukan baris
    datar) supaya status kelengkapan (``percent_available``) tetap utuh.
    """
    payload: Dict[str, Any] = {
        "export_metadata": statistics.get("export_metadata") or {},
        "totals": statistics.get("totals"),
        "daily": statistics.get("daily"),
        "breakdowns": statistics.get("breakdowns"),
        "consultant": statistics.get("consultant"),
        "data_quality": statistics.get("data_quality"),
        "cost": statistics.get("cost"),
        "tasks": [task.to_dict(include_prompt=False) for task in tasks],
    }
    if trend_rows is not None:
        payload["trends"] = statistics.get("trends") or {}
    if "anomalies" in statistics:
        payload["anomalies"] = statistics.get("anomalies")
    return payload


def export_metadata(
    *,
    project_id: str,
    from_date: Optional[str],
    to_date: Optional[str],
    group_by: str,
    activity: str,
    provider: Optional[str],
    model: Optional[str],
    status: Optional[str],
    generated_at: str,
    task_count: int,
    data_quality: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Wrapper publik untuk :func:`_metadata` (dipakai gateway/views)."""
    base = _metadata(
        project_id=project_id,
        from_date=from_date,
        to_date=to_date,
        group_by=group_by,
        activity=activity,
        provider=provider,
        model=model,
        status=status,
        generated_at=generated_at,
        task_count=task_count,
        extra=extra,
    )
    if data_quality is not None:
        base["data_quality"] = data_quality
    return base
