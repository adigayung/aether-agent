<script setup>
// AETHER Task List / Queue panel (UI).
//
// Panel ini adalah SALAH SATU UI dari SATU queue GLOBAL AETHER. Sumber data =
// TaskRecord in-memory backend (GET /api/tasks/queue), sama dengan yang dibaca
// Agent Workbench nanti. TIDAK ada TaskManager/queue subsystem kedua.
//
// Pada tahap ini belum ada scheduler serial: queue_state (pending/running/
// disabled/done) adalah proyeksi UI + niat user (disable = jangan dieksekusi).
// Disable != Cancel: Stop task RUNNING tetap memakai cancel_task existing.
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import {
  disableQueueTask,
  enableQueueTask,
  listTaskQueue,
  moveQueueTask,
  removeQueueTask,
} from "../api.js";

const props = defineProps({
  // Penanda refresh dari parent (mis. setelah Run Task / event terminal).
  refreshKey: { type: Number, default: 0 },
});

const emit = defineEmits(["stop-task", "view-task"]);

const tasks = ref([]);
const error = ref("");
const collapsed = ref(false);
const loading = ref(false);

// Context menu state (reuse pola .ctx-menu existing di project).
const ctxOpen = ref(false);
const ctxMenu = ref(null);
const ctxTask = ref(null);

const runningCount = computed(
  () => tasks.value.filter((t) => t.queue_state === "running").length
);

function runLabel(t) {
  if (t.queue_state === "running") return "RUNNING";
  if (t.queue_state === "disabled") return "DISABLED";
  return "PENDING";
}

function stateIcon(t) {
  if (t.queue_state === "running") return "●";
  if (t.queue_state === "disabled") return "◌";
  return "①";
}

function queuePosition(t) {
  // Posisi 1,2,3... untuk item pending (urutan eksekusi antrian).
  let n = 0;
  for (const item of tasks.value) {
    if (item.queue_state === "pending") {
      n += 1;
      if (item.task_id === t.task_id) return n;
    }
  }
  return null;
}

function shortId(id) {
  return (id || "").slice(0, 8);
}

function shortText(t) {
  const s = (t.task || "").replace(/\s+/g, " ").trim();
  if (!s) return "(no prompt)";
  return s.length > 60 ? s.slice(0, 60) + "…" : s;
}

async function load() {
  loading.value = true;
  try {
    const data = await listTaskQueue();
    tasks.value = data.tasks || [];
    error.value = "";
  } catch (e) {
    // Endpoint queue mungkin belum tersedia; UI tetap aman.
    error.value = "";
  } finally {
    loading.value = false;
  }
}

function toggleCollapse() {
  collapsed.value = !collapsed.value;
}

// --- Context menu ----------------------------------------------------------
function openContext(e, t) {
  e.preventDefault();
  e.stopPropagation();
  // Menu mengikuti posisi kursor; tetap dalam viewport.
  const x = Math.min(e.clientX, window.innerWidth - 190);
  const y = Math.min(e.clientY, window.innerHeight - 200);
  ctxMenu.value = { x, y };
  ctxTask.value = t;
  ctxOpen.value = true;
}

function closeContextMenu() {
  ctxOpen.value = false;
  ctxTask.value = null;
}

function onDocumentClick(e) {
  if (ctxOpen.value && !e.target.closest(".ctx-menu")) closeContextMenu();
}
function onDocumentKeydown(e) {
  if (e.key === "Escape" && ctxOpen.value) closeContextMenu();
}

// --- Aksi queue ------------------------------------------------------------
async function ctxMove(direction) {
  const t = ctxTask.value;
  closeContextMenu();
  if (!t) return;
  try {
    const data = await moveQueueTask(t.task_id, direction);
    tasks.value = data.tasks || tasks.value;
  } catch (e) {
    error.value = e.message || "Gagal menggeser task.";
  }
}

async function ctxDisable() {
  const t = ctxTask.value;
  closeContextMenu();
  if (!t) return;
  try {
    await disableQueueTask(t.task_id);
    await load();
  } catch (e) {
    error.value = e.message || "Gagal men-disable task.";
  }
}

async function ctxEnable() {
  const t = ctxTask.value;
  closeContextMenu();
  if (!t) return;
  try {
    await enableQueueTask(t.task_id);
    await load();
  } catch (e) {
    error.value = e.message || "Gagal meng-enable task.";
  }
}

async function ctxRemove() {
  const t = ctxTask.value;
  closeContextMenu();
  if (!t) return;
  const msg =
    t.queue_state === "disabled"
      ? `Remove disabled task "${shortText(t)}"? This task has not started yet.`
      : `Remove task "${shortText(t)}"? This task has not started yet.`;
  if (!window.confirm(msg)) return;
  try {
    await removeQueueTask(t.task_id);
    await load();
  } catch (e) {
    error.value = e.message || "Gagal menghapus task.";
  }
}

function ctxStop() {
  const t = ctxTask.value;
  closeContextMenu();
  if (!t) return;
  emit("stop-task", t.task_id);
}

function ctxView() {
  const t = ctxTask.value;
  closeContextMenu();
  if (!t) return;
  emit("view-task", t);
}

let poll = null;
onMounted(() => {
  load();
  document.addEventListener("click", onDocumentClick);
  document.addEventListener("keydown", onDocumentKeydown);
  // Refresh ringan berkala (tidak ada event queue khusus di tahap ini).
  // Event terminal task (SSE) juga memicu refresh via prop refreshKey.
  poll = setInterval(load, 5000);
});
onBeforeUnmount(() => {
  document.removeEventListener("click", onDocumentClick);
  document.removeEventListener("keydown", onDocumentKeydown);
  if (poll) clearInterval(poll);
});

watch(
  () => props.refreshKey,
  () => load()
);
</script>

<template>
  <section class="block queue-block" :class="{ collapsed }">
    <div class="q-head" @click="toggleCollapse">
      <span class="q-caret">{{ collapsed ? "▸" : "▾" }}</span>
      <span class="q-title">TASKS</span>
      <span class="q-badge">[{{ tasks.length }}]</span>
      <span v-if="runningCount" class="q-running-dot" title="Task running">●</span>
      <span class="q-count">{{ tasks.length }} task(s)</span>
    </div>

    <div v-show="!collapsed" class="q-body">
      <div v-if="!tasks.length" class="q-empty">No tasks in queue.</div>
      <ul v-else class="q-list">
        <li
          v-for="t in tasks"
          :key="t.task_id"
          class="q-item"
          :class="t.queue_state"
          :title="t.task || ''"
          @contextmenu="openContext($event, t)"
        >
          <span class="q-ico" :class="t.queue_state">{{ stateIcon(t) }}</span>
          <span class="q-state" :class="t.queue_state">{{ runLabel(t) }}</span>
          <span v-if="t.queue_state === 'pending'" class="q-pos">{{ queuePosition(t) }}</span>
          <span class="q-text">{{ shortText(t) }}</span>
          <span class="q-id">{{ shortId(t.task_id) }}</span>
        </li>
      </ul>
      <div v-if="error" class="q-err">{{ error }}</div>
    </div>

    <!-- Context Menu (pola .ctx-menu existing) -->
    <div
      v-if="ctxOpen && ctxMenu && ctxTask"
      class="ctx-menu"
      :style="{ left: ctxMenu.x + 'px', top: ctxMenu.y + 'px' }"
      @click.stop
      @contextmenu.prevent
    >
      <template v-if="ctxTask.queue_state === 'running'">
        <div class="ctx-item" @click="ctxStop()">Stop</div>
        <div class="ctx-sep"></div>
        <div class="ctx-item" @click="ctxView()">View Task</div>
      </template>
      <template v-else>
        <div class="ctx-item" @click="ctxMove('up')">Move Up</div>
        <div class="ctx-item" @click="ctxMove('down')">Move Down</div>
        <div class="ctx-sep"></div>
        <div v-if="ctxTask.queue_state === 'disabled'" class="ctx-item" @click="ctxEnable()">Enable</div>
        <div v-else class="ctx-item" @click="ctxDisable()">Disable</div>
        <div class="ctx-sep"></div>
        <div class="ctx-item" @click="ctxView()">View Task</div>
        <div class="ctx-sep"></div>
        <div class="ctx-item ctx-danger" @click="ctxRemove()">Remove</div>
      </template>
    </div>
  </section>
</template>
