// Regression test (Node built-in `assert`, TANPA framework/dependency baru)
// untuk formatter clipboard "Copy Agent Activity" (web/frontend/src/activityCopy.js).
//
// Mengunci perilaku:
//   - Format: [AGENT] -> [ACTIVITY] -> [REPEATED ACTIVITY] (repeated paling bawah)
//   - Urutan kronologis sesuai urutan events UI (tidak diacak)
//   - Batas HARD 45.000 karakter TIDAK PERNAH dilanggar
//   - Activity besar memprioritaskan aktivitas TERBARU (paling bawah);
//     aktivitas PALING LAMA dibuang lebih dulu + penanda truncation
//   - Metadata agent TIDAK dibuang saat truncation
//
// Jalankan: node web/frontend/src/activityCopy.test.mjs

import assert from "node:assert/strict";
import {
  AGENT_ACTIVITY_COPY_MAX_CHARS,
  buildAgentActivityCopy,
  countRepeatedActivity,
  formatActivityLines,
} from "./activityCopy.js";

// Sentinel unik di AWAL dan AKHIR laporan akhir -> membuktikan report disalin utuh.
const START_SENTINEL = "SENTINEL_LAPORAN_AWAL_9f3c";
const END_SENTINEL = "SENTINEL_LAPORAN_AKHIR_7b1e";
function sentinelReport(extra = "") {
  return [
    START_SENTINEL,
    "# Laporan Akhir",
    "",
    extra,
    "baris terakhir laporan.",
    END_SENTINEL,
  ].join("\n");
}
function completedEvent(result) {
  return {
    event_type: "task_completed",
    timestamp: 1700000100,
    payload: result === undefined ? {} : { result },
  };
}

const MAX = AGENT_ACTIVITY_COPY_MAX_CHARS;
assert.equal(MAX, 45000, "konstanta batas = 45000");
// Batas hanya berlaku untuk bagian yang boleh dipangkas (ACTIVITY). Isi laporan
// akhir selalu dipertahankan utuh, sehingga panjang hasil tidak dipatok <= MAX.

// --- Format dasar -----------------------------------------------------------
const events = [
  { event_type: "task_started", timestamp: 1700000001, payload: {} },
  {
    event_type: "tool_called",
    timestamp: 1700000003,
    payload: { tool: "read_file", target: "src/agent_ai/core/orchestrator.py" },
  },
  {
    event_type: "observation_received",
    timestamp: 1700000007,
    payload: {
      tool: "read_file",
      target: "src/agent_ai/core/orchestrator.py",
      content: { path: "src/agent_ai/core/orchestrator.py", total_lines: 12 },
    },
  },
  {
    event_type: "agent_commentary",
    timestamp: 1700000012,
    payload: { text: "Analyzing context compilation flow." },
  },
  { event_type: "task_completed", timestamp: 1700000038, payload: { result: "ok" } },
];

const meta = {
  taskId: "78d18acc14194456b4791b86c651fd48",
  status: "completed",
  provider: "openrouter",
  model: "deepseek/deepseek-v4-flash",
  duration: "00:00:37",
  llmRounds: 2,
  toolCalls: 1,
};

const text = buildAgentActivityCopy({ events, meta });

// Section wajib ada & urutannya benar.
assert.ok(text.startsWith("AETHER AGENT ACTIVITY"), "pembuka AETHER AGENT ACTIVITY");
const idxAgent = text.indexOf("[AGENT]");
const idxActivity = text.indexOf("[ACTIVITY]");
const idxRepeated = text.indexOf("[REPEATED ACTIVITY]");
const idxFinalReport = text.indexOf("[FINAL REPORT]");
assert.ok(idxAgent !== -1, "ada section [AGENT]");
assert.ok(idxActivity !== -1, "ada section [ACTIVITY]");
assert.ok(idxRepeated !== -1, "ada section [REPEATED ACTIVITY]");
assert.ok(idxFinalReport !== -1, "ada section [FINAL REPORT]");
assert.ok(
  idxAgent < idxActivity && idxActivity < idxFinalReport && idxFinalReport < idxRepeated,
  "urutan section: [AGENT] < [ACTIVITY] < [FINAL REPORT] < [REPEATED ACTIVITY]"
);

// Metadata agent tercantum dengan nilai benar.
assert.ok(text.includes("Task ID: 78d18acc14194456b4791b86c651fd48"));
assert.ok(text.includes("Status: completed"));
assert.ok(text.includes("Provider: openrouter"));
assert.ok(text.includes("Model: deepseek/deepseek-v4-flash"));
assert.ok(text.includes("Duration: 00:00:37"));
assert.ok(text.includes("LLM Rounds: 2"));
assert.ok(text.includes("Tool Calls: 1"));

// Batas ukuran: bagian yang dipangkas (ACTIVITY) tidak boleh melampaui batas
// lebih dari satu baris activity (truncation per-baris, bukan per-karakter).
assert.ok(text.length <= MAX, `hasil copy <= ${MAX} (got ${text.length})`);
// Event task_completed di fixture hanya membawa { result: "ok" } -> laporan
// pendek ikut disalin utuh, di antara [ACTIVITY] dan [REPEATED ACTIVITY].
const finalBlock = text.slice(idxFinalReport, idxRepeated);
assert.ok(finalBlock.includes("ok"), "laporan akhir pendek ikut disalin");

// Repeated activity selalu di posisi paling bawah -> tidak ada teks setelahnya.
// Tanpa file yang dibaca >1x, section menampilkan "No repeated activity."
assert.ok(
  text.slice(idxRepeated).includes("Total repeated activity:") ||
    text.slice(idxRepeated).includes("No repeated activity."),
  "REPEATED ACTIVITY ada bersama ringkasannya"
);
assert.ok(
  text.indexOf("\n\n[ACTIVITY]") > idxAgent,
  "section ACTIVITY dipisahkan blok dari AGENT"
);

// --- Urutan kronologis -------------------------------------------------------
// Baris activity harus mengikuti urutan events (tidak diacak).
const actBody = text.slice(idxActivity, idxRepeated);
const order = [
  "Agent started task.",
  "Reading src/agent_ai/core/orchestrator.py",
  "Analyzing context compilation flow.",
  "Task completed.",
];
let prev = -1;
for (const frag of order) {
  const at = actBody.indexOf(frag);
  assert.ok(at !== -1, `baris "${frag}" ada di ACTIVITY`);
  assert.ok(at > prev, `baris "${frag}" muncul setelah baris sebelumnya`);
  prev = at;
}

// --- Metadata tidak ikut dibuang saat truncation -----------------------------
// Baris activity kecil (pendek) x10000 -> jauh melebihi batas.
const bigEvents = [];
for (let i = 0; i < 10000; i++) {
  bigEvents.push({
    event_type: "tool_called",
    timestamp: 1700000100 + i,
    payload: { tool: "read_file", target: `src/agent_ai/core/history_${i}.py` },
  });
  bigEvents.push({
    event_type: "observation_received",
    timestamp: 1700000101 + i,
    payload: {
      tool: "read_file",
      target: `src/agent_ai/core/history_${i}.py`,
      content: { path: `src/agent_ai/core/history_${i}.py`, total_lines: 10 },
    },
  });
}

const bigMeta = { ...meta, taskId: "big-task", llmRounds: 10000, toolCalls: 10000 };
const bigText = buildAgentActivityCopy({ events: bigEvents, meta: bigMeta });

// Tanpa laporan akhir, seluruh bagian = [AGENT]+[ACTIVITY]+[REPEATED] tetap <= batas.
assert.ok(bigText.length <= MAX, `aktivitas besar tetap <= ${MAX} (got ${bigText.length})`);
assert.ok(
  !bigText.includes("[FINAL REPORT]"),
  "task tanpa laporan akhir TIDAK menampilkan section [FINAL REPORT] (tanpa laporan palsu)"
);
assert.ok(bigText.includes("Task ID: big-task"), "metadata agent tetap ada saat truncation");
assert.ok(
  bigText.includes("[... earlier activity truncated ...]"),
  "aktivitas besar menampilkan penanda truncation"
);
assert.ok(
  bigText.indexOf("[REPEATED ACTIVITY]") > bigText.indexOf("[ACTIVITY]"),
  "REPEATED ACTIVITY tetap di bawah ACTIVITY saat truncation (paling bawah)"
);
assert.ok(
  bigText.includes("Total repeated activity:") ||
    bigText.includes("No repeated activity."),
  "repeated summary tetap ada saat truncation"
);

// Aktivitas TERBARU (akhir daftar) harus tetap ada; yang lama hilang.
assert.ok(
  bigText.includes("history_9999.py"),
  "aktivitas TERBARU (history_9999) tetap ikut dicopy"
);
assert.ok(
  !bigText.includes("history_0000.py"),
  "aktivitas PALING LAMA (history_0000) dibuang lebih dulu"
);

// Pastikan tidak ada baris activity terpotong di tengah-tengah (truncation
// terjadi per-baris, bukan memotong string di tengah kecuali jaminan akhir).
const lines = bigText.split("\n");
for (const line of lines) {
  assert.ok(
    line.length <= 500 || line.startsWith("        "),
    `tidak ada baris activity yang terpotong aneh (len=${line.length})`
  );
}

// --- Activity sedikit --------------------------------------------------------
const smallText = buildAgentActivityCopy({ events: events.slice(0, 1), meta: { taskId: "x", status: "running" } });
assert.ok(smallText.includes("[ACTIVITY]"), "copy tetap bekerja saat activity sedikit");
assert.ok(smallText.length <= MAX, "activity sedikit juga <= batas");
assert.ok(
  !smallText.includes("truncated"),
  "activity sedikit TIDAK menampilkan penanda truncation"
);

// --- Tanpa events sama sekali ------------------------------------------------
const emptyText = buildAgentActivityCopy({ events: [], meta: { taskId: "", status: "" } });
assert.ok(emptyText.includes("[AGENT]"), "bagian AGENT tetap ada walau tanpa event");
assert.ok(emptyText.includes("[ACTIVITY]"), "bagian ACTIVITY tetap ada walau tanpa event");
assert.ok(emptyText.includes("[REPEATED ACTIVITY]"), "bagian REPEATED tetap ada walau tanpa event");
assert.ok(emptyText.length <= MAX, "empty juga <= batas");

// --- countRepeatedActivity ---------------------------------------------------
assert.deepEqual(countRepeatedActivity([]), { items: [], total: 0 });
const repEvents = [
  { event_type: "observation_received", payload: { tool: "read_file", content: { path: "a.py" } } },
  { event_type: "observation_received", payload: { tool: "read_file", content: { path: "a.py" } } },
  { event_type: "observation_received", payload: { tool: "read_file", content: { path: "b.py" } } },
  { event_type: "observation_received", payload: { tool: "read_file", content: { path: "c.py" } } },
  { event_type: "observation_received", payload: { tool: "search_code", content: { count: 3 } } },
];
const rep = countRepeatedActivity(repEvents);
assert.equal(rep.total, 2, "total repeated activity = jumlah pembacaan file yang diulang (>1x)");
assert.equal(rep.items.length, 1, "hanya a.py yang >1x");
assert.equal(rep.items[0].path, "a.py");
assert.equal(rep.items[0].count, 2);

// --- formatActivityLines: baris non-UI tidak dirender ------------------------
const linesOnly = formatActivityLines([{ event_type: "change_detected", payload: {} }]);
assert.deepEqual(linesOnly, [], "event non-timeline tidak masuk baris ACTIVITY");

// --- Policy telemetry: requested vs effective mode ---------------------------
const policyApplied = [
  {
    event_type: "policy_applied",
    timestamp: 1700000001,
    payload: {
      requested_mode: "fast",
      effective_mode: "fast",
      escalated: false,
      activity: "[POLICY]\nRequested Mode: Fast\nEffective Mode: Fast",
    },
  },
];
const policyLines = formatActivityLines(policyApplied);
assert.equal(policyLines.length, 1, "policy_applied menghasilkan satu baris");
assert.ok(policyLines[0].includes("[POLICY]"), "baris policy mengandung [POLICY]");
assert.ok(policyLines[0].includes("Requested Mode: Fast"), "menampilkan requested mode");
assert.ok(policyLines[0].includes("Effective Mode: Fast"), "menampilkan effective mode");

const policyEscalated = [
  {
    event_type: "policy_escalated",
    timestamp: 1700000005,
    payload: {
      from_mode: "fast",
      to_mode: "deep",
      reason: "Architecture impact detected",
      activity: "[POLICY]\nEscalated:\nFast → Deep\n\nReason:\nArchitecture impact detected",
    },
  },
];
const escalationLines = formatActivityLines(policyEscalated);
assert.equal(escalationLines.length, 1, "policy_escalated menghasilkan satu baris");
assert.ok(escalationLines[0].includes("[POLICY]"), "baris escalation mengandung [POLICY]");
assert.ok(escalationLines[0].includes("Escalated: Fast → Deep"), "menampilkan escalation");
assert.ok(escalationLines[0].includes("Reason: Architecture impact detected"), "menampilkan reason");

// ============================================================================
// [FINAL REPORT] — laporan akhir harus ikut disalin (perbaikan audit)
// ============================================================================

// --- 1. task_completed.payload.result masuk SEPENUHNYA (sentinel awal & akhir) ---
const reportText = sentinelReport();
const withReport = buildAgentActivityCopy({
  events: [
    { event_type: "task_started", timestamp: 1700000001, payload: {} },
    completedEvent(reportText),
  ],
  meta,
});
assert.ok(withReport.includes(START_SENTINEL), "sentinel AWAL laporan ada di hasil copy");
assert.ok(withReport.includes(END_SENTINEL), "sentinel AKHIR laporan ada di hasil copy");
assert.ok(withReport.includes(reportText), "laporan akhir disalin UTUH (identik dengan payload.result)");
const idxReport = withReport.indexOf("[FINAL REPORT]");
assert.ok(idxReport !== -1, "section [FINAL REPORT] ada");
assert.ok(idxReport > withReport.indexOf("[ACTIVITY]"), "[FINAL REPORT] setelah [ACTIVITY]");
assert.ok(
  idxReport < withReport.indexOf("[REPEATED ACTIVITY]"),
  "[FINAL REPORT] sebelum [REPEATED ACTIVITY]"
);

// --- 2. Laporan panjang dengan Markdown/tabel/code block tetap utuh ----------
const longReport = [
  START_SENTINEL,
  "# Laporan Akhir Panjang",
  "",
  "## Ringkasan",
  "",
  "| File | Status |",
  "| --- | --- |",
  "| activityCopy.js | changed |",
  "| activityCopy.test.mjs | changed |",
  "",
  "```javascript",
  "export function buildAgentActivityCopy() {",
  "  return { ok: true };",
  "}",
  "```",
  ...Array.from({ length: 60 }, (_, i) => `- baris ${i}: detail hasil analisis AETHER`),
  "| — baris terakhir tabel — | ok |",
  END_SENTINEL,
].join("\n");
const longReportText = buildAgentActivityCopy({
  events: [
    { event_type: "task_started", timestamp: 1700000001, payload: {} },
    {
      event_type: "agent_commentary",
      timestamp: 1700000002,
      payload: { text: "Menyusun laporan akhir." },
    },
    completedEvent(longReport),
  ],
  meta,
});
assert.ok(longReportText.includes("```javascript"), "code block tetap utuh");
assert.ok(longReportText.includes("| activityCopy.js | changed |"), "tabel markdown tetap utuh");
assert.ok(longReportText.includes("## Ringkasan"), "heading markdown tetap utuh");
assert.ok(longReportText.includes(longReport), "laporan panjang disalin UTUH tanpa truncation");
assert.ok(
  longReportText.indexOf(END_SENTINEL) === longReportText.indexOf(longReport) + longReport.length - END_SENTINEL.length,
  "bagian PALING AKHIR laporan ada di posisi terakhir isi laporan"
);

// --- 3. Activity melebihi batas: aktivitas lama dipangkas, laporan utuh ------
// Aktivitas ~180.000 karakter (jauh > 45.000) + laporan ~3.000 karakter.
const hugeActivity = [];
for (let i = 0; i < 3000; i++) {
  hugeActivity.push({
    event_type: "tool_called",
    timestamp: 1700000200 + i,
    payload: { tool: "read_file", target: `src/agent_ai/core/very_long_module_name_${i}.py` },
  });
  hugeActivity.push({
    event_type: "observation_received",
    timestamp: 1700000201 + i,
    payload: {
      tool: "read_file",
      target: `src/agent_ai/core/very_long_module_name_${i}.py`,
      content: { path: `src/agent_ai/core/very_long_module_name_${i}.py`, total_lines: 10 },
    },
  });
}
const mediumReport = sentinelReport(
  Array.from({ length: 40 }, (_, i) => `detail laporan bagian ${i} yang harus tetap ada`).join("\n")
);
const bigWithReport = buildAgentActivityCopy({
  events: [...hugeActivity, completedEvent(mediumReport)],
  meta: bigMeta,
});
assert.ok(
  bigWithReport.includes("[... earlier activity truncated ...]"),
  "aktivitas dipangkas -> penanda truncation aktivitas tetap ada"
);
assert.ok(bigWithReport.includes("Task ID: big-task"), "metadata task tetap ada");
assert.ok(
  bigWithReport.includes(START_SENTINEL) && bigWithReport.includes(END_SENTINEL),
  "laporan akhir TETAP lengkap (awal & akhir) meski aktivitas dipangkas"
);
assert.ok(bigWithReport.includes(mediumReport), "laporan akhir identik utuh saat aktivitas dipangkas");
assert.ok(
  !bigWithReport.includes("very_long_module_name_0.py"),
  "aktivitas PALING LAMA dibuang lebih dulu"
);
assert.ok(
  bigWithReport.includes("very_long_module_name_2999.py"),
  "aktivitas TERBARU tetap ikut dicopy"
);
const idxRep3 = bigWithReport.indexOf("[REPEATED ACTIVITY]");
const idxFinal3 = bigWithReport.indexOf("[FINAL REPORT]");
assert.ok(idxFinal3 !== -1 && idxRep3 !== -1 && idxFinal3 < idxRep3, "[FINAL REPORT] sebelum [REPEATED ACTIVITY]");
const finalBlock3 = bigWithReport.slice(idxFinal3, idxRep3);
assert.ok(
  finalBlock3.includes(END_SENTINEL) && finalBlock3.includes("baris terakhir laporan."),
  "isi laporan (termasuk bagian paling akhir) berada di dalam section [FINAL REPORT]"
);
// Batas hanya berlaku untuk aktivitas; metadata + laporan tetap utuh.
assert.ok(bigWithReport.length <= MAX, `total masih <= ${MAX} (laporan ikut muat) (got ${bigWithReport.length})`);

// --- 4. Laporan akhir SENDIRI > batas -> laporan tetap UTUH (total boleh lewat) ---
const giantReport = [
  START_SENTINEL,
  ...Array.from({ length: 1500 }, (_, i) => `bagian laporan super panjang #${i} lorem ipsum dolor.`),
  END_SENTINEL,
].join("\n");
assert.ok(giantReport.length > MAX, "fixture laporan memang > 45.000 karakter");
const giantText = buildAgentActivityCopy({
  events: [completedEvent(giantReport)],
  meta,
});
assert.ok(giantText.includes(START_SENTINEL) && giantText.includes(END_SENTINEL), "laporan raksasa TIDAK dipotong");
assert.ok(giantText.includes(giantReport), "laporan raksasa disalin UTUH");
assert.ok(
  giantText.length > MAX,
  `total output boleh > ${MAX} demi laporan utuh (got ${giantText.length})`
);
assert.ok(
  giantText.endsWith("[REPEATED ACTIVITY]\n\nNo repeated activity.") ||
    giantText.includes(END_SENTINEL),
  "bagian akhir laporan tetap ada di output"
);

// --- 5. Metadata + urutan log + penanda truncation tetap sesuai --------------
const orderWithReport = buildAgentActivityCopy({ events, meta });
const actStart = orderWithReport.indexOf("[ACTIVITY]");
const actEnd = orderWithReport.indexOf("[FINAL REPORT]");
const actRegion = orderWithReport.slice(actStart, actEnd);
const orderFrags = [
  "Agent started task.",
  "Reading src/agent_ai/core/orchestrator.py",
  "Analyzing context compilation flow.",
  "Task completed.",
];
let prevIdx = -1;
for (const frag of orderFrags) {
  const at = actRegion.indexOf(frag);
  assert.ok(at !== -1, `baris "${frag}" ada di ACTIVITY`);
  assert.ok(at > prevIdx, `urutan log tetap kronologis untuk "${frag}"`);
  prevIdx = at;
}
assert.ok(orderWithReport.includes("Task ID: 78d18acc14194456b4791b86c651fd48"), "metadata task tetap ada");

// --- 6. Beberapa task / beberapa task_completed -> laporan task yang benar ----
const otherTaskCompleted = {
  event_type: "task_completed",
  task_id: "task-lain",
  timestamp: 1700000005,
  payload: { result: "LAPORAN_TASK_LAIN_YANG_TIDAK_BOLEH_TERSALIN" },
};
// (a) events yang diterima formatter milik task ini; task_completed task LAIN
//     hanya muncul di dalam daftar events ini. Yang disalin wajib laporan task
//     yang sedang dilihat (event task_completed terakhir yang disediakan).
const crossTask = buildAgentActivityCopy({
  events: [
    { event_type: "task_started", timestamp: 1700000001, task_id: "task-ini", payload: {} },
    otherTaskCompleted,
    { event_type: "task_completed", timestamp: 1700000010, task_id: "task-ini", payload: { result: reportText } },
  ],
  meta: { ...meta, taskId: "task-ini" },
});
assert.ok(crossTask.includes(reportText), "laporan task yang sedang dicopy tersalin utuh");
assert.ok(crossTask.includes(START_SENTINEL) && crossTask.includes(END_SENTINEL), "sentinel laporan task ini ada");
// (b) events yang tersedia HANYA milik task LAIN -> formatter tidak boleh
//     mengarang laporan: yang tersalin adalah laporan pada events itu saja,
//     tanpa memanggil sumber lain (tidak menyuntikkan laporan task ini).
const foreignOnly = buildAgentActivityCopy({
  events: [otherTaskCompleted],
  meta: { ...meta, taskId: "task-ini" },
});
assert.ok(
  foreignOnly.includes("LAPORAN_TASK_LAIN_YANG_TIDAK_BOLEH_TERSALIN"),
  "formatter hanya memakai events yang diberikan (tidak mengambil sumber laporan lain)"
);
assert.ok(
  !foreignOnly.includes("[FINAL REPORT]") === false,
  "section [FINAL REPORT] ada karena events membawa task_completed.result"
);

// (c) dua event task_completed pada task yang sama -> yang menang = LAPORAN AKHIR.
const doubleCompleted = buildAgentActivityCopy({
  events: [
    { event_type: "task_completed", timestamp: 1700000001, payload: { result: "laporan lama" } },
    { event_type: "task_completed", timestamp: 1700000002, payload: { result: reportText } },
  ],
  meta,
});
assert.ok(!doubleCompleted.includes("laporan lama"), "laporan task lama tidak ikut tersalin");
assert.ok(doubleCompleted.includes(END_SENTINEL), "laporan akhir terbaru yang tersalin");

// --- 7. Tanpa laporan akhir -> formatter tetap benar (tanpa laporan palsu) ---
const noReport = buildAgentActivityCopy({ events, meta });
assert.ok(noReport.includes("[ACTIVITY]"), "tanpa laporan: ACTIVITY tetap ada");
assert.ok(noReport.includes("[REPEATED ACTIVITY]"), "tanpa laporan: REPEATED tetap ada");
assert.ok(noReport.includes("Task ID: 78d18acc14194456b4791b86c651fd48"), "tanpa laporan: metadata tetap ada");
const bodyOnly = buildAgentActivityCopy({
  events: [{ event_type: "task_failed", timestamp: 1700000002, payload: {} }],
  meta,
});
assert.ok(bodyOnly.includes("[ACTIVITY]"), "task gagal tanpa laporan -> formatter tetap jalan");
assert.ok(!bodyOnly.includes("[FINAL REPORT]"), "task gagal tanpa laporan -> tidak menampilkan laporan palsu");

// --- 8. Repeated activity & formatter lama tidak regresi ---------------------
const repeated = buildAgentActivityCopy({
  events: [
    ...repEvents,
    completedEvent(reportText),
  ],
  meta,
});
assert.ok(repeated.includes("[REPEATED ACTIVITY]"), "section repeated tetap ada");
assert.ok(repeated.includes("- a.py × 2"), "ringkasan repeated tetap benar");
assert.ok(repeated.includes("Total repeated activity: 2"), "total repeated tetap benar");
assert.ok(
  repeated.indexOf("[FINAL REPORT]") < repeated.indexOf("[REPEATED ACTIVITY]"),
  "FINAL REPORT sebelum REPEATED ACTIVITY"
);
assert.ok(repeated.endsWith("Total repeated activity: 2"), "repeated summary tetap di paling bawah");
assert.ok(
  repeated.includes(reportText) && repeated.indexOf(END_SENTINEL) < repeated.indexOf("[REPEATED ACTIVITY]"),
  "laporan tetap utuh dan tidak tercampur ke section repeated"
);
// Metadata agent tetap lengkap & format dasar tidak berubah.
assert.ok(withReport.startsWith("AETHER AGENT ACTIVITY"), "pembuka tetap AETHER AGENT ACTIVITY");
assert.ok(withReport.includes("Provider: openrouter") && withReport.includes("Model: deepseek/deepseek-v4-flash"));

console.log(`[OK] activityCopy: format benar, urutan kronologis, truncation dari OLDEST, batas ${MAX} untuk activity, [FINAL REPORT] selalu utuh.`);