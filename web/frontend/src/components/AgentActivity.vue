<script setup>
// Agent Activity (#activity rework).
//
// Unified chronological timeline untuk satu Task. Menampilkan event AETHER
// existing sebagai satu alur:
//
//   [time] AGENT       commentary
//   [time] TOOL        tool call
//   [time] RESULT      tool result
//   [time] OBSERVATION observation
//
// Sumber event bisa berupa:
//   - live SSE (#51): { event_type, payload, timestamp, ... }
//   - Activity API/persistent log: { event, data, timestamp, ... }
// Keduanya dinormalisasi di sini (bentuk payload sama, hanya kunci berbeda).
//
// Bukan terminal mentah: observation ditampilkan sebagai ringkasan (bukan dump
// JSON panjang). Semua event tetap berasal dari sistem yang sudah ada.
import { computed, nextTick, ref, watch } from "vue";

const props = defineProps({
  events: { type: Array, default: () => [] },
  status: { type: String, default: "idle" },
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

// Ringkas observation agar tidak menampilkan raw JSON panjang.
function summarizeObservation(content) {
  if (content == null) return "ok";
  if (typeof content === "string") {
    const t = content.trim().replace(/\s+/g, " ");
    return t.length > 140 ? `${t.slice(0, 140)}…` : t || "ok";
  }
  if (typeof content === "object") {
    if (typeof content.count === "number") {
      const scope = content.query || content.path || "";
      return `returned ${content.count} item(s)${scope ? ` for ${scope}` : ""}`;
    }
    if (Array.isArray(content.matches)) {
      return `${content.matches.length} match(es)`;
    }
    const keys = Object.keys(content);
    return keys.length ? `result: ${keys.slice(0, 6).join(", ")}` : "ok";
  }
  return String(content);
}

// Bangun timeline chronological dari event (commentary, tool, result, obs).
const timeline = computed(() => {
  const items = [];
  props.events.forEach((raw, i) => {
    const e = normalize(raw);
    const d = e.data || {};
    switch (e.type) {
      case "task_started":
        items.push({ key: i, kind: "status", label: "AGENT", text: "Task started", ts: e.ts });
        break;
      case "agent_commentary": {
        const text = (d.text || "").trim();
        if (text) items.push({ key: i, kind: "agent", label: "AGENT", text, ts: e.ts });
        break;
      }
      case "tool_called":
        items.push({ key: i, kind: "tool", label: "TOOL", tool: d.tool || "", target: d.target || "", ts: e.ts });
        break;
      case "tool_completed":
        items.push({
          key: i,
          kind: "result",
          label: "RESULT",
          tool: d.tool || "",
          target: d.target || "",
          success: d.success !== false,
          error: d.error || "",
          ts: e.ts,
        });
        break;
      case "observation_received":
        items.push({
          key: i,
          kind: "observation",
          label: "OBSERVATION",
          tool: d.tool || "",
          success: d.success !== false,
          summary: summarizeObservation(d.content),
          ts: e.ts,
        });
        break;
      case "task_completed":
        items.push({ key: i, kind: "status", label: "AGENT", text: "Task completed", ts: e.ts });
        break;
      case "task_failed":
        items.push({ key: i, kind: "status", label: "AGENT", text: "Task failed", ts: e.ts });
        break;
      case "task_cancelled":
        items.push({ key: i, kind: "status", label: "AGENT", text: "Task cancelled", ts: e.ts });
        break;
      default:
        break;
    }
  });
  return items;
});

const empty = computed(() => !timeline.value.length);

watch(timeline, scrollToLatest, { flush: "post" });
</script>

<template>
  <div ref="scroller" class="term-body act-body">
    <div v-if="empty" class="log-line info">
      <span class="msg">
        <template v-if="status === 'running'">AETHER is working…</template>
        <template v-else>Give AETHER a task to begin. Its activity will appear here.</template>
      </span>
    </div>

    <div v-for="item in timeline" :key="item.key" class="act-row" :class="item.kind">
      <span class="act-label" :class="item.kind">{{ item.label }}</span>
      <span class="act-body-col">
        <span v-if="item.ts" class="act-ts">[{{ item.ts }}]</span>
        <!-- commentary -->
        <span v-if="item.kind === 'agent' || item.kind === 'status'" class="act-text">{{ item.text }}</span>
        <!-- tool call -->
        <span v-else-if="item.kind === 'tool'" class="act-text">
          <span class="fn">{{ item.tool }}</span><template v-if="item.target"> <span class="act-target">{{ item.target }}</span></template>
        </span>
        <!-- tool result -->
        <span v-else-if="item.kind === 'result'" class="act-text" :class="item.success ? 'ok' : 'err'">
          {{ item.success ? "✓" : "✗" }} {{ item.tool }}<template v-if="item.target"> <span class="act-target">{{ item.target }}</span></template><template v-if="!item.success && item.error"> — {{ item.error }}</template>
        </span>
        <!-- observation -->
        <span v-else-if="item.kind === 'observation'" class="act-text">
          {{ item.tool }} → {{ item.summary }}
        </span>
      </span>
    </div>

    <div v-if="!empty && status === 'running'" class="log-line info">
      <span class="ts">&nbsp;</span><span class="cursor"></span>
    </div>
  </div>
</template>
