<script setup>
// Changes / Diff + Result. Data dari event SSE AETHER.
// TIDAK ada diff engine di frontend: hanya menampilkan perubahan yang
// dilaporkan AETHER. Diff detail ditampilkan bila payload menyediakannya.
import { computed, ref } from "vue";

const props = defineProps({
  changes: { type: Array, default: () => [] },
  validation: { type: Object, default: () => ({}) },
});

const emit = defineEmits(["open-file"]);

// Collapsible section (AETHER Workbench right column).
// Default: CHANGES tertutup. State hanya di frontend selama sesi aktif.
const collapsed = ref(true);
function toggleCollapse() {
  collapsed.value = !collapsed.value;
}

// Accordion: hanya SATU baris terbuka pada satu waktu (index terpilih).
// -1 = semua tertutup (diff TIDAK dirender secara default).
const expandedIndex = ref(-1);
function toggleRow(i) {
  expandedIndex.value = expandedIndex.value === i ? -1 : i;
}

// Klasifikasi kind perubahan (created/added/new, deleted/removed, modified/...).
function kindOf(c) {
  return (c.kind || "change").toLowerCase();
}
function tagClass(c) {
  const k = kindOf(c);
  if (k.includes("creat") || k.includes("add") || k.includes("new")) return "created";
  if (k.includes("delet") || k.includes("remov")) return "failed";
  if (k.includes("move") || k.includes("renam")) return "modified";
  if (k.includes("modif") || k.includes("edit") || k.includes("updat")) return "modified";
  return "idle";
}
function tagLabel(c) {
  const k = kindOf(c);
  if (k.includes("creat") || k.includes("add") || k.includes("new")) return "added";
  if (k.includes("delet") || k.includes("remov")) return "removed";
  if (k.includes("move")) return "moved";
  if (k.includes("renam")) return "renamed";
  if (k.includes("modif") || k.includes("edit") || k.includes("updat")) return "modified";
  return k;
}
// Kode pendek untuk indikator status pada kartu satu-baris.
function tagCode(c) {
  const k = kindOf(c);
  if (k.includes("move") || k.includes("renam")) return "R";
  const cls = tagClass(c);
  if (cls === "created") return "A";
  if (cls === "failed") return "D";
  if (cls === "modified") return "M";
  return "•";
}

// Judul baris: path + asal (untuk move/rename).
function rowTitle(c) {
  const base = c.path || c.detail || "(unknown)";
  return c.old_path ? `${base} (dari ${c.old_path})` : base;
}

const validationLabel = computed(() => {
  const s = props.validation.state;
  if (s === "ok") return "Passed";
  if (s === "err") return "Failed";
  if (s === "running") return "Running";
  return "Pending";
});

const validationClass = computed(() => {
  const s = props.validation.state;
  if (s === "ok") return "ok";
  if (s === "err") return "err";
  if (s === "running") return "running";
  return "";
});
</script>

<template>
  <section class="block changes-block" :class="{ collapsed }">
    <div class="block-head" @click="toggleCollapse">
      <span class="sec-caret" aria-hidden="true">{{ collapsed ? "▸" : "▾" }}</span>
      <div class="block-title">Changes</div>
      <span class="chip chip-sm">{{ changes.length }} files</span>
    </div>

    <!-- Body = SATU scroll owner (meniru pola .ex-head/.explorer): header tetap,
         kartu perubahan + diff + result mengalir & scroll di area ini saja.
         Tidak ada scrollbox bersarang di dalamnya. -->
    <div v-show="!collapsed" class="changes-body">
      <div v-if="!changes.length" class="ex-empty">No changes yet.</div>

      <template v-else>
        <div class="changes-list">
          <template v-for="(c, i) in changes" :key="i">
            <!-- Kartu satu baris: caret + status + nama file (ellipsis) + +/- -->
            <div class="file-row" :class="{ open: expandedIndex === i }" @click="toggleRow(i)">
              <span class="file-caret" aria-hidden="true">{{ expandedIndex === i ? "▾" : "▸" }}</span>
              <span class="file-status" :class="tagClass(c)" :title="tagLabel(c)">{{ tagCode(c) }}</span>
              <span class="file-name" :title="rowTitle(c)">{{ c.path || c.detail || "(unknown)" }}</span>
              <span class="file-stat">
                <span v-if="c.additions != null" class="st-add">+{{ c.additions }}</span>
                <span v-if="c.deletions != null" class="st-del">-{{ c.deletions }}</span>
              </span>
              <button class="file-edit" type="button" title="Edit" @click.stop="emit('open-file', c)">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>
              </button>
            </div>

            <!-- Diff muncul hanya saat baris diklik (accordion), dan mengalir di
                 dalam scroll owner .changes-body (bukan scrollbox kedua). -->
            <div v-if="expandedIndex === i" class="file-diff">
              <div v-if="c.diff" class="file-diff-body"><pre>{{ c.diff }}</pre></div>
              <div v-else class="file-diff-note">Diff detail is not available from AETHER.</div>
            </div>
          </template>
        </div>

        <!-- Result ringkas. -->
        <div class="cp-result">
          <span class="cp-result-label">Result</span>
          <span class="cp-result-val">
            {{ changes.length }} file(s) changed
            <span class="cp-result-validation" :class="validationClass">· Validation {{ validationLabel }}</span>
          </span>
        </div>
      </template>
    </div>
  </section>
</template>
