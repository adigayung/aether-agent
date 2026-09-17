<script setup>
// File Explorer (#52 rework). Menampilkan file project aktif (read-only).
// Data dari ListFilesTool AETHER via gateway (#50). TIDAK ada abstraksi
// filesystem baru di frontend. Root = active project root (bukan ".").
import { computed, ref, watch } from "vue";
import { listFiles } from "../api.js";

const props = defineProps({
  project: { type: Object, default: null },
  // Penanda refresh dari parent (mis. setelah agent selesai membuat file).
  refreshKey: { type: Number, default: 0 },
});

const entries = ref([]);
const currentPath = ref(".");
const loading = ref(false);
const error = ref("");
// Folder yang di-expand: map path -> entries anak.
const expanded = ref({});
// Path file terpilih (selected state).
const selected = ref("");

// Label root = nama project (bukan ".").
const rootLabel = computed(() => (props.project && props.project.name) || "project");

// Icon folder/file dirender inline sebagai SVG di template (gaya IDE tree).

async function load(path = ".") {
  if (!props.project) return;
  loading.value = true;
  error.value = "";
  try {
    const data = await listFiles(path);
    entries.value = data.entries || [];
    currentPath.value = data.path || path;
  } catch (e) {
    error.value = e.message || "Gagal memuat file.";
    entries.value = [];
  } finally {
    loading.value = false;
  }
}

// Path relatif untuk sebuah entry di dalam direktori `base`.
function childPath(base, name) {
  return base === "." || !base ? name : `${base}/${name}`;
}

// Expand/collapse folder (tree sederhana, seperti VS Code).
async function toggleDir(entry) {
  await toggleDirByPath(childPath(currentPath.value, entry.name));
}
// Klik file: buka editor/modal (disediakan parent via event).
const emit = defineEmits(["open-file"]);
function openFile(fullPath, name) {
  selected.value = fullPath;
  emit("open-file", { path: fullPath, name });
}

// Klik entry anak (di dalam folder yang di-expand).
function openChild(parentFull, child) {
  const full = `${parentFull}/${child.name}`;
  if (child.type === "dir") {
    toggleDirByPath(full);
  } else {
    openFile(full, child.name);
  }
}

// Toggle folder berdasarkan path lengkap (dipakai untuk anak).
async function toggleDirByPath(full) {
  if (expanded.value[full]) {
    const next = { ...expanded.value };
    delete next[full];
    expanded.value = next;
    return;
  }
  try {
    const data = await listFiles(full);
    expanded.value = { ...expanded.value, [full]: data.entries || [] };
  } catch (e) {
    error.value = e.message || "Gagal memuat folder.";
  }
}

function up() {
  if (currentPath.value === "." || !currentPath.value) return;
  const parts = currentPath.value.split("/");
  parts.pop();
  load(parts.length ? parts.join("/") : ".");
}
// Muat ulang saat project aktif berubah atau refresh diminta.
watch(
  () => [props.project && props.project.id, props.refreshKey],
  () => {
    expanded.value = {};
    load(".");
  },
  { immediate: true }
);
</script>

<template>
  <section class="block explorer-block">
    <div class="ex-head">
      <span class="ex-head-ico">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
      </span>
      <span class="ex-head-title">EXPLORER</span>
      <span class="ex-head-ws" :title="currentPath === '.' ? rootLabel : currentPath">{{ currentPath === "." ? rootLabel : currentPath }}</span>
      <button class="ex-icon-btn" type="button" title="Refresh" @click="load(currentPath)">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5"/></svg>
      </button>
      <button v-if="currentPath !== '.'" class="ex-icon-btn" type="button" title="Up" @click="up">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>
      </button>
    </div>

    <div class="explorer">
      <div v-if="loading" class="ex-empty">Loading…</div>
      <div v-else-if="error" class="ex-empty ex-err">{{ error }}</div>
      <div v-else-if="!entries.length" class="ex-empty">Empty.</div>

      <template v-else>
        <template v-for="e in entries" :key="e.name">
          <div
            class="ex-row"
            :class="{ 'ex-folder': e.type === 'dir', selected: selected === childPath(currentPath, e.name) }"
            @click="e.type === 'dir' ? toggleDir(e) : openFile(childPath(currentPath, e.name), e.name)"
          >
            <span class="ex-caret">{{ e.type === "dir" ? (expanded[childPath(currentPath, e.name)] ? "▾" : "▸") : "" }}</span>
            <span class="ex-ico" :class="e.type === 'dir' ? 'folder' : 'file'">
              <svg v-if="e.type === 'dir'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
              <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
            </span>
            <span class="ex-name">{{ e.name }}</span>
          </div>

          <!-- Anak folder yang di-expand (tree sederhana). -->
          <div
            v-for="c in expanded[childPath(currentPath, e.name)] || []"
            :key="childPath(currentPath, e.name) + '/' + c.name"
            class="ex-row ex-child"
            :class="{ 'ex-folder': c.type === 'dir', selected: selected === childPath(currentPath, e.name) + '/' + c.name }"
            @click="openChild(childPath(currentPath, e.name), c)"
          >
            <span class="ex-caret">{{ c.type === "dir" ? "▸" : "" }}</span>
            <span class="ex-ico" :class="c.type === 'dir' ? 'folder' : 'file'">
              <svg v-if="c.type === 'dir'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
              <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
            </span>
            <span class="ex-name">{{ c.name }}</span>
          </div>
        </template>
      </template>
  </div>
  </section>
</template>

