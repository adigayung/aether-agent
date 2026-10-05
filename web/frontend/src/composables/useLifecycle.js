import { computed } from "vue";
import { buildLifecycleStates } from "../lifecycle.js";
import { executionLabel, normalizeExecutionMode } from "../executionMode.js";

// Lifecycle & status task untuk UI (Task Card):
//   - label execution mode (Queue/Parallel),
//   - 6 step lifecycle (Planning..Completed) dari activity phase NYATA agent
//     (`phase_changed`) + status task + milestone yang sudah dicapai,
//   - tag status + dot status sidebar.
// TIDAK memakai `runtime.phase` internal (replan/provider_fallback) sebagai
// sumber step lifecycle (lihat lifecycle.js).
const LIFECYCLE_STEPS = ["Planning", "Inspecting", "Editing", "Running", "Validating", "Completed"];

export function useLifecycle({ task, activityPhase, lifecycleMilestones, isRunning }) {
  // --- Execution Mode (Task 04) — ditampilkan di Task Card / History / Queue ---
  const taskExecutionMode = computed(() => normalizeExecutionMode(task.executionMode));
  const taskExecutionLabel = computed(() => executionLabel(task.executionMode));

  const lifecycleStates = computed(() =>
    buildLifecycleStates({
      hasTask: Boolean(task.id),
      status: task.status,
      currentPhase: activityPhase.value,
      milestones: lifecycleMilestones.value,
      count: LIFECYCLE_STEPS.length,
    })
  );
  const lifecycleSteps = computed(() =>
    LIFECYCLE_STEPS.map((label, i) => ({ label, state: lifecycleStates.value[i] || "" }))
  );
  const lifecyclePct = computed(() => {
    let max = -1;
    lifecycleStates.value.forEach((state, i) => {
      if (state === "done" || state === "active") max = i;
    });
    if (max <= 0) return 0;
    return Math.round((max / (LIFECYCLE_STEPS.length - 1)) * 100);
  });

  // Agent status kecil (dari state/event AETHER sebenarnya, bukan fake).
  const agentStatus = computed(() => {
    const s = (task.status || "idle").toLowerCase();
    if (s === "running" || s === "prepared" || s === "planning" || s === "executing")
      return { label: "Running", cls: "running" };
    if (s === "validating") return { label: "Validating", cls: "running" };
    // Menunggu execution slot di Global Task Queue (bukan running).
    if (s === "queued") return { label: "Queued", cls: "queued" };
    if (s === "completed") return { label: "Completed", cls: "completed" };
    if (s === "failed") return { label: "Failed", cls: "failed" };
    if (s === "cancelled") return { label: "Stopping", cls: "warn" };
    return { label: "Ready", cls: "ready" };
  });

  // Dot warna Agent di sidebar.
  const agentDotClass = computed(() => {
    const c = agentStatus.value.cls;
    if (c === "failed") return "err";
    if (c === "warn" || c === "running") return "warn";
    return "";
  });

  // Tag status task (di header card).
  const taskTag = computed(() => {
    const s = (task.status || "idle").toLowerCase();
    if (s === "completed") return { label: "completed", cls: "validated" };
    if (s === "failed") return { label: "failed", cls: "failed" };
    if (s === "cancelled") return { label: "stopped", cls: "modified" };
    if (s === "queued") return { label: "queued", cls: "queued" };
    if (isRunning.value) return { label: "running", cls: "validated" };
    return { label: "idle", cls: "idle" };
  });

  return {
    taskExecutionMode,
    taskExecutionLabel,
    lifecycleStates,
    lifecycleSteps,
    lifecyclePct,
    agentStatus,
    agentDotClass,
    taskTag,
  };
}
