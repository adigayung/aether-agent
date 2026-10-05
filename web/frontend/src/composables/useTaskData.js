import { computed, ref } from "vue";
import { listTasks, listTaskHistory } from "../api.js";
import { groupTaskHistory } from "../taskHistory.js";

// Data daftar task untuk sebuah project + helper presentasi tabel HISTORY:
//   - `tasks`       : GET /api/tasks (TaskRecord aktif)
//   - `taskHistory` : History API (.aether/log/, newest first)
// Modul ini hanya memuat data + feedback copy prompt; QueuePanel/History table
// mengonsumsi hasilnya.
export function useTaskData({ selectedProjectId }) {
  const tasks = ref([]);
  // Task History (persistent .aether/log/ via History API) — newest first.
  const taskHistory = ref([]);

  let refreshTasksGen = 0;
  async function refreshTasks() {
    const cur = ++refreshTasksGen;
    try {
      const data = await listTasks(selectedProjectId.value || null);
      if (cur !== refreshTasksGen) return;
      tasks.value = data.tasks || [];
    } catch {
      // Endpoint list mungkin belum tersedia; UI tetap aman.
    }
  }

  // Task History dari persistent log (.aether/log/ via History API), newest first.
  async function refreshTaskHistory() {
    try {
      const data = await listTaskHistory(selectedProjectId.value || null);
      taskHistory.value = data.tasks || [];
    } catch {
      // Endpoint history mungkin belum tersedia; UI tetap aman.
    }
  }

  // Grouping task history by time (TODAY, YESTERDAY, OLDER).
  const groupedTaskHistory = computed(() => groupTaskHistory(taskHistory.value));

  // Feedback copy prompt untuk History table (highlight tombol sesaat).
  const historyCopyFeedback = ref("");

  async function copyHistoryPrompt(t) {
    const text = t.task || "";
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      return;
    }
    historyCopyFeedback.value = t.task_id;
    setTimeout(() => {
      if (historyCopyFeedback.value === t.task_id) historyCopyFeedback.value = "";
    }, 1200);
  }

  return {
    tasks,
    taskHistory,
    refreshTasks,
    refreshTaskHistory,
    groupedTaskHistory,
    historyCopyFeedback,
    copyHistoryPrompt,
  };
}
