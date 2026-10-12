<script setup>
// Statics page (TASK 4) — dashboard statistik PER-PROJECT.
//
// Halaman ini HANYA mengonsumsi Statistics API existing (`GET /api/statics`,
// TASK 3) lewat composable `useStatics`. TIDAK ada logic agent, agregasi
// lintas-project, atau scan filesystem di frontend: semua angka berasal dari
// backend untuk SATU project aktif.
//
// Kontrak respons (lihat `src/agent_ai/statics/aggregate.py`) yang dipakai:
//   project_id, generated_at, range, filters,
//   totals{ total_tasks, tasks_by_status, success_rate,
//            success_rate_denominator, tokens{input,output,total},
//            llm_requests, llm_rounds, llm_failed_responses,
//            provider_retries, provider_fallbacks, tool_calls,
//            duration{total_seconds, average_seconds, measured_tasks},
//            usage_completeness{actual,partial,unavailable}, consultant },
//   daily[], breakdowns{provider[],model[]}, tasks{items,total,...},
//   consultant, data_quality{usage_completeness, notes}, cost{available,reason}.
//
// TASK 5 — Efficiency & Reliability analytics (blok baru, additive):
//   efficiency{ tasks, llm_requests, llm_rounds, llm_failed_responses,
//               tool_calls, tool_success, tool_failed, tool_failure_rate,
//               tool_failure_rate_denominator, retrieval_repeats,
//               tokens{...}, tokens_per_task{total/input/output_per_task,
//               total_denominator, measured_tasks},
//               context_compaction{available, events, reason} },
//   reliability{ provider_retries, provider_retry_succeeded,
//                provider_retry_exhausted, provider_fallbacks,
//                fallback_transitions[{from_provider,to_provider,count}],
//                failed_responses, successful_responses,
//                truncation_events, truncation_aborts, truncation_recoveries,
//                tasks_failed, tasks_cancelled,
//                verification{checks,gate_passed,nudges},
//                max_steps_aborts{available,count,reason} },
//   tool_usage[{tool, calls}],
//   attention{ high_tokens[], long_running[], reliability_flags[] }.
import { computed, onMounted, watch } from "vue";
import { useStatics } from "../composables/useStatics.js";
import { formatTokens, formatTokensFull } from "../tokenFormat.js";
import { formatTs, statusTagClass } from "../taskHistory.js";

const props = defineProps({
  // Project AKTIF. Statistik selalu milik project ini (tidak ada agregasi
  // lintas-project). Bila id kosong -> dashboard kosong (bukan data lama).
  projectId: { type: String, default: "" },
  projectName: { type: String, default: "" },
});

const statics = useStatics({ selectedProjectId: computed(() => props.projectId) });
const {
  data,
  loading,
  error,
  exporting,
  exportError,
  range,
  activity,
  groupBy,
  status,
  limit,
  offset,
  isCurrent,
  refresh,
  setRange,
  setActivity,
  setGroupBy,
  setStatus,
  setPage,
  exportData,
} = statics;

// Emit task link supaya App dapat membuka History/Activity task terkait.
const emit = defineEmits(["open-task"]);

// Data yang ditampilkan HANYA valid bila milik project terpilih.
const view = computed(() => (isCurrent.value ? data.value : null));
const totals = computed(() => {
  const t = view.value && view.value.totals;
  if (!t) return null;
  return {
    ...t,
    tokens: t.tokens || { input_tokens: null, output_tokens: null, total_tokens: null },
    duration: t.duration || { total_seconds: null, average_seconds: null, measured_tasks: 0 },
    tasks_by_status: t.tasks_by_status || {},
  };
});
const daily = computed(() => {
  const d = (view.value && view.value.daily) || [];
  // Normalisasi agar chart tidak error bila `tokens` tidak ada.
  return d.map((row) => ({
    ...row,
    tasks: row.tasks || 0,
    tokens: row.tokens || { input_tokens: null, output_tokens: null, total_tokens: null },
  }));
});
const breakdowns = computed(() => (view.value && view.value.breakdowns) || {});
const taskBlock = computed(() => (view.value && view.value.tasks) || { items: [], total: 0 });
const consultant = computed(() => (view.value && view.value.consultant) || null);
const dataQuality = computed(() => (view.value && view.value.data_quality) || null);
const cost = computed(() => (view.value && view.value.cost) || null);

// --- TASK 5 analytics blocks ------------------------------------------------
const efficiency = computed(() => (view.value && view.value.efficiency) || null);
const reliability = computed(() => (view.value && view.value.reliability) || null);
const toolUsage = computed(() => (view.value && view.value.tool_usage) || []);
const attention = computed(() => (view.value && view.value.attention) || null);

// --- TASK 6: trend comparison + anomaly indicators --------------------------
const trends = computed(() => (view.value && view.value.trends) || null);
const anomalies = computed(() => (view.value && view.value.anomalies) || null);

// Baris tren yang relevan untuk dashboard (urut tetap, sesuai kontrak).
const TREND_ORDER = [
  "total_tasks",
  "llm_requests",
  "total_tokens",
  "success_rate",
  "tokens_per_task",
  "avg_duration_seconds",
];
const trendRows = computed(() => {
  const m = trends.value && trends.value.metrics;
  if (!m) return [];
  return TREND_ORDER.filter((k) => m[k]).map((k) => ({ key: k, ...m[k] }));
});

// Format perubahan: absolut selalu bila tersedia; persen HANYA bila dasar valid.
function changeValue(change, unit) {
  if (!change || !change.available) return "—";
  const abs = change.absolute;
  if (abs == null || !Number.isFinite(abs)) return "—";
  const sign = abs > 0 ? "+" : "";
  if (unit === "rate") return `${sign}${(abs * 100).toFixed(1)}pp`;
  if (unit === "seconds") return `${sign}${seconds(Math.abs(abs))}`;
  if (unit === "tokens") return `${sign}${formatTokens(Math.abs(abs))}`;
  return `${sign}${num(abs)}`;
}
function changePercent(change) {
  // Persentase TIDAK ditampilkan bila dasar tidak valid / tidak tersedia.
  if (!change || !change.available || !change.percent_available) return "—";
  if (change.percent == null || !Number.isFinite(change.percent)) return "—";
  const sign = change.percent > 0 ? "+" : "";
  return `${sign}${(change.percent * 100).toFixed(1)}%`;
}
function trendClass(change) {
  if (!change || !change.available || change.absolute == null) return "flat";
  if (change.absolute > 0) return "up";
  if (change.absolute < 0) return "down";
  return "flat";
}

// Teks nilai metrik per unit (mengikuti format existing dashboard).
function metricText(row) {
  const v = row.current;
  if (v == null || !Number.isFinite(v)) return "—";
  if (row.unit === "rate") return pct(v);
  if (row.unit === "seconds") return seconds(v);
  if (row.unit === "tokens") return formatTokens(v);
  return num(v);
}

// Label indikator anomali (istilah netral, bukan klaim keamanan).
const ANOMALY_LABELS = {
  high_token_tasks: "Task with unusually high token usage",
  long_duration_tasks: "Task with unusually long duration",
  daily_token_spikes: "Daily token spike",
  failure_rate_increase: "Tool failure rate increase",
};
function anomalyLabel(kind) {
  return ANOMALY_LABELS[kind] || kind;
}

// Nilai satu item indikator (durasi vs token/rate) + satuan yang tepat.
function itemValue(item, ind) {
  if (ind.kind === "long_duration_tasks") return seconds(item.value);
  return num(item.value);
}

// Indikator anomali yang benar-benar terisi (untuk ringkasan di dashboard).
const anomalyIndicators = computed(() => (anomalies.value && anomalies.value.indicators) || []);
const anomalyFlags = computed(() =>
  anomalyIndicators.value.filter(
    (i) => i.available && ((i.items && i.items.length) || i.raised)
  )
);
const anomalyInsufficient = computed(() => (anomalies.value && anomalies.value.insufficient) || []);

// Maksimum pemanggilan tool (untuk lebar bar relatif).
const toolUsageMax = computed(() => {
  let max = 0;
  for (const r of toolUsage.value) if (r.calls > max) max = r.calls;
  return max || 1;
});

// Tool failure rate: denominator = hasil tool yang TERCATAT (success+failed).
const toolFailurePct = computed(() => {
  const e = efficiency.value;
  if (!e || e.tool_failure_rate == null) return "—";
  return pct(e.tool_failure_rate);
});

// --- Format helpers --------------------------------------------------------
function num(value) {
  if (value == null || !Number.isFinite(value)) return "—";
  return Math.round(value).toLocaleString("en-US");
}
function pct(rate) {
  if (rate == null || !Number.isFinite(rate)) return "—";
  return `${(rate * 100).toFixed(1)}%`;
}
function seconds(value) {
  if (value == null || !Number.isFinite(value)) return "—";
  if (value < 60) return `${value.toFixed(1)}s`;
  const m = Math.floor(value / 60);
  const s = Math.round(value % 60);
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}
function tokenTitle(input, output, total) {
  // Tooltip angka PENUH (tanpa pembulatan); „—" bila provider tidak melaporkan.
  return `in ${formatTokensFull(input) || "—"} / out ${formatTokensFull(output) || "—"} / total ${formatTokensFull(total) || "—"}`;
}

// Status breakdown (bar sederhana).
const statusRows = computed(() => {
  const t = totals.value;
  if (!t) return [];
  const m = t.tasks_by_status || {};
  return [
    { key: "completed", label: "Completed", value: m.completed || 0, cls: "ok" },
    { key: "failed", label: "Failed", value: m.failed || 0, cls: "err" },
    { key: "cancelled", label: "Cancelled", value: m.cancelled || 0, cls: "muted" },
    { key: "incomplete", label: "Incomplete", value: m.incomplete || 0, cls: "warn" },
  ];
});
const statusMax = computed(() =>
  Math.max(1, ...statusRows.value.map((r) => r.value))
);

// Daily chart — tinggi bar relatif terhadap total token maksimum.
const dailyMax = computed(() => {
  let max = 0;
  for (const d of daily.value) {
    const t = d.tokens && d.tokens.total_tokens;
    if (typeof t === "number" && t > max) max = t;
  }
  return max || 1;
});

// Breakdown provider/model (gabungan kedua dimensi bila tersedia).
const providerRows = computed(() => breakdowns.value.provider || []);
const modelRows = computed(() => breakdowns.value.model || []);
const breakdownMax = computed(() => {
  let max = 0;
  for (const r of [...providerRows.value, ...modelRows.value]) {
    const t = r.tokens && r.tokens.total_tokens;
    if (typeof t === "number" && t > max) max = t;
  }
  return max || 1;
});
function barWidth(tokens) {
  const t = tokens && tokens.total_tokens;
  if (typeof t !== "number") return "0%";
  return `${Math.max(2, Math.round((t / breakdownMax.value) * 100))}%`;
}

// Pagination daftar task.
const taskTotal = computed(() => taskBlock.value.total || 0);
const pageStart = computed(() => (taskTotal.value ? offset.value + 1 : 0));
const pageEnd = computed(() =>
  Math.min(offset.value + (taskBlock.value.items || []).length, taskTotal.value)
);
const hasPrev = computed(() => offset.value > 0);
const hasNext = computed(() => offset.value + limit.value < taskTotal.value);

// Klik item anomali / baris task -> minta shell membuka History/Activity task
// terkait (pola existing: openHistoryTask).
function openTask(taskId) {
  if (!taskId) return;
  emit("open-task", taskId);
}

// --- Lifecycle -------------------------------------------------------------
onMounted(() => {
  if (props.projectId) refresh();
});

// Project berubah -> muat ulang statistik project baru. Data di-reset lebih
// dulu (via isCurrent=false saat id berubah) sehingga tidak ada sisa data
// project sebelumnya yang tampil; respons basi juga dibuang oleh guard generasi
// di composable.
watch(
  () => props.projectId,
  (id) => {
    offset.value = 0;
    if (id) refresh();
  }
);
</script>

<template>
  <section class="panel statics">
    <div class="panel-head">
      <div>
        <div class="title">Statics</div>
        <div class="desc">
          Token usage, task activity, and provider/model usage for
          <span class="mono">{{ projectName || "—" }}</span>
        </div>
      </div>
      <button class="btn-ghost" :disabled="loading || !projectId" @click="refresh">
        <span v-if="loading">Loading…</span>
        <span v-else>Refresh</span>
      </button>
      <div class="statics-export">
        <span class="statics-lbl">Export</span>
        <button
          class="btn-ghost"
          :disabled="exporting || !projectId || !view"
          @click="exportData('csv')"
        >
          CSV
        </button>
        <button
          class="btn-ghost"
          :disabled="exporting || !projectId || !view"
          @click="exportData('json')"
        >
          JSON
        </button>
      </div>
    </div>
    <div v-if="exportError" class="statics-state err">Export failed: {{ exportError }}</div>

    <!-- Filter bar: rentang tanggal + activity + group_by + status. -->
    <div class="panel-body statics-filters">
      <label class="statics-field">
        <span class="statics-lbl">From</span>
        <input
          class="input-a"
          type="date"
          :value="range.from"
          @change="setRange({ from: $event.target.value, to: range.to })"
        />
      </label>
      <label class="statics-field">
        <span class="statics-lbl">To</span>
        <input
          class="input-a"
          type="date"
          :value="range.to"
          @change="setRange({ from: range.from, to: $event.target.value })"
        />
      </label>
      <label class="statics-field">
        <span class="statics-lbl">Activity</span>
        <select class="input-a" :value="activity" @change="setActivity($event.target.value)">
          <option value="all">All</option>
          <option value="agent">Agent</option>
          <option value="consultant">Consultant</option>
        </select>
      </label>
      <label class="statics-field">
        <span class="statics-lbl">Group by</span>
        <select class="input-a" :value="groupBy" @change="setGroupBy($event.target.value)">
          <option value="day">Day</option>
          <option value="provider">Provider</option>
          <option value="model">Model</option>
        </select>
      </label>
      <label class="statics-field">
        <span class="statics-lbl">Status</span>
        <select class="input-a" :value="status" @change="setStatus($event.target.value)">
          <option value="">Any</option>
          <option value="completed">Completed</option>
          <option value="failed">Failed</option>
          <option value="cancelled">Cancelled</option>
          <option value="incomplete">Incomplete</option>
          <option value="running">Running</option>
        </select>
      </label>
      <button
        class="btn-ghost"
        :disabled="loading || !projectId"
        @click="setRange({ from: '', to: '' })"
      >
        Clear range
      </button>
    </div>

    <!-- Error state. -->
    <div v-if="error" class="statics-state err">
      <span>Failed to load statistics: {{ error }}</span>
    </div>

    <!-- Empty / loading state (tanpa project -> jangan tampilkan data lama). -->
    <div v-else-if="!projectId" class="statics-state">
      <span>No active project selected.</span>
    </div>
    <div v-else-if="!view" class="statics-state">
      <span v-if="loading">Loading statistics…</span>
      <span v-else>No statistics available for this project.</span>
    </div>

    <div v-else class="panel-body statics-body">
      <!-- ===================== OVERVIEW CARDS ===================== -->
      <div class="cards">
        <div class="card ov">
          <div class="ov-k">Tasks</div>
          <div class="ov-v">{{ num(totals.total_tasks) }}</div>
          <div class="ov-s">agent tasks in range</div>
        </div>
        <div class="card ov">
          <div class="ov-k">Tokens</div>
          <div class="ov-v" :title="tokenTitle(null, null, totals.tokens.total_tokens)">
            {{ formatTokens(totals.tokens.total_tokens) }}
          </div>
          <div class="ov-s">
            in {{ formatTokens(totals.tokens.input_tokens) }} ·
            out {{ formatTokens(totals.tokens.output_tokens) }}
          </div>
        </div>
        <div class="card ov">
          <div class="ov-k">Success rate</div>
          <div class="ov-v">{{ pct(totals.success_rate) }}</div>
          <div class="ov-s">{{ num(totals.success_rate_denominator) }} terminal tasks</div>
        </div>
        <div class="card ov">
          <div class="ov-k">LLM requests</div>
          <div class="ov-v">{{ num(totals.llm_requests) }}</div>
          <div class="ov-s">
            {{ num(totals.llm_rounds) }} rounds ·
            {{ num(totals.llm_failed_responses) }} failed
          </div>
        </div>
        <div class="card ov">
          <div class="ov-k">Tool calls</div>
          <div class="ov-v">{{ num(totals.tool_calls) }}</div>
          <div class="ov-s">{{ num(totals.provider_fallbacks) }} provider fallback(s)</div>
        </div>
        <div class="card ov">
          <div class="ov-k">Duration</div>
          <div class="ov-v">{{ seconds(totals.duration.total_seconds) }}</div>
          <div class="ov-s">
            avg {{ seconds(totals.duration.average_seconds) }} ·
            {{ num(totals.duration.measured_tasks) }} measured
          </div>
        </div>
      </div>

      <!-- Tasks by status. -->
      <div class="card block">
        <div class="block-head">Tasks by status</div>
        <div class="status-bars">
          <div v-for="row in statusRows" :key="row.key" class="sbar">
            <span class="sbar-k">{{ row.label }}</span>
            <span class="sbar-track">
              <span
                class="sbar-fill"
                :class="row.cls"
                :style="{ width: `${Math.round((row.value / statusMax) * 100)}%` }"
              ></span>
            </span>
            <span class="sbar-v">{{ num(row.value) }}</span>
          </div>
        </div>
      </div>

      <!-- ===================== DAILY ACTIVITY ===================== -->
      <div class="card block">
        <div class="block-head">Daily activity</div>
        <div v-if="daily.length" class="daily">
          <div
            v-for="d in daily"
            :key="d.date"
            class="dcol"
            :title="`${d.date} · tasks ${d.tasks} · tokens ${formatTokensFull(d.tokens.total_tokens) || '—'}`"
          >
            <span class="dcol-bar-wrap">
              <span
                class="dcol-bar"
                :style="{
                  height: `${Math.max(2, Math.round(((d.tokens.total_tokens || 0) / dailyMax) * 100))}%`,
                }"
              ></span>
            </span>
            <span class="dcol-lbl">{{ (d.date || '').slice(5) }}</span>
          </div>
        </div>
        <div v-else class="muted">No daily data in range.</div>
      </div>

      <!-- ===================== TREND COMPARISON (TASK 6) ============ -->
      <div v-if="trends" class="card block">
        <div class="block-head">
          Trend vs previous period
          <span v-if="trends.available && trends.previous_range" class="block-sub">
            {{ trends.previous_range.from }} → {{ trends.previous_range.to }}
          </span>
        </div>
        <div v-if="!trends.available" class="muted trend-note">{{ trends.note }}</div>
        <table v-else class="aether-table trend-table">
          <thead>
            <tr>
              <th>Metric</th>
              <th class="ta-r">Current</th>
              <th class="ta-r">Previous</th>
              <th class="ta-r">Δ</th>
              <th class="ta-r">Δ%</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in trendRows" :key="'tr-' + row.key">
              <td>{{ row.label }}</td>
              <td class="ta-r mono">{{ metricText(row) }}</td>
              <td class="ta-r mono">{{ metricText({ ...row, current: row.previous }) }}</td>
              <td class="ta-r mono" :class="'delta-' + trendClass(row.change)">
                {{ changeValue(row.change, row.unit) }}
              </td>
              <td class="ta-r mono" :class="'delta-' + trendClass(row.change)">
                <span
                  v-if="row.change && row.change.available && !row.change.percent_available"
                  :title="row.change.reason || ''"
                >—</span>
                <span v-else>{{ changePercent(row.change) }}</span>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="muted trend-note">{{ trends.method }}</div>
      </div>

      <!-- ===================== ANOMALY INDICATORS (TASK 6) ========== -->
      <div v-if="anomalies" class="card block">
        <div class="block-head">
          Usage indicators
          <span class="block-sub">{{ num(anomalyFlags.length) }} flagged</span>
        </div>
        <div class="muted trend-note">{{ anomalies.disclaimer }}</div>

        <!-- Data belum mencukupi -> status eksplisit (bukan "tidak ada"). -->
        <div v-if="anomalyInsufficient.length" class="muted insufficient">
          <div v-for="(i, idx) in anomalyInsufficient" :key="'ins-' + idx">
            {{ i.reason }}
          </div>
        </div>

        <div v-if="anomalyFlags.length" class="two-col anom-cols">
          <div v-for="ind in anomalyFlags" :key="'an-' + ind.kind" class="anom-group">
            <div class="block-sub">{{ anomalyLabel(ind.kind) }}</div>
            <div class="bk-list">
              <div
                v-for="(item, idx) in (ind.items || [])"
                :key="'ai-' + ind.kind + '-' + idx"
                class="bk-row"
                :class="{ clickable: !!item.task_id }"
                @click="openTask(item.task_id)"
              >
                <div class="bk-top">
                  <span class="bk-key mono ellip" :title="item.task_id || item.date">
                    {{ item.task_id || item.date }}
                  </span>
                  <span class="bk-tok">{{ itemValue(item, ind) }}</span>
                </div>
                <div class="bk-meta">{{ item.reason }}</div>
              </div>
            </div>
            <div v-if="ind.kind === 'failure_rate_increase'" class="anom-fail">
              <span class="bk-meta">{{ ind.reason }}</span>
            </div>
            <div class="muted anom-basis">{{ ind.basis }}</div>
          </div>
        </div>
        <div v-else-if="!anomalyInsufficient.length" class="muted">
          No indicator exceeded its threshold in this period.
        </div>
        <div class="muted trend-note">{{ anomalies.method }}</div>
      </div>

      <!-- ===================== EFFICIENCY (TASK 5) ===================== -->
      <div v-if="efficiency" class="card block">
        <div class="block-head">Efficiency</div>
        <div class="cards compact">
          <div class="card ov">
            <div class="ov-k">Requests / rounds</div>
            <div class="ov-v">{{ num(efficiency.llm_requests) }} / {{ num(efficiency.llm_rounds) }}</div>
            <div class="ov-s">{{ num(efficiency.llm_failed_responses) }} failed response(s)</div>
          </div>
          <div class="card ov">
            <div class="ov-k">Tokens / task</div>
            <div class="ov-v">{{ formatTokens(efficiency.tokens_per_task.total_per_task) }}</div>
            <div class="ov-s">
              in {{ formatTokens(efficiency.tokens_per_task.input_per_task) }} ·
              out {{ formatTokens(efficiency.tokens_per_task.output_per_task) }} ·
              over {{ num(efficiency.tokens_per_task.total_denominator) }} task(s)
            </div>
          </div>
          <div class="card ov">
            <div class="ov-k">Tool failure rate</div>
            <div class="ov-v">{{ toolFailurePct }}</div>
            <div class="ov-s">
              {{ num(efficiency.tool_failed) }} failed / {{ num(efficiency.tool_failure_rate_denominator) }} recorded
            </div>
          </div>
          <div class="card ov">
            <div class="ov-k">Retrieval repeats</div>
            <div class="ov-v">{{ num(efficiency.retrieval_repeats) }}</div>
            <div class="ov-s">repeat read/search events</div>
          </div>
        </div>

        <!-- Tool usage breakdown. -->
        <div v-if="toolUsage.length" class="bk-list tool-list">
          <div class="block-sub">Tool usage by tool</div>
          <div v-for="r in toolUsage" :key="'tu-' + r.tool" class="bk-row">
            <div class="bk-top">
              <span class="bk-key mono">{{ r.tool }}</span>
              <span class="bk-tok">{{ num(r.calls) }}</span>
            </div>
            <span class="bk-track">
              <span class="bk-fill" :style="{ width: `${Math.max(2, Math.round((r.calls / toolUsageMax) * 100))}%` }"></span>
            </span>
          </div>
        </div>
        <div v-else class="muted">No tool usage data.</div>

        <!-- Context compaction: selalu ditandai tidak tersedia bila tidak ada. -->
        <div class="muted compaction-note">
          <template v-if="efficiency.context_compaction && efficiency.context_compaction.available">
            Context compaction events: {{ num(efficiency.context_compaction.events) }}
          </template>
          <template v-else>
            Context compaction: not available.
            {{ efficiency.context_compaction ? efficiency.context_compaction.reason : "" }}
          </template>
        </div>
      </div>

      <!-- ===================== RELIABILITY (TASK 5) ===================== -->
      <div v-if="reliability" class="card block">
        <div class="block-head">Reliability</div>
        <div class="cards compact">
          <div class="card ov">
            <div class="ov-k">Provider retries</div>
            <div class="ov-v">{{ num(reliability.provider_retries) }}</div>
            <div class="ov-s">
              {{ num(reliability.provider_retry_succeeded) }} recovered ·
              {{ num(reliability.provider_retry_exhausted) }} exhausted
            </div>
          </div>
          <div class="card ov">
            <div class="ov-k">Provider fallbacks</div>
            <div class="ov-v">{{ num(reliability.provider_fallbacks) }}</div>
            <div class="ov-s">{{ (reliability.fallback_transitions || []).length }} transition(s)</div>
          </div>
          <div class="card ov">
            <div class="ov-k">Failed responses</div>
            <div class="ov-v">{{ num(reliability.failed_responses) }}</div>
            <div class="ov-s">{{ num(reliability.successful_responses) }} successful</div>
          </div>
          <div class="card ov">
            <div class="ov-k">Truncation recovery</div>
            <div class="ov-v">{{ num(reliability.truncation_recoveries) }}</div>
            <div class="ov-s">
              {{ num(reliability.truncation_events) }} truncated ·
              {{ num(reliability.truncation_aborts) }} abort(s)
            </div>
          </div>
        </div>

        <!-- Fallback transitions (from -> to). -->
        <div v-if="(reliability.fallback_transitions || []).length" class="bk-list">
          <div class="block-sub">Provider fallback transitions</div>
          <div
            v-for="(r, i) in reliability.fallback_transitions"
            :key="'fb-' + i"
            class="bk-row"
          >
            <div class="bk-top">
              <span class="bk-key mono">{{ r.from_provider }} → {{ r.to_provider }}</span>
              <span class="bk-tok">{{ num(r.count) }}</span>
            </div>
          </div>
        </div>

        <!-- max_steps abort: tandai gap bila tidak tersedia. -->
        <div class="muted compaction-note">
          <template v-if="reliability.max_steps_aborts && reliability.max_steps_aborts.available">
            Max-steps aborts: {{ num(reliability.max_steps_aborts.count) }}
          </template>
          <template v-else>
            Max-steps aborts: not available.
            {{ reliability.max_steps_aborts ? reliability.max_steps_aborts.reason : "" }}
          </template>
        </div>
      </div>

      <!-- ===================== ATTENTION ITEMS (TASK 5) ================== -->
      <div v-if="attention && attention.available" class="card block">
        <div class="block-head">Attention items</div>
        <div class="two-col">
          <div>
            <div class="block-sub">Highest token usage</div>
            <div v-if="(attention.high_tokens || []).length" class="bk-list">
              <div v-for="t in attention.high_tokens" :key="'ht-' + t.task_id" class="bk-row">
                <div class="bk-top">
                  <span class="bk-key mono ellip" :title="t.task_id">{{ t.task_id }}</span>
                  <span class="bk-tok">{{ formatTokens(t.total_tokens) }}</span>
                </div>
                <div class="bk-meta">{{ t.status }} · {{ num(t.provider_fallbacks) }} fallback(s)</div>
              </div>
            </div>
            <div v-else class="muted">No token data.</div>
          </div>
          <div>
            <div class="block-sub">Longest duration</div>
            <div v-if="(attention.long_running || []).length" class="bk-list">
              <div v-for="t in attention.long_running" :key="'lr-' + t.task_id" class="bk-row">
                <div class="bk-top">
                  <span class="bk-key mono ellip" :title="t.task_id">{{ t.task_id }}</span>
                  <span class="bk-tok">{{ seconds(t.duration_seconds) }}</span>
                </div>
                <div class="bk-meta">{{ t.status }}</div>
              </div>
            </div>
            <div v-else class="muted">No duration data.</div>
          </div>
        </div>
        <div v-if="(attention.reliability_flags || []).length" class="bk-list flags">
          <div class="block-sub">Reliability flags (failed / cancelled / retry exhausted)</div>
          <div v-for="t in attention.reliability_flags" :key="'rf-' + t.task_id" class="bk-row">
            <div class="bk-top">
              <span class="bk-key mono ellip" :title="t.task_id">{{ t.task_id }}</span>
              <span class="bk-tok">{{ t.status }}</span>
            </div>
            <div class="bk-meta">
              {{ num(t.provider_retry_exhausted) }} exhausted · {{ num(t.provider_fallbacks) }} fallback(s)
            </div>
          </div>
        </div>
      </div>

      <!-- ===================== BREAKDOWNS ===================== -->
      <div class="two-col">
        <div class="card block">
          <div class="block-head">Provider usage</div>
          <div v-if="providerRows.length" class="bk-list">
            <div v-for="r in providerRows" :key="'p-' + r.key" class="bk-row">
              <div class="bk-top">
                <span class="bk-key">{{ r.key }}</span>
                <span class="bk-tok">{{ formatTokens(r.tokens.total_tokens) }}</span>
              </div>
              <div class="bk-meta">
                {{ num(r.requests) }} req · {{ num(r.tasks) }} task(s) ·
                {{ formatTokens(r.tokens.input_tokens) }} in /
                {{ formatTokens(r.tokens.output_tokens) }} out
              </div>
              <span class="bk-track"><span class="bk-fill" :style="{ width: barWidth(r.tokens) }"></span></span>
            </div>
          </div>
          <div v-else class="muted">No provider data.</div>
        </div>

        <div class="card block">
          <div class="block-head">Model usage</div>
          <div v-if="modelRows.length" class="bk-list">
            <div v-for="r in modelRows" :key="'m-' + r.key" class="bk-row">
              <div class="bk-top">
                <span class="bk-key">{{ r.key }}</span>
                <span class="bk-tok">{{ formatTokens(r.tokens.total_tokens) }}</span>
              </div>
              <div class="bk-meta">
                {{ num(r.requests) }} req · {{ num(r.tasks) }} task(s) ·
                {{ formatTokens(r.tokens.input_tokens) }} in /
                {{ formatTokens(r.tokens.output_tokens) }} out
              </div>
              <span class="bk-track"><span class="bk-fill" :style="{ width: barWidth(r.tokens) }"></span></span>
            </div>
          </div>
          <div v-else class="muted">No model data.</div>
        </div>
      </div>

      <!-- ===================== CONSULTANT ===================== -->
      <div v-if="consultant" class="card block">
        <div class="block-head">Consultant usage</div>
        <div class="cards compact">
          <div class="card ov">
            <div class="ov-k">Responses</div>
            <div class="ov-v">{{ num(consultant.responses) }}</div>
            <div class="ov-s">{{ num(consultant.sessions) }} session(s) · {{ num(consultant.turns) }} turn(s)</div>
          </div>
          <div class="card ov">
            <div class="ov-k">Tokens</div>
            <div class="ov-v">{{ formatTokens(consultant.total_tokens) }}</div>
            <div class="ov-s">
              in {{ formatTokens(consultant.input_tokens) }} ·
              out {{ formatTokens(consultant.output_tokens) }}
            </div>
          </div>
          <div class="card ov">
            <div class="ov-k">Usage coverage</div>
            <div class="ov-v">
              {{ num(consultant.usage_actual) }}/{{ num(consultant.responses) }}
            </div>
            <div class="ov-s">
              partial {{ num(consultant.usage_partial) }} ·
              unavailable {{ num(consultant.usage_unavailable) }}
            </div>
          </div>
        </div>
      </div>

      <!-- ===================== TASK DETAIL ===================== -->
      <div class="card block">
        <div class="block-head">
          Task detail
          <span class="block-sub">{{ num(pageStart) }}–{{ num(pageEnd) }} of {{ num(taskTotal) }}</span>
        </div>
        <div v-if="taskBlock.items && taskBlock.items.length" class="tbl-wrap">
          <table class="aether-table">
            <thead>
              <tr>
                <th>Task</th>
                <th>Status</th>
                <th>Provider / Model</th>
                <th class="ta-r">Tokens</th>
                <th class="ta-r">Requests</th>
                <th class="ta-r">Tools</th>
                <th class="ta-r">Duration</th>
                <th>Finished</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="t in taskBlock.items" :key="t.task_id" class="clickable" @click="openTask(t.task_id)">
                <td class="mono ellip" :title="t.prompt || t.task_id">
                  {{ t.prompt || t.task_id }}
                </td>
                <td>
                  <span class="status-tag" :class="statusTagClass(t.status)">{{ t.status }}</span>
                </td>
                <td class="ellip">
                  {{ (t.providers && t.providers.join(', ')) || '—' }} /
                  {{ (t.models && t.models.join(', ')) || '—' }}
                </td>
                <td class="ta-r mono" :title="tokenTitle(t.input_tokens, t.output_tokens, t.total_tokens)">
                  {{ formatTokens(t.total_tokens) }}
                </td>
                <td class="ta-r mono">{{ num(t.llm_requests) }}</td>
                <td class="ta-r mono">{{ num(t.tool_calls) }}</td>
                <td class="ta-r mono">{{ seconds(t.duration_seconds) }}</td>
                <td class="mono">{{ formatTs(t.finished_at) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="muted">No tasks in range.</div>

        <div class="pager">
          <button class="btn-ghost" :disabled="!hasPrev || loading" @click="setPage(offset - limit)">
            Prev
          </button>
          <button class="btn-ghost" :disabled="!hasNext || loading" @click="setPage(offset + limit)">
            Next
          </button>
        </div>
      </div>

      <!-- ===================== DATA QUALITY / COST ===================== -->
      <div class="card block notes">
        <div class="block-head">Data quality</div>
        <ul class="note-list">
          <li v-for="(note, i) in (dataQuality && dataQuality.notes) || []" :key="i">{{ note }}</li>
        </ul>
        <div v-if="cost && !cost.available" class="cost-note">
          Cost estimation unavailable: {{ cost.reason }}
        </div>
        <div v-if="view.generated_at" class="generated mono">
          generated_at {{ formatTs(view.generated_at) }}
          <template v-if="view.range">({{ view.range.timezone }})</template>
        </div>
      </div>
    </div>
  </section>
</template>
