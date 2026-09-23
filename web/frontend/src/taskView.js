// AETHER Workbench — logika UI: task mana yang DIPANTAU vs task yang baru
// dibuat tapi masih PENDING di Global Task Queue.
//
// Root cause bug yang diperbaiki: saat Task A sedang RUNNING lalu user submit
// Task B, `submitTask()` SELALU meng-adopsi B sebagai task yang dipantau
// (`task.id = B`, `resetWorkspace()`, `connectStream()`) walaupun backend
// mengembalikan `queue_state="pending"` (B belum dieksekusi). Akibatnya panel
// Agent Activity berpindah ke task yang masih pending dan stream Task A
// terputus — Agent seolah berhenti/pindah dari A.
//
// Helper murni (tanpa Vue/DOM) ini memisahkan dua konsep yang sebelumnya
// tercampur:
//   - viewed/current task  = task yang sedang BENAR-BENAR dikerjakan & dipantau
//   - newly submitted task = task yang baru dibuat (bisa masih pending)
// sehingga keputusan ini dapat diuji tanpa menambah framework test frontend.

/** Apakah task yang dipantau saat ini benar-benar menempati slot eksekusi? */
export function isViewedTaskRunning(viewedTaskId, runningTaskId) {
  return Boolean(viewedTaskId) && viewedTaskId === runningTaskId;
}

/**
 * Boleh task yang BARU dibuat langsung menggantikan task yang sedang dipantau?
 *
 * - `queue_state === "running"`: task ini langsung mendapat slot -> YA.
 * - `queue_state === "pending"`: menunggu slot di Global Task Queue ->
 *     * bila sedang memantau task yang BENAR-BENAR running -> TIDAK
 *       (Task A tetap tampil; B hanya masuk antrian sebagai pending),
 *     * bila tidak memantau task running (idle / melihat riwayat) -> YA
 *       (task baru menjadi yang dipantau, ditampilkan sebagai "queued").
 * - nilai lain: perilaku lama (status diambil dari backend).
 */
export function shouldAdoptSubmittedTask({ queueState, isViewingRunning }) {
  if (queueState === "pending") return !isViewingRunning;
  return true;
}

/**
 * Boleh UI berpindah memantau task yang baru BENAR-BENAR mulai running
 * (event `task_started`)? Hanya bila SEMUA terpenuhi:
 *   - task itu BUKAN task yang sedang dipantau,
 *   - sedang TIDAK memantau task yang benar-benar running (agar tidak menutupi
 *     task aktif),
 *   - user TIDAK sedang sengaja melihat riwayat/log task lain (`viewingHistory`),
 *   - task itu memang kita antrikan sebelumnya (`deferredTaskIds`) — yaitu task
 *     yang dibuat ketika task lain sedang running.
 * Inilah yang membuat UI mengikuti B SETELAH A selesai dan scheduler
 * benar-benar menjalankan B.
 */
export function shouldFollowStartedTask({
  startedTaskId,
  viewedTaskId,
  isViewingRunning,
  viewingHistory = false,
  deferredTaskIds,
}) {
  if (!startedTaskId) return false;
  if (startedTaskId === viewedTaskId) return false;
  if (isViewingRunning) return false;
  if (viewingHistory) return false;
  return Boolean(
    deferredTaskIds &&
      typeof deferredTaskIds.has === "function" &&
      deferredTaskIds.has(startedTaskId)
  );
}
