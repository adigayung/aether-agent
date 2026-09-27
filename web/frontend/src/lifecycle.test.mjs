// Regression test (Node built-in `assert`, TANPA framework/dependency baru)
// untuk logika lifecycle UI (planning/inspecting/editing/running/validating/
// completed) yang diturunkan dari event `phase_changed` (activity phase Task 1)
// + status task.
//
// Mengunci perilaku yang diminta Task 2:
//   - task_started            -> Planning aktif
//   - phase_changed inspecting-> Inspecting
//   - phase_changed editing   -> Editing
//   - phase_changed running   -> Running
//   - phase_changed validating-> Validating
//   - task_completed          -> Completed (terminal)
//   - current phase boleh mundur; milestone tetap `done`
//   - failed/cancelled        -> BUKAN Completed, tetap di aktivitas terakhir
//   - nilai internal runtime (replan/provider_fallback) DIABAIKAN
//
// Jalankan: node web/frontend/src/lifecycle.test.mjs

import assert from "node:assert/strict";
import {
  VALIDATING_STEP,
  activityPhaseIndex,
  addMilestone,
  buildLifecycleStates,
  isActivityPhase,
  lifecycleFromEvents,
} from "./lifecycle.js";

// Nama step (sama dengan LIFECYCLE_STEPS di App.vue) — untuk assertion berlabel.
const STEPS = ["Planning", "Inspecting", "Editing", "Running", "Validating", "Completed"];
const P = ""; // pending = tanpa class (template menampilkan nomor step)
const D = "done";
const A = "active";

// --- activityPhaseIndex / isActivityPhase -----------------------------------
assert.equal(activityPhaseIndex("planning"), 0);
assert.equal(activityPhaseIndex("INSPECTING"), 1);
assert.equal(activityPhaseIndex(" editing "), 2);
assert.equal(activityPhaseIndex("running"), 3);
assert.equal(activityPhaseIndex("validating"), 4);
// Nilai internal lama BUKAN activity phase -> tidak menggerakkan lifecycle.
assert.equal(activityPhaseIndex("replan"), -1);
assert.equal(activityPhaseIndex("provider_fallback"), -1);
assert.equal(activityPhaseIndex(""), -1);
assert.equal(activityPhaseIndex(null), -1);
assert.equal(isActivityPhase("editing"), true);
assert.equal(isActivityPhase("replan"), false);
assert.equal(VALIDATING_STEP, 4);

// --- addMilestone (immutable, unik, terurut) --------------------------------
assert.deepEqual(addMilestone([], 2), [2]);
assert.deepEqual(addMilestone([2], 2), [2], "tidak duplikat");
assert.deepEqual(addMilestone([3, 1], 2), [1, 2, 3], "terurut");
assert.deepEqual(addMilestone([], 9), [], "index di luar rentang diabaikan");

// --- Helper: bangun state sambil melacak milestone ---------------------------
function lifecycle(status = "running") {
  let milestones = [];
  let currentPhase = "";
  const step = (p) => {
    const idx = activityPhaseIndex(p);
    if (idx >= 0) {
      milestones = addMilestone(milestones, idx);
      currentPhase = String(p).trim().toLowerCase();
    }
  };
  const render = () =>
    buildLifecycleStates({
      hasTask: true,
      status,
      currentPhase,
      milestones,
      count: STEPS.length,
    });
  return {
    step,
    setStatus: (s) => (status = s),
    render,
    get milestones() {
      return milestones;
    },
  };
}

const expectStates = (steps, expected) => {
  assert.equal(steps.length, STEPS.length);
  assert.deepEqual(steps, expected);
  return steps;
};

// --- Acceptance 1..6 --------------------------------------------------------
// task_started -> Planning (diwakili set phase planning).
{
  const lc = lifecycle();
  lc.step("planning");
  expectStates(lc.render(), [A, P, P, P, P, P]);
}

// phase_changed inspecting -> Inspecting, Planning jadi milestone done.
{
  const lc = lifecycle();
  lc.step("planning");
  lc.step("inspecting");
  expectStates(lc.render(), [D, A, P, P, P, P]);
}

// phase_changed editing -> Editing.
{
  const lc = lifecycle();
  ["planning", "inspecting", "editing"].forEach(lc.step);
  expectStates(lc.render(), [D, D, A, P, P, P]);
}

// phase_changed running -> Running.
{
  const lc = lifecycle();
  ["planning", "inspecting", "editing", "running"].forEach(lc.step);
  expectStates(lc.render(), [D, D, D, A, P, P]);
}

// phase_changed validating -> Validating.
{
  const lc = lifecycle();
  ["planning", "inspecting", "editing", "running", "validating"].forEach(lc.step);
  expectStates(lc.render(), [D, D, D, D, A, P]);
}

// task_completed -> Completed (terminal), semua step done.
{
  const lc = lifecycle();
  ["planning", "inspecting", "editing", "running", "validating"].forEach(lc.step);
  lc.setStatus("completed");
  expectStates(lc.render(), [D, D, D, D, D, A]);
}

// --- Acceptance 7/8: current phase boleh mundur, milestone tetap done -------
// Inspecting -> Editing -> Inspecting (Case 3).
{
  const lc = lifecycle();
  ["planning", "inspecting", "editing", "inspecting"].forEach(lc.step);
  const steps = lc.render();
  expectStates(steps, [D, A, D, P, P, P]);
  assert.equal(steps[2], D, `Editing (${STEPS[2]}) tetap milestone done`);
}

// Editing -> Running -> Validating -> Editing (Case 4).
{
  const lc = lifecycle();
  ["planning", "inspecting", "editing", "running", "validating", "editing"].forEach(lc.step);
  expectStates(lc.render(), [D, D, A, D, D, P]);
}

// "Running lalu kembali Inspecting": Running tetap done.
{
  const lc = lifecycle();
  ["planning", "inspecting", "editing", "running", "inspecting"].forEach(lc.step);
  expectStates(lc.render(), [D, A, D, D, P, P]);
}

// Case 2: Planning -> Editing (langsung, tanpa Inspecting).
{
  const lc = lifecycle();
  lc.step("planning");
  lc.step("editing");
  expectStates(lc.render(), [D, P, A, P, P, P]);
}

// Case 1: task_started -> Planning, lalu langsung inspecting.
{
  const lc = lifecycle();
  lc.step("planning");
  assert.equal(lc.render()[0], A);
  lc.step("inspecting");
  expectStates(lc.render(), [D, A, P, P, P, P]);
}

// --- Acceptance 9/10: failed & cancelled BUKAN Completed --------------------
{
  const lc = lifecycle();
  ["planning", "inspecting", "editing"].forEach(lc.step);
  lc.setStatus("failed");
  const steps = lc.render();
  expectStates(steps, [D, D, A, P, P, P]);
  assert.notEqual(steps[5], A, "Failed TIDAK menampilkan Completed");
}
{
  const lc = lifecycle();
  ["planning", "inspecting", "editing", "running"].forEach(lc.step);
  lc.setStatus("cancelled");
  const steps = lc.render();
  expectStates(steps, [D, D, D, A, P, P]);
  assert.notEqual(steps[5], A, "Cancelled TIDAK menampilkan Completed");
}

// --- Acceptance 11: runtime.phase internal tidak menggerakkan lifecycle ------
{
  const lc = lifecycle();
  ["planning", "editing"].forEach(lc.step);
  // Nilai internal (replan/provider_fallback) tidak diklasifikasi -> diabaikan.
  lc.step("replan");
  lc.step("provider_fallback");
  expectStates(lc.render(), [D, P, A, P, P, P]);
}

// --- Task belum mulai / idle ------------------------------------------------
expectStates(
  buildLifecycleStates({ hasTask: false, status: "idle", currentPhase: "", milestones: [] }),
  [P, P, P, P, P, P]
);
// Task running tanpa phase apa pun -> Planning (fallback aman).
expectStates(
  buildLifecycleStates({ hasTask: true, status: "running", currentPhase: "", milestones: [] }),
  [A, P, P, P, P, P]
);

// --- lifecycleFromEvents (history task lama) --------------------------------
{
  const events = [
    { event: "task_started", data: {} },
    { event: "phase_changed", data: { phase: "inspecting" } },
    { event: "phase_changed", data: { phase: "editing" } },
    { event: "phase_changed", data: { phase: "running" } },
    { event: "task_failed", data: {} },
  ];
  const { milestones, currentPhase } = lifecycleFromEvents(events);
  assert.deepEqual(milestones, [0, 1, 2, 3]);
  assert.equal(currentPhase, "running");
  const steps = buildLifecycleStates({
    hasTask: true,
    status: "failed",
    currentPhase,
    milestones,
    count: STEPS.length,
  });
  expectStates(steps, [D, D, D, A, P, P]);
  assert.notEqual(steps[5], A);
}

// Event validation existing (validation_started) -> milestone Validating.
{
  const { milestones, currentPhase } = lifecycleFromEvents([
    { event: "task_started", data: {} },
    { event: "validation_started", data: {} },
  ]);
  assert.deepEqual(milestones, [0, 4]);
  assert.equal(currentPhase, "planning");
}

console.log(
  "[OK] lifecycle: phase_changed -> 6 step; milestone tetap done saat mundur; completed/failed/cancelled benar."
);
