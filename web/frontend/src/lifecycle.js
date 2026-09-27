// AETHER Workbench — logika lifecycle (Frontend-only, TANPA Vue/DOM).
//
// Lifecycle UI 6 step (Planning, Inspecting, Editing, Running, Validating,
// Completed) diturunkan dari AKTIVITAS AGENT NYATA yang dikirim backend lewat
// event `phase_changed` (Task 1), BUKAN dari nilai internal runtime lama
// (`replan`/`provider_fallback`) dan BUKAN dari timer/round count/jumlah tool.
//
// Dua konsep dipisah dengan sengaja:
//   - current activity phase : step yang SEDANG aktif (`●`).
//   - milestones             : step yang SUDAH pernah dicapai (tetap `✓`).
//
// Karena Agent dapat KEMBALI ke aktivitas sebelumnya (Editing -> Inspecting),
// milestone yang sudah dicapai TIDAK dihitung ulang dari index current phase:
// current phase boleh bergerak mundur, milestone tetap `done`.
//
// Label step (teks "Planning".."Completed") TETAP didefinisikan di App.vue
// (satu sumber untuk markup stepper); modul murni ini hanya menghitung STATE
// tiap step (index-based) sehingga bisa diuji tanpa menambah framework test
// frontend (lihat lifecycle.test.mjs).

//: Jumlah step lifecycle (Planning..Completed). Harus selaras dengan
//: LIFECYCLE_STEPS di App.vue (dipakai sebagai default bila tak diberikan).
export const LIFECYCLE_STEP_COUNT = 6;

//: Activity phase (Task 1) -> index step lifecycle. Hanya nilai ini yang
//: menggerakkan lifecycle; nilai runtime internal lain diabaikan.
const ACTIVITY_PHASE_INDEX = {
  planning: 0,
  inspecting: 1,
  editing: 2,
  running: 3,
  validating: 4,
};

//: Index step "Validating" (dipakai saat mekanisme validation existing aktif).
export const VALIDATING_STEP = ACTIVITY_PHASE_INDEX.validating;

/**
 * Index step lifecycle untuk sebuah activity phase.
 *
 * @returns {number} 0..4 untuk phase yang dikenal, -1 untuk phase tak dikenal
 *   (mis. nilai internal `replan`/`provider_fallback`, atau kosong).
 */
export function activityPhaseIndex(phase) {
  if (phase == null) return -1;
  const key = String(phase).trim().toLowerCase();
  return Object.prototype.hasOwnProperty.call(ACTIVITY_PHASE_INDEX, key)
    ? ACTIVITY_PHASE_INDEX[key]
    : -1;
}

/** True bila nilai ini activity phase UI yang dikenal (bukan nilai internal). */
export function isActivityPhase(phase) {
  return activityPhaseIndex(phase) >= 0;
}

/**
 * Tambahkan milestone step yang SUDAH dicapai (immutable).
 *
 * @param {number[]} milestones daftar index yang sudah dicapai.
 * @param {number} index index step (0..count-1).
 * @returns {number[]} array baru (unik, terurut); array lama bila tidak berubah.
 */
export function addMilestone(milestones, index, count = LIFECYCLE_STEP_COUNT) {
  const list = Array.isArray(milestones) ? milestones : [];
  if (!Number.isInteger(index) || index < 0 || index >= count) return list;
  if (list.includes(index)) return list;
  return [...list, index].sort((a, b) => a - b);
}

function normalizeMilestones(milestones, count) {
  const set = new Set();
  for (const i of milestones || []) {
    if (Number.isInteger(i) && i >= 0 && i < count) set.add(i);
  }
  return set;
}

function maxMilestone(milestones) {
  let max = -1;
  for (const i of milestones || []) {
    if (Number.isInteger(i) && i > max) max = i;
  }
  return max;
}

/**
 * Hitung STATE tiap step lifecycle dari state frontend yang sudah ada.
 *
 * state = `task.status` (status task backend) + `currentPhase` (activity phase
 * terakhir dari `phase_changed`) + `milestones` (step yang pernah dicapai).
 * TIDAK ada state machine kedua: hanya proyeksi dari event yang sudah diterima.
 *
 * Aturan:
 *   - `completed`  -> semua milestone utama done, `Completed` aktif (TERMINAL).
 *   - `failed`/`cancelled` -> TETAP di aktivitas terakhir yang benar-benar
 *     terjadi (BUKAN Completed, BUKAN dipaksa ke Running).
 *   - selain itu   -> current step = activity phase terakhir; sebelum ada event
 *     phase (mis. task baru mulai) -> `Planning`.
 *   - Step yang sudah dicapai & bukan current -> `done` (tetap `✓`, walau
 *     current phase kini berada di step sebelumnya).
 *
 * @returns {string[]} array state (""|"done"|"active") sepanjang `count`.
 */
export function buildLifecycleStates({
  hasTask,
  status,
  currentPhase,
  milestones,
  count = LIFECYCLE_STEP_COUNT,
}) {
  const states = new Array(Math.max(0, count | 0)).fill("");
  if (!hasTask || states.length === 0) return states;

  const s = String(status || "").toLowerCase();
  const done = normalizeMilestones(milestones, states.length);
  const phaseIdx = activityPhaseIndex(currentPhase);
  const lastIndex = states.length - 1;

  let activeIdx;
  if (s === "completed") {
    // Terminal sukses: seluruh step sampai Validating tercapai, Completed titik akhir.
    for (let i = 0; i < lastIndex; i += 1) done.add(i);
    activeIdx = lastIndex;
  } else if (s === "failed" || s === "cancelled") {
    // Gagal/dibatalkan BUKAN Completed: pertahankan aktivitas terakhir nyata.
    activeIdx = phaseIdx >= 0 ? phaseIdx : maxMilestone(milestones);
  } else {
    // Berjalan: ikuti activity phase terakhir; sebelum ada -> Planning.
    activeIdx = phaseIdx >= 0 ? phaseIdx : maxMilestone(milestones);
  }
  if (activeIdx < 0) activeIdx = 0;
  if (activeIdx > lastIndex) activeIdx = lastIndex;

  for (let i = 0; i < states.length; i += 1) {
    if (i === activeIdx) states[i] = "active";
    else if (done.has(i)) states[i] = "done";
    else states[i] = "";
  }
  return states;
}

/**
 * Rekonstruksi lifecycle (milestones + current activity phase) dari daftar event
 * (history/persistent log). Dipakai saat membuka task lama dari Activity API,
 * sehingga lifecycle task lama menampilkan aktivitas nyata yang pernah terjadi.
 */
export function lifecycleFromEvents(events) {
  let milestones = [];
  let currentPhase = "";
  for (const raw of events || []) {
    const type = (raw && (raw.event_type || raw.event)) || "";
    const payload = (raw && (raw.payload || raw.data)) || {};
    if (type === "task_started") {
      milestones = addMilestone(milestones, 0);
      if (!currentPhase) currentPhase = "planning";
    } else if (type === "phase_changed") {
      const idx = activityPhaseIndex(payload.phase);
      if (idx >= 0) {
        milestones = addMilestone(milestones, idx);
        currentPhase = String(payload.phase).trim().toLowerCase();
      }
    } else if (type === "validation_started") {
      milestones = addMilestone(milestones, VALIDATING_STEP);
      if (!currentPhase) currentPhase = "validating";
    }
  }
  return { milestones, currentPhase };
}
