import { computed, ref } from "vue";

// Filter tampilan Changes: file di dalam `.aether/**` adalah metadata internal
// AETHER (Bible, log, dsb.), BUKAN perubahan project. Ini murni layer
// presentasi UI; ChangeTracker/ProjectBrain/logging backend tidak diubah.
export function isAetherMetadata(path) {
  if (!path) return false;
  const normalized = String(path).replace(/\\/g, "/").replace(/^\.\//, "");
  return (
    normalized === ".aether" ||
    normalized.startsWith(".aether/") ||
    normalized.includes("/.aether/")
  );
}

// Changes PER-TASK (key = task_id). Task baru tidak menimpa changes milik task
// lain. Modul ini HANYA mengelola bucket per-task + upsert + agregasi; sumber
// datanya tetap event SSE (#51) live atau Activity API (persistent log).
//
// `task`            = task yang sedang dibuka (reactive).
// `isViewingRunningTask` = apakah task yang dipantau benar-benar berjalan.
// `getActiveTaskIds`     = id task aktif/pending (dari queue + daftar task).
export function useChanges({ task, isViewingRunningTask, getActiveTaskIds }) {
  // Changes per-task. Key = task_id. Nilai = array of change objects
  // (path, old_path, kind, diff, dll).
  const changesByTask = ref({});

  // Changes yang sedang ditampilkan mengikuti task yang sedang dibuka.
  const changes = computed(() => changesByTask.value[task.id || ""] || []);

  // Header: gabungan file unik dari seluruh task aktif/pending.
  const aggregateChanges = computed(() => {
    const ids = new Set(getActiveTaskIds());
    if (task.id && isViewingRunningTask()) ids.add(task.id);
    const paths = new Set();
    for (const id of ids) for (const change of changesByTask.value[id] || []) {
      if (change.path) paths.add(change.path);
    }
    return paths;
  });

  // Upsert perubahan per task (berdasarkan task_id pada event live).
  // Satu file = satu baris di Changes panel tiap task; file yang sama diedit
  // berkali-kali memperbarui barisnya (bukan menumpuk duplikat). Move/rename
  // memindahkan baris lama ke path baru. Bucket task_id dibuat otomatis.
  function upsertChange(entry, taskId) {
    const bucketId = taskId || task.id || "";
    const list = changesByTask.value[bucketId] || (changesByTask.value[bucketId] = []);
    const idx = list.findIndex((c) => c.path === entry.path);
    if (idx >= 0) {
      list[idx] = { ...list[idx], ...entry };
      return;
    }
    const kind = String(entry.kind || "").toLowerCase();
    if ((kind.includes("move") || kind.includes("renam")) && entry.old_path) {
      const oi = list.findIndex((c) => c.path === entry.old_path);
      if (oi >= 0) {
        list[oi] = { ...list[oi], ...entry };
        return;
      }
    }
    list.push(entry);
  }

  // Rekonstruksi changes per task dari event history (persistent log).
  // Sumber persisten = Activity API (.aether/log/<task_id>.log) yang menulis
  // record dengan kunci `event`/`data`; SSE live memakai `event_type`/`payload`.
  // Kedua bentuk harus dibaca agar daftar Changes tetap utuh saat task selesai
  // atau dibuka kembali (lihat juga lifecycle.js/useTaskTelemetry.js).
  function loadChangesFromEvents(events, taskId) {
    changesByTask.value[taskId] = [];
    for (const raw of events || []) {
      const type = raw && (raw.event_type || raw.event);
      if (type === "change_detected") {
        const p = (raw && (raw.payload || raw.data)) || {};
        if (!isAetherMetadata(p.path)) {
          upsertChange(
            {
              kind: p.kind || "change",
              path: p.path,
              old_path: p.old_path,
              detail: p.detail,
              additions: p.additions,
              deletions: p.deletions,
              diff: p.diff,
            },
            taskId
          );
        }
      }
    }
  }

  // Total reset changes: HANYA dipanggil ketika tidak ada task aktif/pending
  // (semua task terminal) dan user memulai task baru / menutup project.
  function resetAllChanges() {
    changesByTask.value = {};
  }

  return {
    changesByTask,
    changes,
    aggregateChanges,
    upsertChange,
    loadChangesFromEvents,
    resetAllChanges,
  };
}
