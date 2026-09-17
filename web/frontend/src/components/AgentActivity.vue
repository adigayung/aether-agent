<script setup>
// Agent Activity (#52 rework). BUKAN terminal, BUKAN code editor.
//
// Activity HANYA menampilkan commentary natural dari LLM (event
// `agent_commentary`). TIDAK menampilkan prompt/task user, nama tool, status
// tool, target/path, command, hasil command, atau technical event apa pun.
// TIDAK mengarang teks dari nama tool: bila tidak ada commentary, tampilkan
// empty state sederhana.
//
// Semua informasi teknis eksekusi ada di Terminal (dipisah total).
import { computed, nextTick, ref, watch } from "vue";

// Rolling window: tampilkan maksimal 20 commentary terbaru.
const MAX_ITEMS = 20;

const props = defineProps({
  events: { type: Array, default: () => [] },
  status: { type: String, default: "idle" },
});

// Container scroll Activity (bukan halaman utama).
const scroller = ref(null);

// Auto-scroll ke commentary terbaru setelah DOM selesai diperbarui.
async function scrollToLatest() {
  await nextTick();
  const el = scroller.value;
  if (el) el.scrollTop = el.scrollHeight;
}

// Waktu event (bila tersedia dari payload) untuk prefix log. Tidak mengarang.
function eventTime(e) {
  const raw = e.timestamp ?? e.ts ?? e.created_at ?? e.time ?? null;
  if (raw == null) return "";
  if (typeof raw === "number") {
    const d = new Date(raw < 1e12 ? raw * 1000 : raw);
    return Number.isNaN(d.getTime()) ? "" : d.toTimeString().slice(0, 8);
  }
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? "" : d.toTimeString().slice(0, 8);
}

// HANYA commentary LLM (event agent_commentary). Tidak ada technical event.
const lines = computed(() =>
  props.events
    .filter((e) => e.event_type === "agent_commentary")
    .map((e) => {
        const text = (e.payload && e.payload.text) || "";
      if (!text.trim()) return null;
      return { id: e.event_id || e.sequence, text, ts: eventTime(e) };
    })
    .filter(Boolean)
    // Rolling window: hanya 20 commentary terbaru.
    .slice(-MAX_ITEMS)
);

const idle = computed(() => !lines.value.length);

// Auto-scroll setiap kali daftar commentary berubah (event SSE masuk async).
watch(lines, scrollToLatest, { flush: "post" });
</script>

<template>
  <div ref="scroller" class="term-body">
    <div v-if="idle" class="log-line info">
      <span class="msg">
        <template v-if="status === 'running'">AETHER is working…</template>
        <template v-else>Give AETHER a task to begin. It will explain its work here.</template>
      </span>
    </div>

    <!-- Commentary LLM (satu-satunya konten Activity). -->
    <div v-for="line in lines" :key="line.id" class="log-line info">
      <span v-if="line.ts" class="ts">[{{ line.ts }}]</span>
      <span class="msg">{{ line.text }}</span>
    </div>
    <div v-if="!idle && status === 'running'" class="log-line info">
      <span class="ts">&nbsp;</span><span class="cursor"></span>
    </div>
  </div>
</template>


