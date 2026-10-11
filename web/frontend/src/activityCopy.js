// Agent Activity → clipboard (frontend-only formatter).
//
// Membangun snapshot teks dari data activity yang SUDAH ADA di UI (props
// `events` = live SSE atau persistent log via Activity API) + metadata Agent
// Card yang sudah dihitung App.vue. TIDAK ada sumber data baru, TIDAK ada
// panggilan backend, TIDAK mengubah cara activity dikumpulkan/dikirim.
//
// Struktur: [AGENT] -> [ACTIVITY] -> [FINAL REPORT] -> [REPEATED ACTIVITY].
//
// Batas panjang (AGENT_ACTIVITY_COPY_MAX_CHARS = 45.000 karakter) berlaku untuk
// bagian yang BOLEH dipangkas: baris ACTIVITY. Bila terlalu besar, baris PALING
// LAMA dibuang lebih dulu (daftar tersusun kronologis, termuda di akhir) dan
// diberi penanda "[... earlier activity truncated ...]" agar penerima tahu ada
// bagian yang tidak ikut dicopy.
//
// [FINAL REPORT] = laporan akhir Agent yang SAMA dengan yang dirender di box
// hasil user (dari event `task_completed.payload.result`) dan TIDAK PERNAH
// dipotong/diringkas; total output boleh melewati batas bila laporannya sendiri
// lebih panjang dari batas (lebih baik utuh daripada laporan terpotong diam-diam).

export const AGENT_ACTIVITY_COPY_MAX_CHARS = 45000;

const LINE_MAX = 400;

// Tool → kata kerja manusiawi (sama dengan label baris Agent Activity UI).
const TOOL_VERB = {
  list_files: "Exploring",
  read_file: "Reading",
  search_code: "Searching",
  write_file: "Editing",
  edit_file: "Editing",
  run_command: "Running command",
};

// Mode label untuk activity copy
const MODE_LABEL_COPY = {
  fast: "Fast",
  balanced: "Balanced",
  deep: "Deep",
};

// Waktu event -> HH:MM:SS (mendukung epoch detik/ms dan ISO string).
function formatTime(raw) {
  if (raw == null) return "";
  if (typeof raw === "number") {
    const d = new Date(raw < 1e12 ? raw * 1000 : raw);
    return Number.isNaN(d.getTime()) ? "" : d.toTimeString().slice(0, 8);
  }
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? "" : d.toTimeString().slice(0, 8);
}

function normalizeEvent(e) {
  return {
    type: e.event_type || e.event || "",
    data: e.payload || e.data || {},
    ts: formatTime(e.timestamp),
  };
}

function asObject(content) {
  if (content && typeof content === "object") return content;
  if (typeof content === "string") {
    const text = content.trim();
    if (text.startsWith("{") || text.startsWith("[")) {
      try {
        const parsed = JSON.parse(text);
        if (parsed && typeof parsed === "object") return parsed;
      } catch (e) {
        return null;
      }
    }
  }
  return null;
}

// Satu baris teks ringkas (whitespace dinormalisasi) dengan batas panjang.
function oneLine(value, max) {
  const text = String(value == null ? "" : value).replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.substring(0, max)}…` : text;
}

function safeJson(value) {
  try {
    return JSON.stringify(value);
  } catch (e) {
    return String(value);
  }
}

// Ringkasan isi observation (field kunci) — konsisten dengan preview UI.
function rawPreview(content) {
  if (content == null) return "";
  if (typeof content === "string") return oneLine(content, 140);
  const obj = asObject(content) || content;
  const parts = [];
  if (typeof obj.count === "number") parts.push(`${obj.count} matches`);
  if (typeof obj.total_lines === "number") parts.push(`${obj.total_lines} lines`);
  if (Array.isArray(obj.entries)) parts.push(`${obj.entries.length} entries`);
  if (Array.isArray(obj.matches)) parts.push(`${obj.matches.length} matches`);
  if (obj.path) parts.push(String(obj.path));
  if (obj.exit_code !== undefined && obj.exit_code !== null) parts.push(`exit ${obj.exit_code}`);
  if (obj.already_available || obj.already_searched) parts.push("already available");
  return parts.length ? oneLine(parts.join(" · "), 300) : oneLine(safeJson(obj), 300);
}

// Satu event -> satu baris teks (atau null bila tidak ikut ditampilkan UI).
function describeLine(e) {
  const d = e.data || {};
  const prefix = e.ts ? `[${e.ts}] ` : "";
  switch (e.type) {
    case "task_started":
      return `${prefix}Agent started task.`;
    case "task_completed":
      return `${prefix}Task completed.`;
    case "task_failed":
      return `${prefix}Task failed.`;
    case "task_cancelled":
      return `${prefix}Task cancelled.`;
    case "agent_commentary": {
      const text = String(d.text || "").trim();
      return text ? `${prefix}${oneLine(text, LINE_MAX)}` : null;
    }
    case "validation_started":
      return `${prefix}Validation started.`;
    case "validation_completed":
      return `${prefix}${d.success === false ? "Validation failed." : "Validation completed."}`;
    case "recovery_started":
      return `${prefix}Recovery started.`;
    case "recovery_completed":
      return `${prefix}Recovery completed.`;
    case "policy_applied": {
      const req = MODE_LABEL_COPY[d.requested_mode] || d.requested_mode || "Balanced";
      const eff = MODE_LABEL_COPY[d.effective_mode] || d.effective_mode || "Balanced";
      if (d.escalated) {
        const reason = d.reason ? ` · Reason: ${oneLine(d.reason, 120)}` : "";
        return `${prefix}[POLICY] Requested Mode: ${req} · Effective Mode: ${eff}${reason}`;
      }
      return `${prefix}[POLICY] Requested Mode: ${req} · Effective Mode: ${eff}`;
    }
    case "policy_escalated": {
      const from = MODE_LABEL_COPY[d.from_mode] || d.from_mode || "Fast";
      const to = MODE_LABEL_COPY[d.to_mode] || d.to_mode || "Deep";
      const reason = d.reason ? ` · Reason: ${oneLine(d.reason, 120)}` : "";
      return `${prefix}[POLICY] Escalated: ${from} → ${to}${reason}`;
    }
    case "tool_called": {
      const tool = String(d.tool || "");
      const target = d.target ? String(d.target) : "";
      const verb = TOOL_VERB[tool] || tool || "Tool";
      return target ? `${prefix}${verb} ${target}` : `${prefix}${verb}`;
    }
    case "tool_completed": {
      const tool = String(d.tool || "");
      if (d.success !== false) return null; // sukses -> cukup baris observation
      const error = d.error ? ` — ${oneLine(String(d.error), 200)}` : "";
      return `${prefix}✕ ${tool} failed${error}`;
    }
    case "observation_received": {
      const tool = String(d.tool || "");
      const ok = d.success !== false;
      const preview = rawPreview(d.content);
      const head = `${ok ? "✓" : "✕"} ${tool}`;
      return preview ? `${prefix}${head} → ${preview}` : `${prefix}${head}`;
    }
    default:
      return null;
  }
}

// Baris aktivitas kronologis (urutan events = urutan yang dipakai UI).
export function formatActivityLines(events) {
  const out = [];
  for (const raw of events || []) {
    const line = describeLine(normalizeEvent(raw));
    if (line) out.push(line);
  }
  return out;
}

// Repeated activity: file yang dibaca (read_file) lebih dari sekali.
// Logika SAMA dengan perhitungan baris "Repeated activity" di Activity UI
// (counting dilakukan frontend atas events yang sama; tanpa backend baru).
export function countRepeatedActivity(events) {
  const counts = new Map();
  for (const raw of events || []) {
    const e = normalizeEvent(raw);
    if (e.type !== "observation_received") continue;
    const d = e.data || {};
    if (String(d.tool || "") !== "read_file") continue;
    const obj = asObject(d.content);
    const path = (obj && obj.path) || d.target || "";
    if (!path) continue;
    const key = String(path);
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const items = [];
  counts.forEach((count, path) => {
    if (count > 1) items.push({ path, count });
  });
  items.sort((a, b) => b.count - a.count);
  const total = items.reduce((acc, item) => acc + item.count, 0);
  return { items, total };
}

function buildRepeatedSection(events) {
  const { items, total } = countRepeatedActivity(events);
  const body = items.length
    ? `${items.map((i) => `- ${i.path} × ${i.count}`).join("\n")}\n\nTotal repeated activity: ${total}`
    : "No repeated activity.";
  return `[REPEATED ACTIVITY]\n\n${body}`;
}

// Final Agent Report dari event terminal task yang sedang dicopy.
// Kontrak backend (SUDAH ADA, tidak diubah di sini): event `task_completed`
// membawa payload/`data` `result` = teks laporan akhir UTUH sampai ke user
// (lihat runtime._lifecycle_finalize -> {"result": report_text}, disanitasi
// sebagai VerbatimText). Diambil HANYA dari event task_completed pada daftar
// events task ini -> tidak pernah memakai laporan task lain / laporan lama.
// Return null bila task tidak punya laporan akhir (mis. masih running/gagal)
// sehingga TIDAK ada laporan palsu yang ditampilkan.
function extractFinalReport(events) {
  const list = Array.isArray(events) ? events : [];
  for (let i = list.length - 1; i >= 0; i--) {
    const e = normalizeEvent(list[i]);
    if (e.type !== "task_completed") continue;
    const d = e.data || {};
    const raw = d.result != null ? d.result : d.report;
    const text = typeof raw === "string" ? raw : "";
    if (text.trim()) return text;
  }
  return null;
}

function buildFinalReportSection(events) {
  const report = extractFinalReport(events);
  // Teks laporan disalin APA ADANYA (tanpa slice/ellipsis/ringkasan) agar
  // Markdown, heading, tabel, code block, baris baru, dan bagian paling akhir
  // (sentinel) tetap utuh.
  return report == null ? null : ["[FINAL REPORT]", "", report].join("\n");
}

// Bagian [AGENT]: hanya baris yang nilainya ada (provider/model/duration dll
// bisa kosong untuk task yang belum sempat menjalankan provider).
function buildAgentHeader(meta) {
  const m = meta || {};
  const rows = [];
  const add = (label, value) => {
    const v = value == null ? "" : String(value).trim();
    if (v) rows.push(`${label}: ${v}`);
  };
  add("Task ID", m.taskId);
  add("Status", m.status);
  add("Provider", m.provider);
  add("Model", m.model);
  add("Execution", m.execution);
  add("Round", m.llmRounds != null && m.llmRounds !== "" ? String(m.llmRounds) : "");
  add("Duration", m.duration);
  add("LLM Rounds", m.llmRounds);
  add("Tool Calls", m.toolCalls);
  add("Tokens", m.tokens);
  return ["AETHER AGENT ACTIVITY", "", "[AGENT]", ...rows].join("\n");
}

// Snapshot lengkap Agent Activity untuk clipboard.
// Urutan: [AGENT] -> [ACTIVITY] -> [FINAL REPORT] -> [REPEATED ACTIVITY].
//
// Kebijakan batas (AGENT_ACTIVITY_COPY_MAX_CHARS):
//   - Metadata [AGENT] selalu dipertahankan.
//   - [FINAL REPORT] selalu disalin UTUH (tidak pernah dipotong/diringkas).
//   - Bila total melebihi batas, yang dikorbankan adalah AKTIVITAS PALING LAMA
//     (baris teratas) + penanda truncation, bukan laporan akhir.
//   - Karena laporan akhir dipertahankan utuh, total hasil boleh melebihi batas
//     ketika laporannya sendiri lebih panjang dari batas.
export function buildAgentActivityCopy({ events, meta }) {
  const list = Array.isArray(events) ? events : [];
  const headerBlock = buildAgentHeader(meta);
  const repeatedBlock = buildRepeatedSection(list);
  const finalReportBlock = buildFinalReportSection(list);
  const activityLines = formatActivityLines(list);

  let kept = activityLines.slice();
  let truncated = false;

  const assemble = () => {
    const body = truncated ? ["[... earlier activity truncated ...]", ...kept] : kept;
    return [
      headerBlock,
      ["[ACTIVITY]", "", ...body].join("\n"),
      finalReportBlock,
      repeatedBlock,
    ]
      .filter((block) => block != null)
      .join("\n\n");
  };

  let text = assemble();
  // Pangkas aktivitas PALING LAMA (paling atas) sampai muat. `kept` hanya
  // berisi baris ACTIVITY -> metadata & laporan akhir TIDAK pernah ikut dibuang.
  while (text.length > AGENT_ACTIVITY_COPY_MAX_CHARS && kept.length > 0) {
    kept.shift();
    truncated = true;
    text = assemble();
  }
  return text;
}