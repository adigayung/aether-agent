<script setup>
// Agent Activity (compact activity feed).
//
// Semua aktivitas satu Task ditampilkan dalam SATU alur chronological, tetapi
// tiap aktivitas adalah BARIS RINGKAS (bukan blok besar):
//
//   [05:38:26] 📖 Reading source   src/core         10 files      ✓ completed  ▸ detail
//   [05:38:29] 🔎 Searching code   needle           13 matches    ✓ completed  ▸ detail
//   [05:38:38] 📂 Exploring files  .                4 directories ✓ completed  ▸ detail
//   [05:38:36] ▶ Running command   npm run build                  ✓ completed  ▸ detail
//   [05:39:25] ✓ Task completed
//
// Dua lapisan informasi (pola CLI/agent modern):
//   1. Baris ringkas   : waktu + ikon + aksi manusiawi + scope + hasil + status
//   2. Detail opsional : daftar file/dir + event mentah yang SUDAH ADA
//                        (TOOL / RESULT / OBSERVATION) + report final
//
// Grouping: aktivitas berurutan dari tool yang sama (mis. 10x read_file)
// digabung menjadi SATU baris. Grouping dilakukan murni di layer tampilan ini;
// sumber event (SSE live #51 atau Activity API/persistent log) tidak diubah dan
// isi raw event tidak dimodifikasi.
//
// Ini BUKAN terminal mentah: observation ditampilkan sebagai ringkasan angka
// (jumlah file/baris/match), bukan dump JSON/file panjang.
//
// Agent Report final (task_completed.data.result) ditampilkan UTUH di alur ini
// tanpa truncate/ellipsis, memakai Markdown renderer yang sama dengan
// ReportViewer (web/frontend/src/markdown.js). Teks report asli juga dipakai
// tombol Copy kecil (pojok kiri bawah area report) yang memakai pola
// .cmsg-actions/.copy-btn yang sama dengan Consultant Chat.
import { computed, nextTick, ref, watch } from "vue";
import { renderMarkdown } from "../markdown.js";

const props = defineProps({
  events: { type: Array, default: () => [] },
  status: { type: String, default: "idle" },
  // Live "Agent reasoning." indicator. Dikendalikan oleh App.vue dari event SSE
  // existing (provider_request -> true, provider_response/terminal -> false).
  // Ini SATU elemen yang sama (bukan entri timeline/log baru).
  isReasoning: { type: Boolean, default: false },
});

const scroller = ref(null);

async function scrollToLatest() {
  await nextTick();
  const el = scroller.value;
  if (el) el.scrollTop = el.scrollHeight;
}

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

// Normalisasi dua bentuk event ke { type, data, ts }.
function normalize(e) {
  return {
    type: e.event_type || e.event || "",
    data: e.payload || e.data || {},
    ts: formatTime(e.timestamp),
  };
}

// ---------------------------------------------------------------------------
// Label manusiawi per tool (menggantikan nama tool mentah di baris utama).
// ---------------------------------------------------------------------------
const TOOL_META = {
  list_files: { icon: "📂", verb: "Exploring files" },
  read_file: { icon: "📖", verb: "Reading source" },
  search_code: { icon: "🔎", verb: "Searching code" },
  write_file: { icon: "✏", verb: "Editing files" },
  edit_file: { icon: "✏", verb: "Editing files" },
  run_command: { icon: "▶", verb: "Running command" },
};

// Label event mentah (dipakai di layer detail) — mempertahankan istilah yang
// sudah dipakai timeline AETHER.
const RAW_LABEL = {
  tool_called: "TOOL",
  tool_completed: "RESULT",
  observation_received: "OBSERVATION",
};

function rawLabel(type) {
  return RAW_LABEL[type] || "EVENT";
}

function rawClass(type) {
  if (type === "tool_called") return "tool";
  if (type === "tool_completed") return "result";
  if (type === "observation_received") return "observation";
  return "status";
}

function uniqueList(list) {
  const out = [];
  (list || []).forEach((value) => {
    const text = value == null ? "" : String(value);
    if (text && !out.includes(text)) out.push(text);
  });
  return out;
}

function shortName(path) {
  const text = String(path == null ? "" : path).replace(/[\\/]+$/, "");
  const parts = text.replace(/[\\/]+/g, "/").split("/");
  return parts[parts.length - 1] || text;
}

function stamp(ts) {
  return ts ? `[${ts}]` : "";
}

// Konten observation bisa berupa objek (jalur tool loop) atau string JSON
// (jalur payload tool). Parse aman, tanpa melempar error.
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

// Ukur hasil observation: jumlah file/dir (list_files) atau match (search_code).
function measure(tool, content) {
  const obj = asObject(content);
  if (!obj) return null;
  if (tool === "list_files" && Array.isArray(obj.entries)) {
    let files = 0;
    let dirs = 0;
    obj.entries.forEach((entry) => {
      if (entry && entry.type === "dir") dirs += 1;
      else files += 1;
    });
    return { files, dirs };
  }
  if (tool === "search_code" && typeof obj.count === "number") {
    return { matches: obj.count };
  }
  if (typeof obj.total_lines === "number") return { lines: obj.total_lines };
  return null;
}

function isRepeatContent(content) {
  const obj = asObject(content);
  return Boolean(obj && (obj.already_available || obj.already_searched));
}

// ---------------------------------------------------------------------------
// Pengelompokan event (murni tampilan) -> daftar unit tool.
// ---------------------------------------------------------------------------
function buildUnits(events) {
  const units = [];
  const lastByTool = new Map();
  const openByTool = new Map();
  let seq = 0;

  events.forEach((raw, index) => {
    const e = normalize(raw);
    const type = e.type;
    if (type !== "tool_called" && type !== "tool_completed" && type !== "observation_received") {
      return;
    }
    const d = e.data || {};
    const tool = String(d.tool || "");

    let unit = null;
    if (type !== "tool_called") {
      unit = openByTool.get(tool) || lastByTool.get(tool) || null;
    }
    if (!unit) {
      unit = {
        key: `tool-${tool}-${seq++}`,
        tool,
        at: index,
        atEnd: index,
        ts: e.ts,
        events: [],
        targets: [],
        called: 0,
        done: 0,
        success: true,
        error: "",
        measures: [],
        repeated: false,
      };
      units.push(unit);
    }
    unit.atEnd = index;
    unit.ts = unit.ts || e.ts;
    unit.events.push({ type, ts: e.ts, data: d });
    lastByTool.set(tool, unit);

    if (type === "tool_called") {
      unit.called += 1;
      if (d.target) unit.targets.push(String(d.target));
      openByTool.set(tool, unit);
      return;
    }
    if (type === "tool_completed") {
      unit.done += 1;
      if (d.target) unit.targets.push(String(d.target));
      if (d.success === false) {
        unit.success = false;
        if (d.error) unit.error = String(d.error);
      }
      return;
    }
    // observation_received: hasil akhir satu panggilan tool.
    if (d.success === false) unit.success = false;
    const measured = measure(tool, d.content);
    if (measured) unit.measures.push(measured);
    if (isRepeatContent(d.content)) unit.repeated = true;
    openByTool.delete(tool);
  });

  return units;
}

// Gabungkan unit BERURUTAN dari tool yang sama menjadi satu baris aktivitas,
// kecuali ada event non-tool (mis. commentary) di antaranya.
function mergeUnits(units, nonToolIdx) {
  const groups = [];
  units.forEach((u) => {
    const last = groups[groups.length - 1];
    const adjacent =
      Boolean(last) &&
      last.tool === u.tool &&
      !nonToolIdx.some((idx) => idx > last.atEnd && idx < u.at);
    if (adjacent) {
      last.events = last.events.concat(u.events);
      last.targets = last.targets.concat(u.targets);
      last.measures = last.measures.concat(u.measures);
      last.called += u.called;
      last.done += u.done;
      last.atEnd = Math.max(last.atEnd, u.atEnd);
      if (!u.success) {
        last.success = false;
        last.error = last.error || u.error;
      }
      if (u.repeated) last.repeated = true;
      return;
    }
    groups.push({
      key: u.key,
      tool: u.tool,
      at: u.at,
      atEnd: u.atEnd,
      ts: u.ts,
      events: u.events.slice(),
      targets: u.targets.slice(),
      measures: u.measures.slice(),
      called: u.called,
      done: u.done,
      success: u.success,
      error: u.error,
      repeated: u.repeated,
    });
  });
  return groups;
}

// "4 files", "4 directories", "3 files, 1 directory", "13 matches".
function countLabel(group) {
  if (group.tool === "list_files") {
    let files = 0;
    let dirs = 0;
    group.measures.forEach((m) => {
      files += m.files || 0;
      dirs += m.dirs || 0;
    });
    const parts = [];
    if (files) parts.push(`${files} file${files === 1 ? "" : "s"}`);
    if (dirs) parts.push(`${dirs} director${dirs === 1 ? "y" : "ies"}`);
    return parts.join(", ");
  }
  if (group.tool === "search_code") {
    const total = group.measures.reduce((acc, m) => acc + (m.matches || 0), 0);
    return total ? `${total} match${total === 1 ? "" : "es"}` : "";
  }
  if (group.tool === "run_command") {
    const total = group.measures.length || group.called;
    return total > 1 ? `${total} commands` : "";
  }
  const total = uniqueList(group.targets).length || group.called;
  return total > 1 ? `${total} files` : "";
}

// Scope ringkas (direktori bersama, file tunggal, query, atau command).
function scopeLabel(tool, targets) {
  if (!targets.length) return "";
  if (tool === "search_code" || tool === "run_command") return targets[0];
  if (targets.length === 1) return targets[0];
  const dirs = uniqueList(
    targets.map((target) => {
      const parts = String(target).replace(/[\\/]+/g, "/").split("/");
      parts.pop();
      return parts.join("/");
    })
  );
  return dirs.length === 1 ? dirs[0] : "";
}

function describeTool(group, status) {
  const meta = TOOL_META[group.tool] || { icon: "🔧", verb: group.tool || "Tool" };
  const targets = uniqueList(group.targets);
  const failed = !group.success;
  const running = !failed && group.done === 0 && status === "running";

  let state = "ok";
  let stateText = "completed";
  let stateIcon = "✓";
  if (failed) {
    state = "err";
    stateText = "failed";
    stateIcon = "✕";
  } else if (running) {
    state = "run";
    stateText = "running";
    stateIcon = "⏳";
  } else if (group.repeated) {
    state = "warn";
    stateText = "repeated";
    stateIcon = "⚠";
  }

  return {
    key: group.key,
    at: group.at,
    ts: group.ts,
    kind: "tool",
    icon: meta.icon,
    title: meta.verb,
    scope: scopeLabel(group.tool, targets),
    count: countLabel(group),
    state,
    stateText,
    stateIcon,
    error: failed ? group.error : "",
    files: targets.map(shortName),
    events: group.events,
  };
}

// Aktivitas non-tool -> baris status/notice ringkas.
function describeEvent(e, index) {
  const d = e.data || {};
  const base = { at: index, ts: e.ts, events: [] };
  switch (e.type) {
    case "agent_commentary": {
      const text = String(d.text || "").trim();
      if (!text) return null;
      return {
        ...base,
        key: `agent-${index}`,
        kind: "reasoning",
        icon: "🤔",
        title: "Reasoning",
        detailText: text,
      };
    }
    case "task_started":
      return { ...base, key: `status-${index}`, kind: "status", icon: "▶", title: "Task started" };
    case "task_completed": {
      // Agent Report final = data.result (isi UTUH dari log/SSE, bukan preview
      // terpotong). Dirender penuh sebagai Markdown; teks aslinya disimpan
      // terpisah untuk tombol Copy.
      const report = typeof d.result === "string" ? d.result : "";
      return {
        ...base,
        key: `status-${index}`,
        kind: "status",
        icon: "✓",
        title: "Task completed",
        reportText: report,
        reportHtml: report ? renderMarkdown(report) : "",
      };
    }
    case "task_failed":
      return { ...base, key: `status-${index}`, kind: "status", icon: "✕", title: "Task failed" };
    case "task_cancelled":
      return { ...base, key: `status-${index}`, kind: "status", icon: "⚠", title: "Task cancelled" };
    case "validation_started":
      return { ...base, key: `notice-${index}`, kind: "notice", icon: "✓", title: "Validating" };
    case "validation_completed":
      return {
        ...base,
        key: `notice-${index}`,
        kind: "notice",
        icon: d.success === false ? "✕" : "✓",
        title: d.success === false ? "Validation failed" : "Validation completed",
      };
    case "recovery_started":
      return { ...base, key: `notice-${index}`, kind: "notice", icon: "⚠", title: "Recovery started" };
    case "recovery_completed":
      return { ...base, key: `notice-${index}`, kind: "notice", icon: "✓", title: "Recovery completed" };
    default:
      return null;
  }
}

// Timeline chronological: baris tool yang sudah digabung + event non-tool,
// diurutkan kembali berdasarkan posisi event aslinya.
const timeline = computed(() => {
  const events = props.events || [];
  const nonToolIdx = [];

  events.forEach((raw, index) => {
    const type = (raw && (raw.event_type || raw.event)) || "";
    if (type !== "tool_called" && type !== "tool_completed" && type !== "observation_received") {
      nonToolIdx.push(index);
    }
  });

  const items = [];

  mergeUnits(buildUnits(events), nonToolIdx).forEach((group) => {
    items.push(describeTool(group, props.status));
  });

  events.forEach((raw, index) => {
    const item = describeEvent(normalize(raw), index);
    if (item) items.push(item);
  });

  items.sort((a, b) => a.at - b.at);
  return items;
});

const empty = computed(() => !timeline.value.length);

// Aktivitas berulang: target yang dibaca lebih dari sekali.
const repeats = computed(() => {
  const counts = new Map();
  (props.events || []).forEach((raw) => {
    const e = normalize(raw);
    if (e.type !== "observation_received") return;
    const d = e.data || {};
    if (String(d.tool || "") !== "read_file") return;
    const obj = asObject(d.content);
    const path = (obj && obj.path) || d.target || "";
    if (!path) return;
    const key = String(path);
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  const out = [];
  counts.forEach((count, path) => {
    if (count > 1) out.push({ name: shortName(path), path, count });
  });
  return out.sort((a, b) => b.count - a.count);
});

// --- Detail opsional (buka/tutup per baris, state lokal di komponen) --------
const expanded = ref({});

function isOpen(key) {
  return expanded.value[key] === true;
}

function toggle(key) {
  expanded.value = { ...expanded.value, [key]: !expanded.value[key] };
}

function hasDetail(item) {
  return Boolean(
    (item.detailText && item.detailText.length) ||
      (item.files && item.files.length) ||
      (item.events && item.events.length)
  );
}

// Satu baris teks ringkas (tanpa newline ganda) dengan batas panjang.
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

// Ringkasan isi observation (angka/field kunci) — BUKAN dump isi file.
function rawPreview(content) {
  if (content == null) return "";
  if (typeof content === "string") return oneLine(content, 300);
  const obj = asObject(content) || content;
  const parts = [];
  if (typeof obj.count === "number") {
    parts.push(`${obj.count} ${obj.query ? "matches" : "items"}`);
  }
  if (typeof obj.total_lines === "number") parts.push(`${obj.total_lines} lines`);
  if (Array.isArray(obj.entries)) parts.push(`${obj.entries.length} entries`);
  if (Array.isArray(obj.matches)) parts.push(`${obj.matches.length} matches`);
  if (obj.path) parts.push(String(obj.path));
  if (obj.exit_code !== undefined && obj.exit_code !== null) parts.push(`exit ${obj.exit_code}`);
  if (obj.already_available || obj.already_searched) parts.push("already available");
  return parts.length ? oneLine(parts.join(" · "), 300) : oneLine(safeJson(obj), 300);
}

// Teks satu event mentah (label TOOL/RESULT/OBSERVATION + isinya).
function rawText(ev) {
  const d = ev.data || {};
  const tool = d.tool || "";
  const target = d.target ? ` ${d.target}` : "";
  if (ev.type === "tool_called") return `${tool}${target}`.trim();
  if (ev.type === "tool_completed") {
    const ok = d.success !== false;
    const error = !ok && d.error ? ` — ${d.error}` : "";
    return `${ok ? "✓" : "✕"} ${tool}${target}${error}`.trim();
  }
  if (ev.type === "observation_received") {
    const ok = d.success !== false;
    const body = rawPreview(d.content);
    const head = `${ok ? "✓" : "✕"} ${tool}`;
    return body ? `${head} → ${body}` : head;
  }
  return "";
}

function rawTextClass(ev) {
  if (ev.type === "tool_completed" || ev.type === "observation_received") {
    return ev.data && ev.data.success === false ? "err" : "ok";
  }
  return "";
}

// --- Copy final report ------------------------------------------------------
// Salin SELURUH isi final report (teks Markdown sumber dari LLM), bukan hanya
// teks yang terlihat di viewport dan bukan markup HTML hasil render. Memakai
// pola .cmsg-actions/.copy-btn yang sama dengan tombol Copy di Consultant Chat
// (termasuk feedback label "Copied" sementara).
const copiedReportKey = ref(null);
let copiedTimer = null;

async function copyReport(item) {
  const text = item && typeof item.reportText === "string" ? item.reportText : "";
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    return;
  }
  copiedReportKey.value = item.key;
  if (copiedTimer) clearTimeout(copiedTimer);
  copiedTimer = setTimeout(() => {
    copiedReportKey.value = null;
    copiedTimer = null;
  }, 1400);
}

watch(timeline, scrollToLatest, { flush: "post" });

// Saat reasoning status MUNCUL, pastikan barisnya terlihat (scroll ke bawah)
// tanpa mengubah mekanisme scroll yang sudah ada.
watch(
  () => props.isReasoning,
  (on) => {
    if (on) scrollToLatest();
  },
  { flush: "post" }
);
</script>

<template>
  <div ref="scroller" class="term-body act-body">
    <div v-if="empty" class="log-line info">
      <span class="msg">
        <template v-if="status === 'running'">AETHER is working…</template>
        <template v-else>Give AETHER a task to begin. Its activity will appear here.</template>
      </span>
    </div>

    <template v-for="item in timeline" :key="item.key">
      <!-- Lapisan 1: baris aktivitas ringkas (satu aktivitas = satu baris). -->
      <div :class="['act-row', item.kind, item.state]">
        <span class="act-time">{{ stamp(item.ts) }}</span>
        <span class="act-ico" aria-hidden="true">{{ item.icon }}</span>
        <span class="act-title" :title="item.title">{{ item.title }}</span>
        <span v-if="item.scope" class="act-scope" :title="item.scope">{{ item.scope }}</span>
        <span v-if="item.count" class="act-count">{{ item.count }}</span>
        <span v-if="item.error" class="act-error" :title="item.error">{{ item.error }}</span>
        <span v-if="item.stateText" class="act-state" :class="item.state">{{ item.stateIcon }} {{ item.stateText }}</span>
        <button
          v-if="hasDetail(item)"
          type="button"
          class="act-toggle"
          :class="{ open: isOpen(item.key) }"
          :aria-expanded="isOpen(item.key) ? 'true' : 'false'"
          :title="isOpen(item.key) ? 'Tutup detail' : 'Baca detail'"
          @click="toggle(item.key)"
        >
          <span class="act-caret">{{ isOpen(item.key) ? "▾" : "▸" }}</span>
          <span class="act-toggle-label">{{ isOpen(item.key) ? "tutup" : "detail" }}</span>
        </button>
      </div>

      <!-- Lapisan 2: detail mentah (hanya dirender saat baris dibuka). -->
      <div v-if="isOpen(item.key)" class="act-detail">
        <div v-if="item.detailText" class="act-detail-text">{{ item.detailText }}</div>
        <div v-if="item.files && item.files.length" class="act-files">
          <span v-for="(name, fi) in item.files" :key="fi" class="act-file">{{ name }}</span>
        </div>
        <div v-if="item.events && item.events.length" class="act-raw">
          <div v-for="(ev, ei) in item.events" :key="ei" class="act-raw-row">
            <span class="act-label" :class="rawClass(ev.type)">{{ rawLabel(ev.type) }}</span>
            <span class="act-body-col">
              <span v-if="ev.ts" class="act-ts">[{{ ev.ts }}]</span>
              <span class="act-text" :class="rawTextClass(ev)">{{ rawText(ev) }}</span>
            </span>
          </div>
        </div>
      </div>

      <!-- Agent Report final: ditampilkan UTUH (tanpa truncate/ellipsis).
           Markdown mengikuti renderer yang sama dengan ReportViewer.
           Tombol Copy kecil di pojok kiri bawah area report. -->
      <div v-if="item.reportHtml" class="act-report-block">
        <!-- eslint-disable-next-line vue/no-v-html -->
        <div class="md act-report" v-html="item.reportHtml"></div>
        <div class="cmsg-actions">
          <button
            type="button"
            class="copy-btn"
            :class="{ copied: copiedReportKey === item.key }"
            :title="copiedReportKey === item.key ? 'Copied' : 'Copy final report'"
            :aria-label="copiedReportKey === item.key ? 'Copied' : 'Copy final report'"
            @click="copyReport(item)"
          >
            <svg v-if="copiedReportKey === item.key" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>
            <svg v-else width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            <span class="copy-label">{{ copiedReportKey === item.key ? "Copied" : "Copy" }}</span>
          </button>
        </div>
      </div>
    </template>

    <!-- Aktivitas berulang (mis. file yang sama dibaca beberapa kali) dibuat
         mudah dibaca sebagai satu baris ringkas. -->
    <div v-if="repeats.length" class="act-row repeat warn">
      <span class="act-time"></span>
      <span class="act-ico" aria-hidden="true">⚠</span>
      <span class="act-title">Repeated activity</span>
      <span v-for="(rep, ri) in repeats" :key="ri" class="act-repeat-item">{{ rep.name }} · read {{ rep.count }}×</span>
    </div>

    <!-- Live "Agent reasoning." — SATU elemen yang sama selama AETHER menunggu
         respons LLM. Titik dianimasikan oleh CSS (@keyframes), BUKAN timer JS,
         sehingga tidak ada interval yang tertinggal; elemen hanya ada saat
         isReasoning true. TIDAK menambah entri activity/log apa pun. -->
    <div
      v-if="isReasoning"
      class="act-row reasoning reasoning-indicator"
      role="status"
      aria-live="polite"
    >
      <span class="act-label reasoning">AGENT</span>
      <span class="act-body-col">
        <span class="act-text reasoning-text"><span class="act-ico" aria-hidden="true">🤔</span> Agent reasoning<span class="reasoning-dots" aria-hidden="true"><i>.</i><i>.</i><i>.</i></span></span>
      </span>
    </div>

    <div v-if="!empty && status === 'running'" class="log-line info">
      <span class="ts">&nbsp;</span><span class="cursor"></span>
    </div>
  </div>
</template>
