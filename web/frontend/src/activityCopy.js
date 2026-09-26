// Agent Activity → clipboard (frontend-only formatter).
//
// Membangun snapshot teks dari data activity yang SUDAH ADA di UI (props
// `events` = live SSE atau persistent log via Activity API) + metadata Agent
// Card yang sudah dihitung App.vue. TIDAK ada sumber data baru, TIDAK ada
// panggilan backend, TIDAK mengubah cara activity dikumpulkan/dikirim.
//
// Batas keras: hasil akhir ≤ AGENT_ACTIVITY_COPY_MAX_CHARS (45.000 karakter).
// Bila activity terlalu besar, baris PALING LAMA dibuang lebih dulu (daftar
// baris tersusun kronologis, termuda di akhir) dan diberi penanda
// "[... earlier activity truncated ...]" agar LLM tahu ada bagian yang tidak
// ikut dicopy. [REPEATED ACTIVITY] selalu berada paling bawah.

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
  add("Duration", m.duration);
  add("LLM Rounds", m.llmRounds);
  add("Tool Calls", m.toolCalls);
  return ["AETHER AGENT ACTIVITY", "", "[AGENT]", ...rows].join("\n");
}

// Snapshot lengkap Agent Activity untuk clipboard, ≤ MAX characters.
// Aktivitas PALING LAMA dibuang lebih dulu bila melebihi batas.
export function buildAgentActivityCopy({ events, meta }) {
  const list = Array.isArray(events) ? events : [];
  const headerBlock = buildAgentHeader(meta);
  const repeatedBlock = buildRepeatedSection(list);
  const activityLines = formatActivityLines(list);

  let kept = activityLines.slice();
  let truncated = false;

  const assemble = () => {
    const body = truncated ? ["[... earlier activity truncated ...]", ...kept] : kept;
    return [headerBlock, ["[ACTIVITY]", "", ...body].join("\n"), repeatedBlock].join("\n\n");
  };

  let text = assemble();
  while (text.length > AGENT_ACTIVITY_COPY_MAX_CHARS && kept.length > 0) {
    kept.shift(); // buang baris PALING LAMA (paling atas)
    truncated = true;
    text = assemble();
  }

  // Jaminan absolut: hasil akhir TIDAK PERNAH > MAX (bahkan bila repeated
  // activity ekstrem sehingga tidak ada satu pun baris activity yang muat).
  const TAIL_MARKER = "\n[... output truncated for length ...]";
  if (text.length > AGENT_ACTIVITY_COPY_MAX_CHARS) {
    text = `${text.slice(0, AGENT_ACTIVITY_COPY_MAX_CHARS - TAIL_MARKER.length)}${TAIL_MARKER}`;
  }
  return text;
}