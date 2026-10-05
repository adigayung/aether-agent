import { ref } from "vue";
import { openEventStream } from "../api.js";

// SSE (#51) — dua stream yang sah:
//   (a) stream GLOBAL queueSource — TIDAK memakai taskId (membawa
//       task_queued/task_started untuk SEMUA task),
//   (b) stream detail/task-scoped — MEMAKAI taskId (melanjutkan detail Activity
//       task yang sedang dipantau).
// Stream global TIDAK boleh difilter per-task: kalau difilter, event task
// antrian berikutnya yang mulai running tak akan pernah terlihat.
//
// `task`        = task yang sedang dipantau (untuk task-scoped stream).
// `handleEvent` = handler event #51 (dari useAgentActivity).
export function useEventStream({ task, handleEvent }) {
  const connected = ref(false);
  let source = null;
  let queueSource = null;

  function connectStream() {
    // Close existing detail stream
    if (source) source.close();

    // Ensure global queue stream is open (for task_queued/task_started events)
    if (!queueSource) {
      queueSource = openEventStream({ onEvent: handleEvent });
      queueSource.onopen = () => {
        connected.value = true;
      };
      queueSource.onerror = () => {
        connected.value = false;
      };
    }

    // Open task-scoped stream when task.id is set
    if (task.id) {
      source = openEventStream({ taskId: task.id, onEvent: handleEvent });
      source.onopen = () => {
        connected.value = true;
      };
      source.onerror = () => {
        connected.value = false;
      };
    } else {
      source = null;
    }
  }

  // Pastikan stream SSE terbuka (tanpa menutup/membuka ulang bila sudah ada).
  function ensureStream() {
    if (!source) connectStream();
  }

  function closeStream() {
    if (source) source.close();
    if (queueSource) queueSource.close();
    source = null;
    queueSource = null;
  }

  return { connected, connectStream, ensureStream, closeStream };
}
