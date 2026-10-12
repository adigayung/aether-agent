"""Statistics aggregation (TASK 3/5) — project-local, read-only.

TASK 5 menambahkan blok EFISIENSI & RELIABILITY (``efficiency``,
``reliability``, ``tool_usage``, ``attention``) yang dihitung dari event Task
Log yang SUDAH ada (tool_called/tool_completed/retrieval_repeat/provider_*),
tanpa storage/cache baru dan tanpa instrumentation tambahan di runtime.

Modul ini menyediakan agregasi PURE (tanpa penyimpanan/cache baru) atas data
historis yang SUDAH ada di dalam satu project root:

    * Agent      -> ``<root>/.aether/log/<task_id>.log`` (Task Log, via
                    :class:`agent_ai.projects.aether_store.TaskLogReader`).
    * Consultant -> ``<root>/.aether/consultant/usage.jsonl`` (telemetry TASK 2,
                    via :class:`agent_ai.consultant.telemetry.ConsultantUsageStore`).

PRINSIP:
    * TIDAK ada storage agregat/cache persisten kedua.
    * TIDAK ada scan global lintas-project: pembacaan HANYA pada satu root
      project yang sudah tervalidasi (isolasi project).
    * TIDAK membaca ``ResponseLog`` untuk menjumlahkan usage Agent: angka yang
      sama sudah tersedia di Task Log (event ``provider_response``).
    * TIDAK menghitung biaya/harga model (sumber harga valid belum tersedia).
    * Akurasi: hanya menjumlahkan angka yang BENAR-BENAR diketahui; membedakan
      ``actual`` / ``partial`` / ``unavailable``; komponen yang hilang TIDAK
      dianggap nol.
    * ``llm_requests`` dihitung dari event REQUEST yang benar-benar direkam
      (``provider_request``); log legacy tanpa event request jatuh ke jumlah
      respons sukses agar tetap kompatibel. Respons gagal dan retry provider
      dibedakan (``llm_failed_responses`` / ``provider_retries``) — tidak
      diklaim sebagai request/retry yang tidak tercatat.

TASK 6 menambahkan blok analitik lanjutan (``trends`` & ``anomalies``) plus
helper ekspor CSV/JSON (``agent_ai.statics.analytics``). Semua tetap PURE,
project-local, dan hanya membaca sumber yang sudah dipakai TASK 3–5.
"""

from agent_ai.statics.aggregate import build_statistics
from agent_ai.statics.analytics import (
    TASK_EXPORT_COLUMNS,
    build_anomalies,
    build_csv_export,
    build_json_export,
    build_trends,
    export_metadata,
    previous_range,
)
from agent_ai.statics.records import (
    AGENT_TASK_STATUSES,
    TASK_USAGE_ACTUAL,
    TASK_USAGE_PARTIAL,
    TASK_USAGE_UNAVAILABLE,
    AgentTaskStats,
    ConsultantUsageStats,
    aggregate_consultant_usage,
    aggregate_agent_tasks,
    read_agent_task_stats,
    read_consultant_usage_stats,
)

__all__ = [
    "build_statistics",
    # TASK 6 — tren, anomali, dan ekspor.
    "build_trends",
    "build_anomalies",
    "previous_range",
    "build_csv_export",
    "build_json_export",
    "export_metadata",
    "TASK_EXPORT_COLUMNS",
    "AgentTaskStats",
    "ConsultantUsageStats",
    "read_agent_task_stats",
    "read_consultant_usage_stats",
    "aggregate_agent_tasks",
    "aggregate_consultant_usage",
    "AGENT_TASK_STATUSES",
    "TASK_USAGE_ACTUAL",
    "TASK_USAGE_PARTIAL",
    "TASK_USAGE_UNAVAILABLE",
]
