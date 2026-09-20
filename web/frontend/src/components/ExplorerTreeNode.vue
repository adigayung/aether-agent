<script setup>
// ExplorerTreeNode (#explorer-recursive): satu node baris Explorer AETHER.
//
// Komponen ini MERENDER SATU baris (folder/file) lalu MERENDER ANAK-ANAKNYA
// SECARA RECURSIVE lewat dirinya sendiri (implicit self-reference by filename).
// Dengan ini, kedalaman tree TIDAK dibatasi di template (dulu hanya 2 level
// hard-coded di FileExplorer.vue).
//
// Sumber state tetap SATU: `dirs` (map path -> daftar anak) dan `selected`
// dikelola oleh FileExplorer.vue (single source of truth). Node ini hanya
// presentasional + memancarkan event ke parent.
import { computed } from "vue";

// Nama eksplisit agar self-reference recursive (implicit) tetap dapat diandalkan.
defineOptions({ name: "ExplorerTreeNode" });

const props = defineProps({
  // Entri ini: { name, type: 'dir'|'file' }.
  entry: { type: Object, required: true },
  // Path penuh entri ini relatif root project (mis. "src/agent/tools").
  path: { type: String, required: true },
  // Kedalaman (untuk indentasi) — 0 untuk level root.
  depth: { type: Number, default: 0 },
  // Map path -> daftar anak (folder yang sedang di-expand).
  dirs: { type: Object, default: () => ({}) },
  // Path node yang sedang terpilih.
  selected: { type: String, default: "" },
  // Helper tipe/indikasi ikon file (dimiliki FileExplorer, bukan diduplikasi).
  iconFn: { type: Function, required: true },
  colorFn: { type: Function, required: true },
});

const emit = defineEmits(["toggle", "open", "context"]);

const isDir = computed(() => props.entry.type === "dir");
const isOpen = computed(() => isDir.value && !!props.dirs[props.path]);
const children = computed(() => props.dirs[props.path] || []);

// Indentasi per kedalaman. Level 1 = 30px agar konsisten dengan tampilan lama.
const indentStyle = computed(() => ({ paddingLeft: `${8 + props.depth * 22}px` }));

function joinPath(base, name) {
  return base ? `${base}/${name}` : name;
}

function onClick() {
  if (isDir.value) emit("toggle", props.path);
  else emit("open", props.path, props.entry.name);
}

function onContext(e) {
  emit("context", e, props.entry, props.path, isDir.value ? "folder" : "file");
}
</script>

<template>
  <div
    class="ex-row"
    :class="{
      'ex-folder': isDir,
      selected: selected === path,
      'ex-active': selected === path && !isDir,
    }"
    :style="indentStyle"
    @click="onClick"
    @contextmenu.prevent="onContext($event)"
  >
    <span class="ex-caret">{{ isDir ? (isOpen ? '▾' : '▸') : '' }}</span>
    <span class="ex-ico" :class="isDir ? 'folder' : 'file'">
      <!-- Folder icon SVG -->
      <svg v-if="isDir" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>

      <!-- File type SVG icons -->
      <svg v-else-if="iconFn(entry.name) === 'python'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'html'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 8h4v8H7zM13 8h4v8h-4z" fill="currentColor" opacity="0.3"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'javascript'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 22 8.5 22 15.5 12 22 2 15.5 2 8.5 12 2" fill="currentColor" opacity="0.2"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'typescript'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16v16H4z" stroke-dasharray="2 2"/><path d="M8 12h4v4M8 12h4M12 12h4v4M12 12h4"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'vue'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3L2 9l10 6 10-6-10-6z"/><path d="M2 15l10 6 10-6M2 9l10 6 10-6"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'css'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 8h4v8H7zM13 8h4v8h-4z" fill="currentColor" opacity="0.3"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'json'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3h8l2 4-2 4h-4l2-4h-4l2 4h-4l-2-4z"/><path d="M8 13h8l2 4-2 4h-4l2-4h-4l2 4h-4l-2-4z"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'yaml'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16v16H4z" stroke-dasharray="2 2"/><path d="M8 8h8M8 12h8M8 16h8"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'markdown'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 8v8M12 8v8M17 8v8" stroke-dasharray="1.5 1.5"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'text'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 8h10M7 12h10M7 16h6"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'xml'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M12 8l-4 4h8l-4 4"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'sql'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3h8v18H8z"/><path d="M11 8h2v8h-2zM8 11h2v2H8z"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'shell'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16v16H4z" stroke-dasharray="2 2"/><path d="M8 12h8M8 10h2v4h-2z"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'batch'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16v16H4z"/><path d="M8 10h8M8 13h6M8 16h4"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'php'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3h8v18H8z"/><path d="M12 8v8"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'java'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v12M6 12h12"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'cpp'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 8h8v8H8z" fill="currentColor" opacity="0.15"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'csharp'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 9-9 9-9-9 9-9z"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'go'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 12h8M12 8v8"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'rust'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6l4 6-4 6-4-6 4-6z" fill="currentColor" opacity="0.2"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'ruby'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 8l4 8M16 8l-4 8"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'toml'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 8h10M7 12h10M7 16h10"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'config'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'env'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 12h8M12 8v8"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'license'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 8h8M8 12h6M8 16h4"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'gitignore'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 8l8 8M16 8l-8 8"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'env-example'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 12h8M12 8v8"/><path d="M8 8h8" stroke-dasharray="1.5 1.5"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'dockerfile'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 8h8v8H8z" fill="currentColor" opacity="0.15"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'makefile'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 10h10M7 14h6"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'pyproject'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 8h8v8H8z" fill="currentColor" opacity="0.15"/><path d="M8 11h8"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'package'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M12 6v12M6 12h12"/></svg>
      <svg v-else-if="iconFn(entry.name) === 'requirements'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 8h4v4h-4zM12 8h4v4h-4zM8 12h4v4h-4zM12 12h4v4h-4z"/></svg>
      <!-- Generic file icon -->
      <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
    </span>
    <span
      class="ex-name"
      :style="{ color: !isDir ? (colorFn(entry.name) || 'inherit') : 'inherit' }"
      :title="entry.name"
    >{{ entry.name }}</span>
  </div>

  <!-- Anak folder yang di-expand — recursion tanpa batas kedalaman. -->
  <template v-if="isOpen">
    <ExplorerTreeNode
      v-for="c in children"
      :key="joinPath(path, c.name)"
      :entry="c"
      :path="joinPath(path, c.name)"
      :depth="depth + 1"
      :dirs="dirs"
      :selected="selected"
      :icon-fn="iconFn"
      :color-fn="colorFn"
      @toggle="(...a) => emit('toggle', ...a)"
      @open="(...a) => emit('open', ...a)"
      @context="(...a) => emit('context', ...a)"
    />
  </template>
</template>
