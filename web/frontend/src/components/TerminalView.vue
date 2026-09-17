<script setup>
// Terminal viewer (#52 rework). HANYA viewer output agent, BUKAN terminal
// interaktif. Tidak ada eksekusi command di frontend.
// Hanya muncul bila ada output (observability detail, bukan pusat UI).
//
// Menampilkan tool + target (path/query/command) dari payload event AETHER.
// TIDAK menampilkan isi file panjang. TIDAK hardcode nama file.
import { computed, nextTick, ref, watch } from "vue";

// Rolling window: tampilkan maksimal 20 entry/output terbaru.
const MAX_ENTRIES = 20;

const props = defineProps({
  lines: { type: Array, default: () => [] },
});

// Container scroll Terminal (bukan halaman utama).
const scroller = ref(null);

// Rolling window: hanya 20 entry terbaru (entry terbaru - 19 ... terbaru).
const visibleLines = computed(() => props.lines.slice(-MAX_ENTRIES));

// Kelas warna baris (memetakan kind -> gaya log template).
function lineClass(line) {
  if (line.kind === "call") return "run";
  if (line.kind === "result") return line.success ? "ok" : "err";
  return "warn";
}

// Auto-scroll ke output terbaru setelah DOM selesai diperbarui.
async function scrollToLatest() {
  await nextTick();
  const el = scroller.value;
  if (el) el.scrollTop = el.scrollHeight;
}

// Auto-scroll setiap kali output baru masuk (event SSE async).
watch(visibleLines, scrollToLatest, { flush: "post" });
</script>

<template>
  <div v-if="visibleLines.length" ref="scroller" class="term-body">
    <div v-for="(line, i) in visibleLines" :key="i" class="log-line" :class="lineClass(line)">
      <!-- Tool call: nama tool + target. -->
      <template v-if="line.kind === 'call'">
        <span class="msg"
          ><span class="fn">{{ line.tool }}</span
          ><template v-if="line.target"> {{ line.target }}</template></span
        >
      </template>
      <!-- Tool result: status + target. -->
      <template v-else-if="line.kind === 'result'">
        <span class="msg"
          >{{ line.success ? "✓" : "✗" }} {{ line.target || line.tool
          }}<template v-if="!line.success && line.error"> — {{ line.error }}</template></span
        >
      </template>
      <!-- Catatan sistem (recovery, dll). -->
      <template v-else>
        <span class="msg">{{ line.text }}</span>
      </template>
    </div>
  </div>
</template>


