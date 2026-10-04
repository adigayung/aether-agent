<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { fetchBackupCommits } from "../api";

const props = defineProps({
  project: { type: Object, default: null },
});

const commits = ref([]);
const loading = ref(false);
const error = ref("");
const page = ref(1);
const perPage = 20;
const total = ref(0);
const total_pages = computed(() => Math.max(1, Math.ceil(total.value / perPage)));
const has_next = computed(() => page.value < total_pages.value);
const has_prev = computed(() => page.value > 1);

function projectId() {
  return props.project && (props.project.id || props.project.project_id);
}

async function load() {
  const id = projectId();
  if (!id) {
    commits.value = [];
    total.value = 0;
    page.value = 1;
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    const data = await fetchBackupCommits(id, page.value, perPage);
    commits.value = data.commits || [];
    total.value = data.total || 0;
    page.value = data.page || page.value;
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    loading.value = false;
  }
}

function prev() {
  if (!has_prev.value) return;
  page.value -= 1;
  load();
}

function next() {
  if (!has_next.value) return;
  page.value += 1;
  load();
}

function formatDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const now = new Date();
  const diffMs = now - d;
  const sec = Math.round(diffMs / 1000);
  if (sec < 60) return "just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} min ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr} hour${hr === 1 ? "" : "s"} ago`;
  const day = Math.round(hr / 24);
  if (day < 30) return `${day} day${day === 1 ? "" : "s"} ago`;
  return d.toLocaleDateString();
}

watch(
  () => projectId(),
  () => {
    page.value = 1;
    load();
  },
  { immediate: true }
);
</script>

<template>
  <div class="bcl">
    <div class="bcl-head">
      <span class="bcl-title">Backup history</span>
      <span v-if="total" class="bcl-meta mono">Page {{ page }} of {{ total_pages }} · {{ total }} commits</span>
    </div>
    <div v-if="error" class="bcl-alert err">{{ error }}</div>
    <div v-else-if="loading" class="bcl-empty">Loading…</div>
    <div v-else-if="!commits.length" class="bcl-empty">No commits yet</div>
    <div v-else class="bcl-list">
      <div v-for="c in commits" :key="c.sha" class="bcl-item">
        <span class="bcl-sha mono">{{ c.sha }}</span>
        <span class="bcl-msg">{{ c.message }}</span>
        <span class="bcl-author">{{ c.author }}</span>
        <span class="bcl-date mono">{{ formatDate(c.date) }}</span>
      </div>
    </div>
    <div v-if="total > perPage" class="bcl-pager">
      <button class="bcl-btn" type="button" :disabled="!has_prev || loading" @click="prev">Previous</button>
      <span class="bcl-indicator">Page {{ page }} of {{ total_pages }}</span>
      <button class="bcl-btn" type="button" :disabled="!has_next || loading" @click="next">Next</button>
    </div>
  </div>
</template>

<style scoped>
.bcl {
  display: grid;
  gap: 10px;
}
.bcl-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.bcl-title {
  font-size: 11.5px;
  font-weight: 700;
  letter-spacing: 0.6px;
  color: var(--text-dim);
}
.bcl-meta {
  font-size: 11px;
  color: var(--text-faint);
}
.bcl-alert {
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 12px;
}
.bcl-alert.err {
  color: #fecaca;
  background: rgba(248, 113, 113, 0.1);
  border: 1px solid rgba(248, 113, 113, 0.4);
}
.bcl-empty {
  color: var(--text-faint);
  font-size: 12px;
}
.bcl-list {
  display: grid;
  gap: 6px;
  max-height: 240px;
  overflow-y: auto;
}
.bcl-item {
  display: grid;
  grid-template-columns: auto 1fr auto auto;
  gap: 8px;
  align-items: center;
  padding: 8px 10px;
  border: 1px solid var(--border-soft);
  border-radius: 8px;
}
.bcl-sha {
  color: #c4b5fd;
  font-size: 11.5px;
}
.bcl-msg {
  font-size: 12.5px;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bcl-author {
  font-size: 11px;
  color: var(--text-faint);
}
.bcl-date {
  font-size: 11px;
  color: var(--text-faint);
}
.bcl-pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.bcl-btn {
  padding: 5px 10px;
  border-radius: 7px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-dim);
  font-size: 11.5px;
  cursor: pointer;
}
.bcl-btn:hover:not(:disabled) {
  color: var(--text);
  border-color: var(--accent);
}
.bcl-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.bcl-indicator {
  font-size: 11px;
  color: var(--text-faint);
}
</style>