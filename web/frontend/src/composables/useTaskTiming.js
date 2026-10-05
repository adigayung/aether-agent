import { computed, ref, watch } from "vue";
import { createDurationTicker, eventTimeMs, formatDuration } from "../timeUtils.js";

// Timing eksekusi Task Card (Provider/Model/Duration bagian durasi).
//
// Sumber waktu = timestamp event lifecycle AETHER yang SUDAH ADA:
//   - SSE live (#51): `timestamp` epoch detik
//   - history log (.aether/log via Activity API): `timestamp` ISO string
// TIDAK ada polling/timer backend baru. Ticker frontend hanya me-refresh
// TAMPILAN durasi live (bukan sumber kebenaran durasi).
export function useTaskTiming({ getStatus }) {
  const taskStartedAt = ref(null); // ms epoch saat task BENAR-BENAR mulai dieksekusi
  const taskEndedAt = ref(null); // ms epoch saat task mencapai status terminal
  const nowTick = ref(Date.now()); // detak tampilan durasi live

  // Ticker TAMPILAN durasi live: interval hidup HANYA selama task berjalan dan
  // dibersihkan saat terminal/unmount (implementasi di ./timeUtils.js).
  const durationTicker = createDurationTicker(() => {
    nowTick.value = Date.now();
  });

  // Durasi hidup (task masih dieksekusi) -> timer tampilan berjalan. Begitu
  // `taskEndedAt` terisi (status terminal diterima UI) timer berhenti.
  const taskTimerLive = computed(
    () => taskStartedAt.value != null && taskEndedAt.value == null
  );

  function startDurationTimer() {
    durationTicker.start();
  }

  function stopDurationTimer() {
    durationTicker.stop();
  }

  // Interval hidup hanya selama task berjalan; dibersihkan saat terminal/unmount
  // (tidak ada timer nyangkut / memory leak).
  watch(taskTimerLive, (on) => (on ? startDurationTimer() : stopDurationTimer()));

  const taskDurationMs = computed(() => {
    if (taskStartedAt.value == null) return null;
    const end = taskEndedAt.value != null ? taskEndedAt.value : nowTick.value;
    return Math.max(0, end - taskStartedAt.value);
  });
  const taskDurationLabel = computed(() => formatDuration(taskDurationMs.value));

  // Reset timing (workspace/task baru) + hentikan timer tampilan.
  function resetTiming() {
    taskStartedAt.value = null;
    taskEndedAt.value = null;
    stopDurationTimer();
  }

  // Set waktu mulai SEKALI per task (reactive update berikutnya tidak me-reset).
  function markStartedIfUnset(ms = Date.now()) {
    if (taskStartedAt.value == null) taskStartedAt.value = ms;
  }

  // Task lama (persistent log): hitung timing dari event lifecycle yang ada.
  // task_started = mulai eksekusi; task_completed/failed/cancelled = selesai.
  // Fallback AMAN: first/last timestamp log bila event start/terminal tidak ada.
  function applyHistoryTiming(info, evts) {
    taskStartedAt.value = null;
    taskEndedAt.value = null;
    for (const raw of evts || []) {
      const type = (raw && (raw.event_type || raw.event)) || "";
      if (type === "task_started" && taskStartedAt.value == null) {
        taskStartedAt.value = eventTimeMs(raw);
      } else if (
        (type === "task_completed" ||
          type === "task_failed" ||
          type === "task_cancelled") &&
        taskEndedAt.value == null
      ) {
        taskEndedAt.value = eventTimeMs(raw);
      }
    }
    const status = String((info && info.status) || getStatus() || "").toLowerCase();
    const terminal =
      status === "completed" || status === "failed" || status === "cancelled";
    if (taskStartedAt.value == null && info && info.first_timestamp) {
      taskStartedAt.value = eventTimeMs({ timestamp: info.first_timestamp });
    }
    if (terminal && taskEndedAt.value == null && info && info.last_timestamp) {
      taskEndedAt.value = eventTimeMs({ timestamp: info.last_timestamp });
    }
    if (!terminal) taskEndedAt.value = null;
    stopDurationTimer();
  }

  return {
    taskStartedAt,
    taskEndedAt,
    nowTick,
    taskTimerLive,
    taskDurationMs,
    taskDurationLabel,
    startDurationTimer,
    stopDurationTimer,
    resetTiming,
    markStartedIfUnset,
    applyHistoryTiming,
  };
}
