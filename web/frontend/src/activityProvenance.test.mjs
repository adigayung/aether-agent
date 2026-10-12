// Regression test (Node built-in `assert`, tanpa framework/dependency baru)
// untuk GUARD PROVENANCE di useAgentActivity.handleEvent():
//
//     event "aktivitas task" (tool_called/tool_completed/provider_*/phase_*/
//     validation_*/recovery_*/observation_received/...) TANPA task_id
//         -> DIBUANG dari feed Agent Activity (return sebelum pushRolling).
//
// Bug yang dikunci: event runtime Consultant dipancarkan dengan session_id
// consultant dan TANPA task_id. Stream GLOBAL frontend (/api/events) tidak
// difilter, sehingga event itu lolos ke timeline "aether — agent activity" dan
// tampil sebagai baris tool konsultasi (Reading source/Searching code/dll).
// Diskriminator resmi = ada/tidaknya task_id pada event aktivitas-task.
//
// Test mengimpor komposabel ASLI (bukan menyalin logikanya) sehingga kontrak DI
// + perilaku reactive benar-benar diuji. Asset audio (.wav) di-shim karena Node
// tidak bisa memuatnya (Vite yang membundel).
//
// Jalankan: node web/frontend/src/activityProvenance.test.mjs

import assert from "node:assert/strict";
import { register } from "node:module";
import { reactive, ref } from "vue";

// --- Shim: import asset non-JS (.wav) -> modul inert (khusus Node test) -----
const LOADER_HOOKS = `export async function resolve(specifier, context, nextResolve) {
  if (specifier.endsWith(".wav")) {
    return { url: "data:text/javascript,export default 'asset.wav'", shortCircuit: true };
  }
  return nextResolve(specifier, context);
}`;
register(
  `data:text/javascript,${encodeURIComponent(LOADER_HOOKS)}`,
  import.meta.url
);

const { useChanges } = await import("./composables/useChanges.js");
const { useAgentActivity } = await import("./composables/useAgentActivity.js");

// --- Harness: composition root minimum (sama seperti App.vue) ---------------
function makeHarness({ taskId = "task-1" } = {}) {
  const task = reactive({ id: taskId, status: "" });
  const timing = { taskStartedAt: ref(null), taskEndedAt: ref(null), resetTiming() {} };
  const changes = useChanges({
    task,
    isViewingRunningTask: () => true,
    getActiveTaskIds: () => new Set(),
  });
  const queuedRefreshes = [];
  const queue = {
    runningTaskId: ref(""),
    releaseRunningTask() {},
    adoptRunningTask() {},
    isViewingRunningTask: () => true,
    deferredTaskIds: new Map(),
    terminalTaskId: ref(""),
  };
  const handledApprovals = [];
  const approvals = {
    handled: handledApprovals,
    handleApprovalEvent(evt) { handledApprovals.push(evt); },
    dropApprovalsForTask() {},
  };
  const activity = useAgentActivity({
    task,
    timing,
    changes,
    queue,
    approvals,
    bumpQueueRefresh() { queuedRefreshes.push(Date.now()); },
    refreshTaskHistory() {},
    clearReport() {},
    getCopyMeta: () => ({}),
  });
  return { task, changes, activity, queuedRefreshes, approvals };
}

// Bentuk event SSE live = ExecutionEvent.to_dict() (dikirim oleh /api/events).
function sseEvent(overrides = {}) {
  return {
    event_id: overrides.event_id || `evt-${Math.random().toString(36).slice(2)}`,
    session_id: overrides.session_id || "session-1",
    task_id: Object.prototype.hasOwnProperty.call(overrides, "task_id") ? overrides.task_id : "task-1",
    event_type: overrides.event_type || "tool_called",
    timestamp: overrides.timestamp || 1700000000,
    payload: overrides.payload || { tool: "read_file", target: "src/app.py" },
  };
}

// --- Test 1: tool_called TANPA task_id (Consultant) DIBUANG ----------------
{
  const h = makeHarness({ taskId: "task-1" });
  h.activity.handleEvent(sseEvent({ event_type: "tool_called", task_id: null, payload: { tool: "read_file", target: "x.py" } }));
  h.activity.handleEvent(sseEvent({ event_type: "tool_completed", task_id: "", payload: { tool: "read_file" } }));

  assert.equal(h.activity.events.value.length, 0, "Consultant tool events (no task_id) must NOT enter activity feed");
  assert.equal(h.activity.activityEvents.value.length, 0, "activityEvents also empty");
  console.log("PASS: tool_called/tool_completed tanpa task_id dibuang dari Agent Activity");
}

// --- Test 2: tool_called DENGAN task_id task aktif TETAP masuk -------------
{
  const h = makeHarness({ taskId: "task-1" });
  h.activity.handleEvent(sseEvent({ event_type: "tool_called", task_id: "task-1", payload: { tool: "read_file", target: "a.py" } }));

  assert.equal(h.activity.events.value.length, 1, "Agent task tool_called (with task_id) must enter feed");
  assert.equal(h.activity.events.value[0].event_type, "tool_called");
  console.log("PASS: tool_called dengan task_id task aktif tetap masuk");
}

// --- Test 3: task_queued tanpa task_id TETAP lolos + refresh queue --------
{
  const h = makeHarness({ taskId: "task-1" });
  const before = h.queuedRefreshes.length;
  h.activity.handleEvent(sseEvent({ event_type: "task_queued", task_id: null, payload: { task_id: "task-9" } }));

  assert.ok(h.queuedRefreshes.length > before, "task_queued tanpa task_id tetap memicu refresh Queue/badge");
  console.log("PASS: task_queued tanpa task_id tetap lolos (queue refresh)");
}

// --- Test 4: task_started tanpa task_id tetap lolos ----------------------
{
  const h = makeHarness({ taskId: "task-1" });
  const before = h.queuedRefreshes.length;
  h.activity.handleEvent(sseEvent({ event_type: "task_started", task_id: null }));

  assert.ok(h.queuedRefreshes.length > before, "task_started (global) tetap memicu refresh");
  console.log("PASS: task_started tanpa task_id tetap lolos");
}

// --- Test 5: approval tanpa task_id tetap ditangani (Allow/Deny lintas task) ---
{
  const h = makeHarness({ taskId: "task-1" });
  h.activity.handleEvent(sseEvent({ event_type: "approval_requested", task_id: null }));

  assert.equal(h.approvals.handled.length, 1, "approval_requested ditangani SEBELUM guard provenance");
  console.log("PASS: approval tanpa task_id tetap diproses");
}

// --- Test 6: SEMUA tipe aktivitas-task tanpa task_id dibuang --------------
{
  const activityTypes = [
    "tool_called", "tool_completed", "observation_received", "tool_result",
    "agent_observation", "provider_request", "provider_response", "phase_changed",
    "validation_started", "validation_completed", "recovery_started",
    "recovery_completed", "agent_commentary",
  ];
  for (const t of activityTypes) {
    const h = makeHarness({ taskId: "task-1" });
    h.activity.handleEvent(sseEvent({ event_type: t, task_id: null, payload: {} }));
    assert.equal(h.activity.events.value.length, 0, `${t} tanpa task_id harus dibuang`);
  }
  console.log("PASS: seluruh tipe aktivitas-task tanpa task_id dibuang");
}

// --- Test 7: change_detected tanpa task_id TIDAK dibuang (panel Changes) --
{
  const h = makeHarness({ taskId: "task-1" });
  h.activity.handleEvent(sseEvent({
    event_type: "change_detected",
    task_id: null,
    payload: { change: { path: "src/other.py", kind: "modified" } },
  }));

  assert.equal(h.activity.events.value.length, 1, "change_detected bukan aktivitas-task -> tidak boleh ikut dibuang");
  console.log("PASS: change_detected tanpa task_id tidak terpengaruh guard");
}

console.log("\n=== ALL activityProvenance GUARD TESTS PASSED ===");
