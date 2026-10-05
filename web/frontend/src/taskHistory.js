// AETHER Workbench — presentasi Task History (arsip `.aether/log/` via History
// API). Helper murni (tanpa Vue/DOM) untuk tabel HISTORY: grouping waktu,
// formatting timestamp, dan kelas status.

export const HISTORY_GROUPS = ["TODAY", "YESTERDAY", "OLDER"];

/** Format timestamp persistent log (ISO string) untuk kolom History. */
export function formatTs(raw) {
  if (!raw) return "—";
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? String(raw) : d.toLocaleString();
}

/** Kelas CSS tag status task (dipakai tabel HISTORY). */
export function statusTagClass(s) {
  const v = (s || "").toLowerCase();
  if (v === "completed") return "status-on";
  if (v === "failed") return "status-err";
  if (v === "running" || v === "prepared" || v === "validating" || v === "planning")
    return "status-run";
  return "status-off";
}

/** Kelompokkan satu item history (TODAY / YESTERDAY / OLDER). */
export function taskTimeGroup(raw) {
  if (!raw || !raw.last_timestamp) return "OLDER";
  const d = new Date(raw.last_timestamp);
  if (Number.isNaN(d.getTime())) return "OLDER";
  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterdayStart = new Date(todayStart);
  yesterdayStart.setDate(yesterdayStart.getDate() - 1);
  if (d >= todayStart) return "TODAY";
  if (d >= yesterdayStart && d < todayStart) return "YESTERDAY";
  return "OLDER";
}

/** Grouping task history by time (TODAY, YESTERDAY, OLDER) berdasarkan last_timestamp. */
export function groupTaskHistory(list) {
  const groups = {};
  for (const t of list || []) {
    const g = taskTimeGroup(t);
    if (!groups[g]) groups[g] = [];
    groups[g].push(t);
  }
  const result = [];
  for (const key of HISTORY_GROUPS) {
    if (groups[key] && groups[key].length) {
      result.push({ label: key, items: groups[key] });
    }
  }
  return result;
}
