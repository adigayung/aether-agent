// Regression test (Node built-in `assert`, TANPA framework/dependency baru)
// untuk logika pemisahan "viewed/running task" vs "newly submitted task".
//
// Mengunci perilaku bug yang diperbaiki: submit Task B saat Task A RUNNING
// TIDAK boleh meng-adopsi B sebagai task yang dipantau; B hanya masuk antrian
// (pending). UI baru boleh mengikuti B ketika B benar-benar mulai running.
//
// Jalankan: node web/frontend/src/taskView.test.mjs
// (Atau via scripts/check_queue_view_follows_running.py.)

import assert from "node:assert/strict";
import {
  isViewedTaskRunning,
  shouldAdoptSubmittedTask,
  shouldFollowStartedTask,
} from "./taskView.js";

const A = "task-a";
const B = "task-b";

// --- isViewedTaskRunning -----------------------------------------------------
assert.equal(isViewedTaskRunning(A, A), true);
assert.equal(isViewedTaskRunning(A, B), false);
assert.equal(isViewedTaskRunning("", ""), false, "idle bukan running");

// --- shouldAdoptSubmittedTask: skenario bug utama ----------------------------
// A RUNNING (dipantau) -> submit B pending -> JANGAN adopsi B.
assert.equal(
  shouldAdoptSubmittedTask({ queueState: "pending", isViewingRunning: true }),
  false,
  "B pending saat A running TIDAK boleh menggantikan tampilan A"
);
// Idle (tidak memantau task running) -> submit pending -> adopsi (tampil queued).
assert.equal(
  shouldAdoptSubmittedTask({ queueState: "pending", isViewingRunning: false }),
  true,
  "task pending saat idle menjadi task yang dipantau"
);
// Task langsung dapat slot -> selalu adopsi.
assert.equal(
  shouldAdoptSubmittedTask({ queueState: "running", isViewingRunning: true }),
  true
);
assert.equal(
  shouldAdoptSubmittedTask({ queueState: "running", isViewingRunning: false }),
  true
);
// Status non-antrian -> perilaku lama (adopsi, status dari backend).
assert.equal(
  shouldAdoptSubmittedTask({ queueState: undefined, isViewingRunning: true }),
  true
);

// --- shouldFollowStartedTask: A selesai, scheduler menjalankan B -------------
const deferred = new Set([B]);
// B mulai running, A (yang dipantau) tidak running lagi -> IKUTI B.
assert.equal(
  shouldFollowStartedTask({
    startedTaskId: B,
    viewedTaskId: A,
    isViewingRunning: false,
    deferredTaskIds: deferred,
  }),
  true,
  "UI harus mengikuti B setelah A selesai dan B benar-benar running"
);
// Masih memantau task running -> JANGAN ikuti (jangan menutupi task aktif).
assert.equal(
  shouldFollowStartedTask({
    startedTaskId: B,
    viewedTaskId: A,
    isViewingRunning: true,
    deferredTaskIds: deferred,
  }),
  false
);
// Task yang mulai = task yang sudah dipantau -> bukan kasus follow.
assert.equal(
  shouldFollowStartedTask({
    startedTaskId: A,
    viewedTaskId: A,
    isViewingRunning: false,
    deferredTaskIds: deferred,
  }),
  false
);
// Task yang TIDAK kita antrikan (mis. hasil buka riwayat) -> jangan culik UI.
assert.equal(
  shouldFollowStartedTask({
    startedTaskId: "task-x",
    viewedTaskId: A,
    isViewingRunning: false,
    deferredTaskIds: deferred,
  }),
  false
);
// User sedang sengaja melihat riwayat/log -> jangan pindahkan pantauan.
assert.equal(
  shouldFollowStartedTask({
    startedTaskId: B,
    viewedTaskId: A,
    isViewingRunning: false,
    viewingHistory: true,
    deferredTaskIds: deferred,
  }),
  false
);
assert.equal(
  shouldFollowStartedTask({
    startedTaskId: B,
    viewedTaskId: A,
    isViewingRunning: false,
    deferredTaskIds: null,
  }),
  false
);

// --- Skenario end-to-end kecil (urutan keputusan) ---------------------------
// 1) A running & dipantau. 2) submit B pending -> tidak diadopsi.
let viewed = A;
let running = A;
let viewingRunning = isViewedTaskRunning(viewed, running);
assert.equal(
  shouldAdoptSubmittedTask({ queueState: "pending", isViewingRunning: viewingRunning }),
  false
);
const deferredIds = new Set([B]); // B diantrikan.
// 3) A selesai (terminal) -> bukan running lagi.
running = "";
viewingRunning = isViewedTaskRunning(viewed, running);
// 4) B benar-benar mulai running -> UI mengikuti.
assert.equal(
  shouldFollowStartedTask({
    startedTaskId: B,
    viewedTaskId: viewed,
    isViewingRunning: viewingRunning,
    deferredTaskIds: deferredIds,
  }),
  true
);
viewed = B;
deferredIds.delete(B);
assert.equal(viewed, B, "UI kini memantau B");

console.log("[OK] taskView: viewed/running dipsisahkan dari newly-submitted; follow setelah A selesai benar.");
