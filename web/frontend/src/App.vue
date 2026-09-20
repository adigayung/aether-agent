<script setup>
// AETHER Engineering Workbench (#52 rework).
//
// Frontend TIPIS: hanya HTTP ke Django Gateway (#50) dan SSE (#51).
// TIDAK ada logic agent (runtime/loop/orchestrator/planning/tools/terminal/
// validation/recovery/routing/fallback/filesystem) di frontend.
// TIDAK ada Project Registry / Session Store / Task Lifecycle / Event System
// kedua: semua dari backend AETHER yang sudah ada.
//
// Layout 3 area: Sidebar | Agent Workbench | Changes/File Explorer.
// Bootstrap 5 dipakai untuk layout/spacing/form/button/dropdown/responsive.

import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import ProjectLauncher from "./components/ProjectLauncher.vue";
import AgentActivity from "./components/AgentActivity.vue";
import TaskComposer from "./components/TaskComposer.vue";
import ChangesPanel from "./components/ChangesPanel.vue";
import FileExplorer from "./components/FileExplorer.vue";
import ReportViewer from "./components/ReportViewer.vue";
import SettingsView from "./components/SettingsView.vue";
import ConsultantChat from "./components/ConsultantChat.vue";
import {
  cancelTask,
  closeActiveProject,
  createProject,
  createTask,
  deleteProject,
  getActiveProject,
  getConfig,
  getHealth,
  getLLMProviders,
  getProjects,
  getTaskActivity,
  getTaskHistory,
  getTaskReport,
  listTaskHistory,
  listTasks,
  openEventStream,
  openInExplorer,
  setActiveProject,
} from "./api.js";
import { playStatusSound, resetAudioTracker } from "./audioRegistry.js";

// Navigasi berorientasi user (bukan subsystem internal AETHER).
// `icon` = path SVG (stroke) inline — tanpa dependency icon baru.
const navItems = [
  {
    id: "agent",
    label: "Workbench",
    icon: "M3 4h18v12H3zM8 20h8M12 16v4",
  },
  {
    id: "tasks",
    label: "Tasks",
    icon: "M9 6h11M9 12h11M9 18h11M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2",
  },
  {
    id: "projects",
    label: "Projects",
    icon: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  },
  {
    id: "history",
    label: "History",
    icon: "M12 8v4l3 2M3 12a9 9 0 1 0 3-6.7L3 8M3 4v4h4",
  },
  {
    id: "settings",
    label: "Settings",
    icon: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 7 19.4a1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0-1.2-2.9H1a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 2.6 7a1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H7a1.7 1.7 0 0 0 1-1.5V1a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V7a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  },
];

const activeNav = ref("agent");
const health = ref(null);
const projects = ref([]);
const tasks = ref([]);
// Task History (persistent .aether/log/ via History API) — newest first.
const taskHistory = ref([]);
const selectedProjectId = ref("");
const submitting = ref(false);
const error = ref("");
const connected = ref(false);

// Active Project (single-user local app; bukan login/session user).
const activeProject = ref(null);
const launcherProjects = ref([]);
const launcherBusy = ref(false);
// Project/session terakhir (persisted active project) untuk ditawarkan
// "buka kembali" di Project Launcher saat AETHER dibuka.
const lastProject = ref(null);

// Konfigurasi AETHER (provider/model/mode) — TIDAK hardcode di frontend.
const config = ref({});
const selectedMode = ref("");
// Provider Instance + Model dari konfigurasi LLM tersimpan (SQLite).
// New Task memakai ini (bukan settings/.env) untuk memilih provider+model.
const llmProviders = ref([]);
const selectedProviderInstanceId = ref("");
const selectedModelId = ref("");

// State task/workspace (diisi dari #50 + #51).
const task = reactive({ id: "", text: "", status: "idle" });
const events = ref([]);
// Activity dari persistent log (Activity API). null = pakai live events (SSE).
const historyEvents = ref(null);
// Final Agent Report (Report API, .aether/log/). null = belum dimuat.
const currentReport = ref(null);
const reportOpen = ref(false);
const reportTaskId = ref("");
const reportStatus = ref("");
const changes = ref([]);
const validation = reactive({ state: "pending" });
const runtime = reactive({ phase: "", activity: "", provider: "", model: "", tool: "" });
// Penanda refresh File Explorer (dinaikkan setelah agent selesai membuat file).
const explorerRefresh = ref(0);

// Rolling window frontend untuk feed SSE live. Bukan pagination/history tanpa
// batas: window dibatasi, sedangkan history lengkap dibaca dari .aether/log/
// via Activity API saat membuka task lama.
const MAX_ACTIVITY = 500;

function pushRolling(list, item, max) {
  list.push(item);
  if (list.length > max) list.splice(0, list.length - max);
}

// Event yang ditampilkan Agent Activity: live SSE atau history dari API.
const activityEvents = computed(() => historyEvents.value || events.value);

let source = null;

const hasActiveTask = computed(() => Boolean(task.id));
const isRunning = computed(() =>
  ["running", "prepared", "planning", "executing", "validating"].includes(
    (task.status || "").toLowerCase()
  )
);

// Agent status kecil (dari state/event AETHER sebenarnya, bukan fake).
const agentStatus = computed(() => {
  const s = (task.status || "idle").toLowerCase();
  if (s === "running" || s === "prepared" || s === "planning" || s === "executing")
    return { label: "Running", cls: "running" };
  if (s === "validating") return { label: "Validating", cls: "running" };
  if (s === "completed") return { label: "Completed", cls: "completed" };
  if (s === "failed") return { label: "Failed", cls: "failed" };
  if (s === "cancelled") return { label: "Stopping", cls: "warn" };
  return { label: "Ready", cls: "ready" };
});

// Sidebar: Workspace (agent/tasks/projects/history) & Configuration (settings).
const workspaceNav = computed(() => navItems.filter((i) => i.id !== "settings"));
const settingsItem = computed(() => navItems.find((i) => i.id === "settings") || {});
function navBadge(id) {
  if (id === "tasks" || id === "history") return taskHistory.value.length || null;
  if (id === "projects") return projects.value.length || null;
  return null;
}

// Footer status bar (dari runtime AETHER, bukan hardcode).
const modelLabel = computed(() => runtime.model || config.value.model || "—");
const providerLabel = computed(() => runtime.provider || config.value.provider || "—");

// Task Composer modal (dibuka dari agent input).
const composerOpen = ref(false);
function openComposer() {
  composerOpen.value = true;
}
function closeComposer() {
  composerOpen.value = false;
}

// Consultant (modal). Reasoning, Project Bible, tool boundary, dan session
// context dijalankan backend (endpoint Consultant). App hanya membuka modal dan
// mengirim Task Proposal yang dihasilkan ke alur task Agent yang sudah ada.
const consultantOpen = ref(false);
function openConsultant() {
  consultantOpen.value = true;
}
function closeConsultant() {
  consultantOpen.value = false;
}

// Task Proposal dari Consultant -> task Agent (alur task existing submitTask).
// Modal Consultant SENGAJA tetap terbuka agar user dapat terus melihat
// percakapan/aktivitas Consultant setelah task dikirim ke Agent.
async function runConsultantTask(text) {
  if (!text) return;
  await submitTask(text);
}

// Dot warna Agent di sidebar.
const agentDotClass = computed(() => {
  const c = agentStatus.value.cls;
  if (c === "failed") return "err";
  if (c === "warn" || c === "running") return "warn";
  return "";
});

// Tag status task (di header card).
const taskTag = computed(() => {
  const s = (task.status || "idle").toLowerCase();
  if (s === "completed") return { label: "completed", cls: "validated" };
  if (s === "failed") return { label: "failed", cls: "failed" };
  if (s === "cancelled") return { label: "stopped", cls: "modified" };
  if (isRunning.value) return { label: "running", cls: "validated" };
  return { label: "idle", cls: "idle" };
});

// Lifecycle: presentasi status task AETHER (tidak mengarang progres backend).
const LIFECYCLE_STEPS = ["Planning", "Inspecting", "Editing", "Running", "Validating", "Completed"];
const lifecycleIndex = computed(() => {
  const s = (task.status || "idle").toLowerCase();
  const phase = (runtime.phase || "").toLowerCase();
  if (!task.id || s === "idle") return -1;
  if (s === "completed") return LIFECYCLE_STEPS.length - 1;
  if (s === "validating") return 4;
  if (s === "cancelled" || s === "failed") return 3;
  if (s === "prepared" || s === "planning" || s === "queued") return 0;
  if (phase.includes("plan")) return 0;
  if (phase.includes("inspect") || phase.includes("analyz") || phase.includes("read")) return 1;
  if (phase.includes("edit") || phase.includes("writ") || phase.includes("patch")) return 2;
  if (phase.includes("valid") || phase.includes("test")) return 4;
  return 3;
});
const lifecycleSteps = computed(() =>
  LIFECYCLE_STEPS.map((label, i) => {
    const idx = lifecycleIndex.value;
    let state = "";
    if (idx >= 0) {
      if (i < idx) state = "done";
      else if (i === idx) state = "active";
    }
    return { label, state };
  })
);
const lifecyclePct = computed(() => {
  const idx = lifecycleIndex.value;
  if (idx <= 0) return 0;
  return Math.round((idx / (LIFECYCLE_STEPS.length - 1)) * 100);
});

// Page header (tasks/projects/history/settings).
const pageTitle = computed(() => {
  if (activeNav.value === "tasks") return "Tasks";
  if (activeNav.value === "history") return "History";
  if (activeNav.value === "projects") return "Projects";
  if (activeNav.value === "settings") return "Settings";
  return "Workbench";
});
const pageDesc = computed(() => {
  if (activeNav.value === "tasks") return "Tasks executed in this workspace.";
  if (activeNav.value === "history") return "Past tasks and their outcomes.";
  if (activeNav.value === "projects") return "Workspaces registered in AETHER.";
  if (activeNav.value === "settings") return "Configure providers and models used by the AETHER workbench.";
  return "";
});
function statusTagClass(s) {
  const v = (s || "").toLowerCase();
  if (v === "completed") return "status-on";
  if (v === "failed") return "status-err";
  if (v === "running" || v === "prepared" || v === "validating" || v === "planning") return "status-run";
  return "status-off";
}

// Format timestamp persistent log (ISO string) untuk kolom History.
function formatTs(raw) {
  if (!raw) return "—";
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? String(raw) : d.toLocaleString();
}

// --- Event handling (#51) --------------------------------------------------
// Filter tampilan Changes: file di dalam `.aether/**` adalah metadata internal
// AETHER (Bible, log, dsb.), BUKAN perubahan project. Ini murni layer
// presentasi UI; ChangeTracker/ProjectBrain/logging tidak diubah.
function isAetherMetadata(path) {
  if (!path) return false;
  const normalized = String(path).replace(/\\/g, "/").replace(/^\.\//, "");
  return (
    normalized === ".aether" ||
    normalized.startsWith(".aether/") ||
    normalized.includes("/.aether/")
  );
}

function handleEvent(evt) {
  if (!evt || !evt.event_type) return;
  // Event live untuk task aktif -> tampilkan alur SSE (bukan history lama).
  if (evt.task_id && task.id && evt.task_id === task.id) {
    historyEvents.value = null;
  }
  pushRolling(events.value, evt, MAX_ACTIVITY);
  const p = evt.payload || {};

  switch (evt.event_type) {
    case "task_started":
      task.status = "running";
      runtime.activity = "Starting task";
      // Audio feedback HANYA saat task BENAR-BENAR mulai berjalan (transisi
      // status nyata), bukan saat user klik Run Task/Send. Dedup di
      // audioRegistry mencegah dobel-putar bila event running diterima ulang.
      playStatusSound("running");
      break;
    case "phase_changed":
      if (p.phase) {
        runtime.phase = p.phase;
        runtime.activity = p.phase;
      }
      break;
    case "provider_request":
    case "provider_response":
      if (p.provider) runtime.provider = p.provider;
      if (p.model) runtime.model = p.model;
      break;
    case "tool_called":
      if (p.tool) {
        runtime.tool = p.tool;
        runtime.activity = `Running ${p.tool}`;
      }
      break;
    case "tool_completed":
      // Tool events tampil di Agent Activity (unified timeline).
      break;
    case "observation_received":
      // Observation mentah tidak ditampilkan (hindari dump isi file panjang).
      break;
    case "validation_started":
      validation.state = "running";
      task.status = "validating";
      break;
    case "validation_completed":
      validation.state = p.success === false ? "err" : "ok";
      break;
    case "recovery_started":
      runtime.activity = "Recovery started";
      break;
    case "recovery_completed":
      runtime.activity = "Recovery completed";
      break;
    case "change_detected":
      // Sembunyikan `.aether/**` di daftar Changes (metadata internal AETHER).
      if (!isAetherMetadata(p.path)) {
        changes.value.push({
          kind: p.kind || "change",
          path: p.path,
          detail: p.detail,
          additions: p.additions,
          deletions: p.deletions,
          diff: p.diff,
        });
      }
      // File baru berubah -> refresh File Explorer agar file muncul.
      explorerRefresh.value += 1;
      break;
    case "task_completed":
      task.status = "completed";
      runtime.activity = "";
      // Refresh File Explorer setelah agent selesai (file baru terlihat).
      explorerRefresh.value += 1;
      playStatusSound("completed");
      refreshTaskHistory();
      break;
    case "task_failed":
      task.status = "failed";
      playStatusSound("failed");
      refreshTaskHistory();
      break;
    case "task_cancelled":
      task.status = "cancelled";
      // Execution benar-benar berhenti -> indikator Agent kembali idle.
      runtime.activity = "";
      runtime.tool = "";
      playStatusSound("cancelled");
      refreshTaskHistory();
      break;
    default:
      break;
  }
}

function connectStream() {
  if (source) source.close();
  source = openEventStream({ taskId: task.id || null, onEvent: handleEvent });
  source.onopen = () => {
    connected.value = true;
  };
  source.onerror = () => {
    connected.value = false;
  };
}

function resetWorkspace() {
  events.value = [];
  changes.value = [];
  historyEvents.value = null;
  currentReport.value = null;
  validation.state = "pending";
  runtime.phase = "";
  runtime.activity = "";
  runtime.provider = "";
  runtime.model = "";
  runtime.tool = "";
}

// --- Data loading ----------------------------------------------------------
async function refreshTasks() {
  try {
    const data = await listTasks();
    tasks.value = data.tasks || [];
  } catch {
    // Endpoint list mungkin belum tersedia; UI tetap aman.
  }
}

// Task History dari persistent log (.aether/log/ via History API), newest first.
async function refreshTaskHistory() {
  try {
    const data = await listTaskHistory(selectedProjectId.value || null);
    taskHistory.value = data.tasks || [];
  } catch {
    // Endpoint history mungkin belum tersedia; UI tetap aman.
  }
}

// --- Task submit / stop (#50) ----------------------------------------------
async function submitTask(text) {
  error.value = "";
  submitting.value = true;
  try {
    // Provider Instance + Model dari konfigurasi LLM tersimpan (SQLite)
    // diteruskan sebagai metadata ke mekanisme AETHER existing. Runtime
    // merakit provider + api_url + api_key + model dari DB ini (bukan .env).
    // Mode tetap routing signal.
    const metadata = {};
    if (selectedProviderInstanceId.value)
      metadata.provider_instance_id = selectedProviderInstanceId.value;
    if (selectedModelId.value) metadata.model_id = selectedModelId.value;
    if (selectedMode.value) metadata.mode = selectedMode.value;
    const record = await createTask(
      text,
      selectedProjectId.value || null,
      Object.keys(metadata).length ? metadata : null
    );
    task.id = record.task_id;
    task.text = record.task;
    task.status = record.status || "prepared";
    resetAudioTracker();
    resetWorkspace();
    await refreshTasks();
    connectStream();
    composerOpen.value = false;
  } catch (e) {
    error.value = e.message || "Failed to create task.";
  } finally {
    submitting.value = false;
  }
}

async function stopTask() {
  if (!task.id) return;
  try {
    const record = await cancelTask(task.id);
    task.status = record.status || "cancelled";
  } catch (e) {
    error.value = e.message || "Failed to stop task.";
  }
}

// Buka task dari persistent log (History/Activity/Report API). Berfungsi untuk
// task lama walau SessionStore sudah kosong / proses sudah restart.
async function openHistoryTask(taskId) {
  error.value = "";
  const projectId = selectedProjectId.value || null;
  try {
    const info = await getTaskHistory(taskId, projectId);
    task.id = info.task_id || taskId;
    task.text = info.task || "";
    task.status = info.status || "incomplete";
    // Activity (chronological) dari persistent log.
    const activity = await getTaskActivity(taskId, projectId);
    historyEvents.value = activity.events || [];
    // Report final (bila ada) dari persistent log.
    try {
      const report = await getTaskReport(taskId, projectId);
      currentReport.value = report.report ?? null;
    } catch {
      currentReport.value = null;
    }
    activeNav.value = "agent";
  } catch (e) {
    error.value = e.message || "Failed to load task history.";
  }
}

// Buka Report viewer untuk sebuah task (Report API -> .aether/log/).
async function openReport(taskId) {
  error.value = "";
  if (!taskId) return;
  try {
    const data = await getTaskReport(taskId, selectedProjectId.value || null);
    reportTaskId.value = data.task_id || taskId;
    reportStatus.value = data.status || "";
    currentReport.value = data.report ?? null;
    reportOpen.value = true;
  } catch (e) {
    error.value = e.message || "Failed to load report.";
  }
}

// --- Project Launcher / Active Project -------------------------------------
async function refreshLauncherProjects() {
  try {
    const data = await getProjects();
    launcherProjects.value = data.projects || [];
  } catch {
    launcherProjects.value = [];
  }
}

async function openProject(projectId) {
  launcherBusy.value = true;
  error.value = "";
  try {
    const data = await setActiveProject(projectId);
    activeProject.value = data.active_project || null;
    selectedProjectId.value = projectId;
    await enterWorkbench();
  } catch (e) {
    error.value = e.message || "Failed to open project.";
  } finally {
    launcherBusy.value = false;
  }
}

async function createNewProject({ name, path }) {
  launcherBusy.value = true;
  error.value = "";
  try {
    const record = await createProject(name, path);
    await refreshLauncherProjects();
    activeProject.value = record;
    selectedProjectId.value = record.id;
    await enterWorkbench();
  } catch (e) {
    error.value = e.message || "Failed to create project.";
  } finally {
    launcherBusy.value = false;
  }
}

async function removeProject(projectId) {
  launcherBusy.value = true;
  error.value = "";
  try {
    await deleteProject(projectId);
    await refreshLauncherProjects();
  } catch (e) {
    error.value = e.message || "Failed to delete project.";
  } finally {
    launcherBusy.value = false;
  }
}

// Close Project: clear active project -> kembali ke Project Launcher.
// TIDAK menghapus folder filesystem.
async function closeProject() {
  const previous = activeProject.value;
  try {
    await closeActiveProject();
  } catch {
    // Tetap lanjut ke launcher walau request gagal.
  }
  activeProject.value = null;
  // Tampilkan project terakhir di launcher agar bisa dibuka kembali.
  lastProject.value = previous || null;
  selectedProjectId.value = "";
  task.id = "";
  task.text = "";
  task.status = "idle";
  resetWorkspace();
  await refreshLauncherProjects();
}

// Buka Windows Explorer pada active project (path dari backend, bukan frontend).
async function openExplorer() {
  try {
    await openInExplorer();
  } catch (e) {
    error.value = e.message || "Gagal membuka Explorer.";
  }
}

// Klik file di File Explorer -> buka editor/modal (placeholder aman).
function openFileInEditor(file) {
  // Tidak ada editor kedua: cukup tampilkan path terpilih sebagai info.
  error.value = "";
  runtime.activity = `File: ${file.path}`;
}

async function enterWorkbench() {
  activeNav.value = "agent";
  try {
    const data = await getProjects();
    projects.value = data.projects || [];
  } catch {
    projects.value = [];
  }
  await refreshTasks();
  await refreshTaskHistory();
  connectStream();
}

// Muat Provider Instance + Model dari konfigurasi LLM tersimpan (SQLite).
// New Task memakai ini (bukan settings/.env). Default: instance enabled
// pertama + model enabled pertama (bila belum ada pilihan).
async function refreshLLMProviders() {
  try {
    const data = await getLLMProviders();
    llmProviders.value = data.providers || [];
  } catch {
    llmProviders.value = [];
  }
  const enabled = llmProviders.value.filter((p) => p.enabled !== false);
  const current = enabled.find((p) => p.id === selectedProviderInstanceId.value);
  if (!current) {
    const first = enabled[0] || null;
    selectedProviderInstanceId.value = first ? first.id : "";
    selectedModelId.value = "";
  }
  const inst = enabled.find((p) => p.id === selectedProviderInstanceId.value);
  const models = inst ? (inst.models || []).filter((m) => m.enabled !== false) : [];
  if (!models.some((m) => m.id === selectedModelId.value)) {
    selectedModelId.value = models[0] ? models[0].id : "";
  }
}

// --- Lifecycle -------------------------------------------------------------
onMounted(async () => {
  try {
    health.value = await getHealth();
  } catch {
    health.value = null;
  }
  try {
    config.value = await getConfig();
    selectedMode.value = config.value.mode || "balanced";
  } catch {
    config.value = {};
  }
  // Provider Instance + Model untuk New Task dari konfigurasi LLM tersimpan.
  await refreshLLMProviders();
  await refreshLauncherProjects();
  // Baca project/session terakhir untuk ditawarkan "buka kembali" di launcher.
  // AETHER TIDAK auto-masuk Workbench: user harus menentukan workspace dulu.
  try {
    const data = await getActiveProject();
    lastProject.value = data.active_project || null;
  } catch {
    lastProject.value = null;
  }
});

onBeforeUnmount(() => {
  if (source) source.close();
});
</script>

<template>
  <!-- Project Launcher: ditampilkan bila tidak ada active project. -->
  <ProjectLauncher
    v-if="!activeProject"
    :projects="launcherProjects"
    :last-project="lastProject"
    :busy="launcherBusy"
    @open="openProject"
    @create="createNewProject"
    @delete="removeProject"
  />

  <!-- Workbench shell (dummy index.html): sidebar | main + statusbar. -->
  <div v-else class="app">
    <!-- ===================== SIDEBAR ===================== -->
    <aside class="sidebar">
      <div class="side-brand">
        <span class="logo">A</span>
        <div>
          <div class="name">AETHER</div>
          <div class="sub">WORKBENCH</div>
        </div>
      </div>

      <div class="side-scroll">
        <div class="side-section">Workspace</div>
        <div
          v-for="item in workspaceNav"
          :key="item.id"
          class="nav-item"
          :class="{ active: activeNav === item.id }"
          @click="activeNav = item.id"
        >
          <svg
            class="ico"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.8"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path :d="item.icon" />
          </svg>
          <span>{{ item.label }}</span>
          <span v-if="navBadge(item.id)" class="badge-n">{{ navBadge(item.id) }}</span>
        </div>

        <div class="side-section">Configuration</div>
        <div
          class="nav-item"
          :class="{ active: activeNav === 'settings' }"
          @click="activeNav = 'settings'"
        >
          <svg
            class="ico"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.8"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path :d="settingsItem.icon" />
          </svg>
          <span>{{ settingsItem.label }}</span>
        </div>
        <div class="nav-item" @click="closeProject">
          <svg
            class="ico"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.8"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4M10 17l5-5-5-5M15 12H3" />
          </svg>
          <span>Close Project</span>
        </div>
      </div>

      <!-- AETHER Consultant card -->
      <div class="consultant-card" @click="openConsultant">
        <svg class="ci" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2a4 4 0 0 1 4 4c0 1.95-1.4 3.58-3.25 3.93L12 22l-.75-12.07A4.001 4.001 0 0 1 12 2z"/><circle cx="12" cy="6" r="1.5" fill="currentColor" stroke="none"/><path d="M9 14l-3 3 3 3M15 14l3 3-3 3"/></svg>
        <div class="ci-body">
          <div class="ci-title">AETHER Consultant</div>
          <div class="ci-sub">Chat with the AI assistant</div>
        </div>
      </div>

      <div class="side-foot">
        <div class="sys-row">
          <span class="k"><span class="dot" :class="connected ? '' : 'err'"></span> System</span>
          <span class="v">{{ connected ? "Online" : "Offline" }}</span>
        </div>
        <div class="sys-row">
          <span class="k"><span class="dot"></span> Gateway</span>
          <span class="v">127.0.0.1:8000</span>
        </div>
        <div class="sys-row">
          <span class="k"><span class="dot" :class="agentDotClass"></span> Agent</span>
          <span class="v">{{ agentStatus.label }}</span>
        </div>
      </div>
    </aside>

    <!-- ===================== MAIN ===================== -->
    <div class="main">
      <header class="ws-header">
        <div class="ws-title">
          <span class="proj">Workspace: {{ activeProject.name }}</span>
          <span
            class="path clickable"
            :title="`Open in Explorer: ${activeProject.path}`"
            @click="openExplorer"
            >{{ activeProject.path }}</span
          >
        </div>
        <div class="ws-meta">
          <span class="chip" :class="agentStatus.cls">Task: <span class="mono">{{ agentStatus.label }}</span></span>
          <span class="chip">Changes: <span class="mono">{{ changes.length }}</span></span>
          <span class="chip" :class="{ accent: connected }">
            <span class="dot" :class="connected ? '' : 'err'"></span>{{ connected ? "live" : "offline" }}
          </span>
        </div>
      </header>

      <!-- Workbench content -->
      <div v-if="activeNav === 'agent'" class="ws-body">
        <div class="ws-col left">
          <div class="ws-scroll">
            <!-- Latest Task -->
            <section class="block">
              <div class="block-head">
                <div class="block-title">Latest Task</div>
                <div class="block-actions">
                  <button
                    v-if="task.id"
                    class="report-btn"
                    type="button"
                    title="View Agent Report"
                    @click="openReport(task.id)"
                  >
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M9 13h6M9 17h4"/></svg>
                    Report
                  </button>
                  <span class="tag" :class="taskTag.cls">{{ taskTag.label }}</span>
                </div>
              </div>
              <div class="task-card">
                <div class="task-top">
                  <span class="task-ico">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M9 13h6M9 17h4"/></svg>
                  </span>
                  <div class="task-info">
                    <div class="task-name">Process :</div>
                    <span class="prompt-pill" :title="task.text || 'No task yet'">{{ task.text || "No task yet" }}</span>
                    <div class="task-sub">{{ task.id ? task.id : "idle" }} · status: {{ task.status || "idle" }}</div>
                  </div>
                </div>

                <div class="lifecycle">
                  <template v-for="(step, i) in lifecycleSteps" :key="step.label">
                    <div class="step" :class="step.state">
                      <span class="mark">{{ step.state === "done" ? "✓" : i + 1 }}</span>
                      <span>{{ step.label }}</span>
                    </div>
                    <span
                      v-if="i < lifecycleSteps.length - 1"
                      class="step-line"
                      :class="{ done: step.state === 'done' }"
                    ></span>
                  </template>
                </div>

                <div class="progress-a"><div class="bar" :style="{ width: lifecyclePct + '%' }"></div></div>
                <div class="progress-meta">
                  <span>{{ lifecyclePct }}% complete</span>
                  <span>{{ runtime.phase || task.status || "idle" }}</span>
                </div>
              </div>
            </section>

            <!-- Agent Activity: unified chronological timeline (commentary,
                 tool call, tool result, observation). Live dari SSE; history
                 dari Activity API/persistent log saat membuka task lama. -->
            <section class="block">
              <div class="term">
                <div class="term-head">
                  <span class="tl r"></span><span class="tl y"></span><span class="tl g"></span>
                  <span class="tt">aether — agent activity</span>
                </div>
                <AgentActivity :events="activityEvents" :status="task.status" />
              </div>
            </section>
          </div>

          <!-- Agent input -> Task Composer modal -->
          <div class="agent-input">
            <div class="agent-input-row">
              <input
                class="input-a"
                type="text"
                readonly
                :value="task.text"
                placeholder="Ask AETHER to build something…"
                @click="openComposer"
                @focus="openComposer"
              />
              <button
                v-if="!isRunning"
                class="send-btn"
                type="button"
                title="Compose task"
                @click="openComposer"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 5l7 7-7 7"/></svg>
              </button>
              <button
                v-else
                class="stop-btn stop-btn-run"
                type="button"
                title="Stop task"
                @click="stopTask"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
                <span class="stop-label">Running…</span>
              </button>
            </div>
            <div v-if="error" class="wb-error">{{ error }}</div>
          </div>
        </div>

        <div class="ws-col right">
          <ChangesPanel :changes="changes" :validation="validation" @open-file="openFileInEditor" />
          <FileExplorer
            :project="activeProject"
            :refresh-key="explorerRefresh"
            @open-file="openFileInEditor"
          />
        </div>
      </div>

      <!-- Other pages (tasks/projects/history/settings) -->
      <div v-else class="page-host">
        <main class="shell">
          <div class="page-head">
            <h1>{{ pageTitle }}</h1>
            <p>{{ pageDesc }}</p>
          </div>

          <!-- Tasks / History (persistent .aether/log/ via History API) -->
          <section v-if="activeNav === 'tasks' || activeNav === 'history'" class="panel">
            <div class="panel-head">
              <div>
                <div class="title">{{ activeNav === 'history' ? 'History' : 'Tasks' }}</div>
                <div class="desc">{{ taskHistory.length }} task(s) from persistent log</div>
              </div>
            </div>
            <div v-if="!taskHistory.length" class="panel-body"><div class="wb-empty">No tasks yet.</div></div>
            <table v-else class="aether-table">
              <thead>
                <tr>
                  <th style="width: 52%">Task</th>
                  <th style="width: 18%">Status</th>
                  <th style="width: 18%">Updated</th>
                  <th style="width: 12%"></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="t in taskHistory" :key="t.task_id" class="clickable" @click="openHistoryTask(t.task_id)">
                  <td>
                    <div class="cell-name">
                      <span class="avatar">T</span>
                      <div>
                        <div class="name">{{ t.task || "(no prompt)" }}</div>
                        <div class="meta">{{ t.task_id }}</div>
                      </div>
                    </div>
                  </td>
                  <td><span class="status-tag" :class="statusTagClass(t.status)">{{ t.status }}</span></td>
                  <td><span class="mono meta">{{ formatTs(t.last_timestamp) }}</span></td>
                  <td class="row-actions">
                    <button class="report-btn" type="button" title="View Agent Report" @click.stop="openReport(t.task_id)">Report</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </section>

          <!-- Projects -->
          <section v-else-if="activeNav === 'projects'" class="panel">
            <div class="panel-head">
              <div>
                <div class="title">Projects</div>
                <div class="desc">{{ projects.length }} workspace(s)</div>
              </div>
            </div>
            <div v-if="!projects.length" class="panel-body"><div class="wb-empty">No projects yet.</div></div>
            <table v-else class="aether-table">
              <thead>
                <tr>
                  <th style="width: 40%">Project</th>
                  <th style="width: 60%">Path</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="p in projects" :key="p.id" class="clickable" @click="selectedProjectId = p.id">
                  <td>
                    <div class="cell-name">
                      <span class="avatar">P</span>
                      <div>
                        <div class="name">{{ p.name }}</div>
                        <div class="meta">{{ (p.id || "").slice(0, 8) }}</div>
                      </div>
                    </div>
                  </td>
                  <td><span class="mono">{{ p.root || p.path }}</span></td>
                </tr>
              </tbody>
            </table>
          </section>

          <!-- Settings (kelola provider/model/credential via Gateway). -->
          <SettingsView v-else :config="config" />
        </main>
      </div>
    </div>

    <!-- ===================== STATUS BAR ===================== -->
    <footer class="statusbar">
      <span class="sb">model <span class="v">{{ modelLabel }}</span></span>
      <span class="sep">|</span>
      <span class="sb">provider <span class="v">{{ providerLabel }}</span></span>
      <span class="sep">|</span>
      <span class="sb">task <span class="v">{{ task.status || "idle" }}</span></span>
      <span class="right">
        <span class="sb">
          <span class="dot" :class="connected ? '' : 'err'"></span> workspace
          <span class="v">{{ connected ? "synced" : "offline" }}</span>
        </span>
        <span class="sb">AETHER v0.1.0</span>
      </span>
    </footer>

    <!-- ===================== TASK COMPOSER MODAL ===================== -->
    <div v-if="composerOpen" class="modal-backdrop" @click.self="closeComposer">
      <div class="modal composer-a" role="dialog" aria-modal="true">
        <TaskComposer
          :disabled="submitting"
          :running="isRunning"
          :config="config"
          :providers="llmProviders"
          :provider-instance-id="selectedProviderInstanceId"
          :model-id="selectedModelId"
          :mode="selectedMode"
          @submit="submitTask"
          @stop="stopTask"
          @update:provider-instance-id="selectedProviderInstanceId = $event"
          @update:model-id="selectedModelId = $event"
          @update:mode="selectedMode = $event"
        />
      </div>
    </div>

    <!-- ===================== CONSULTANT CHAT MODAL ===================== -->
    <!-- v-show (bukan v-if) agar komponen TIDAK di-unmount saat modal ditutup,
         sehingga riwayat percakapan tetap hidup selama sesi browser. -->
    <ConsultantChat
      v-show="consultantOpen"
      :providers="llmProviders"
      :provider-instance-id="selectedProviderInstanceId"
      :model-id="selectedModelId"
      :running="isRunning"
      @close="closeConsultant"
      @update:provider-instance-id="selectedProviderInstanceId = $event"
      @update:model-id="selectedModelId = $event"
      @run-task="runConsultantTask"
    />

    <!-- ===================== AGENT REPORT MODAL ===================== -->
    <ReportViewer
      v-if="reportOpen"
      :task-id="reportTaskId"
      :status="reportStatus"
      :report="currentReport"
      @close="reportOpen = false"
    />
  </div>
</template>
