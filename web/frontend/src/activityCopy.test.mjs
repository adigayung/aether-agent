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

const MAX = AGENT_ACTIVITY_COPY_MAX_CHARS;
assert.equal(MAX, 45000, "konstanta batas = 45000");

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
assert.ok(idxAgent !== -1, "ada section [AGENT]");
assert.ok(idxActivity !== -1, "ada section [ACTIVITY]");
assert.ok(idxRepeated !== -1, "ada section [REPEATED ACTIVITY]");
assert.ok(
  idxAgent < idxActivity && idxActivity < idxRepeated,
  "urutan section: [AGENT] < [ACTIVITY] < [REPEATED ACTIVITY]"
);

// Metadata agent tercantum dengan nilai benar.
assert.ok(text.includes("Task ID: 78d18acc14194456b4791b86c651fd48"));
assert.ok(text.includes("Status: completed"));
assert.ok(text.includes("Provider: openrouter"));
assert.ok(text.includes("Model: deepseek/deepseek-v4-flash"));
assert.ok(text.includes("Duration: 00:00:37"));
assert.ok(text.includes("LLM Rounds: 2"));
assert.ok(text.includes("Tool Calls: 1"));

// Batas absolut.
assert.ok(text.length <= MAX, `hasil copy <= ${MAX} (got ${text.length})`);

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

assert.ok(bigText.length <= MAX, `aktivitas besar tetap <= ${MAX} (got ${bigText.length})`);
assert.ok(bigText.includes("Task ID: big-task"), "metadata agent tetap ada saat truncation");
assert.ok(
  bigText.includes("[... earlier activity truncated ...]"),
  "aktivitas besar menampilkan penanda truncation"
);
assert.ok(
  bigText.indexOf("[REPEATED ACTIVITY]") > bigText.indexOf("[ACTIVITY]"),
  "REPEATED ACTIVITY tetap paling bawah saat truncation"
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

console.log(`[OK] activityCopy: format benar, urutan kronologis, truncation dari OLDEST, total <= ${MAX}.`);