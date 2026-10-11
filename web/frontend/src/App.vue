<script setup>
// AETHER Engineering Workbench (#52 rework).
//
// COMPOSITION ROOT. Frontend TIPIS: hanya HTTP ke Django Gateway (#50) dan SSE
// (#51). TIDAK ada logic agent (runtime/loop/orchestrator/planning/tools/
// terminal/validation/recovery/routing/fallback/filesystem) di frontend.
// TIDAK ada Project Registry / Session Store / Task Lifecycle / Event System
// kedua: semua dari backend AETHER yang sudah ada.
//
// Semua logic dipindahkan ke modul dengan satu tanggung jawab di ./composables:
//   - useAgentActivity : event stream state + dedup + Agent Activity (#51)
//   - useEventStream   : SSE connection (global queue + task-scoped) & reconnect
//   - useTaskQueue     : Global Task Queue, task running/dipantau, adopt,
//                        deferred follow, view/stop queue task
//   - useTaskLifecycle : submit/stop task, buka task history, report final
//   - useWorkspace     : project registry, active project, launcher, policy
//   - useChanges       : changes per task (changesByTask + agregasi)
//   - useTaskTiming    : timing/durasi Task Card + ticker tampilan
//   - useTaskTelemetry : provider/model + LLM rounds/tool calls/tokens
//   - useLifecycle     : lifecycle step + status/tag task
//   - useTaskData      : daftar task + history (queue & arsip)
//   - useApprovals     : approval (ASK) Allow/Deny
//   - useConsultant    : modal Consultant + session aktif + Task Proposal
//   - useComposer      : Task Composer + konfigurasi LLM New Task
//   - useCodeEditor    : modal editor kode (Monaco)
//   - useShellState    : navigasi sidebar + label header/footer
//   - useNotifications : banner error + notice sementara
// App.vue hanya merakit modul di atas (wiring) dan merender root UI.
//
// Layout 3 area: Sidebar | Agent Workbench | Changes/File Explorer.
// Bootstrap 5 dipakai untuk layout/spacing/form/button/dropdown/responsive.

import { onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
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
import ProjectPolicyPanel from "./components/ProjectPolicyPanel.vue";
import ConsultantChat from "./components/ConsultantChat.vue";
import ExtensionManager from "./components/ExtensionManager.vue";
import { getHealth } from "./api.js";
// Helper murni presentasi (dipakai langsung oleh template).
import { formatTs, statusTagClass } from "./taskHistory.js";
import { ACTIVE_TASK_STATES, executionLabel } from "./executionMode.js";
// Modul (satu tanggung jawab per file).
import { useNotifications } from "./composables/useNotifications.js";
import { useShellState } from "./composables/useShellState.js";
import { useTaskData } from "./composables/useTaskData.js";
import { useTaskTiming } from "./composables/useTaskTiming.js";
import { useChanges } from "./composables/useChanges.js";
import { useApprovals } from "./composables/useApprovals.js";
import { useTaskQueue } from "./composables/useTaskQueue.js";
import { useEventStream } from "./composables/useEventStream.js";
import { useAgentActivity } from "./composables/useAgentActivity.js";
import { useLifecycle } from "./composables/useLifecycle.js";
import { useTaskTelemetry } from "./composables/useTaskTelemetry.js";
import { useTaskLifecycle } from "./composables/useTaskLifecycle.js";
import { useComposer } from "./composables/useComposer.js";
import { useWorkspace } from "./composables/useWorkspace.js";
import { useConsultant } from "./composables/useConsultant.js";
import { useCodeEditor } from "./composables/useCodeEditor.js";

// ---------------------------------------------------------------------------
// State BERSAMA (dipakai lintas modul): task yang SEDANG dipantau + project
// aktif. Dibuat di composition root agar hanya ada SATU task yang dipantau.
// ---------------------------------------------------------------------------
const task = reactive({ id: "", text: "", status: "idle", executionMode: "queue" });
const selectedProjectId = ref("");

// Penanda refresh QueuePanel (dinaikkan saat submit/terminal/stop task).
const queueRefresh = ref(0);
const bumpQueueRefresh = () => { queueRefresh.value += 1; };

// Halaman Tasks: SATU halaman, DUA mode (QUEUE live + HISTORY arsip). Default
// saat halaman dibuka = QUEUE. Data queue & history TIDAK dicampur.
const taskPageMode = ref("queue");

// Health gateway (indikator sistem).
const health = ref(null);

// Notifikasi global (banner error + notice sementara).
const { error, notice, setError, clearNotice, showNotice } = useNotifications();

// ---------------------------------------------------------------------------
// Wiring modul. Ketergantungan lintas-modul yang melingkar diberikan sebagai
// callback LAZY (dievaluasi saat runtime) melalui `let` di bawah, sehingga
// tidak ada modul yang perlu meng-import modul lain.
// ---------------------------------------------------------------------------
let activity = null;
let stream = null;

// Chrome shell (navigasi + label header/footer).
const shell = useShellState({
  getRuntime: () => activity.runtime,
  getConfig: () => composer.config,
  getQueueCount: () => queue.queueItems.value.length,
  getProjectCount: () => workspace.projects.value.length,
});

// Daftar task + history (arsip) untuk project aktif.
const taskData = useTaskData({ selectedProjectId });

// Timing/durasi Task Card.
const timing = useTaskTiming({ getStatus: () => task.status });

// Changes per-task (isolasi antar task).
const changesState = useChanges({
  task,
  isViewingRunningTask: () => queue.isViewingRunningTask(),
  // Id task aktif/pending dari queue + daftar task (untuk agregasi changes).
  getActiveTaskIds: () => {
    const ids = new Set();
    for (const item of [...queue.queueItems.value, ...taskData.tasks.value]) {
      const state = String(item.queue_state || item.status || "").toLowerCase();
      if (ACTIVE_TASK_STATES.includes(state)) {
        if (item.task_id || item.id) ids.add(item.task_id || item.id);
      }
    }
    return ids;
  },
});

// Approval (ASK) Allow/Deny.
const approvalsState = useApprovals();

// Global Task Queue + task yang dipantau.
const queue = useTaskQueue({
  task,
  selectedProjectId,
  resetWorkspace: () => activity && activity.resetWorkspace(),
  ensureStream: () => stream && stream.ensureStream(),
  markStartedIfUnset: timing.markStartedIfUnset,
  bumpQueueRefresh,
  getViewingHistory: () => Boolean(activity && activity.historyEvents.value),
  openTask: (taskId) => taskLifecycle.openHistoryTask(taskId),
  setError,
});

// SSE (#51): stream global queue + stream task-scoped.
stream = useEventStream({
  task,
  handleEvent: (evt) => activity && activity.handleEvent(evt),
});

// Agent Activity + event state (#51).
activity = useAgentActivity({
  task,
  timing,
  changes: changesState,
  queue,
  approvals: approvalsState,
  bumpQueueRefresh,
  refreshTaskHistory: () => taskData.refreshTaskHistory(),
  clearReport: () => taskLifecycle && taskLifecycle.clearReport(),
  getCopyMeta: () => ({
    taskId: task.id,
    status: task.status,
    provider: telemetry.taskProvider.value,
    model: telemetry.taskModel.value,
    duration: timing.taskDurationLabel.value,
    execution: lifecycle.taskExecutionLabel.value,
    llmRounds: telemetry.taskLlmRounds.value,
    toolCalls: telemetry.taskToolCalls.value,
    tokens: telemetry.taskTokensLabel.value,
  }),
});

// Lifecycle step + status/tag task (presentasi).
const lifecycle = useLifecycle({
  task,
  activityPhase: activity.activityPhase,
  lifecycleMilestones: activity.lifecycleMilestones,
  isRunning: queue.isRunning,
});

// Telemetry Agent Card (provider/model + rounds/tool calls/tokens).
const telemetry = useTaskTelemetry({
  activityEvents: activity.activityEvents,
  runtime: activity.runtime,
  task,
  taskExecutionLabel: lifecycle.taskExecutionLabel,
  taskDurationLabel: timing.taskDurationLabel,
});

// Task Composer + konfigurasi LLM New Task.
const composer = useComposer({
  submitTask: (text, providerId, modelId, executionMode, images) =>
    taskLifecycle.submitTask(text, providerId, modelId, executionMode, images),
});

// Lifecycle task: submit/stop + buka task history + report.
const taskLifecycle = useTaskLifecycle({
  task,
  taskData,
  timing,
  changes: changesState,
  activity,
  queue,
  stream,
  selectedProjectId,
  selectedMode: composer.selectedMode,
  selectedProviderInstanceId: composer.selectedProviderInstanceId,
  selectedModelId: composer.selectedModelId,
  selectedExecutionMode: composer.selectedExecutionMode,
  closeComposer: composer.closeComposer,
  setActiveNav: (v) => { shell.activeNav.value = v; },
  setError,
  bumpQueueRefresh,
});

// Workspace / project state.
const workspace = useWorkspace({
  task,
  selectedProjectId,
  taskData,
  changes: changesState,
  activity,
  stream,
  setActiveNav: (v) => { shell.activeNav.value = v; },
  setError,
  clearNotice,
  showNotice,
});

// Consultant (modal) + Task Proposal -> task Agent.
const consultant = useConsultant({
  submitTask: (text, providerId, modelId, executionMode) =>
    taskLifecycle.submitTask(text, providerId, modelId, executionMode),
  bumpQueueRefresh,
});

// Modal editor kode (Monaco).
const codeEditor = useCodeEditor({ setError });

// ---------------------------------------------------------------------------
// Watchers (composition-level, bukan logic domain).
// ---------------------------------------------------------------------------

// Setiap refresh antrian (submit/terminal/stop) -> perbarui "task running".
watch(queueRefresh, () => {
  queue.refreshRunningTask();
});

// Halaman Tasks dibuka -> mode QUEUE + segarkan dari sumber masing-masing.
watch(shell.activeNav, (nav) => {
  if (nav === "tasks") {
    taskPageMode.value = "queue";
    queue.refreshRunningTask();
    taskData.refreshTaskHistory();
  }
});

// Project aktif berubah -> refresh daftar task (queue), history, & running.
// Sekaligus ganti slot sesi Consultant aktif ke project baru (ISOLASI: tidak
// me-resume sesi project lain).
watch(selectedProjectId, () => {
  taskData.refreshTasks();
  taskData.refreshTaskHistory();
  queue.refreshRunningTask();
  consultant.setActiveProject(selectedProjectId.value);
});

// Konfirmasi Stop Task: pasang/lepas listener Escape.
watch(taskLifecycle.stopConfirmOpen, (open) => {
  if (open) {
    document.addEventListener('keydown', taskLifecycle.onStopConfirmKeydown);
  } else {
    document.removeEventListener('keydown', taskLifecycle.onStopConfirmKeydown);
  }
});

// ---------------------------------------------------------------------------
// Lifecycle komponen.
// ---------------------------------------------------------------------------
onMounted(async () => {
  try {
    health.value = await getHealth();
  } catch {
    health.value = null;
  }
  // Konfigurasi AETHER + Provider Instance/Model untuk New Task.
  await composer.loadConfig();
  await composer.refreshLLMProviders();
  await workspace.refreshLauncherProjects();
  // Baca project/session terakhir untuk ditawarkan "buka kembali" di launcher.
  // AETHER TIDAK auto-masuk Workbench: user harus menentukan workspace dulu.
  await workspace.loadLastProject();
  // Sinkronkan slot sesi Consultant aktif ke project yang sedang aktif
  // (ISOLASI per project; tidak me-resume sesi project lain).
  consultant.setActiveProject(selectedProjectId.value);
  // Sinkronkan "task running" saat ini dari Global Task Queue (mis. task yang
  // sudah berjalan sebelum halaman dimuat/di-refresh).
  queue.refreshRunningTask();
});

onBeforeUnmount(() => {
  // Bersihkan interval durasi Task Card agar tidak ada timer nyangkut.
  timing.stopDurationTimer();
  stream.closeStream();
  document.removeEventListener('keydown', taskLifecycle.onStopConfirmKeydown);
  // Bersihkan timer feedback tombol Copy Agent Activity.
  activity.clearActivityCopyTimer();
});

// ---------------------------------------------------------------------------
// Expose ke template (nama tetap sama agar template tidak berubah).
// ---------------------------------------------------------------------------
// Shell / navigation.
const {
  activeNav,
  workspaceNav,
  settingsItem,
  navBadge,
  pageTitle,
  pageDesc,
  gatewayAddress,
  modelLabel,
  providerLabel,
  aetherVersion,
  extensionRefreshKey,
} = shell;

// Workspace / project.
const {
  activeProject,
  projects,
  launcherProjects,
  launcherBusy,
  lastProject,
  projectToDelete,
  policyProject,
  closeProjectConfirm,
  openProject,
  createNewProject,
  removeProject,
  askProjectDelete,
  cancelProjectDelete,
  confirmProjectDelete,
  openProjectPolicy,
  closeProjectPolicy,
  askCloseProject,
  cancelCloseProject,
  confirmCloseProject,
  openExplorer,
} = workspace;

// Task data + history.
const { tasks, taskHistory, groupedTaskHistory, historyCopyFeedback, copyHistoryPrompt } = taskData;

// Changes per task.
const { changes, aggregateChanges } = changesState;

// Agent Activity + event state.
const {
  activityEvents,
  showReasoning,
  activityCopied,
  copyAgentActivity,
  validation,
  activityPhase,
  explorerRefresh,
  liveFsChange,
} = activity;

// Global Task Queue.
const {
  runningTaskId,
  queueCount,
  terminalTaskId,
  isRunning,
  stopQueueTask,
  viewQueueTask,
} = queue;

// Lifecycle / status.
const { agentStatus, agentDotClass, taskTag, lifecycleSteps, lifecyclePct, taskExecutionLabel } =
  lifecycle;

// Timing.
const { taskDurationLabel, taskTimerLive } = timing;

// Telemetry.
const {
  showTaskMeta,
  taskProvider,
  taskModel,
  taskRoundLabel,
  showTaskTelemetry,
  taskLlmRounds,
  taskToolCalls,
  taskTokensLabel,
  taskTokensTooltip,
} = telemetry;

// Task lifecycle actions.
const {
  submitting,
  currentReport,
  reportOpen,
  reportTaskId,
  reportStatus,
  stopConfirmOpen,
  stopInProgress,
  requestStop,
  cancelStopConfirm,
  confirmStop,
  openHistoryTask,
  openReport,
} = taskLifecycle;

// Composer + konfigurasi LLM.
const {
  composerOpen,
  config,
  selectedMode,
  llmProviders,
  selectedProviderInstanceId,
  selectedModelId,
  selectedExecutionMode,
  openComposer,
  closeComposer,
  onComposerSubmit,
} = composer;

// Consultant.
const {
  consultantOpen,
  activeConsultantSessionId,
  submittedTaskId,
  openConsultant,
  closeConsultant,
  onConsultantSessionChange,
  runConsultantTask,
} = consultant;

// Approval (ASK).
const { approvals, approvalBusy, approvalError, decideApproval } = approvalsState;

// SSE connection status (indikator Online/Offline + "live").
const { connected } = stream;

// Code editor.
const { editorOpen, editorFile, openFileInEditor, closeCodeEditor, onEditorError } = codeEditor;
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
        <div class="nav-item" @click="askCloseProject">
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
          <span class="v">{{ gatewayAddress }}</span>
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
          <span class="chip">Changes: <span class="mono">{{ aggregateChanges.size }}</span></span>
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
                    <!-- Metadata eksekusi: provider/model + execution + round + duration.
                         Execution (Queue/Parallel) selalu tampil agar task
                         parallel mudah dibedakan. Sumber = execution_mode
                         task (fallback "queue" untuk task lama). -->
                    <div v-if="showTaskMeta" class="task-meta">
                      <span v-if="taskProvider" class="tm-item" :title="`Provider: ${taskProvider}`">
                        <svg class="tm-ico" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.5 19a4.5 4.5 0 0 0 0-9 6 6 0 0 0-11.6 1.5A3.5 3.5 0 0 0 6.5 19z"/></svg>
                        <span class="tm-key">Provider</span>
                        <span class="tm-val">{{ taskProvider }}</span>
                      </span>
                      <span v-if="taskProvider && taskModel" class="tm-sep">·</span>
                      <span v-if="taskModel" class="tm-item" :title="`Model: ${taskModel}`">
                        <svg class="tm-ico" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></svg>
                        <span class="tm-key">Model</span>
                        <span class="tm-val">{{ taskModel }}</span>
                      </span>
                      <span v-if="task.id && taskExecutionLabel" class="tm-sep">·</span>
                      <span v-if="task.id" class="tm-item" :title="`Execution: ${taskExecutionLabel}`">
                        <svg class="tm-ico" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z"/></svg>
                        <span class="tm-key">Execution</span>
                        <span class="tm-val">{{ taskExecutionLabel }}</span>
                      </span>
                      <span v-if="taskRoundLabel" class="tm-sep">·</span>
                      <span v-if="taskRoundLabel" class="tm-item" :title="taskRoundLabel">
                        <svg class="tm-ico" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 8a6 6 0 1 0-8 5.7V21l8-4v-4.3A6 6 0 0 0 16 8z"/></svg>
                        <span class="tm-key">Round</span>
                        <span class="tm-val">{{ taskRoundLabel }}</span>
                      </span>
                      <span v-if="(taskProvider || taskModel || (task.id && taskExecutionLabel)) && taskDurationLabel" class="tm-sep">·</span>
                      <span
                        v-if="taskDurationLabel"
                        class="tm-item tm-duration"
                        :class="{ live: taskTimerLive }"
                        :title="taskTimerLive ? 'Elapsed (live)' : 'Total duration'"
                      >
                        <svg class="tm-ico" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
                        <span class="tm-key">Duration</span>
                        <span class="tm-val">{{ taskDurationLabel }}</span>
                      </span>
                      <!-- Telemetry: LLM Rounds / Tool Calls / Tokens. Nilai
                           AKTUAL dari event lifecycle AETHER existing (bukan
                           dummy/hardcoded). Menyatu dengan item meta lain
                           (kelas .tm-item/.tm-key/.tm-val yang sama). -->
                      <template v-if="showTaskTelemetry">
                        <span
                          v-if="taskProvider || taskModel || (task.id && taskExecutionLabel) || taskDurationLabel"
                          class="tm-sep"
                        >·</span>
                        <span class="tm-item" title="Actual LLM/provider invocations">
                          <svg class="tm-ico" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 0 1 15.5-6.3L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.5 6.3L3 16"/><path d="M3 21v-5h5"/></svg>
                          <span class="tm-key">LLM Rounds</span>
                          <span class="tm-val">{{ taskLlmRounds }}</span>
                        </span>
                        <span class="tm-sep">·</span>
                        <span class="tm-item" title="Tool executions">
                          <svg class="tm-ico" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17l3 3 5.3-5.3a4 4 0 0 0 5.4-5.4l-2.3 2.3-2-2z"/></svg>
                          <span class="tm-key">Tool Calls</span>
                          <span class="tm-val">{{ taskToolCalls }}</span>
                        </span>
                        <span class="tm-sep">·</span>
                        <span
                          class="tm-item"
                          :title="taskTokensTooltip"
                        >
                          <svg class="tm-ico" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 9h16M4 15h16M10 3 8 21M16 3l-2 18"/></svg>
                          <span class="tm-key">Tokens</span>
                          <span class="tm-val">{{ taskTokensLabel }}</span>
                        </span>
                      </template>
                    </div>
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
                  <span>{{ activityPhase || task.status || "idle" }}</span>
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
                  <button
                    type="button"
                    class="act-copy-btn"
                    :class="{ copied: activityCopied }"
                    :title="activityCopied ? 'Copied' : 'Copy agent activity'"
                    :aria-label="activityCopied ? 'Copied' : 'Copy agent activity'"
                    @click="copyAgentActivity"
                  >
                    <svg
                      class="act-copy-ico"
                      width="12"
                      height="12"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="1.9"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                      aria-hidden="true"
                    >
                      <template v-if="activityCopied">
                        <path d="M20 6L9 17l-5-5"></path>
                      </template>
                      <template v-else>
                        <rect x="9" y="9" width="12" height="12" rx="2"></rect>
                        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                      </template>
                    </svg>
                    <span class="act-copy-label">{{ activityCopied ? "Copied" : "Copy" }}</span>
                  </button>
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
                :disabled="stopInProgress"
                @click="requestStop"
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
            :project-id="selectedProjectId || null"
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
              :project-id="selectedProjectId || null"
              :refresh-key="queueRefresh"
              @stop-task="stopQueueTask"
              @view-task="viewQueueTask"
            />

            <!-- MODE 2: HISTORY (arsip read-only). -->
            <div v-show="taskPageMode === 'history'">
              <div v-if="!taskHistory.length" class="panel-body"><div class="wb-empty">No task history yet.</div></div>
              <div v-else class="history-groups">
                <div v-for="grp in groupedTaskHistory" :key="grp.label" class="hist-group">
                  <div class="hist-group-head">{{ grp.label }} <span class="hist-group-count">({{ grp.items.length }})</span></div>
                  <table class="aether-table hist-table">
                    <thead>
                      <tr>
                        <th style="width: 52%">Task</th>
                        <th style="width: 16%">Status</th>
                        <th style="width: 20%">Updated</th>
                        <th style="width: 12%"></th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="t in grp.items" :key="t.task_id" class="clickable" @click="openHistoryTask(t.task_id)">
                        <td>
                          <div class="cell-name">
                            <span class="avatar">T</span>
                            <div>
                              <div class="name name-clamp">{{ t.task || "(no prompt)" }}</div>
                              <div class="meta meta-clamp">{{ t.task_id }} · {{ executionLabel(t.execution_mode || t.executionMode) }}</div>
                            </div>
                          </div>
                        </td>
                        <td><span class="status-tag" :class="statusTagClass(t.status)">{{ t.status }}</span><span class="q-exec hist-exec" :class="String(t.execution_mode || t.executionMode || '').toLowerCase() === 'parallel' ? 'parallel' : 'queue'">{{ executionLabel(t.execution_mode || t.executionMode) }}</span></td>
                        <td><span class="mono meta">{{ formatTs(t.last_timestamp) }}</span></td>
                        <td class="row-actions hist-actions">
                          <button
                            type="button"
                            class="q-copy-btn hist-copy"
                            :class="{ copied: historyCopyFeedback === t.task_id }"
                            :title="historyCopyFeedback === t.task_id ? 'Copied' : 'Copy prompt'"
                            aria-label="Copy prompt"
                            @click.stop="copyHistoryPrompt(t)"
                          >
                            <svg v-if="historyCopyFeedback === t.task_id" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>
                            <svg v-else width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                          </button>
                          <button class="report-btn" type="button" title="View Agent Report" @click.stop="openReport(t.task_id)">Report</button>
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
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
                  <th style="width: 34%">Project</th>
                  <th style="width: 48%">Path</th>
                  <th class="th-actions"></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="p in projects" :key="p.id" class="clickable">
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
                  <td class="td-actions">
                    <!-- Project Settings: policy melekat PER PROJECT
                         (`.aether/settings/permissions.json` + prompt Agents). -->
                    <button
                      type="button"
                      class="icon-btn policy"
                      title="Project Settings"
                      aria-label="Project Settings"
                      @click.stop="openProjectPolicy(p)"
                    >
                      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H1a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H7a1.7 1.7 0 0 0 1-1.5V1a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V7a1.7 1.7 0 0 0 1.5 1H23a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>
                    </button>
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

            <!-- Project Settings / Policy dibuka sebagai MODAL (bukan panel
                 inline di bawah tombol gear). Permissions untuk project yang
                 dipilih di-render di area modal (lihat bawah template). -->
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

          <!-- Extension Management -->
          <section v-else-if="activeNav === 'extension'" class="panel">
            <div class="panel-body">
              <ExtensionManager :key="extensionRefreshKey" @error="(m) => { error = m; }" />
            </div>
          </section>

          <!-- Settings (kelola provider/model/credential via Gateway). -->
          <SettingsView v-else-if="activeNav === 'settings'" :config="config" />
          <div v-else class="panel"><div class="panel-body"><div class="wb-empty">Unknown section.</div></div></div>
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
        <span class="sb">AETHER v{{ aetherVersion }}</span>
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
          :execution-mode="selectedExecutionMode"
          @submit="onComposerSubmit"
          @stop="requestStop"
          @update:provider-instance-id="selectedProviderInstanceId = $event"
          @update:model-id="selectedModelId = $event"
          @update:mode="selectedMode = $event"
          @update:execution-mode="selectedExecutionMode = $event"
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
      :running-task-id="runningTaskId"
      :submitted-task-id="submittedTaskId"
      :terminal-task-id="terminalTaskId"
      :queue-refresh-key="queueRefresh"
      :project-id="selectedProjectId || null"
      :active-session-id="activeConsultantSessionId"
      @close="closeConsultant"
      @update:provider-instance-id="selectedProviderInstanceId = $event"
      @update:model-id="selectedModelId = $event"
      @update:active-session-id="onConsultantSessionChange"
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

    <!-- ============ PROJECT SETTINGS MODAL (per project) =============== -->
    <!-- Dibuka dari tombol gear pada baris project (Sidebar -> Projects).
         Tab Security = policy AKTUAL project (`.aether/settings/permissions.json`).
         Tab Agents   = System Prompt Agent/Consultant project
                        (`.aether/settings/agent.json` / `consultant.json`).
         TIDAK lagi berupa panel inline di bawah tombol gear. -->
    <ProjectPolicyPanel
      v-if="policyProject"
      :key="policyProject.id"
      :project="policyProject"
      @close="closeProjectPolicy"
    />

    <!-- ============ APPROVAL (ASK) MODAL =============================== -->
    <!-- Action Agent ditahan policy (mode ASK) -> user Allow/Deny di sini.
         Menampilkan jenis action + target/path; Allow melanjutkan action
         tertahan, Deny membatalkan & mengembalikan hasil ke Agent. Decision
         dikirim via endpoint resolve (terikat task/session yang benar). -->
    <div
      v-if="approvals.length"
      class="modal-backdrop"
      @click.self="decideApproval(false)"
    >
      <div class="modal approval-m" role="dialog" aria-modal="true" aria-labelledby="approval-title">
        <div class="modal-title" id="approval-title">Approval required</div>
        <div class="modal-body approval-body">
          <div class="approval-line">
            The agent wants to <strong>{{ approvals[0].tool || "run an action" }}</strong>.
          </div>
          <div class="approval-kv">
            <span class="approval-k">Action</span>
            <span class="mono">{{ approvals[0].tool || "—" }}</span>
          </div>
          <div class="approval-kv">
            <span class="approval-k">Target / path</span>
            <span class="mono">{{ approvals[0].target || "—" }}</span>
          </div>
          <div v-if="approvals[0].matrix_action || approvals[0].scope" class="approval-kv">
            <span class="approval-k">Policy cell</span>
            <span class="mono">
              {{ approvals[0].matrix_action || "—" }} / {{ approvals[0].scope || "—" }}
            </span>
          </div>
          <div v-if="approvals[0].task_id" class="approval-kv">
            <span class="approval-k">Task</span>
            <span class="mono">{{ approvals[0].task_id }}</span>
          </div>
          <div v-if="approvals[0].reason" class="approval-reason">{{ approvals[0].reason }}</div>
          <div v-if="approvals.length > 1" class="approval-more">
            +{{ approvals.length - 1 }} more pending approval(s)
          </div>
          <div v-if="approvalError" class="wb-error">{{ approvalError }}</div>
        </div>
        <div class="modal-actions">
          <button type="button" class="btn-ghost" :disabled="approvalBusy" @click="decideApproval(false)">
            Deny
          </button>
          <button type="button" class="btn-primary" :disabled="approvalBusy" @click="decideApproval(true)">
            {{ approvalBusy ? "Applying…" : "Allow" }}
          </button>
        </div>
      </div>
    </div>

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

    <!-- ============ CLOSE PROJECT (konfirmasi) ========================= -->
    <div v-if="closeProjectConfirm" class="modal-backdrop" @click.self="cancelCloseProject">
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-title">Apakah Anda yakin ingin menutup project?</div>
        <div class="modal-body">
          Project akan ditutup dan Anda kembali ke Project Launcher. File/folder di disk TIDAK dihapus.
        </div>
        <div class="modal-actions">
          <button type="button" class="btn-ghost" @click="cancelCloseProject">Cancel</button>
          <button type="button" class="btn-primary" @click="confirmCloseProject">Close</button>
        </div>
      </div>
    </div>

    <!-- ============ STOP TASK (konfirmasi) =============================== -->
    <div v-if="stopConfirmOpen" class="modal-backdrop" @click.self="cancelStopConfirm">
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="stop-confirm-title">
        <div id="stop-confirm-title" class="modal-title">Stop Task?</div>
        <div class="modal-body">
          Task yang sedang berjalan akan dihentikan.<br />
          Apakah Anda yakin ingin berhenti?
        </div>
        <div class="modal-actions">
          <button type="button" class="btn-ghost" :disabled="stopInProgress" @click="cancelStopConfirm">Cancel</button>
          <button type="button" class="btn-danger" :disabled="stopInProgress" @click="confirmStop">Stop</button>
        </div>
      </div>
    </div>
  </div>
</template>
