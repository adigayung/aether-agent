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

function isSpecialFile(name) {
  const lower = name.toLowerCase();
  return (
    lower === "readme.md" ||
    lower === "readme.markdown" ||
    lower === "license" ||
    lower === ".gitignore" ||
    lower === ".env" ||
    lower === ".env.example" ||
    lower === "dockerfile" ||
    lower === "makefile" ||
    lower === "pyproject.toml" ||
    lower === "package.json" ||
    lower === "requirements.txt"
  );
}

// Path helpers.
function childPath(base, name) {
  return base === "." || !base ? name : `${base}/${name}`;
}

function absolutePath(base, name) {
  if (!props.project) return name;
  const root = props.project.root || props.project.path || "";
  const rel = childPath(base, name);
  if (rel === ".") return root;
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

async function toggleDir(entry) {
  await toggleDirByPath(childPath(currentPath.value, entry.name));
}

const emit = defineEmits(["open-file", "open-file-editor"]);
function openFile(fullPath, name) {
  selected.value = fullPath;
  emit("open-file", { path: fullPath, name });
}

function openChild(parentFull, child) {
  const full = `${parentFull}/${child.name}`;
  if (child.type === "dir") {
    toggleDirByPath(full);
  } else {
    openFile(full, child.name);
  }
}

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

function up() {
  if (currentPath.value === "." || !currentPath.value) return;
  const parts = currentPath.value.split("/");
  parts.pop();
  load(parts.length ? parts.join("/") : ".");
}

// --- Context Menu ---
function onContextMenu(e, entry, type) {
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
  contextMenu.value = { x, y, entry, type };
  contextOpen.value = true;
}

function stopPropagation(e) {
  e.stopPropagation();
}

function closeContextMenu() {
  contextOpen.value = false;
  contextMenu.value = null;
}

function ctxOpen(entry) {
  closeContextMenu();
  if (entry.type === 'dir') {
    toggleDirByPath(childPath(currentPath.value, entry.name));
  } else {
    openFile(childPath(currentPath.value, entry.name), entry.name);
  }
}

function ctxOpenWithEditor(entry) {
  closeContextMenu();
  emit("open-file-editor", {
    path: childPath(currentPath.value, entry.name),
    name: entry.name,
  });
}

function ctxCopyPath(entry) {
  closeContextMenu();
  const rel = childPath(currentPath.value, entry.name);
  navigator.clipboard.writeText(rel).catch(() => {});
}

function ctxCopyFullPath(entry) {
  closeContextMenu();
  const abs = absolutePath(currentPath.value, entry.name);
  navigator.clipboard.writeText(abs).catch(() => {});
}

function ctxReveal(entry) {
  closeContextMenu();
  const abs = absolutePath(currentPath.value, entry.name);
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
  closeContextMenu();
  const rel = childPath(currentPath.value, entry.name);
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

    <div class="explorer" @click="closeContextMenu" @contextmenu.prevent>
      <div v-if="loading" class="ex-empty">Loading…</div>
      <div v-else-if="error" class="ex-empty ex-err">{{ error }}</div>
      <div v-else-if="!entries.length" class="ex-empty">Empty.</div>

      <template v-else>
        <template v-for="e in entries" :key="e.name">
          <div
            class="ex-row"
            :class="{ 'ex-folder': e.type === 'dir', selected: selected === childPath(currentPath, e.name), 'ex-active': selected === childPath(currentPath, e.name) && e.type === 'file' }"
            @click="e.type === 'dir' ? toggleDir(e) : openFile(childPath(currentPath, e.name), e.name)"
            @contextmenu.prevent="onContextMenu($event, e, e.type === 'dir' ? 'folder' : 'file')"
          >
            <span class="ex-caret">{{ e.type === 'dir' ? (expanded[childPath(currentPath, e.name)] ? '▾' : '▸') : '' }}</span>
            <span class="ex-ico" :class="e.type === 'dir' ? 'folder' : 'file'">
              <!-- Folder icon SVG -->
              <svg v-if="e.type === 'dir'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>

              <!-- File type SVG icons -->
              <svg v-else-if="fileTypeIcon(e.name) === 'python'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'html'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 8h4v8H7zM13 8h4v8h-4z" fill="currentColor" opacity="0.3"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'javascript'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 22 8.5 22 15.5 12 22 2 15.5 2 8.5 12 2" fill="currentColor" opacity="0.2"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'typescript'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16v16H4z" stroke-dasharray="2 2"/><path d="M8 12h4v4M8 12h4M12 12h4v4M12 12h4"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'vue'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3L2 9l10 6 10-6-10-6z"/><path d="M2 15l10 6 10-6M2 9l10 6 10-6"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'css'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 8h4v8H7zM13 8h4v8h-4z" fill="currentColor" opacity="0.3"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'json'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3h8l2 4-2 4h-4l2-4h-4l2 4h-4l-2-4z"/><path d="M8 13h8l2 4-2 4h-4l2-4h-4l2 4h-4l-2-4z"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'yaml'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16v16H4z" stroke-dasharray="2 2"/><path d="M8 8h8M8 12h8M8 16h8"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'markdown'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 8v8M12 8v8M17 8v8" stroke-dasharray="1.5 1.5"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'text'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 8h10M7 12h10M7 16h6"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'xml'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M12 8l-4 4h8l-4 4"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'sql'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3h8v18H8z"/><path d="M11 8h2v8h-2zM8 11h2v2H8z"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'shell'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16v16H4z" stroke-dasharray="2 2"/><path d="M8 12h8M8 10h2v4h-2z"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'batch'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16v16H4z"/><path d="M8 10h8M8 13h6M8 16h4"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'php'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3h8v18H8z"/><path d="M12 8v8"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'java'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v12M6 12h12"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'cpp'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 8h8v8H8z" fill="currentColor" opacity="0.15"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'csharp'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 9-9 9-9-9 9-9z"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'go'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 12h8M12 8v8"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'rust'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6l4 6-4 6-4-6 4-6z" fill="currentColor" opacity="0.2"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'ruby'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 8l4 8M16 8l-4 8"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'toml'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 8h10M7 12h10M7 16h10"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'config'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'env'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 12h8M12 8v8"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'license'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 8h8M8 12h6M8 16h4"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'gitignore'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 8l8 8M16 8l-8 8"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'env-example'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 12h8M12 8v8"/><path d="M8 8h8" stroke-dasharray="1.5 1.5"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'dockerfile'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 8h8v8H8z" fill="currentColor" opacity="0.15"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'makefile'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 10h10M7 14h6"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'pyproject'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 8h8v8H8z" fill="currentColor" opacity="0.15"/><path d="M8 11h8"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'package'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M12 6v12M6 12h12"/></svg>
              <svg v-else-if="fileTypeIcon(e.name) === 'requirements'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 8h4v4h-4zM12 8h4v4h-4zM8 12h4v4h-4zM12 12h4v4h-4z"/></svg>
              <!-- Generic file icon -->
              <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
            </span>
            <span
              class="ex-name"
              :style="{ color: e.type === 'file' ? (fileTypeColor(e.name) || 'inherit') : 'inherit' }"
              :title="e.name"
            >{{ e.name }}</span>
          </div>

          <!-- Anak folder yang di-expand -->
          <div
            v-for="c in expanded[childPath(currentPath, e.name)] || []"
            :key="childPath(currentPath, e.name) + '/' + c.name"
            class="ex-row ex-child"
            :class="{ 'ex-folder': c.type === 'dir', selected: selected === childPath(currentPath, e.name) + '/' + c.name, 'ex-active': selected === childPath(currentPath, e.name) + '/' + c.name && c.type === 'file' }"
            @click="openChild(childPath(currentPath, e.name), c)"
            @contextmenu.prevent="onContextMenu($event, c, c.type === 'dir' ? 'folder' : 'file')"
          >
            <span class="ex-caret">{{ c.type === 'dir' ? '▸' : '' }}</span>
            <span class="ex-ico" :class="c.type === 'dir' ? 'folder' : 'file'">
              <svg v-if="c.type === 'dir'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'python'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'html'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 8h4v8H7zM13 8h4v8h-4z" fill="currentColor" opacity="0.3"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'javascript'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 22 8.5 22 15.5 12 22 2 15.5 2 8.5 12 2" fill="currentColor" opacity="0.2"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'typescript'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16v16H4z" stroke-dasharray="2 2"/><path d="M8 12h4v4M8 12h4M12 12h4v4M12 12h4"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'vue'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3L2 9l10 6 10-6-10-6z"/><path d="M2 15l10 6 10-6M2 9l10 6 10-6"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'css'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 8h4v8H7zM13 8h4v8h-4z" fill="currentColor" opacity="0.3"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'json'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3h8l2 4-2 4h-4l2-4h-4l2 4h-4l-2-4z"/><path d="M8 13h8l2 4-2 4h-4l2-4h-4l2 4h-4l-2-4z"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'yaml'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16v16H4z" stroke-dasharray="2 2"/><path d="M8 8h8M8 12h8M8 16h8"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'markdown'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 8v8M12 8v8M17 8v8" stroke-dasharray="1.5 1.5"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'text'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 8h10M7 12h10M7 16h6"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'xml'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M12 8l-4 4h8l-4 4"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'sql'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3h8v18H8z"/><path d="M11 8h2v8h-2zM8 11h2v2H8z"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'shell'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16v16H4z" stroke-dasharray="2 2"/><path d="M8 12h8M8 10h2v4h-2z"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'batch'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16v16H4z"/><path d="M8 10h8M8 13h6M8 16h4"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'php'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3h8v18H8z"/><path d="M12 8v8"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'java'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v12M6 12h12"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'cpp'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 8h8v8H8z" fill="currentColor" opacity="0.15"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'csharp'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l9 9-9 9-9-9 9-9z"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'go'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 12h8M12 8v8"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'rust'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6l4 6-4 6-4-6 4-6z" fill="currentColor" opacity="0.2"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'ruby'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 8l4 8M16 8l-4 8"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'toml'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 8h10M7 12h10M7 16h10"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'config'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'env'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 12h8M12 8v8"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'license'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 8h8M8 12h6M8 16h4"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'gitignore'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 8l8 8M16 8l-8 8"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'env-example'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 12h8M12 8v8"/><path d="M8 8h8" stroke-dasharray="1.5 1.5"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'dockerfile'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 8h8v8H8z" fill="currentColor" opacity="0.15"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'makefile'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M7 10h10M7 14h6"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'pyproject'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 8h8v8H8z" fill="currentColor" opacity="0.15"/><path d="M8 11h8"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'package'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M12 6v12M6 12h12"/></svg>
              <svg v-else-if="fileTypeIcon(c.name) === 'requirements'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v18H3z"/><path d="M8 8h4v4h-4zM12 8h4v4h-4zM8 12h4v4h-4zM12 12h4v4h-4z"/></svg>
              <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
            </span>
            <span
              class="ex-name"
              :style="{ color: c.type === 'file' ? (fileTypeColor(c.name) || 'inherit') : 'inherit' }"
              :title="c.name"
            >{{ c.name }}</span>
          </div>
        </template>
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