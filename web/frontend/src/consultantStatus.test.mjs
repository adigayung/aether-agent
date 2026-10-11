// Regression test (Node built-in `assert`, no framework) untuk Live Status
// Consultant (.consultant-thinking).
//
// Menguji kontrak event runtime NYATA:
//     tool_called    -> status per tool (mis. "Searching source code…")
//     tool_completed -> "Analyzing results…"
//     event lain     -> "" TIDAK boleh mengosongkan status yang sedang tampil
//     session_id     -> hanya event milik sesi yang dipantau yang dipakai
//     payload        -> format frame SSE nyata (event_type + session_id + payload)
//
// Run: node web/frontend/src/consultantStatus.test.mjs

import assert from "node:assert/strict";

import {
  applyConsultantEvent,
  eventBelongsToSession,
  normalizeConsultantEvent,
  statusTextForEvent,
} from "./consultantStatus.js";

// Frame SSE nyata: api/streaming.py::format_sse -> ExecutionEvent.to_dict().
function frame(eventType, { sessionId = "s1", taskId = null, payload = {} } = {}) {
  return {
    event_id: "e" + Math.random().toString(16).slice(2),
    sequence: 1,
    timestamp: 1700000000,
    session_id: sessionId,
    task_id: taskId,
    event_type: eventType,
    payload,
  };
}

let checks = 0;
function check(cond, msg) {
  assert.ok(cond, msg);
  checks += 1;
}

// --- 1. tool_completed -> "Analyzing results…" (inti bug) ------------------
{
  const ev = frame("tool_completed", { payload: { tool: "read_file", success: true } });
  assert.equal(statusTextForEvent(ev), "Analyzing results…");
  checks += 1;
  console.log('PASS: tool_completed -> "Analyzing results…"');
}

// Semua tool (bukan hanya satu status tertentu) memetakan hal yang sama.
for (const tool of [
  "read_file",
  "search_code",
  "list_files",
  "atlas_query",
  "rig_query",
  "project_map_status",
  "run_command",
  "update_project_bible",
  "tool_tidak_dikenal",
]) {
  const ev = frame("tool_completed", { payload: { tool, success: true } });
  assert.equal(
    statusTextForEvent(ev),
    "Analyzing results…",
    `tool_completed(${tool}) harus "Analyzing results…"`
  );
  checks += 1;
}
console.log("PASS: tool_completed untuk SEMUA tool -> \"Analyzing results…\"");

// --- 2. Status tidak tertimpa oleh event non-status ------------------------
{
  // Urutan nyata: tool_called -> tool_completed -> observation_received.
  let status = "";
  status = applyConsultantEvent(status, frame("tool_called", { payload: { tool: "read_file" } }));
  assert.equal(status, "Reading source code…");
  status = applyConsultantEvent(status, frame("tool_completed", { payload: { tool: "read_file", success: true } }));
  assert.equal(status, "Analyzing results…");
  const before = status;
  // Event observability lain TIDAK boleh mereset status.
  status = applyConsultantEvent(status, frame("observation_received", { payload: { tool: "read_file" } }));
  status = applyConsultantEvent(status, frame("provider_response", { payload: {} }));
  status = applyConsultantEvent(status, frame("phase_changed", { payload: {} }));
  assert.equal(status, before, "event non-status tidak boleh mengosongkan status");
  checks += 3;
  console.log('PASS: status "Analyzing results…" BERTAHAN melewati event non-status');
}

// Payload cacat / non-objek tidak boleh mengosongkan status.
{
  let status = "Analyzing results…";
  for (const bad of [null, undefined, 0, "", "tool_completed", [], { raw: "x" }]) {
    status = applyConsultantEvent(status, bad);
  }
  assert.equal(status, "Analyzing results…");
  checks += 1;
  console.log("PASS: payload cacat / kosong tidak mengosongkan status");
}

// --- 3. Pemetaan tool_called lainnya tetap berfungsi ----------------------
const CALLED_CASES = [
  ["read_file", "Reading source code…"],
  ["search_code", "Searching source code…"],
  ["list_files", "Inspecting project files…"],
  ["atlas_query", "Checking project map…"],
  ["rig_query", "Checking project map…"],
  ["project_map_status", "Checking project map…"],
  ["skill_catalog", "Checking available skills…"],
  ["load_skill", "Loading skill…"],
  ["load_skill_reference", "Loading skill reference…"],
  ["consultant_bible", "Reading Project Bible…"],
  ["bible_retrieval", "Reading Project Bible…"],
  ["run_command", "Running command…"],
  ["update_project_bible", "Updating Project Bible…"],
];
for (const [tool, expected] of CALLED_CASES) {
  assert.equal(
    statusTextForEvent(frame("tool_called", { payload: { tool } })),
    expected,
    `tool_called(${tool}) -> ${expected}`
  );
  checks += 1;
}
assert.equal(
  statusTextForEvent(frame("tool_called", { payload: { tool: "tool_baru" } })),
  "Working…",
  "tool tak dikenal tetap memberi status informatif"
);
assert.equal(
  statusTextForEvent(frame("tool_called", { payload: {} })),
  "",
  "tool_called tanpa nama tool -> tidak ada perubahan status"
);
checks += 2;
console.log(`PASS: ${CALLED_CASES.length} pemetaan tool_called OK (+ fallback)`);

// --- 4. Filter session_id (payload SSE nyata) -----------------------------
{
  const ev = frame("tool_completed", { sessionId: "s1" });
  check(eventBelongsToSession(ev, "s1"), "event sesi yang dipantau harus diterima");
  check(!eventBelongsToSession(ev, "s2"), "event sesi lain harus ditolak");
  check(!eventBelongsToSession(frame("tool_completed", { sessionId: "" }), "s1"),
    "event tanpa session_id tidak boleh menimpa status sesi");
  check(!eventBelongsToSession(ev, ""), "tanpa sid tidak ada event yang cocok");
  console.log("PASS: filter session_id OK");
}

// --- 5. Normalisasi payload (enum + string) ------------------------------
{
  const n1 = normalizeConsultantEvent(frame("tool_called", { sessionId: "s1", payload: { tool: "read_file" } }));
  assert.deepEqual(n1, {
    eventType: "tool_called",
    sessionId: "s1",
    taskId: "",
    body: { tool: "read_file" },
  });
  // event_type sebagai "enum" (objek dengan .value) juga diterima.
  const n2 = normalizeConsultantEvent({
    event_type: { value: "tool_completed" },
    session_id: "s1",
    payload: { tool: "read_file" },
  });
  assert.equal(n2.eventType, "tool_completed");
  assert.equal(statusTextForEvent({ event_type: { value: "tool_completed" }, payload: {} }), "Analyzing results…");
  checks += 2;
  console.log("PASS: normalisasi payload (string + enum) OK");
}

console.log(`\n=== SEMUA CHECK LIVE STATUS OK (${checks} assertion) ===`);
