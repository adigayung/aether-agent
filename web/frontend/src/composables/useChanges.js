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

// Parser event perubahan CANONICAL (satu format, dipakai producer & consumer).
// Backend memakai SATU `payload` baik untuk SSE live maupun Task Log; Activity
// API mengembalikan record log dengan kunci `event`/`data`. Kedua bentuk harus
// dinormalisasi di SATU tempat agar tidak ada bentuk yang diterima diam-diam,
// dan agar path yang tidak valid TIDAK pernah masuk ke bucket Changes.
//
// Mengembalikan `{ task_id, change }` atau `null` bila event bukan
// `change_detected` yang valid (path wajib ada & non-kosong).
export function parseChangeEvent(raw) {
  if (!raw || typeof raw !== "object") return null;
  const type = raw.event_type || raw.event;
  if (type !== "change_detected") return null;
  const p = raw.payload || raw.data;
  if (!p || typeof p !== "object") return null;
  const path = typeof p.path === "string" ? p.path.trim() : "";
  // Path WAJIB valid: entry dengan path kosong/undefined membuat baris tak
  // terlihat dan tidak pernah dihitung -> jangan pernah di-upsert.
  if (!path) return null;
  const oldPath = typeof p.old_path === "string" ? p.old_path.trim() : "";
  return {
    task_id: raw.task_id || "",
    change: {
      kind: p.kind || "change",
      path,
      old_path: oldPath || undefined,
      detail: p.detail,
      additions: p.additions,
      deletions: p.deletions,
      diff: p.diff,
    },
  };
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
    if (!entry || !entry.path) return; // path invalid -> tidak ada baris "hantu"
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
  // Keduanya dinormalisasi oleh parseChangeEvent() sehingga satu format
  // canonical dipakai konsisten.
  function loadChangesFromEvents(events, taskId) {
    changesByTask.value[taskId] = [];
    for (const raw of events || []) {
      const parsed = parseChangeEvent(raw);
      if (!parsed) continue;
      if (isAetherMetadata(parsed.change.path)) continue;
      upsertChange(parsed.change, taskId);
    }
  }

  // Total reset changes: HANYA dipanggil ketika tidak ada task aktif/pending
  // (semua task terminal) dan user memulai task baru / menutup project.
  function resetAllChanges() {
    changesByTask.value = {};
  }

  // Kontrak DI (dipakai useAgentActivity): SEMUA helper yang di-destructure
  // konsumen HARUS ada di objek ini. Sebelumnya `isAetherMetadata` hanya
  // di-export sebagai named export, sehingga `const { isAetherMetadata } =
  // changes` bernilai undefined dan `case "change_detected"` melempar
  // TypeError -> event perubahan dibuang, panel Changes tetap 0 files.
  return {
    changesByTask,
    changes,
    aggregateChanges,
    upsertChange,
    loadChangesFromEvents,
    resetAllChanges,
    isAetherMetadata,
    parseChangeEvent,
  };
}
