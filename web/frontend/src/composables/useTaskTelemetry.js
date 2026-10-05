import { computed } from "vue";
import { formatTokens, formatTokensFull, usageTokens } from "../tokenFormat.js";

// Telemetry Agent Card (Provider/Model + LLM Rounds / Tool Calls / Tokens).
//
// Ditambahkan sebagai BAGIAN DARI metadata Agent Card yang sama (`.task-meta`),
// BUKAN sistem telemetry kedua. Semua angka dihitung dari event lifecycle
// AETHER yang SUDAH ADA (SSE live #51 atau persistent log via Activity API):
//   - LLM Rounds : jumlah event `provider_request` = jumlah pemanggilan LLM.
//   - Tool Calls : jumlah event `tool_called` = jumlah eksekusi tool AKTUAL.
//   - Tokens     : token usage AKTUAL dari provider (payload `usage` pada
//                  `provider_response`) bila dilaporkan; TIDAK ada estimasi.
// Format angka memakai helper murni `./tokenFormat.js`.
export function useTaskTelemetry({
  activityEvents,
  runtime,
  task,
  taskExecutionLabel,
  taskDurationLabel,
}) {
  // Provider/Model yang BENAR-BENAR dipakai task. Sumber: event lifecycle
  // provider_request/provider_response (payload provider + model) dari SSE live
  // ATAU persistent log saat task lama dibuka. TIDAK memakai default/global.
  const taskProviderModel = computed(() => {
    let provider = "";
    let model = "";
    const list = activityEvents.value || [];
    for (let i = list.length - 1; i >= 0; i--) {
      const raw = list[i] || {};
      const type = raw.event_type || raw.event || "";
      if (type === "provider_request" || type === "provider_response") {
        const d = raw.payload || raw.data || {};
        if (!provider && d.provider) provider = String(d.provider);
        if (!model && d.model) model = String(d.model);
        if (provider && model) break;
      }
    }
    // Fallback terakhir: nilai runtime task AKTIF (tetap task-specific, bukan
    // konfigurasi global). Bila tetap kosong -> bagian ini tidak ditampilkan.
    if (!provider) provider = runtime.provider || "";
    if (!model) model = runtime.model || "";
    return { provider, model };
  });
  const taskProvider = computed(() => taskProviderModel.value.provider);
  const taskModel = computed(() => taskProviderModel.value.model);

  // REDUCE sederhana atas event yang ditampilkan: nilai ikut lifecycle task
  // (naik saat event baru tiba, diam saat task mencapai status final).
  const taskTelemetry = computed(() => {
    let rounds = 0;
    let toolCalls = 0;
    let tokens = 0;
    let hasTokens = false;
    const list = activityEvents.value || [];
    for (const raw of list) {
      const type = (raw && (raw.event_type || raw.event)) || "";
      if (type === "provider_request") {
        rounds += 1;
      } else if (type === "tool_called") {
        toolCalls += 1;
      } else if (type === "provider_response") {
        const total = usageTokens(raw.payload || raw.data || {});
        if (total != null) {
          tokens += total;
          hasTokens = true;
        }
      }
    }
    return { rounds, toolCalls, tokens: hasTokens ? tokens : null };
  });
  const taskLlmRounds = computed(() => taskTelemetry.value.rounds);
  const taskToolCalls = computed(() => taskTelemetry.value.toolCalls);
  const taskTokensLabel = computed(() => formatTokens(taskTelemetry.value.tokens));
  // Tooltip menampilkan angka PENUH (mis. "140,500,000 tokens") bila provider
  // melaporkan usage; selain itu menjelaskan bahwa provider tidak melaporkan.
  const taskTokensTooltip = computed(() => {
    const n = taskTelemetry.value.tokens;
    if (n == null) return "Provider token usage not reported";
    return `Tokens (actual provider usage): ${formatTokensFull(n)}`;
  });
  const showTaskTelemetry = computed(
    () =>
      Boolean(task.id) &&
      (taskLlmRounds.value > 0 ||
        taskToolCalls.value > 0 ||
        taskTelemetry.value.tokens != null)
  );

  // Round: LLM invocation count (provider_request) — ditampilkan sebagai "Round N".
  const taskRoundLabel = computed(() =>
    taskLlmRounds.value > 0 ? `Round ${taskLlmRounds.value}` : ""
  );

  const showTaskMeta = computed(() =>
    Boolean(
      taskProvider.value ||
        taskModel.value ||
        (task.id && taskExecutionLabel.value) ||
        taskRoundLabel.value ||
        taskDurationLabel.value ||
        showTaskTelemetry.value
    )
  );

  return {
    taskProviderModel,
    taskProvider,
    taskModel,
    taskTelemetry,
    taskLlmRounds,
    taskToolCalls,
    taskTokensLabel,
    taskTokensTooltip,
    showTaskTelemetry,
    taskRoundLabel,
    showTaskMeta,
  };
}
