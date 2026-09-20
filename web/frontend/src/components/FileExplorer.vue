<script setup>
// File Explorer (#52 rework). Menampilkan file project aktif (read-only).
// Data dari ListFilesTool AETHER via gateway (#50). TIDAK ada abstraksi
// filesystem baru di frontend. Root = active project root (bukan ".").
import { computed, ref, watch } from "vue";
import { listFiles } from "../api.js";
// Node tree RECURSIVE (menggantikan rendering 2-level hard-coded). Komponen ini
// merender satu baris lalu memanggil dirinya sendiri untuk anak-anaknya, jadi
// kedalaman folder tidak lagi dibatasi di template.
import ExplorerTreeNode from "./ExplorerTreeNode.vue";

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
// Collapsible section (AETHER Workbench right column).
// Default: EXPLORER TERBUKA. State hanya di frontend selama sesi aktif.
const collapsed = ref(false);
function toggleCollapse() {
  collapsed.value = !collapsed.value;
}
// Context menu state.
const contextMenu = ref(null);
const contextOpen = ref(false);

// Nama folder internal AETHER yang disembunyikan dari UI Explorer.
const HIDDEN_NAMES = new Set([".aether"]);

function visibleEntries(list) {
  return (list || []).filter((e) => !HIDDEN_NAMES.has(e.name));
}

const rootLabel = computed(() => (props.project && props.project.name) || "project");

// --- File type icon / color mapping ---

const FILE_ICONS = {
  // Python
  py: "python",
  // HTML
  html: "html", htm: "html",
  // JavaScript
  js: "javascript", jsx: "javascript",
  // TypeScript
  ts: "typescript", tsx: "typescript",
  // Vue
  vue: "vue",
  // CSS
  css: "css", scss: "css", sass: "css",
  // JSON
  json: "json",
  // YAML
  yaml: "yaml", yml: "yaml",
  // Markdown
  md: "markdown", markdown: "markdown",
  // Text
  txt: "text",
  // XML
  xml: "xml",
  // SQL
  sql: "sql",
  // Shell
  sh: "shell", bash: "shell",
  // Windows batch
  bat: "batch", cmd: "batch",
  // PHP
  php: "php",
  // Java
  java: "java",
  // C/C++
  c: "cpp", h: "cpp", cpp: "cpp", hpp: "cpp",
  // C#
  cs: "csharp",
  // Go
  go: "go",
  // Rust
  rs: "rust",
  // Ruby
  rb: "ruby",
  // TOML
  toml: "toml",
  // INI / Config
  ini: "config", conf: "config",
  // Environment
  env: "env",
};

const FILE_COLORS = {
  python: "#6ee7b7",
  html: "#93c5fd",
  javascript: "#fde047",
  typescript: "#67e8f9",
  vue: "#6ee7b7",
  css: "#c4b5fd",
  json: "#fcd34d",
  yaml: "#f9a8d4",
  markdown: "#d4d4d4",
  text: "#9ca3af",
  xml: "#fb923c",
  sql: "#818cf8",
  shell: "#6ee7b7",
  batch: "#93c5fd",
  php: "#c084fc",
  java: "#fb7185",
  cpp: "#7dd3fc",
  csharp: "#c084fc",
  go: "#22d3ee",
  rust: "#fb923c",
  ruby: "#fb7185",
  toml: "#fb923c",
  config: "#9ca3af",
  env: "#6ee7b7",
};

function fileTypeIcon(name) {
  // Check special filenames first.
  const lower = name.toLowerCase();
  if (lower === "readme.md" || lower === "readme.markdown") return "markdown";
  if (lower === "license") return "license";
  if (lower === ".gitignore") return "gitignore";
  if (lower === ".env") return "env";
  if (lower === ".env.example") return "env-example";
  if (lower === "dockerfile") return "dockerfile";
  if (lower === "makefile") return "makefile";
  if (lower === "pyproject.toml") return "pyproject";
  if (lower === "package.json") return "package";
  if (lower === "requirements.txt") return "requirements";

  const dot = name.lastIndexOf(".");
  if (dot < 0) return "generic";
  const ext = name.slice(dot + 1).toLowerCase();
  return FILE_ICONS[ext] || "generic";
}

function fileTypeColor(name) {
  const icon = fileTypeIcon(name);
  return FILE_COLORS[icon] || null;
}

// Path helpers.
function childPath(base, name) {
  return base === "." || !base ? name : `${base}/${name}`;
}

function absoluteFromRel(rel) {
  if (!props.project) return rel;
  const root = props.project.root || props.project.path || "";
  if (!rel || rel === ".") return root;
  return `${root}/${rel}`;
}

// Explorer helpers.
async function load(path = ".") {
  if (!props.project) return;
  loading.value = true;
  error.value = "";
  try {
    const data = await listFiles(path);
    entries.value = visibleEntries(data.entries);
    currentPath.value = data.path || path;
  } catch (e) {
    error.value = e.message || "Gagal memuat file.";
    entries.value = [];
  } finally {
    loading.value = false;
  }
}

const emit = defineEmits(["open-file", "open-file-editor"]);

function openFile(fullPath, name) {
  selected.value = fullPath;
  emit("open-file", { path: fullPath, name });
}

// Expand/collapse SATU folder secara INDEPENDEN (map path -> anak).
// Tidak ada batas kedalaman: anak folder mana pun bisa di-expand/collapse.
async function toggleDirByPath(full) {
  if (expanded.value[full]) {
    const next = { ...expanded.value };
    delete next[full];
    expanded.value = next;
    return;
  }
  try {
    const data = await listFiles(full);
    expanded.value = { ...expanded.value, [full]: visibleEntries(data.entries) };
  } catch (e) {
    error.value = e.message || "Gagal memuat folder.";
  }
}

// Handler dari node tree recursive (ExplorerTreeNode).
function handleToggle(full) {
  toggleDirByPath(full);
}

function handleOpen(full, name) {
  openFile(full, name);
}

function up() {
  if (currentPath.value === "." || !currentPath.value) return;
  const parts = currentPath.value.split("/");
  parts.pop();
  load(parts.length ? parts.join("/") : ".");
}

// --- Context Menu ---
// fullPath = path penuh node (dari ExplorerTreeNode), jadi menu tetap benar
// pada kedalaman berapa pun (bukan dihitung dari root saja).
function onContextMenu(e, entry, fullPath, kind) {
  e.preventDefault();
  stopPropagation(e);
  const menuW = 180;
  const menuH = 280;
  let x = e.clientX;
  let y = e.clientY;
  if (x + menuW > window.innerWidth) x = window.innerWidth - menuW - 4;
  if (y + menuH > window.innerHeight) y = window.innerHeight - menuH - 4;
  if (x < 4) x = 4;
  if (y < 4) y = 4;
  contextMenu.value = { x, y, entry, type: kind, path: fullPath };
  contextOpen.value = true;
}

function stopPropagation(e) {
  e.stopPropagation();
}

function closeContextMenu() {
  contextOpen.value = false;
  contextMenu.value = null;
}

// Path penuh node yang jadi target context menu (dari ExplorerTreeNode).
function ctxTargetPath(entry) {
  const m = contextMenu.value;
  if (m && m.path) return m.path;
  return childPath(currentPath.value, entry.name);
}

function ctxOpen(entry) {
  const full = ctxTargetPath(entry);
  closeContextMenu();
  if (entry.type === 'dir') {
    toggleDirByPath(full);
  } else {
    openFile(full, entry.name);
  }
}

function ctxOpenWithEditor(entry) {
  const full = ctxTargetPath(entry);
  closeContextMenu();
  emit("open-file-editor", {
    path: full,
    name: entry.name,
  });
}

function ctxCopyPath(entry) {
  const rel = ctxTargetPath(entry);
  closeContextMenu();
  navigator.clipboard.writeText(rel).catch(() => {});
}

function ctxCopyFullPath(entry) {
  const abs = absoluteFromRel(ctxTargetPath(entry));
  closeContextMenu();
  navigator.clipboard.writeText(abs).catch(() => {});
}

function ctxReveal(entry) {
  const abs = absoluteFromRel(ctxTargetPath(entry));
  closeContextMenu();
  revealInExplorer(abs);
}

async function revealInExplorer(path) {
  try {
    const resp = await fetch("/api/reveal-in-explorer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      error.value = data.error?.message || "Gagal membuka Explorer.";
    }
  } catch (e) {
    error.value = e.message || "Gagal membuka Explorer.";
  }
}

function ctxRename(entry) {
  closeContextMenu();
  error.value = "Rename is not yet available.";
}

function ctxDelete(entry) {
  const rel = ctxTargetPath(entry);
  closeContextMenu();
  if (!confirm(`Hapus "${entry.name}" dari project?`)) return;
  deleteEntry(rel, entry.type);
}

async function deleteEntry(relPath, type) {
  try {
    const resp = await fetch("/api/delete-entry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: relPath, type }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      error.value = data.error?.message || "Gagal menghapus.";
    } else {
      await load(currentPath.value);
    }
  } catch (e) {
    error.value = e.message || "Gagal menghapus.";
  }
}

// Close context menu on outside click / escape.
function onDocumentClick(e) {
  if (contextOpen.value && !e.target.closest(".ctx-menu")) {
    closeContextMenu();
  }
}

function onDocumentKeydown(e) {
  if (e.key === "Escape" && contextOpen.value) {
    closeContextMenu();
  }
}

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
  <section class="block explorer-block" :class="{ collapsed }">
    <div class="ex-head" @click="toggleCollapse">
      <span class="sec-caret" aria-hidden="true">{{ collapsed ? "▸" : "▾" }}</span>
      <span class="ex-head-ico">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
      </span>
      <span class="ex-head-title">EXPLORER</span>
      <span class="ex-head-ws" :title="currentPath === '.' ? rootLabel : currentPath">{{ currentPath === "." ? rootLabel : currentPath }}</span>
      <button class="ex-icon-btn" type="button" title="Refresh" @click.stop="load(currentPath)">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5"/></svg>
      </button>
      <button v-if="currentPath !== '.'" class="ex-icon-btn" type="button" title="Up" @click.stop="up">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>
      </button>
    </div>

    <div v-show="!collapsed" class="explorer" @click="closeContextMenu" @contextmenu.prevent>
      <div v-if="loading" class="ex-empty">Loading…</div>
      <div v-else-if="error" class="ex-empty ex-err">{{ error }}</div>
      <div v-else-if="!entries.length" class="ex-empty">Empty.</div>

      <!-- Tree recursive: satu komponen node merender dirinya sendiri pada
           kedalaman berapa pun (tidak ada batas level). -->
      <template v-else>
        <ExplorerTreeNode
          v-for="e in entries"
          :key="e.name"
          :entry="e"
          :path="childPath(currentPath, e.name)"
          :depth="0"
          :dirs="expanded"
          :selected="selected"
          :icon-fn="fileTypeIcon"
          :color-fn="fileTypeColor"
          @toggle="handleToggle"
          @open="handleOpen"
          @context="onContextMenu"
        />
      </template>
    </div>

    <!-- Context Menu -->
    <div
      v-if="contextOpen && contextMenu"
      class="ctx-menu"
      :style="{ left: contextMenu.x + 'px', top: contextMenu.y + 'px' }"
      @click.stop
      @contextmenu.prevent
    >
      <template v-if="contextMenu.type === 'file'">
        <div class="ctx-item" @click="ctxOpen(contextMenu.entry)">Open</div>
        <div class="ctx-item" @click="ctxOpenWithEditor(contextMenu.entry)">Open with Editor</div>
        <div class="ctx-sep"></div>
        <div class="ctx-item" @click="ctxCopyPath(contextMenu.entry)">Copy Path</div>
        <div class="ctx-item" @click="ctxCopyFullPath(contextMenu.entry)">Copy Full Path</div>
        <div class="ctx-sep"></div>
        <div class="ctx-item" @click="ctxReveal(contextMenu.entry)">Reveal in Explorer</div>
        <div class="ctx-sep"></div>
        <div class="ctx-item" @click="ctxRename(contextMenu.entry)">Rename</div>
        <div class="ctx-item ctx-danger" @click="ctxDelete(contextMenu.entry)">Delete</div>
      </template>
      <template v-else-if="contextMenu.type === 'folder'">
        <div class="ctx-item" @click="ctxOpen(contextMenu.entry)">Open</div>
        <div class="ctx-sep"></div>
        <div class="ctx-item" @click="ctxCopyPath(contextMenu.entry)">Copy Path</div>
        <div class="ctx-item" @click="ctxCopyFullPath(contextMenu.entry)">Copy Full Path</div>
        <div class="ctx-sep"></div>
        <div class="ctx-item" @click="ctxReveal(contextMenu.entry)">Reveal in Explorer</div>
        <div class="ctx-sep"></div>
        <div class="ctx-item" @click="ctxRename(contextMenu.entry)">Rename</div>
        <div class="ctx-item ctx-danger" @click="ctxDelete(contextMenu.entry)">Delete</div>
      </template>
    </div>
  </section>
</template>
