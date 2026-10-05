import { ref } from "vue";
import { cancelTask, createTask, getTaskActivity, getTaskHistory, getTaskReport } from "../api.js";
import { shouldAdoptSubmittedTask } from "../taskView.js";
import { lifecycleFromEvents } from "../lifecycle.js";
import { normalizeExecutionMode } from "../executionMode.js";
import { resetAudioTracker } from "../audioRegistry.js";

// Lifecycle task (#50): submit, stop (+ konfirmasi), buka task dari persistent
// log, dan report final. Modul ini juga memegang state task yang sedang dibuka
// (id/teks/status) agar modul lain bisa membacanya lewat `task` yang sama.
//
// `task` (reactive) dibuat di composition root dan di-share ke modul lain
// (queue/activity/telemetry) supaya hanya ada SATU task yang dipantau.
export function useTaskLifecycle({
  task,
  taskData,
  timing,
  changes,
  activity,
  queue,
  stream,
  selectedProjectId,
  selectedMode,
  selectedProviderInstanceId,
  selectedModelId,
  selectedExecutionMode,
  closeComposer,
  setActiveNav,
  error,
  setError,
  bumpQueueRefresh,
}) {
  const { tasks, refreshTasks, refreshTaskHistory } = taskData;
  const { queueItems, deferredTaskIds, runningTaskId, isViewingRunningTask, isRunning } = queue;
  const { resetAllChanges, loadChangesFromEvents } = changes;
  const { resetWorkspace, historyEvents, activityPhase, lifecycleMilestones } = activity;

  const submitting = ref(false);

  // Final Agent Report (Report API, .aether/log/). null = belum dimuat.
  const currentReport = ref(null);
  const reportOpen = ref(false);
  const reportTaskId = ref("");
  const reportStatus = ref("");

  // Konfirmasi Stop Task (confirmation layer di depan aksi Stop agent-input).
  const stopConfirmOpen = ref(false);
  const stopInProgress = ref(false);

  // --- Task submit / stop (#50) ----------------------------------------------
  async function submitTask(
    text,
    overrideProviderInstanceId = null,
    overrideModelId = null,
    overrideExecutionMode = null,
    images = null
  ) {
    setError("");
    submitting.value = true;
    try {
      // Provider Instance + Model dari konfigurasi LLM tersimpan (SQLite)
      // diteruskan sebagai metadata ke mekanisme AETHER existing. Runtime
      // merakit provider + api_url + api_key + model dari DB ini (bukan .env).
      // Mode tetap routing signal.
      // Override per-task (mis. dari card Task Proposal Consultant) MENANG atas
      // pilihan global; bila tidak diisi -> perilaku default (pilihan global).
      const providerId = overrideProviderInstanceId || selectedProviderInstanceId.value;
      const modelId = overrideModelId || selectedModelId.value;
      const metadata = {};
      if (providerId) metadata.provider_instance_id = providerId;
      if (modelId) metadata.model_id = modelId;
      if (selectedMode.value) metadata.mode = selectedMode.value;
      // execution_mode — Task 01: queue | parallel (hanya parameter niat).
      const executionMode = overrideExecutionMode || selectedExecutionMode.value || "queue";
      const record = await createTask(
        text,
        selectedProjectId.value || null,
        Object.keys(metadata).length ? metadata : null,
        executionMode,
        images || null
      );
      // Status awal = KEBENARAN backend, BUKAN optimistik. Task yang dikirim
      // masuk SATU Global Task Queue; bila slot eksekusi sedang terpakai,
      // backend mengembalikan queue_state="pending" -> task belum berjalan.
      // Promosi ke running datang dari SSE `task_started`.
      const queueState = record.queue_state;
      // Pisahkan "task yang baru dibuat" dari "task yang sedang dipantau".
      const adopt = shouldAdoptSubmittedTask({
        queueState,
        isViewingRunning: isViewingRunningTask(),
      });

      if (adopt) {
        task.id = record.task_id;
        task.text = record.task;
        task.executionMode = normalizeExecutionMode(record.execution_mode || record.executionMode || executionMode);
        if (queueState === "pending") {
          // Menunggu execution slot Global Task Queue -> "queued" (bukan running).
          task.status = "queued";
        } else if (queueState === "running") {
          task.status = "running";
          // Task ini langsung mendapat slot -> target tombol Stop.
          runningTaskId.value = record.task_id;
        } else {
          task.status = record.status || "prepared";
        }
        // Reset seluruh changes HANYA ketika tidak ada task aktif/pending lain
        // (bukan saat pindah tampilan atau saat task lain masih berjalan).
        const hasOtherActive = [...queueItems.value, ...tasks.value].some((item) => {
          const id = item.task_id || item.id || "";
          const st = String(item.queue_state || item.status || "").toLowerCase();
          return id !== record.task_id &&
            ["pending", "queued", "running", "prepared", "planning", "executing", "validating"].includes(st);
        });
        if (!hasOtherActive) resetAllChanges();
        resetAudioTracker();
        resetWorkspace();
        // Bila task LANGSUNG mendapat slot eksekusi (queue_state="running"), mulai
        // timer dari sekarang. Pengaman bila event task_started terlewat sebelum
        // SSE tersambung; bila event datang, nilai ini TIDAK ditimpa (guard == null).
        if (queueState === "running") {
          timing.markStartedIfUnset();
        }
        stream.connectStream();
      } else {
        // Task A sedang running & dipantau -> biarkan TETAP tampil. Task B baru
        // masuk Global Task Queue sebagai pending (terlihat di panel TASKS),
        // TIDAK diadopsi dan TIDAK me-rebind stream. Ingat B agar UI mengikuti
        // begitu scheduler benar-benar menjalankannya (setelah A selesai).
        deferredTaskIds.set(record.task_id, {
          task: record.task,
          executionMode: normalizeExecutionMode(record.execution_mode || record.executionMode || executionMode),
        });
        // Stream harus tetap terbuka agar event task_started B nanti terlihat.
        stream.ensureStream();
      }
      await refreshTasks();
      closeComposer();
      // Task baru -> panel TASKS (queue global) ikut refresh meski dibuat dari
      // Agent Input (satu queue yang sama).
      bumpQueueRefresh();
      // Kembalikan record: pemanggil (mis. Run Task Consultant) memakai task_id
      // task yang BARU dibuat, yang belum tentu == task yang sedang dipantau.
      return record;
    } catch (e) {
      setError(e.message || "Failed to create task.");
      return null;
    } finally {
      submitting.value = false;
    }
  }

  async function stopTask() {
    // Target Stop = task yang BENAR-BENAR RUNNING (Global Task Queue). Fallback
    // ke task aktif hanya bila id running belum diketahui (mis. antrian sedang
    // dimuat). Mekanisme penghentian tetap cancel_task existing (bukan sistem
    // cancellation baru). Send TIDAK pernah menghentikan task.
    const targetId = runningTaskId.value || task.id;
    if (!targetId) return;
    try {
      const record = await cancelTask(targetId);
      // Jangan menimpa status tampilan task lain yang sedang dibuka.
      if (targetId === task.id) task.status = record.status || "cancelled";
    } catch (e) {
      setError(e.message || "Failed to stop task.");
    } finally {
      runningTaskId.value = "";
      // Scheduler existing akan mempromosikan task pending berikutnya; refresh
      // antrian + sinkronkan target Stop ke task running yang baru (bila ada).
      bumpQueueRefresh();
      queue.refreshRunningTask();
    }
  }

  // --- Stop confirmation (confirmation layer di depan aksi Stop) --------------
  function requestStop() {
    if (!isRunning.value) return;
    if (stopInProgress.value) return;
    stopConfirmOpen.value = true;
  }

  function cancelStopConfirm() {
    if (stopInProgress.value) return;
    stopConfirmOpen.value = false;
  }

  async function confirmStop() {
    if (stopInProgress.value) return;
    stopInProgress.value = true;
    stopConfirmOpen.value = false;
    try {
      await stopTask();
    } finally {
      stopInProgress.value = false;
    }
  }

  function onStopConfirmKeydown(e) {
    if (e.key === 'Escape' && stopConfirmOpen.value) {
      cancelStopConfirm();
    }
  }

  // Buka task dari persistent log (History/Activity/Report API). Berfungsi untuk
  // task lama walau SessionStore sudah kosong / proses sudah restart.
  async function openHistoryTask(taskId) {
    setError("");
    const projectId = selectedProjectId.value || null;
    try {
      const info = await getTaskHistory(taskId, projectId);
      task.id = info.task_id || taskId;
      task.text = info.task || "";
      task.status = info.status || "incomplete";
      // Task lama: execution_mode bisa ada di info.execution_mode (dari TaskRecord
      // to_dict) atau tidak ada sama sekali -> fallback "queue".
      task.executionMode = normalizeExecutionMode(info.execution_mode || info.executionMode);
      // Activity (chronological) dari persistent log.
      const activityData = await getTaskActivity(taskId, projectId);
      historyEvents.value = activityData.events || [];
      // Reconstruct changes per task from history events.
      loadChangesFromEvents(historyEvents.value, taskId);
      // Lifecycle task lama: rekonstruksi activity phase + milestone dari event
      // yang tersimpan (phase_changed/task_started). Status terminal tetap dari
      // info.status sehingga Completed/Failed/Cancelled benar.
      const histLifecycle = lifecycleFromEvents(historyEvents.value);
      activityPhase.value = histLifecycle.currentPhase;
      lifecycleMilestones.value = histLifecycle.milestones;
      // Timing Task Card dari event lifecycle log (start eksekusi -> terminal).
      // Fallback aman bila task lama tidak punya event start/terminal.
      timing.applyHistoryTiming(info, historyEvents.value);
      // Report final (bila ada) dari persistent log.
      try {
        const report = await getTaskReport(taskId, projectId);
        currentReport.value = report.report ?? null;
      } catch {
        currentReport.value = null;
      }
      setActiveNav("agent");
    } catch (e) {
      setError(e.message || "Failed to load task history.");
    }
  }

  // Buka Report viewer untuk sebuah task (Report API -> .aether/log/).
  async function openReport(taskId) {
    setError("");
    if (!taskId) return;
    try {
      const data = await getTaskReport(taskId, selectedProjectId.value || null);
      reportTaskId.value = data.task_id || taskId;
      reportStatus.value = data.status || "";
      currentReport.value = data.report ?? null;
      reportOpen.value = true;
    } catch (e) {
      setError(e.message || "Failed to load report.");
    }
  }

  // Dipakai resetWorkspace (activity) saat pantauan pindah task.
  function clearReport() {
    currentReport.value = null;
  }

  return {
    submitting,
    currentReport,
    reportOpen,
    reportTaskId,
    reportStatus,
    stopConfirmOpen,
    stopInProgress,
    submitTask,
    stopTask,
    requestStop,
    cancelStopConfirm,
    confirmStop,
    onStopConfirmKeydown,
    openHistoryTask,
    openReport,
    clearReport,
    refreshTasks,
    refreshTaskHistory,
  };
}
