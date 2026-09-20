<script setup>
// GitHub Backup panel (OPTIONAL per project).
//
// SATU sumber konfigurasi dipakai bersama oleh halaman Backup (sidebar, project
// aktif) dan Projects (detail project). Panel ini HANYA memanggil HTTP gateway;
// tidak ada Git/checkpoint logic di frontend. Token TIDAK pernah dikembalikan
// backend (hanya `credential_set`), sehingga field token selalu kosong saat
// dibuka dan hanya dikirim saat user mengisi.
import { reactive, ref, watch } from "vue";
import {
  createGithubCheckpoint,
  getGithubConfig,
  listGithubCheckpoints,
  restoreGithubCheckpoint,
  saveGithubConfig,
  testGithubConnection,
} from "../api";

const props = defineProps({
  // Project (dari daftar launcher / active project): { id, name, root|path }.
  project: { type: Object, default: null },
});

const loading = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");

const config = ref({
  configured: false,
  enabled: false,
  repository: "",
  branch: "",
  exclude: [],
  credential_set: false,
  is_repository: false,
  current_branch: null,
  gitignore_ok: false,
  changes: { modified: 0, added: 0, deleted: 0, total: 0 },
});

const form = reactive({
  token: "",
  repository: "",
  branch: "main",
  excludeText: "",
});

const checkpoints = ref([]);
const checkpointDescription = ref("");
const restoreTarget = ref(null);
const restoring = ref(false);
// Saat project belum dikonfigurasi: user "diminta Configure GitHub" dulu
// (callout + [Configure GitHub] / [Cancel]) sebelum form ditampilkan.
const configureOpen = ref(false);

function projectId() {
  return props.project && (props.project.id || props.project.project_id);
}

function applyConfig(data) {
  config.value = { ...config.value, ...(data || {}) };
  // Token SELALU kosong: backend tidak pernah mengembalikannya.
  form.token = "";
  if (data) {
    form.repository = data.repository || "";
    form.branch = data.branch || "main";
    form.excludeText = (data.exclude || []).join("\n");
  }
}

const excludeList = () =>
  form.excludeText
    .split("\n")
    .map((s) => s.trim())
    .filter((s) => s && !s.startsWith("#"));

async function load() {
  const id = projectId();
  configureOpen.value = false;
  if (!id) {
    config.value = { ...config.value, configured: false, repository: "", branch: "" };
    checkpoints.value = [];
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    applyConfig(await getGithubConfig(id));
    if (config.value.configured) {
      const data = await listGithubCheckpoints(id);
      checkpoints.value = data.checkpoints || [];
    } else {
      checkpoints.value = [];
    }
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    loading.value = false;
  }
}

async function save() {
  const id = projectId();
  if (!id) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const payload = {
      repository: form.repository,
      branch: form.branch,
      exclude: excludeList(),
    };
    // Token hanya dikirim bila user mengisinya (tidak pernah ditampilkan ulang).
    if (form.token) payload.token = form.token;
    applyConfig(await saveGithubConfig(id, payload));
    notice.value = "Konfigurasi GitHub disimpan (token terenkripsi).";
    if (config.value.configured) {
      const data = await listGithubCheckpoints(id);
      checkpoints.value = data.checkpoints || [];
    }
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    busy.value = false;
  }
}

async function test() {
  const id = projectId();
  if (!id) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const payload = { repository: form.repository, branch: form.branch };
    if (form.token) payload.token = form.token;
    const result = await testGithubConnection(id, payload);
    if (result.ok) {
      notice.value = result.message || "Connected successfully";
    } else {
      error.value = result.message || "Connection failed";
    }
  } catch (e) {
    error.value = e.message || "Connection failed";
  } finally {
    busy.value = false;
  }
}

async function commitCheckpoint() {
  const id = projectId();
  if (!id || !checkpointDescription.value.trim()) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const result = await createGithubCheckpoint(id, checkpointDescription.value.trim());
    checkpointDescription.value = "";
    notice.value = result.message || "Checkpoint dibuat.";
    if (result.push_error) error.value = result.push_error;
    const data = await listGithubCheckpoints(id);
    checkpoints.value = data.checkpoints || [];
    await refreshStatus(id);
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    busy.value = false;
  }
}

async function refreshStatus(id) {
  try {
    applyConfig(await getGithubConfig(id));
  } catch {
    /* status refresh best-effort */
  }
}

function askRestore(cp) {
  restoreTarget.value = cp;
  error.value = "";
  notice.value = "";
}

function cancelRestore() {
  restoreTarget.value = null;
}

async function confirmRestore() {
  const id = projectId();
  const target = restoreTarget.value;
  if (!id || !target) return;
  restoring.value = true;
  error.value = "";
  notice.value = "";
  try {
    const result = await restoreGithubCheckpoint(id, target.hash, true);
    notice.value = result.message || "Recovery selesai.";
    restoreTarget.value = null;
    await refreshStatus(id);
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    restoring.value = false;
  }
}

function formatCommitTime(ts) {
  if (!ts) return "—";
  const d = new Date(Number(ts) * 1000);
  return Number.isNaN(d.getTime()) ? String(ts) : d.toLocaleString();
}

watch(
  () => projectId(),
  () => {
    load();
  },
  { immediate: true }
);
</script>

<template>
  <div class="gb">
    <div v-if="error" class="gb-alert err">{{ error }}</div>
    <div v-if="notice" class="gb-alert ok">{{ notice }}</div>

    <div v-if="!project" class="gb-empty">Pilih project terlebih dahulu.</div>
    <div v-else-if="loading" class="gb-empty">Memuat konfigurasi GitHub…</div>

    <template v-else>
      <!-- Status -->
      <div class="gb-status">
        <span class="gb-dot" :class="config.configured ? 'ok' : 'off'"></span>
        <span class="gb-status-text">
          GitHub Backup:
          <strong :class="config.configured ? 'ok' : 'off'">
            {{ config.configured ? "Configured" : "Not configured" }}
          </strong>
        </span>
        <span v-if="config.is_repository" class="gb-meta mono">git · {{ config.current_branch || "detached" }}</span>
        <span v-else class="gb-meta mono warn">bukan repository Git</span>
      </div>

      <div v-if="!config.configured" class="gb-callout">
        <div class="gb-callout-title">GitHub Backup belum dikonfigurasi.</div>
        <div class="gb-callout-body">
          Project ini belum memiliki konfigurasi GitHub. Konfigurasikan GitHub
          untuk menggunakan Backup / Checkpoint / Recovery.
        </div>
        <div v-if="!configureOpen" class="gb-callout-actions">
          <button class="btn-aether btn-primary-a" type="button" @click="configureOpen = true">
            Configure GitHub
          </button>
          <button class="btn-aether btn-ghost-a" type="button" @click="configureOpen = false">
            Cancel
          </button>
        </div>
      </div>

      <!-- Konfigurasi (tampil bila project sudah dikonfigurasi atau user memilih Configure). -->
      <div v-if="config.configured || configureOpen" class="gb-form">
        <div class="gb-form-title">
          {{ config.configured ? "GitHub Backup" : "Configure GitHub" }}
        </div>
        <div class="gb-grid">
          <label class="gb-field">
            <span class="gb-label">GitHub Token</span>
            <input
              v-model="form.token"
              class="gb-input"
              type="password"
              autocomplete="new-password"
              :placeholder="config.credential_set ? '•••••••• (tersimpan, terenkripsi)' : 'ghp_…'"
            />
          </label>
          <label class="gb-field">
            <span class="gb-label">Repository</span>
            <input
              v-model="form.repository"
              class="gb-input"
              placeholder="https://github.com/adigayung/aether-agent.git"
            />
          </label>
          <label class="gb-field">
            <span class="gb-label">Branch</span>
            <input v-model="form.branch" class="gb-input" placeholder="main" />
          </label>
          <label class="gb-field gb-field-wide">
            <span class="gb-label">Exclude <span class="gb-hint">(opsional, satu per baris — bukan pengganti .gitignore)</span></span>
            <textarea
              v-model="form.excludeText"
              class="gb-input gb-textarea"
              rows="3"
              placeholder="data/&#10;*.db&#10;*.log"
            ></textarea>
          </label>
        </div>
        <div class="gb-actions">
          <button class="btn-aether btn-primary-a" type="button" :disabled="busy" @click="save">
            Save
          </button>
          <button class="btn-aether btn-ghost-a" type="button" :disabled="busy" @click="test">
            Test Connection
          </button>
          <span class="gb-hint gb-gitignore" :class="config.gitignore_ok ? 'ok' : 'warn'">
            .gitignore: {{ config.gitignore_ok ? ".aether/ ignored" : ".aether/ akan ditambahkan otomatis" }}
          </span>
        </div>
      </div>

      <!-- Checkpoint + Recovery (hanya bila sudah dikonfigurasi) -->
      <template v-if="config.configured">
        <div class="gb-block">
          <div class="gb-block-head">
            <span class="gb-block-title">CREATE CHECKPOINT</span>
          </div>
          <div class="gb-changes mono">
            {{ (config.changes && config.changes.total) || 0 }} perubahan ·
            {{ (config.changes && config.changes.modified) || 0 }} modified ·
            {{ (config.changes && config.changes.added) || 0 }} added ·
            {{ (config.changes && config.changes.deleted) || 0 }} deleted
          </div>
          <div class="gb-commit-row">
            <input
              v-model="checkpointDescription"
              class="gb-input"
              placeholder="Description (commit message) — mis. Fix revisi chat konsultan"
              @keyup.enter="commitCheckpoint"
            />
            <button
              class="btn-aether btn-primary-a"
              type="button"
              :disabled="busy || !checkpointDescription.trim()"
              @click="commitCheckpoint"
            >
              Commit &amp; Upload
            </button>
          </div>
        </div>

        <div class="gb-block">
          <div class="gb-block-head">
            <span class="gb-block-title">CHECKPOINTS</span>
            <span class="gb-count">{{ checkpoints.length }}</span>
          </div>
          <div v-if="!checkpoints.length" class="gb-empty">Belum ada checkpoint.</div>
          <div v-else class="gb-cp-list">
            <div v-for="cp in checkpoints" :key="cp.hash" class="gb-cp">
              <div class="gb-cp-main">
                <span class="gb-cp-hash mono">{{ cp.short_hash || (cp.hash || "").slice(0, 7) }}</span>
                <span class="gb-cp-time mono">{{ formatCommitTime(cp.timestamp) }}</span>
                <span class="gb-cp-subject">{{ cp.subject }}</span>
              </div>
              <button class="gb-mini" type="button" :disabled="busy || restoring" @click="askRestore(cp)">
                Restore
              </button>
            </div>
          </div>
        </div>

        <!-- Konfirmasi restore (safety existing AETHER: konfirmasi sebelum aksi). -->
        <div v-if="restoreTarget" class="gb-confirm">
          <div class="gb-confirm-title">Restore checkpoint ini?</div>
          <div class="gb-confirm-body">
            Perubahan project saat ini akan dikembalikan ke commit
            <span class="mono">{{ restoreTarget.short_hash || restoreTarget.hash }}</span>
            — “{{ restoreTarget.subject }}”.
          </div>
          <div class="gb-confirm-actions">
            <button class="btn-aether btn-ghost-a" type="button" :disabled="restoring" @click="cancelRestore">
              Cancel
            </button>
            <button class="btn-aether btn-danger-a" type="button" :disabled="restoring" @click="confirmRestore">
              Restore
            </button>
          </div>
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.gb {
  display: grid;
  gap: 14px;
  min-width: 0;
}
.gb-alert {
  padding: 10px 12px;
  border-radius: 9px;
  font-size: 12.5px;
  border: 1px solid var(--border-soft);
}
.gb-alert.err {
  color: #fecaca;
  background: rgba(248, 113, 113, 0.1);
  border-color: rgba(248, 113, 113, 0.4);
}
.gb-alert.ok {
  color: #bbf7d0;
  background: rgba(34, 197, 94, 0.1);
  border-color: rgba(34, 197, 94, 0.35);
}
.gb-empty {
  color: var(--text-faint);
  font-size: 12.5px;
}

.gb-status {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12.5px;
  color: var(--text-dim);
}
.gb-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-faint);
  flex: 0 0 auto;
}
.gb-dot.ok {
  background: #4ade80;
}
.gb-dot.off {
  background: var(--text-faint);
}
.gb-status-text strong.ok {
  color: #86efac;
}
.gb-status-text strong.off {
  color: var(--text-faint);
}
.gb-meta {
  font-size: 11.5px;
  color: var(--text-faint);
}
.gb-meta.warn {
  color: #fcd34d;
}

.gb-callout {
  border: 1px solid var(--border-soft);
  border-left: 3px solid var(--accent);
  border-radius: 10px;
  padding: 12px 14px;
  background: rgba(139, 92, 246, 0.06);
}
.gb-callout-title {
  font-size: 12.5px;
  font-weight: 650;
  color: var(--text);
}
.gb-callout-body {
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-dim);
  line-height: 1.55;
}
.gb-callout-actions {
  display: flex;
  gap: 9px;
  margin-top: 12px;
}

.gb-form {
  border: 1px dashed var(--border-soft);
  border-radius: 12px;
  padding: 14px;
  display: grid;
  gap: 12px;
}
.gb-form-title {
  font-size: 12.5px;
  font-weight: 650;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.4px;
}
.gb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 10px;
}
.gb-field {
  display: grid;
  gap: 5px;
  min-width: 0;
}
.gb-field-wide {
  grid-column: 1 / -1;
}
.gb-label {
  font-size: 11.5px;
  color: var(--text-faint);
}
.gb-hint {
  font-size: 11px;
  color: var(--text-faint);
}
.gb-input {
  width: 100%;
  min-width: 0;
  padding: 9px 12px;
  border-radius: 9px;
  border: 1px solid var(--border);
  background: var(--bg-elev);
  color: var(--text);
  font-size: 12.5px;
  font-family: var(--mono);
}
.gb-input:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.18);
}
.gb-textarea {
  resize: vertical;
  min-height: 62px;
}
.gb-actions {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
}
.gb-gitignore.ok {
  color: #86efac;
}
.gb-gitignore.warn {
  color: #fcd34d;
}

.gb-block {
  border: 1px solid var(--border-soft);
  border-radius: 12px;
  padding: 12px 14px;
  display: grid;
  gap: 10px;
}
.gb-block-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.gb-block-title {
  font-size: 11.5px;
  font-weight: 700;
  letter-spacing: 0.6px;
  color: var(--text-dim);
}
.gb-count {
  font-size: 11px;
  color: var(--text-faint);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 1px 8px;
}
.gb-changes {
  font-size: 11.5px;
  color: var(--text-faint);
}
.gb-commit-row {
  display: flex;
  gap: 9px;
  align-items: center;
  flex-wrap: wrap;
}
.gb-commit-row .gb-input {
  flex: 1 1 260px;
}

.gb-cp-list {
  display: grid;
  gap: 6px;
}
.gb-cp {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 7px 9px;
  border-radius: 9px;
  border: 1px solid transparent;
  transition: background 0.15s ease, border-color 0.15s ease;
}
.gb-cp:hover {
  background: rgba(255, 255, 255, 0.02);
  border-color: var(--border-soft);
}
.gb-cp-main {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 0;
}
.gb-cp-hash {
  color: #c4b5fd;
  font-size: 12px;
  flex: 0 0 auto;
}
.gb-cp-time {
  color: var(--text-faint);
  font-size: 11.5px;
  flex: 0 0 auto;
}
.gb-cp-subject {
  color: var(--text);
  font-size: 12.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.gb-mini {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-dim);
  border-radius: 7px;
  padding: 4px 10px;
  font-size: 11.5px;
  cursor: pointer;
  flex: 0 0 auto;
  transition: color 0.15s ease, border-color 0.15s ease;
}
.gb-mini:hover:not(:disabled) {
  color: var(--text);
  border-color: var(--accent);
}
.gb-mini:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.gb-confirm {
  border: 1px solid rgba(248, 113, 113, 0.4);
  background: rgba(248, 113, 113, 0.07);
  border-radius: 10px;
  padding: 12px 14px;
  display: grid;
  gap: 8px;
}
.gb-confirm-title {
  font-size: 12.5px;
  font-weight: 650;
  color: #fecaca;
}
.gb-confirm-body {
  font-size: 12px;
  color: var(--text-dim);
  line-height: 1.55;
}
.gb-confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 9px;
}
</style>
