// AETHER Workbench — execution mode task (queue | parallel).
//
// Helper murni (tanpa Vue/DOM) supaya satu definisi dipakai lintas modul:
// normalisasi (backward compat task lama tanpa execution_mode -> "queue"),
// label tampilan, dan daftar status task yang dianggap "aktif/pending".

/** Normalisasi nilai execution_mode mentah -> "queue" | "parallel". */
export function normalizeExecutionMode(raw) {
  const v = String(raw || "").trim().toLowerCase();
  return v === "parallel" ? "parallel" : "queue";
}

/** Label tampilan untuk execution mode. */
export function executionLabel(mode) {
  return normalizeExecutionMode(mode) === "parallel" ? "Parallel" : "Queue";
}

// Status task non-terminal (belum selesai/gagal/cancel). Dipakai untuk agregasi
// changes per task aktif dan untuk menentukan reset changes saat submit task.
export const ACTIVE_TASK_STATES = [
  "pending",
  "queued",
  "running",
  "prepared",
  "planning",
  "executing",
  "validating",
];

/** Apakah sebuah status task termasuk task aktif/pending (non-terminal)? */
export function isActiveTaskState(state) {
  return ACTIVE_TASK_STATES.includes(String(state || "").toLowerCase());
}
