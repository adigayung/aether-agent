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

import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import ProjectLauncher from "./components/ProjectLauncher.vue";
import AgentActivity from "./components/AgentActivity.vue";
import CodeEditor from "./components/CodeEditor.vue";
import TaskComposer from "./components/TaskComposer.vue";
import ChangesPanel from "./components/ChangesPanel.vue";
import FileExplorer from "./components/FileExplorer.vue";
import GithubBackupPanel from "./components/GithubBackupPanel.vue";
import QueuePanel from "./components/QueuePanel.vue";
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
  getGithubConfig,
  getHealth,
  getLLMProviders,
  getProjects,
  getTaskActivity,
  getTaskHistory,
  getTaskReport,
  listTaskHistory,
  listTaskQueue,
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
    id: "backup",
    label: "Backup",
    icon: "M12 3v10M8 9l4 4 4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2",
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
// Target konfirmasi hapus project (page Projects, registry-only).
const projectToDelete = ref(null);
const submitting = ref(false);
const error = ref("");
const notice = ref("");
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
// Live "Agent reasoning." indicator: true HANYA selama AETHER menunggu respons
// LLM. Ini SATU elemen UI (bukan log/event baru, bukan subsystem baru) yang
// dikendalikan event SSE EXISTING: provider_request (mulai) / provider_response
// (selesai/error), dengan terminal event sebagai pengaman. Tidak ada timer JS
// maupun polling — animasi titik sepenuhnya CSS.
const isReasoning = ref(false);
// Penanda refresh File Explorer (dinaikkan setelah agent selesai membuat file).
const explorerRefresh = ref(0);
// Live filesystem change terakhir (dari event change_detected) untuk update
// INCREMENTAL File Explorer tanpa full reload. `seq` memastikan setiap event
// tetap memicu walau isinya sama.
const liveFsChange = ref(null);
let liveFsChangeSeq = 0;

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

// Reasoning status hanya relevan untuk alur LIVE (bukan saat menampilkan
// activity task lama dari persistent log). Sumber tetap satu: isReasoning.
const showReasoning = computed(() => isReasoning.value && !historyEvents.value);

let source = null;

const hasActiveTask = computed(() => Boolean(task.id));

// ID task yang BENAR-BENAR sedang RUNNING di Global Task Queue (satu sumber
// kebenaran = TaskRecord backend, GET /api/tasks/queue). Bukan state/mesin
// kedua: hanya proyeksi status antrian existing. Dipakai sebagai target tombol
// Stop agar Stop SELALU merujuk ke task yang benar-benar berjalan — bukan task
// terakhir yang dikirim/dibuat/dipilih.
const runningTaskId = ref("");
// Item antrian aktif (pending/running/disabled) dari GET /api/tasks/queue.
// Dipakai badge jumlah mode QUEUE di halaman Tasks. Sumber sama persis dengan
// QueuePanel — BUKAN queue subsystem kedua.
const queueItems = ref([]);
// Badge QUEUE = jumlah item non-terminal (endpoint queue hanya mengembalikan
// pending/running/disabled; task terminal tidak masuk antrian).
const queueCount = computed(() => queueItems.value.length);
// CATATAN: `terminalTaskId` (task_id terakhir yang mencapai status terminal)
// dideklarasikan di bagian Consultant di bawah; dipakai juga di sini agar
// refresh antrian TIDAK memunculkan kembali task yang sudah berhenti.
async function refreshRunningTask() {
  try {
    const data = await listTaskQueue();
    const items = data.tasks || [];
    queueItems.value = items;
    // Jangan anggap "running" task yang sudah kita ketahui terminal: respons
    // antrian bisa saja masih memuat status lama tepat setelah task selesai.
    const running = items.find(
      (t) => t.queue_state === "running" && t.task_id !== terminalTaskId.value
    );
    runningTaskId.value = running ? running.task_id : "";
  } catch {
    // Endpoint queue belum tersedia: pertahankan state existing (fallback).
  }
}

// Task yang berjalan sudah mencapai status terminal -> lepas target tombol Stop
// SECARA SINKRON (tanpa menunggu refresh antrian async). Ini yang menjamin
// tombol Stop LANGSUNG hilang saat task selesai/gagal/cancel, tanpa jendela
// race. Hanya pemilik slot yang dilepas (bila diketahui).
function releaseRunningTask(taskId) {
  if (!runningTaskId.value) return;
  if (!taskId || taskId === runningTaskId.value) runningTaskId.value = "";
}

// isRunning = ADA task yang sedang RUNNING (bukan apakah task yang sedang
// ditampilkan running). Dipakai untuk MENAMPILKAN tombol Stop + indikator UI.
// SENGAJA TIDAK dipakai untuk men-disable input/Submit: Agent Input selalu bisa
// submit (task baru masuk Global Task Queue sebagai pending/queued).
const isRunning = computed(() => Boolean(runningTaskId.value));

// Agent status kecil (dari state/event AETHER sebenarnya, bukan fake).
const agentStatus = computed(() => {
  const s = (task.status || "idle").toLowerCase();
  if (s === "running" || s === "prepared" || s === "planning" || s === "executing")
    return { label: "Running", cls: "running" };
  if (s === "validating") return { label: "Validating", cls: "running" };
  // Menunggu execution slot di Global Task Queue (bukan running).
  if (s === "queued") return { label: "Queued", cls: "queued" };
  if (s === "completed") return { label: "Completed", cls: "completed" };
  if (s === "failed") return { label: "Failed", cls: "failed" };
  if (s === "cancelled") return { label: "Stopping", cls: "warn" };
  return { label: "Ready", cls: "ready" };
});

// Sidebar: Workspace (agent/tasks/projects) & Configuration (settings).
const workspaceNav = computed(() => navItems.filter((i) => i.id !== "settings"));
const settingsItem = computed(() => navItems.find((i) => i.id === "settings") || {});
function navBadge(id) {
  if (id === "tasks") return taskHistory.value.length || null;
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
// Panel TASKS di Consultant (SATU queue global AETHER). Penanda refresh untuk
// QueuePanel; dinaikkan setelah Run Task / event terminal task.
const queueRefresh = ref(0);
// Setiap refresh antrian (submit/terminal/stop) -> perbarui "task running" saat
// ini dari sumbernya. Event-driven (BUKAN polling baru): hanya mengikuti penanda
// refresh yang sudah ada (queueRefresh), yang sama dipakai panel TASKS.
watch(queueRefresh, () => {
  refreshRunningTask();
});

// Mode halaman Tasks: QUEUE (live/actionable) vs HISTORY (arsip read-only).
// SATU halaman, DUA fungsi berbeda — data queue & history TIDAK dicampur.
// Default saat halaman dibuka = QUEUE.
const taskPageMode = ref("queue");
watch(activeNav, (nav) => {
  if (nav === "tasks") {
    taskPageMode.value = "queue";
    // Segarkan kedua mode dari sumbernya masing-masing (queue API + History API).
    refreshRunningTask();
    refreshTaskHistory();
  }
});
function openConsultant() {
  consultantOpen.value = true;
}
function closeConsultant() {
  consultantOpen.value = false;
}

// Task Proposal dari Consultant -> task Agent (alur task existing submitTask).
// Modal Consultant SENGAJA tetap terbuka agar user dapat terus melihat
// percakapan/aktivitas Consultant setelah task dikirim ke Agent.
// `submittedTaskId` = task yang baru dibuat (dipakai ConsultantChat untuk
// men-disable tombol Run Task milik Task Proposal itu sampai task terminal).
const submittedTaskId = ref("");
// task_id terakhir yang mencapai status terminal (completed/failed/cancelled).
const terminalTaskId = ref("");
// Payload bisa object { text, providerInstanceId, modelId } (bentuk baru dari
// card Task Proposal) ATAU string lama (backward compatible). Provider/model
// dari card proposal (runner) dipakai untuk task ini; pilihan header (chat)
// TIDAK diubah.
async function runConsultantTask(payload) {
  const text = typeof payload === "string" ? payload : payload && payload.text;
  if (!text) return;
  const overrideProviderId =
    payload && typeof payload === "object" ? payload.providerInstanceId : null;
  const overrideModelId =
    payload && typeof payload === "object" ? payload.modelId : null;
  await submitTask(text, overrideProviderId, overrideModelId);
  // Task baru (queue_state=pending) -> refresh panel TASKS + beri tahu
  // ConsultantChat task mana yang menjadi milik tombol Run Task.
  submittedTaskId.value = task.id || "";
  queueRefresh.value += 1;
}

// Panel TASKS di Consultant (SATU queue global AETHER). Refresh dipicu setelah
// Run Task / event terminal task. Bukan queue subsystem kedua.
async function stopQueueTask(taskId) {
  if (!taskId) return;
  try {
    await cancelTask(taskId);
  } catch (e) {
    error.value = e.message || "Failed to stop task.";
  } finally {
    queueRefresh.value += 1;
  }
}
// View Task: buka task yang sama lewat alur history/activity existing.
function viewQueueTask(t) {
  if (t && t.task_id) openHistoryTask(t.task_id);
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
  if (s === "queued") return { label: "queued", cls: "queued" };
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

// Page header (tasks/projects/settings).
const pageTitle = computed(() => {
  if (activeNav.value === "tasks") return "Tasks";
  if (activeNav.value === "projects") return "Projects";
  if (activeNav.value === "backup") return "Backup";
  if (activeNav.value === "settings") return "Settings";
  return "Workbench";
});
const pageDesc = computed(() => {
  if (activeNav.value === "tasks") return "Live task queue and past task history.";
  if (activeNav.value === "projects") return "Workspaces registered in AETHER.";
  if (activeNav.value === "backup") return "GitHub backup, checkpoints, and recovery for the active project.";
  if (activeNav.value === "settings") return "Configure providers and models used by the AETHER workbench.";
  return "";
});

// Project yang sedang dipilih di halaman Projects (detail + GitHub Backup).
// Sumber data = daftar project launcher yang sama (bukan registry kedua).
const selectedProject = computed(() =>
  (projects.value || []).find((p) => p.id === selectedProjectId.value) || null
);

// Status GitHub Backup per project (di halaman Projects) — memakai SUMBER DATA
// YANG SAMA dengan halaman Backup (`GET /projects/<id>/github`), bukan store
// kedua. Best-effort: project tanpa konfigurasi berstatus "Not configured".
const projectGithub = ref({});
async function refreshProjectGithubStatuses() {
  const list = projects.value || [];
  const result = {};
  await Promise.all(
    list.map(async (p) => {
      try {
        result[p.id] = await getGithubConfig(p.id);
      } catch {
        result[p.id] = null;
      }
    })
  );
  projectGithub.value = result;
}
watch(activeNav, (nav) => {
  if (nav === "projects") refreshProjectGithubStatuses();
});
function githubStatusLabel(p) {
  const st = projectGithub.value[p.id];
  if (!st) return "—";
  return st.configured ? "Configured" : "Not configured";
}
function githubStatusClass(p) {
  const st = projectGithub.value[p.id];
  return st && st.configured ? "status-on" : "status-off";
}
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

// Upsert perubahan berdasarkan path: satu file = satu baris di Changes panel.
// File yang sama diedit berkali-kali memperbarui baris yang ada (bukan
// menumpuk duplikat). Move/rename memindahkan baris lama ke path baru.
function upsertChange(entry) {
  const list = changes.value;
  const idx = list.findIndex((c) => c.path === entry.path);
  if (idx >= 0) {
    list[idx] = { ...list[idx], ...entry };
    return;
  }
  const kind = String(entry.kind || "").toLowerCase();
  if ((kind.includes("move") || kind.includes("renam")) && entry.old_path) {
    const oi = list.findIndex((c) => c.path === entry.old_path);
    if (oi >= 0) {
      list[oi] = { ...list[oi], ...entry };
      return;
    }
  }
  list.push(entry);
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
      // Task baru mulai: pastikan reasoning status task sebelumnya sudah bersih.
      isReasoning.value = false;
      // Task ini BENAR-BENAR mulai running -> jadikan target tombol Stop.
      runningTaskId.value = evt.task_id || runningTaskId.value || "";
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
      if (p.provider) runtime.provider = p.provider;
      if (p.model) runtime.model = p.model;
      // AETHER mulai menunggu respons LLM -> tampilkan SATU reasoning status.
      // Dipanggil berulang kali pun tetap satu elemen (state boolean), bukan
      // entri activity/log baru.
      isReasoning.value = true;
      break;
    case "provider_response":
      if (p.provider) runtime.provider = p.provider;
      if (p.model) runtime.model = p.model;
      // Respons LLM diterima (atau error provider) -> hentikan reasoning status.
      isReasoning.value = false;
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
        // Upsert (bukan push buta): satu file = satu baris, file yang diedit
        // berkali-kali memperbarui barisnya. Changes panel ikut update live.
        upsertChange({
          kind: p.kind || "change",
          path: p.path,
          old_path: p.old_path,
          detail: p.detail,
          additions: p.additions,
          deletions: p.deletions,
          diff: p.diff,
        });
        // Update File Explorer secara INCREMENTAL (refresh direktori terdampak
        // saja; expanded/selected dipertahankan), TANPA menunggu task selesai.
        liveFsChange.value = {
          seq: ++liveFsChangeSeq,
          path: p.path,
          kind: p.kind || "change",
          old_path: p.old_path || "",
        };
      }
      break;
    case "task_completed":
      task.status = "completed";
      // Task selesai -> reasoning status harus benar-benar berhenti.
      isReasoning.value = false;
      runtime.activity = "";
      // Refresh File Explorer setelah agent selesai (file baru terlihat).
      explorerRefresh.value += 1;
      // Task terminal: lepas target tombol Stop SECARA SINKRON (tombol langsung
      // hilang), baru refresh antrian untuk memilih task running berikutnya.
      terminalTaskId.value = evt.task_id || task.id || "";
      releaseRunningTask(terminalTaskId.value);
      queueRefresh.value += 1;
      playStatusSound("completed");
      refreshTaskHistory();
      break;
    case "task_failed":
      task.status = "failed";
      // Task gagal -> hentikan reasoning status.
      isReasoning.value = false;
      // Task terminal: tombol Stop langsung hilang (sinkron, tanpa race).
      terminalTaskId.value = evt.task_id || task.id || "";
      releaseRunningTask(terminalTaskId.value);
      queueRefresh.value += 1;
      playStatusSound("failed");
      refreshTaskHistory();
      break;
    case "task_cancelled":
      task.status = "cancelled";
      // Task dibatalkan -> hentikan reasoning status (tidak ada animasi nyangkut).
      isReasoning.value = false;
      // Execution benar-benar berhenti -> indikator Agent kembali idle.
      runtime.activity = "";
      runtime.tool = "";
      // Task terminal: tombol Stop langsung hilang (sinkron, tanpa race).
      terminalTaskId.value = evt.task_id || task.id || "";
      releaseRunningTask(terminalTaskId.value);
      queueRefresh.value += 1;
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
  // Workspace direset untuk task baru -> tidak ada reasoning status tersisa.
  isReasoning.value = false;
  liveFsChange.value = null;
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
async function submitTask(text, overrideProviderInstanceId = null, overrideModelId = null) {
  error.value = "";
  submitting.value = true;
  try {
    // Provider Instance + Model dari konfigurasi LLM tersimpan (SQLite)
    // diteruskan sebagai metadata ke mekanisme AETHER existing. Runtime
    // merakit provider + api_url + api_key + model dari DB ini (bukan .env).
    // Mode tetap routing signal.
    // Override per-task (mis. dari card Task Proposal Consultant) MENANG atas
    // pilihan global; bila tidak diisi -> perilaku default (pilihan global).
    const providerId = overrideProviderInstanceId || selectedProviderInstanceId.value;
    const modelId = overrideModelId || selectedModelId.value;
    const metadata = {};
    if (providerId) metadata.provider_instance_id = providerId;
    if (modelId) metadata.model_id = modelId;
    if (selectedMode.value) metadata.mode = selectedMode.value;
    const record = await createTask(
      text,
      selectedProjectId.value || null,
      Object.keys(metadata).length ? metadata : null
    );
    task.id = record.task_id;
    task.text = record.task;
    // Status awal = KEBENARAN backend, BUKAN optimistik. Task yang dikirim
    // (Workbench Agent Input maupun Consultant Run Task) masuk SATU Global Task
    // Queue; bila slot eksekusi sedang terpakai, backend mengembalikan
    // queue_state="pending" -> task belum berjalan (menunggu slot) dan
    // ditampilkan sebagai "queued", bukan "running". Promosi ke running datang
    // dari SSE `task_started` (atau queue_state="running" pada respons ini).
    const queueState = record.queue_state;
    if (queueState === "pending") {
      // Menunggu execution slot Global Task Queue -> "queued" (bukan running).
      task.status = "queued";
    } else if (queueState === "running") {
      task.status = "running";
      // Task ini langsung mendapat slot -> target tombol Stop.
      runningTaskId.value = record.task_id;
    } else {
      task.status = record.status || "prepared";
    }
    resetAudioTracker();
    resetWorkspace();
    await refreshTasks();
    connectStream();
    composerOpen.value = false;
    // Task baru -> panel TASKS (queue global) ikut refresh meski dibuat dari
    // Agent Input (satu queue yang sama).
    queueRefresh.value += 1;
  } catch (e) {
    error.value = e.message || "Failed to create task.";
  } finally {
    submitting.value = false;
  }
}

async function stopTask() {
  // Target Stop = task yang BENAR-BENAR RUNNING (Global Task Queue). Fallback
  // ke task aktif hanya bila id running belum diketahui (mis. antrian sedang
  // dimuat). Mekanisme penghentian tetap cancel_task existing (bukan sistem
  // cancellation baru). Send TIDAK pernah menghentikan task.
  const targetId = runningTaskId.value || task.id;
  if (!targetId) return;
  try {
    const record = await cancelTask(targetId);
    // Jangan menimpa status tampilan task lain yang sedang dibuka.
    if (targetId === task.id) task.status = record.status || "cancelled";
  } catch (e) {
    error.value = e.message || "Failed to stop task.";
  } finally {
    runningTaskId.value = "";
    // Scheduler existing akan mempromosikan task pending berikutnya; refresh
    // antrian + sinkronkan target Stop ke task running yang baru (bila ada).
    queueRefresh.value += 1;
    refreshRunningTask();
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
    // Page Projects memakai daftar yang sama (satu sumber data: GET /projects).
    projects.value = launcherProjects.value;
  } catch {
    launcherProjects.value = [];
    projects.value = [];
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

// --- Projects page: Hapus Project (REGISTRY-ONLY) --------------------------
// Hapus = hapus RECORD project dari daftar AETHER. TIDAK menghapus
// folder/file project di disk (backend: DELETE /api/projects/<id> ->
// ProjectStore.delete_project -> DELETE FROM projects, tanpa menyentuh
// filesystem). Frontend hanya memicu endpoint yang sudah ada.
let noticeTimer = null;

function askProjectDelete(project) {
  if (!project || !project.id) return;
  error.value = "";
  notice.value = "";
  projectToDelete.value = project;
}

function cancelProjectDelete() {
  projectToDelete.value = null;
}

async function confirmProjectDelete() {
  const project = projectToDelete.value;
  if (!project) return;
  launcherBusy.value = true;
  error.value = "";
  try {
    await deleteProject(project.id);
    projectToDelete.value = null;
    const wasActive = Boolean(activeProject.value && activeProject.value.id === project.id);
    if (selectedProjectId.value === project.id) selectedProjectId.value = "";
    if (wasActive) {
      // Project aktif dihapus: backend sudah membersihkan active state ->
      // kembalikan UI ke Project Launcher agar tetap konsisten.
      activeProject.value = null;
      if (lastProject.value && lastProject.value.id === project.id) lastProject.value = null;
      task.id = "";
      task.text = "";
      task.status = "idle";
      resetWorkspace();
    }
    await refreshLauncherProjects();
    notice.value = `"${project.name}" dihapus dari daftar AETHER. File/folder di disk TIDAK dihapus.`;
    if (noticeTimer) clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => {
      notice.value = "";
    }, 5000);
  } catch (e) {
    // Error: JANGAN hapus entri secara optimistik — entri tetap tampil.
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

// Kode editor (Monaco) — dibuka dari File Explorer / panel CHANGES.
// `editorFile` = { path, name } dari Explorer (path relatif, bukan path baru).
// Tidak ada penulisan file dari browser: semua lewat API file backend existing.
const editorOpen = ref(false);
const editorFile = ref(null);

function openFileInEditor(file) {
  const path = file && (file.path || file.file_path);
  if (!path) return;
  error.value = "";
  editorFile.value = { path, name: (file && file.name) || String(path).split("/").pop() };
  editorOpen.value = true;
}

function closeCodeEditor() {
  editorOpen.value = false;
  editorFile.value = null;
}

// Feedback error editor memakai mekanisme error banner AETHER yang sudah ada.
function onEditorError(message) {
  error.value = message || "Editor error.";
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
  // Sinkronkan "task running" saat ini dari Global Task Queue (mis. task yang
  // sudah berjalan sebelum halaman dimuat/di-refresh), sehingga tombol Stop
  // langsung mengarah ke task yang benar.
  refreshRunningTask();
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
                <AgentActivity :events="activityEvents" :status="task.status" :is-reasoning="showReasoning" />
              </div>
            </section>
          </div>

          <!-- Agent input -> Task Composer modal.
               Send dan Stop adalah DUA aksi TERPISAH, bukan satu tombol toggle:
               Send selalu tersedia (task baru masuk Global Task Queue sebagai
               pending/queued bila slot eksekusi terpakai), Stop hanya muncul saat
               ADA task yang benar-benar RUNNING. -->
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
                class="send-btn"
                type="button"
                title="New task"
                @click="openComposer"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 5l7 7-7 7"/></svg>
              </button>
              <button
                v-if="isRunning"
                class="stop-btn"
                type="button"
                title="Stop running task"
                @click="stopTask"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
              </button>
            </div>
            <div v-if="error" class="wb-error">{{ error }}</div>
          </div>
        </div>

        <div class="ws-col right">
          <QueuePanel
            class="wb-queue"
            :default-collapsed="true"
            :refresh-key="queueRefresh"
            @stop-task="stopQueueTask"
            @view-task="viewQueueTask"
          />
          <ChangesPanel :changes="changes" :validation="validation" @open-file="openFileInEditor" />
          <FileExplorer
            :project="activeProject"
            :refresh-key="explorerRefresh"
            :live-change="liveFsChange"
            @open-file="openFileInEditor"
            @open-file-editor="openFileInEditor"
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

          <!-- Tasks: SATU halaman, DUA mode (QUEUE live + HISTORY arsip).
               Data TIDAK dicampur. Mode QUEUE memakai QueuePanel yang sama
               (queue global AETHER), mode HISTORY memakai tabel history
               existing (Report via ReportViewer). -->
          <section v-if="activeNav === 'tasks'" class="panel">
            <div class="panel-head">
              <div>
                <div class="title">Tasks</div>
                <div class="desc">Live task queue and past task history.</div>
              </div>
              <div class="seg-tabs" role="tablist" aria-label="Tasks view">
                <button
                  class="seg-tab"
                  :class="{ active: taskPageMode === 'queue' }"
                  type="button"
                  role="tab"
                  :aria-selected="taskPageMode === 'queue'"
                  @click="taskPageMode = 'queue'"
                >
                  Queue <span class="seg-badge">{{ queueCount }}</span>
                </button>
                <button
                  class="seg-tab"
                  :class="{ active: taskPageMode === 'history' }"
                  type="button"
                  role="tab"
                  :aria-selected="taskPageMode === 'history'"
                  @click="taskPageMode = 'history'"
                >
                  History <span class="seg-badge">{{ taskHistory.length }}</span>
                </button>
              </div>
            </div>

            <!-- MODE 1: QUEUE (live/actionable). v-show agar state antrian tetap
                 hidup saat berpindah mode (tanpa reload / kehilangan state). -->
            <QueuePanel
              v-show="taskPageMode === 'queue'"
              class="page-queue"
              :refresh-key="queueRefresh"
              @stop-task="stopQueueTask"
              @view-task="viewQueueTask"
            />

            <!-- MODE 2: HISTORY (arsip read-only). -->
            <div v-show="taskPageMode === 'history'">
              <div v-if="!taskHistory.length" class="panel-body"><div class="wb-empty">No task history yet.</div></div>
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
            </div>
          </section>

          <!-- Projects -->
          <section v-else-if="activeNav === 'projects'" class="panel">
            <div class="panel-head">
              <div>
                <div class="title">Projects</div>
                <div class="desc">{{ projects.length }} workspace(s)</div>
              </div>
            </div>
            <div v-if="notice" class="wb-notice">{{ notice }}</div>
            <div v-if="error" class="wb-error">{{ error }}</div>
            <div v-if="!projects.length" class="panel-body"><div class="wb-empty">No projects yet.</div></div>
            <table v-else class="aether-table">
              <thead>
                <tr>
                  <th style="width: 32%">Project</th>
                  <th style="width: 42%">Path</th>
                  <th style="width: 18%">GitHub Backup</th>
                  <th class="th-actions"></th>
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
                  <td>
                    <span class="status-tag" :class="githubStatusClass(p)">
                      {{ githubStatusLabel(p) }}
                    </span>
                  </td>
                  <td class="td-actions">
                    <!-- Hapus = hapus RECORD dari daftar AETHER saja.
                         File/folder project di disk TIDAK dihapus. -->
                    <button
                      type="button"
                      class="icon-btn danger"
                      title="Hapus dari daftar AETHER (file/folder di disk tidak dihapus)"
                      aria-label="Hapus project dari daftar AETHER"
                      @click.stop="askProjectDelete(p)"
                    >
                      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>

            <!-- Detail project + konfigurasi GitHub Backup (SUMBER DATA SAMA
                 dengan halaman Backup: endpoint /projects/<id>/github). -->
            <div v-if="selectedProject" class="proj-detail">
              <div class="panel-head">
                <div>
                  <div class="title">{{ selectedProject.name }}</div>
                  <div class="desc mono">{{ selectedProject.root || selectedProject.path }}</div>
                </div>
                <button class="btn-aether btn-ghost-a" type="button" @click="selectedProjectId = ''">
                  Close
                </button>
              </div>
              <div class="panel-body">
                <GithubBackupPanel :project="selectedProject" />
              </div>
            </div>
          </section>

          <!-- Backup (project AKTIF): konfigurasi/checkpoint/recovery GitHub.
               Komponen yang sama dengan detail Projects -> satu sumber data. -->
          <section v-else-if="activeNav === 'backup'" class="panel">
            <div class="panel-head">
              <div>
                <div class="title">Backup</div>
                <div class="desc">
                  GitHub backup for
                  <span class="mono">{{ activeProject ? activeProject.name : "—" }}</span>
                </div>
              </div>
            </div>
            <div class="panel-body">
              <GithubBackupPanel :project="activeProject" />
            </div>
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
      :submitted-task-id="submittedTaskId"
      :terminal-task-id="terminalTaskId"
      :queue-refresh-key="queueRefresh"
      @close="closeConsultant"
      @update:provider-instance-id="selectedProviderInstanceId = $event"
      @update:model-id="selectedModelId = $event"
      @run-task="runConsultantTask"
      @stop-task="stopQueueTask"
      @view-task="viewQueueTask"
    />

    <!-- ===================== AGENT REPORT MODAL ===================== -->
    <ReportViewer
      v-if="reportOpen"
      :task-id="reportTaskId"
      :status="reportStatus"
      :report="currentReport"
      @close="reportOpen = false"
    />

    <!-- ===================== CODE EDITOR MODAL (Monaco) ================ -->
    <!-- `:key` per path: buka file lain = komponen baru, sehingga instance/
         state file sebelumnya tidak bocor (Monaco di-dispose saat unmount). -->
    <CodeEditor
      v-if="editorOpen && editorFile"
      :key="editorFile.path"
      :path="editorFile.path"
      :name="editorFile.name"
      @close="closeCodeEditor"
      @error="onEditorError"
    />

    <!-- ============ HAPUS PROJECT (konfirmasi, registry-only) ========== -->
    <!-- Hapus = hapus dari DAFTAR AETHER. File/folder di disk TIDAK dihapus. -->
    <div v-if="projectToDelete" class="modal-backdrop" @click.self="cancelProjectDelete">
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-title">Hapus "{{ projectToDelete.name }}"?</div>
        <div class="modal-body">
          Project dihapus dari daftar AETHER. File/folder di disk TIDAK dihapus.
        </div>
        <div class="modal-actions">
          <button type="button" class="btn-ghost" @click="cancelProjectDelete">Cancel</button>
          <button type="button" class="btn-danger" :disabled="launcherBusy" @click="confirmProjectDelete">
            Hapus
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
