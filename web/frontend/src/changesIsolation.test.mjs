// Regression test (Node built-in `assert`, TANPA framework/dependency baru)
// untuk modul yang DI-EXTRACT dari App.vue saat refactor:
//   - useChanges()          : isolasi changes per task + upsert + agregasi
//   - isAetherMetadata()    : filter metadata internal `.aether/**`
//   - executionMode.js      : normalisasi/label execution mode
//   - taskHistory.js        : grouping history (TODAY/YESTERDAY/OLDER)
//
// Mengunci perilaku kunci "changes isolation" supaya refactor tidak menurunkan
// semantik: satu file = satu baris per task; task lain TIDAK tertimpa; file
// `.aether/**` tidak tampil; agregasi hanya file dari task aktif/pending.
//
// Jalankan: node web/frontend/src/changesIsolation.test.mjs

import assert from "node:assert/strict";
import { reactive } from "vue";

import { isAetherMetadata, useChanges } from "./composables/useChanges.js";
import { executionLabel, isActiveTaskState, normalizeExecutionMode } from "./executionMode.js";
import { groupTaskHistory, statusTagClass, taskTimeGroup } from "./taskHistory.js";

// ---- 1) isAetherMetadata: sembunyikan metadata internal AETHER -------------
{
  assert.equal(isAetherMetadata(".aether/log/task.log"), true);
  assert.equal(isAetherMetadata(".aether"), true);
  assert.equal(isAetherMetadata("sub/.aether/permissions.json"), true);
  assert.equal(isAetherMetadata("src/index.js"), false);
  assert.equal(isAetherMetadata("my.aether/x"), false);
  assert.equal(isAetherMetadata(""), false);
  console.log("PASS: isAetherMetadata memfilter .aether/** tetapi bukan file project");
}

// ---- 2) useChanges: upsert + isolasi per task + agregasi -------------------
{
  const task = reactive({ id: "task-A" });
  const activeIds = new Set(["task-A", "task-B"]);
  const changes = useChanges({
    task,
    isViewingRunningTask: () => true,
    getActiveTaskIds: () => new Set(activeIds),
  });

  // Changes task A.
  changes.upsertChange({ path: "src/a.js", kind: "modified" }, "task-A");
  changes.upsertChange({ path: "src/a.js", kind: "modified", additions: 3 }, "task-A");
  changes.upsertChange({ path: "src/old.js", kind: "renamed", old_path: "src/old2.js", path: "src/old.js" }, "task-A");
  changes.upsertChange({ path: "docs/readme.md", kind: "added" }, "task-B");

  // Satu file = satu baris (tidak menumpuk duplikat).
  assert.equal(changes.changesByTask.value["task-A"].length, 2, "task-A harus 2 file unik");
  const aRow = changes.changesByTask.value["task-A"].find((c) => c.path === "src/a.js");
  assert.equal(aRow.additions, 3, "upsert harus memperbarui baris yang sama");

  // Task B terisolasi: perubahan A TIDAK muncul di B.
  assert.equal(changes.changesByTask.value["task-B"].length, 1, "task-B harus 1 file");
  assert.equal(
    changes.changesByTask.value["task-B"].some((c) => c.path === "src/a.js"),
    false,
    "changes task A tidak boleh bocor ke task B"
  );

  // `changes` mengikuti task yang sedang dibuka.
  assert.equal(changes.changes.value.length, 2, "changes mengikuti task.id (task-A)");
  task.id = "task-B";
  assert.equal(changes.changes.value.length, 1, "berpindah task -> changes ikut task baru");

  // Agregasi header: gabungan file unik dari seluruh task aktif.
  assert.deepEqual(
    [...changes.aggregateChanges.value].sort(),
    ["docs/readme.md", "src/a.js", "src/old.js"].sort(),
    "agregasi = union path dari task aktif"
  );

  // Reset total (mis. close project) mengosongkan semua bucket.
  changes.resetAllChanges();
  assert.deepEqual(changes.changesByTask.value, {}, "resetAllChanges mengosongkan bucket");
  console.log("PASS: useChanges upsert + isolasi per task + agregasi + reset");
}

// ---- 3) loadChangesFromEvents: rekonstruksi history + filter .aether -------
{
  const task = reactive({ id: "" });
  const changes = useChanges({
    task,
    isViewingRunningTask: () => false,
    getActiveTaskIds: () => [],
  });

  changes.loadChangesFromEvents(
    [
      { event_type: "change_detected", payload: { path: "src/x.js", kind: "modified", additions: 1 } },
      { event_type: "change_detected", payload: { path: ".aether/log/task.log", kind: "added" } },
      { event_type: "tool_called", payload: { tool: "read_file" } },
    ],
    "task-H"
  );
  assert.equal(changes.changesByTask.value["task-H"].length, 1, "hanya change_detected non-.aether");
  assert.equal(changes.changesByTask.value["task-H"][0].path, "src/x.js");
  console.log("PASS: loadChangesFromEvents merekonstruksi + memfilter .aether/**");
}

// ---- 3b) loadChangesFromEvents: bentuk PERSISTENT LOG (`event`/`data`) ------
// Activity API (.aether/log/<task_id>.log) menulis record dengan kunci
// `event`/`data`, BUKAN `event_type`/`payload` (bentuk SSE live). Kedua bentuk
// harus direkonstruksi agar daftar Changes tetap tersedia setelah task selesai
// / dibuka kembali (regression: file diedit tidak muncul di Changes).
{
  const task = reactive({ id: "" });
  const changes = useChanges({
    task,
    isViewingRunningTask: () => false,
    getActiveTaskIds: () => [],
  });

  changes.loadChangesFromEvents(
    [
      { event: "task_requested", data: { prompt: "ubah app.py" } },
      { event: "tool_completed", data: { tool: "edit_file", success: true } },
      { event: "change_detected", data: { path: "app.py", kind: "modified", additions: 2, deletions: 1 } },
      { event: "change_detected", data: { path: ".aether/log/task.log", kind: "added" } },
      { event: "task_completed", data: { result: "selesai" } },
    ],
    "task-P"
  );
  assert.equal(changes.changesByTask.value["task-P"].length, 1, "bentuk log `event`/`data` harus dikenali");
  assert.equal(changes.changesByTask.value["task-P"][0].path, "app.py");
  assert.equal(changes.changesByTask.value["task-P"][0].additions, 2);
  console.log("PASS: loadChangesFromEvents membaca bentuk persistent log (event/data)");
}

// ---- 4) executionMode: normalisasi + label ---------------------------------
{
  assert.equal(normalizeExecutionMode("parallel"), "parallel");
  assert.equal(normalizeExecutionMode("PARALLEL"), "parallel");
  assert.equal(normalizeExecutionMode(""), "queue");
  assert.equal(normalizeExecutionMode(undefined), "queue");
  assert.equal(normalizeExecutionMode("weird"), "queue");
  assert.equal(executionLabel("parallel"), "Parallel");
  assert.equal(executionLabel("queue"), "Queue");
  assert.equal(isActiveTaskState("running"), true);
  assert.equal(isActiveTaskState("completed"), false);
  console.log("PASS: executionMode normalisasi/label/active-state");
}

// ---- 5) taskHistory: grup waktu + kelas status -----------------------------
{
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 9).toISOString();
  const older = new Date(now.getFullYear() - 1, 0, 1).toISOString();
  assert.equal(taskTimeGroup({ last_timestamp: today }), "TODAY");
  assert.equal(taskTimeGroup({ last_timestamp: older }), "OLDER");
  assert.equal(taskTimeGroup({}), "OLDER");

  const groups = groupTaskHistory([
    { task_id: "t1", last_timestamp: today },
    { task_id: "t2", last_timestamp: older },
  ]);
  assert.deepEqual(groups.map((g) => g.label), ["TODAY", "OLDER"]);
  assert.equal(groups[0].items.length, 1);

  assert.equal(statusTagClass("completed"), "status-on");
  assert.equal(statusTagClass("failed"), "status-err");
  assert.equal(statusTagClass("running"), "status-run");
  assert.equal(statusTagClass("idle"), "status-off");
  console.log("PASS: taskHistory grouping + kelas status");
}

console.log("\n=== ALL WORKBENCH MODULE REFACTOR TESTS PASSED ===");
