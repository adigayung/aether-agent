// Regression test (Node built-in `assert`, tanpa framework/dependency baru)
// untuk jalur RUNTIME panel Changes:
//
//     change_detected -> useAgentActivity.handleEvent -> useChanges.upsertChange
//                     -> changesByTask[task_id] -> computed `changes` (panel)
//
// Bug yang dikunci (panel Changes tetap "0 files" walau Agent edit/menulis/
// menghapus file): `useAgentActivity` meng-inject helper dari useChanges()
// (`const { upsertChange, isAetherMetadata } = changes`), tetapi useChanges()
// TIDAK mengembalikan `isAetherMetadata` — hanya mengekspornya sebagai named
// export modul. Akibatnya `case "change_detected"` melempar
// `TypeError: isAetherMetadata is not a function` SEBELUM upsertChange dipanggil,
// sehingga event perubahan dibuang. Agent Activity tetap menampilkan "Editing
// files" karena `pushRolling()` berjalan SEBELUM switch.
//
// Test ini mengimpor komposabel ASLI (bukan menyalin logikanya) sehingga kontrak
// DI + perilaku reactive benar-benar diuji. Asset audio (.wav) di-shim lewat
// hooks module-register karena Node tidak bisa memuatnya (Vite yang membundel).
//
// Jalankan: node web/frontend/src/changesPanelFlow.test.mjs

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
function makeHarness({ activeTaskIds = [], viewingRunning = true } = {}) {
  const task = reactive({ id: "task-1", status: "" });
  const timing = { taskStartedAt: ref(null), taskEndedAt: ref(null), resetTiming() {} };
  const changes = useChanges({
    task,
    isViewingRunningTask: () => viewingRunning,
    getActiveTaskIds: () => new Set(activeTaskIds),
  });
  const queue = {
    runningTaskId: ref(""),
    releaseRunningTask() {},
    adoptRunningTask() {},
    isViewingRunningTask: () => viewingRunning,
    deferredTaskIds: new Map(),
    terminalTaskId: ref(""),
  };
  const approvals = { handleApprovalEvent() {}, dropApprovalsForTask() {} };
  const activity = useAgentActivity({
    task,
    timing,
    changes,
    queue,
    approvals,
    bumpQueueRefresh() {},
    refreshTaskHistory() {},
    clearReport() {},
    getCopyMeta: () => ({}),
  });
  return { task, changes, activity };
}

// Bentuk event SSE live = ExecutionEvent.to_dict() (dikirim oleh /api/events).
function sseEvent(overrides = {}) {
  return {
    event_id: overrides.event_id || "evt-1",
    session_id: "session-1",
    task_id: overrides.task_id || "task-1",
    event_type: overrides.event_type || "change_detected",
    timestamp: 1700000000,
    payload: overrides.payload || { path: "naruto.txt", kind: "created", additions: 1 },
    sequence: overrides.sequence != null ? overrides.sequence : 1,
  };
}

// ---- 1) KONTRAK DI: useChanges() menyediakan SEMUA helper yang di-destructure
//         useAgentActivity. Ini persis titik gagal bug "0 files".
{
  const { changes } = makeHarness();
  for (const name of ["upsertChange", "isAetherMetadata", "parseChangeEvent"]) {
    assert.equal(
      typeof changes[name],
      "function",
      `useChanges() WAJIB mengembalikan '${name}' (dipakai useAgentActivity via DI)`
    );
  }
  console.log("PASS: kontrak DI useChanges() lengkap (upsertChange/isAetherMetadata/parseChangeEvent)");
}

// ---- 2) change_detected LIVE -> masuk bucket task_id -> terlihat di panel ---
{
  const { task, changes, activity } = makeHarness();
  activity.handleEvent(sseEvent());
  const bucket = changes.changesByTask.value["task-1"];
  assert.ok(bucket && bucket.length === 1, "event live harus mengisi bucket task-1");
  assert.equal(bucket[0].path, "naruto.txt", "path harus benar (bukan undefined)");
  assert.equal(bucket[0].kind, "created");
  // Panel membaca `changes` untuk task yang sedang dibuka -> BUKAN 0 files.
  assert.equal(changes.changes.value.length, 1, "panel Changes (task.id) harus 1 file");
  task.id = "task-1";
  assert.equal(changes.changes.value.length, 1);
  console.log("PASS: change_detected live -> changesByTask[task_id] -> panel 1 file (bukan 0)");
}

// ---- 3) Dedup dua stream (queue + task-scoped) -> tetap 1 baris ------------
{
  const { changes, activity } = makeHarness();
  const evt = sseEvent({ event_id: "same-id" });
  activity.handleEvent(evt);
  activity.handleEvent({ ...evt }); // pengiriman kedua (stream lain)
  assert.equal(changes.changesByTask.value["task-1"].length, 1, "duplikat event tidak menambah baris");
  console.log("PASS: event sama dari dua stream tetap satu baris (dedup)");
}

// ---- 4) Isolasi antar-task + file sama diedit berkali-kali = upsert ---------
{
  const { changes, activity } = makeHarness();
  activity.handleEvent(sseEvent({ task_id: "task-A", event_id: "a1", payload: { path: "src/a.js", kind: "modified" } }));
  activity.handleEvent(sseEvent({ task_id: "task-B", event_id: "b1", payload: { path: "docs/b.md", kind: "created" } }));
  activity.handleEvent(sseEvent({ task_id: "task-A", event_id: "a2", payload: { path: "src/a.js", kind: "modified", additions: 7 } }));
  assert.equal(changes.changesByTask.value["task-A"].length, 1, "file sama -> di-upsert, bukan diduplikasi");
  assert.equal(changes.changesByTask.value["task-A"][0].additions, 7, "upsert memperbarui baris yang sama");
  assert.equal(changes.changesByTask.value["task-B"].length, 1, "task B terisolasi");
  assert.equal(
    changes.changesByTask.value["task-B"].some((c) => c.path === "src/a.js"),
    false,
    "changes task A tidak bocor ke task B"
  );
  console.log("PASS: isolasi antar-task + upsert file yang sama");
}

// ---- 5) `.aether/**` disembunyikan; path invalid TIDAK membuat baris hantu ---
{
  const { changes, activity } = makeHarness();
  activity.handleEvent(sseEvent({ event_id: "meta", payload: { path: ".aether/log/task.log", kind: "added" } }));
  assert.equal((changes.changesByTask.value["task-1"] || []).length, 0, ".aether tidak tampil");

  activity.handleEvent(sseEvent({ event_id: "nopath", payload: { kind: "modified" } }));
  activity.handleEvent(sseEvent({ event_id: "empty", payload: { path: "   ", kind: "modified" } }));
  assert.equal((changes.changesByTask.value["task-1"] || []).length, 0, "path kosong tidak di-upsert");
  console.log("PASS: filter .aether/** + tolak path kosong/undefined");
}

// ---- 6) Bentuk log PERSISTEN (`event`/`data`) -> rekonstruksi history --------
// (membuka task completed dari History tetap menampilkan file yang berubah)
{
  const { task, changes } = makeHarness();
  task.id = "task-H";
  changes.loadChangesFromEvents(
    [
      { event: "task_requested", data: { prompt: "tulis naruto.txt" } },
      { event: "tool_completed", data: { tool: "write_file", success: true } },
      { event: "change_detected", data: { path: "naruto.txt", kind: "created", additions: 1 } },
      { event: "change_detected", data: { path: ".aether/log/task.log", kind: "added" } },
      { event: "task_completed", data: { result: "selesai" } },
    ],
    "task-H"
  );
  assert.equal(changes.changesByTask.value["task-H"].length, 1, "rekonstruksi dari Activity API");
  assert.equal(changes.changesByTask.value["task-H"][0].path, "naruto.txt");
  assert.equal(changes.changes.value.length, 1, "task completed dibuka dari history tetap tampil");
  console.log("PASS: loadChangesFromEvents (bentuk log event/data) merekonstruksi Changes");
}

// ---- 7) Format ambigu TIDAK diterima diam-diam (canonical `payload`/`data`) --
{
  const changesState = useChanges({
    task: reactive({ id: "t" }),
    isViewingRunningTask: () => false,
    getActiveTaskIds: () => [],
  });
  const { parseChangeEvent } = changesState;
  // Bentuk lama hipotetis (file_path di root event) harus DITOLAK, bukan
  // menghasilkan baris tanpa path.
  assert.equal(parseChangeEvent({ event_type: "change_detected", file_path: "x.js" }), null);
  assert.equal(parseChangeEvent({ event_type: "tool_called", payload: { path: "x.js" } }), null);
  assert.equal(parseChangeEvent({ event_type: "change_detected", payload: { path: "  " } }), null);
  const ok = parseChangeEvent({
    event_type: "change_detected",
    task_id: "t",
    payload: { path: "ok.js", kind: "modified" },
  });
  assert.equal(ok.task_id, "t");
  assert.equal(ok.change.path, "ok.js");
  console.log("PASS: parser canonical hanya menerima bentuk payload/data yang valid");
}

console.log("\n=== ALL CHANGES PANEL LIVE-FLOW TESTS PASSED ===");
