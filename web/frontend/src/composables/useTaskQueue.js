import { computed, ref } from "vue";
import { cancelTask, listTaskQueue } from "../api.js";
import { isQueueSlotOccupied, isViewedTaskRunning, shouldFollowStartedTask } from "../taskView.js";
import { normalizeExecutionMode } from "../executionMode.js";

// Queue & "task yang dipantau" (Global Task Queue AETHER — SATU sumber
// kebenaran = TaskRecord backend, GET /api/tasks/queue). Modul ini BUKAN queue
// subsystem kedua: hanya proyeksi status antrian existing + penanda UI untuk
// memilih task mana yang ditampilkan.
//
// Dependensi (inject dari composition root):
//   task                = task yang SEDANG dipantau (reactive).
//   selectedProjectId   = project aktif (filter queue).
//   resetWorkspace      = reset activity/timing saat pantauan pindah task.
//   ensureStream        = pastikan stream SSE terbuka (event task_started).
//   markStartedIfUnset  = set waktu mulai task (timing) bila belum ada.
//   bumpQueueRefresh    = naikkan penanda refresh QueuePanel.
//   getViewingHistory   = apakah user sedang melihat riwayat task lama.
//   setError            = laporan error ke banner global.
export function useTaskQueue({
  task,
  selectedProjectId,
  resetWorkspace,
  ensureStream,
  markStartedIfUnset,
  bumpQueueRefresh,
  getViewingHistory,
  openTask,
  setError,
}) {
  // ID task yang BENAR-BENAR sedang RUNNING di Global Task Queue. Dipakai
  // sebagai target tombol Stop agar Stop SELALU merujuk ke task yang benar.
  const runningTaskId = ref("");
  // Item antrian aktif (pending/running/disabled) dari GET /api/tasks/queue.
  const queueItems = ref([]);
  // Badge QUEUE = jumlah item non-terminal (endpoint queue hanya mengembalikan
  // pending/running/disabled; task terminal tidak masuk antrian).
  const queueCount = computed(() => queueItems.value.length);

  // task_id terakhir yang mencapai status terminal (completed/failed/cancelled).
  // Dipakai agar refresh antrian TIDAK memunculkan kembali task yang berhenti
  // (respons antrian bisa saja masih memuat status lama tepat setelah selesai).
  const terminalTaskId = ref("");

  // Task yang kita ANTRIKAN: task yang dibuat saat ADA task lain yang benar-benar
  // running, sehingga UI SENGAJA tidak berpindah ke task itu (tetap pending di
  // daftar antrian). Map task_id -> { task, executionMode }. Ini BUKAN queue
  // subsystem kedua: hanya penanda UI agar UI dapat MENGIKUTI task ini begitu
  // scheduler benar-benar menjalankannya (event task_started).
  const deferredTaskIds = new Map();

  const hasActiveTask = computed(() => Boolean(task.id));
  const isRunning = computed(() => Boolean(runningTaskId.value));

  // Apakah yang SEDANG dipantau benar-benar task yang berjalan? Sumber utama =
  // Global Task Queue (runningTaskId); fallback = status lifecycle task yang
  // dipantau (bila respons antrian belum termuat). SENGAJA bukan state kedua.
  const ACTIVE_TASK_STATUSES = ["running", "prepared", "planning", "executing", "validating"];
  function isViewingRunningTask() {
    if (isViewedTaskRunning(task.id, runningTaskId.value)) return true;
    return (
      Boolean(task.id) &&
      ACTIVE_TASK_STATUSES.includes(String(task.status || "").toLowerCase())
    );
  }

  let refreshRunningTaskGen = 0;
  async function refreshRunningTask() {
    const cur = ++refreshRunningTaskGen;
    try {
      const data = await listTaskQueue(selectedProjectId.value || null);
      if (cur !== refreshRunningTaskGen) return;
      const items = data.tasks || [];
      queueItems.value = items;
      // Jangan anggap "running" task yang sudah kita ketahui terminal: respons
      // antrian bisa saja masih memuat status lama tepat setelah task selesai.
      // Slot juga masih dianggap terpakai selama task "cancelling" (pembatalan
      // sudah diminta tetapi lifecycle eksekusinya belum selesai) sehingga
      // tombol Stop tetap menunjuk task yang benar.
      const running = items.find(
        (t) => isQueueSlotOccupied(t.queue_state) && t.task_id !== terminalTaskId.value
      );
      // Fallback follow: bila scheduler sudah menjalankan task yang kita antrikan
      // (deferred) sementara kita TIDAK memantau task running mana pun -> ikuti
      // task itu. Menutup celah race bila event `task_started` tiba lebih dulu
      // dari respons antrian ini (atau koneksi SSE sempat terputus).
      if (
        running &&
        shouldFollowStartedTask({
          startedTaskId: running.task_id,
          viewedTaskId: task.id || "",
          isViewingRunning: isViewingRunningTask(),
          viewingHistory: Boolean(getViewingHistory()),
          deferredTaskIds,
        })
      ) {
        {
          const d2 = deferredTaskIds.get(running.task_id);
          const d2Mode = typeof d2 === "object" && d2 ? d2.executionMode : null;
          adoptRunningTask(running.task_id, running.task, running.execution_mode || running.executionMode || d2Mode);
        }
      }
      runningTaskId.value = running ? running.task_id : "";
    } catch {
      // Endpoint queue belum tersedia: pertahankan state existing (fallback).
    }
  }

  // Task yang berjalan sudah mencapai status terminal -> lepas target tombol Stop
  // SECARA SINKRON (tanpa menunggu refresh antrian async). Ini yang menjamin
  // tombol Stop LANGSUNG hilang saat task selesai/gagal/cancel, tanpa jendela
  // race. Hanya pemilik slot yang dilepas (bila diketahui).
  function releaseRunningTask(taskId) {
    if (!runningTaskId.value) return;
    if (!taskId || taskId === runningTaskId.value) runningTaskId.value = "";
  }

  // Pindahkan pantauan (Task Card + Agent Activity) ke task yang BENAR-BENAR
  // mulai running. Dipakai HANYA saat UI mengikuti task antrian berikutnya
  // setelah task sebelumnya selesai. Murni memilih task mana yang ditampilkan:
  // TIDAK menyentuh scheduler/queue backend.
  function adoptRunningTask(taskId, text = "", executionMode = null) {
    if (!taskId || taskId === task.id) return;
    task.id = taskId;
    if (text) task.text = text;
    task.status = "running";
    if (executionMode) task.executionMode = normalizeExecutionMode(executionMode);
    runningTaskId.value = taskId;
    // Task ini tidak lagi "tertunda" follow.
    deferredTaskIds.delete(taskId);
    // Pantauan berpindah task -> buang activity/timing task sebelumnya.
    resetWorkspace();
    ensureStream();
    // resetWorkspace mengosongkan timing -> set waktu mulai task yang diadopsi.
    markStartedIfUnset();
  }

  // Panel TASKS di Consultant (SATU queue global AETHER). Refresh dipicu setelah
  // Run Task / event terminal task. Bukan queue subsystem kedua.
  async function stopQueueTask(taskId) {
    if (!taskId) return;
    try {
      await cancelTask(taskId);
    } catch (e) {
      setError(e.message || "Failed to stop task.");
    } finally {
      bumpQueueRefresh();
    }
  }

  // View Task dari Task List (klik kiri pada item RUNNING): buka task yang
  // DIPILIH (task_id item yang diklik — BUKAN runningTaskId global) lewat alur
  // history/activity existing. Guard defensif: hanya object task valid dengan
  // queue_state="running" yang boleh membuka Latest Task; task pending/disabled
  // tidak mengubah tampilan.
  function viewQueueTask(t) {
    if (!t || !t.task_id) return;
    if (t.queue_state !== "running") return;
    openTask(t.task_id);
  }

  return {
    runningTaskId,
    queueItems,
    queueCount,
    terminalTaskId,
    deferredTaskIds,
    hasActiveTask,
    isRunning,
    isViewingRunningTask,
    refreshRunningTask,
    releaseRunningTask,
    adoptRunningTask,
    stopQueueTask,
    viewQueueTask,
  };
}
