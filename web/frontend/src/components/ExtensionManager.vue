<script setup>
// Extension Management UI — Task 07
// Generic management over ExtensionManager (no hardcode extension_id).
// Features: catalog list, filters, install (Git URL), enable/disable,
// update, uninstall (with confirmation), details, config, capability summary.
// Secret-safe: leverages existing config-form (ExtensionUI) mechanism.
import { computed, onMounted, ref, watch } from "vue";
import {
  listExtensions,
  getExtension,
  installExtension,
  enableExtension,
  disableExtension,
  updateExtension,
  uninstallExtension,
} from "../api.js";
import ExtensionUI from "./ExtensionUI.vue";

const emit = defineEmits(["error"]);

// --- State ---
const loading = ref(false);
const listError = ref("");
const notice = ref("");
const extensions = ref([]);
const filter = ref("all"); // all | enabled | disabled | failed
const search = ref("");
const sortKey = ref("name"); // name | id | status
const detailId = ref("");
const detail = ref(null);
const detailLoading = ref(false);
const actionBusy = ref({}); // {id: true} while lifecycle call in flight
const installOpen = ref(false);
const installUrl = ref("");
const installRef = ref("");
const installBusy = ref(false);
const installError = ref("");
const installSuccess = ref("");
const removeConfirm = ref(null); // {id, name}
const failedDetailsOpen = ref(false);
const failedDetailText = ref("");
const showConfig = ref(false);
const configSchema = ref(null);
const configError = ref("");

let noticeTimer = null;
function setNotice(msg) {
  notice.value = msg;
  if (noticeTimer) clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { notice.value = ""; }, 4500);
}

function statusClass(s) {
  const v = String(s || "").toLowerCase();
  if (v === "enabled" || v === "loaded") return "status-on";
  if (v === "disabled") return "status-off";
  if (v === "failed") return "status-err";
  return "status-off";
}

function capableSummary(cap) {
  const c = cap || {};
  const parts = [];
  if (c.tool) parts.push(`Tools: ${c.tool}`);
  if (c.skill) parts.push(`Skills: ${c.skill}`);
  if (c.knowledge) parts.push(`Knowledge: ${c.knowledge}`);
  if (c.service) parts.push(`Services: ${c.service}`);
  if (c.ui) parts.push(`UI: ${c.ui}`);
  if (c.config) parts.push(`Config: ${c.config}`);
  return parts.length ? parts.join(" · ") : "—";
}

async function refresh() {
  loading.value = true;
  listError.value = "";
  try {
    const data = await listExtensions();
    extensions.value = data.extensions || [];
  } catch (e) {
    listError.value = e.message || "Failed to load extensions.";
  } finally {
    loading.value = false;
  }
}

const filteredExtensions = computed(() => {
  let list = [...extensions.value];
  const f = filter.value;
  if (f !== "all") {
    list = list.filter((e) => {
      const s = String(e.status || "").toLowerCase();
      if (f === "enabled") return s === "enabled" || s === "loaded";
      if (f === "disabled") return s === "disabled";
      if (f === "failed") return s === "failed";
      return true;
    });
  }
  const q = search.value.trim().toLowerCase();
  if (q) {
    list = list.filter((e) => {
      const hay = `${e.name || ""} ${e.id || ""} ${e.description || ""}`.toLowerCase();
      return hay.includes(q);
    });
  }
  const key = sortKey.value;
  list.sort((a, b) => {
    if (key === "status") return String(a.status || "").localeCompare(String(b.status || ""));
    if (key === "id") return String(a.id || "").toLowerCase().localeCompare(String(b.id || "").toLowerCase());
    return String(a.name || a.id || "").toLowerCase().localeCompare(String(b.name || b.id || "").toLowerCase());
  });
  return list;
});

const failedCount = computed(() => extensions.value.filter((e) => String(e.status).toLowerCase() === "failed").length);

async function openDetail(ext) {
  const id = ext.id;
  detailId.value = id;
  detailLoading.value = true;
  showConfig.value = false;
  configSchema.value = null;
  try {
    const data = await getExtension(id);
    detail.value = data;
  } catch (e) {
    detail.value = { id, error: e.message || "Failed to load detail" };
  } finally {
    detailLoading.value = false;
  }
}

function closeDetail() {
  detailId.value = "";
  detail.value = null;
  showConfig.value = false;
  configSchema.value = null;
}

async function doEnable(ext) {
  const id = ext.id;
  if (actionBusy.value[id]) return;
  actionBusy.value = { ...actionBusy.value, [id]: true };
  try {
    await enableExtension(id);
    setNotice(`Enabled ${id}`);
    await refresh();
    if (detailId.value === id) await openDetail(ext);
  } catch (e) {
    listError.value = e.message || `Enable failed for ${id}`;
  } finally {
    const copy = { ...actionBusy.value };
    delete copy[id];
    actionBusy.value = copy;
  }
}

async function doDisable(ext) {
  const id = ext.id;
  if (actionBusy.value[id]) return;
  actionBusy.value = { ...actionBusy.value, [id]: true };
  try {
    await disableExtension(id);
    setNotice(`Disabled ${id}`);
    await refresh();
    if (detailId.value === id) await openDetail(ext);
  } catch (e) {
    listError.value = e.message || `Disable failed for ${id}`;
  } finally {
    const copy = { ...actionBusy.value };
    delete copy[id];
    actionBusy.value = copy;
  }
}

async function doUpdate(ext, repositoryUrl = null, refVal = null) {
  const id = ext.id;
  if (actionBusy.value[id]) return;
  actionBusy.value = { ...actionBusy.value, [id]: true };
  try {
    const res = await updateExtension(id, repositoryUrl, refVal);
    if (res.restart_required) {
      setNotice(`Update completed for ${id}. Restart AETHER to activate the new Python code.`);
    } else {
      setNotice(`Updated ${id}`);
    }
    await refresh();
    if (detailId.value === id) await openDetail(ext);
  } catch (e) {
    listError.value = e.message || `Update failed for ${id}`;
  } finally {
    const copy = { ...actionBusy.value };
    delete copy[id];
    actionBusy.value = copy;
  }
}

function askRemove(ext) {
  removeConfirm.value = { id: ext.id, name: ext.name || ext.id };
}

function cancelRemove() {
  removeConfirm.value = null;
}

async function confirmRemove() {
  const target = removeConfirm.value;
  if (!target) return;
  const id = target.id;
  if (actionBusy.value[id]) return;
  actionBusy.value = { ...actionBusy.value, [id]: true };
  try {
    await uninstallExtension(id);
    setNotice(`Removed ${id}. Configuration and runtime data are preserved.`);
    removeConfirm.value = null;
    if (detailId.value === id) closeDetail();
    await refresh();
  } catch (e) {
    listError.value = e.message || `Remove failed for ${id}`;
  } finally {
    const copy = { ...actionBusy.value };
    delete copy[id];
    actionBusy.value = copy;
  }
}

function openInstall() {
  installOpen.value = true;
  installUrl.value = "";
  installRef.value = "";
  installError.value = "";
  installSuccess.value = "";
}

function closeInstall() {
  if (installBusy.value) return;
  installOpen.value = false;
}

async function doInstall() {
  const url = installUrl.value.trim();
  if (!url) {
    installError.value = "Git Repository URL is required.";
    return;
  }
  // Light format check (backend is authoritative for clone/manifest/etc.)
  try {
    const parsed = new URL(url);
    if (!parsed.protocol || !["http:", "https:", "git:", "ssh:", "file:"].includes(parsed.protocol)) {
      // Allow file:// and git@ style? url check above is permissive — only reject obviously invalid
    }
  } catch {
    // Allow non-URL-ish git@host:path style; only validate non-empty for now
  }
  installBusy.value = true;
  installError.value = "";
  installSuccess.value = "";
  try {
    const res = await installExtension(url, installRef.value.trim() || null);
    installSuccess.value = `Installed ${res.id || ""} ${res.version || res.installed_version || ""}`.trim();
    setNotice(`Extension installed successfully. ${res.id || ""} ${res.version || ""}`.trim());
    await refresh();
    // Close modal after short success display
    setTimeout(() => {
      installOpen.value = false;
      installSuccess.value = "";
    }, 900);
  } catch (e) {
    // Map known extension errors to user-friendly messages; preserve backend sanitized message
    const msg = e.message || "Installation failed.";
    // Duplicate / folder collision should be explicit
    if (e.code === "conflict" || /duplicate/i.test(msg) || /already installed/i.test(msg)) {
      installError.value = msg;
    } else {
      installError.value = msg;
    }
  } finally {
    installBusy.value = false;
  }
}

function openFailedDetails(ext) {
  const raw = ext.error || ext.status || "";
  failedDetailText.value = raw || "No details.";
  failedDetailsOpen.value = true;
}

async function toggleConfig() {
  if (showConfig.value) {
    showConfig.value = false;
    return;
  }
  if (!detail.value || !detail.value.id) return;
  configError.value = "";
  configSchema.value = null;
  try {
    const mod = await import("../api.js");
    const res = await mod.getExtensionConfigSchema(detail.value.id);
    configSchema.value = res.schema || res;
    showConfig.value = true;
  } catch (e) {
    configError.value = e.message || "Failed to load configuration.";
    showConfig.value = true;
    configSchema.value = { type: "form", fields: [] };
  }
}

async function onConfigSubmit(payload) {
  if (!detail.value || !detail.value.id) return;
  const id = detail.value.id;
  configError.value = "";
  try {
    const mod = await import("../api.js");
    for (const [key, value] of Object.entries(payload || {})) {
      if (value === "" || value == null) continue;
      await mod.setExtensionConfigValue(id, key, value);
    }
    setNotice(`Configuration saved for ${id}`);
    await openDetail({ id });
    showConfig.value = true;
  } catch (e) {
    configError.value = e.message || "Failed to save configuration.";
  }
}

watch(detailId, (v) => {
  if (!v) {
    showConfig.value = false;
    configSchema.value = null;
  }
});

onMounted(refresh);

// Expose refresh for parent
defineExpose({ refresh });
</script>

<template>
  <div class="ext-mgmt">
    <div class="ext-mgmt-head">
      <div class="ext-mgmt-title">
        <div class="title">Extensions</div>
        <div class="desc">
          Manage AETHER extensions. Install from Git URL, enable/disable, update, or remove.
          <span v-if="failedCount" class="ext-badge-failed">{{ failedCount }} failed</span>
        </div>
      </div>
      <button class="btn-aether btn-primary-a" :disabled="installBusy" @click="openInstall">Install Extension</button>
    </div>

    <div class="ext-toolbar">
      <div class="ext-filters" role="tablist" aria-label="Extension filter">
        <button class="seg-tab" :class="{ active: filter === 'all' }" @click="filter = 'all'">All <span class="seg-badge">{{ extensions.length }}</span></button>
        <button class="seg-tab" :class="{ active: filter === 'enabled' }" @click="filter = 'enabled'">Enabled</button>
        <button class="seg-tab" :class="{ active: filter === 'disabled' }" @click="filter = 'disabled'">Disabled</button>
        <button class="seg-tab" :class="{ active: filter === 'failed' }" @click="filter = 'failed'">Failed <span v-if="failedCount" class="seg-badge seg-badge-err">{{ failedCount }}</span></button>
      </div>
      <div class="ext-search">
        <input class="input-a ext-search-input" type="text" placeholder="Search name or ID…" v-model="search" />
        <select class="input-a ext-sort" v-model="sortKey" aria-label="Sort">
          <option value="name">Sort: Name</option>
          <option value="id">Sort: ID</option>
          <option value="status">Sort: Status</option>
        </select>
      </div>
    </div>

    <div v-if="notice" class="wb-notice ext-notice">{{ notice }}</div>
    <div v-if="listError" class="wb-error ext-error">{{ listError }}</div>

    <div v-if="loading" class="wb-empty" style="padding: 18px 0;">Loading extensions…</div>
    <div v-else-if="!filteredExtensions.length" class="wb-empty" style="padding: 18px 0;">
      <template v-if="extensions.length === 0">No extensions installed.</template>
      <template v-else>No extensions match filter.</template>
    </div>

    <div v-else class="ext-list">
      <div
        v-for="ext in filteredExtensions"
        :key="ext.id"
        class="ext-card"
        :class="{ failed: String(ext.status).toLowerCase() === 'failed' }"
      >
        <div class="ext-card-head" @click="openDetail(ext)">
          <div class="ext-card-main">
            <div class="ext-card-title">{{ ext.name || ext.id }}</div>
            <div class="ext-card-meta">
              <span class="mono ext-id">{{ ext.id }}</span>
              <span class="ext-ver">v{{ ext.version || ext.installed_version || "—" }}</span>
              <span class="status-tag" :class="statusClass(ext.status)">{{ ext.status || "unknown" }}</span>
            </div>
            <div v-if="ext.description" class="ext-desc">{{ ext.description }}</div>
            <div class="ext-cap-summary">{{ capableSummary(ext.capabilities) }}</div>
            <div v-if="String(ext.status).toLowerCase() === 'failed' && ext.error" class="ext-error-line">
              <span class="ext-error-text">{{ ext.error }}</span>
              <button class="btn-aether btn-ghost-a btn-sm" @click.stop="openFailedDetails(ext)">Details</button>
            </div>
          </div>
          <div class="ext-card-chevron">›</div>
        </div>
        <div class="ext-card-actions">
          <template v-if="String(ext.status).toLowerCase() === 'failed'">
            <button class="btn-aether btn-ghost-a btn-sm" @click="openFailedDetails(ext)">Details</button>
          </template>
          <template v-else-if="String(ext.status).toLowerCase() === 'disabled' || ext.enabled === false">
            <button class="btn-aether btn-primary-a btn-sm" :disabled="!!actionBusy[ext.id]" @click="doEnable(ext)">
              <span v-if="actionBusy[ext.id]">Enabling…</span><span v-else>Enable</span>
            </button>
          </template>
          <template v-else>
            <button class="btn-aether btn-ghost-a btn-sm" :disabled="!!actionBusy[ext.id]" @click="doDisable(ext)">
              <span v-if="actionBusy[ext.id]">Disabling…</span><span v-else>Disable</span>
            </button>
          </template>
          <button class="btn-aether btn-ghost-a btn-sm" :disabled="!!actionBusy[ext.id]" @click="doUpdate(ext)">
            <span v-if="actionBusy[ext.id]">Updating…</span><span v-else>Update</span>
          </button>
          <button class="btn-aether btn-danger-a btn-sm" :disabled="!!actionBusy[ext.id]" @click="askRemove(ext)">Remove</button>
        </div>
      </div>
    </div>

    <!-- Detail Drawer / Modal -->
    <div v-if="detailId" class="modal-backdrop" @click.self="closeDetail">
      <div class="modal ext-detail-m" role="dialog" aria-modal="true">
        <div class="ext-detail-head">
          <div>
            <div class="ext-detail-title">{{ detail && detail.name || detailId }}</div>
            <div class="mono ext-detail-id">{{ detailId }}</div>
          </div>
          <button class="close-x" @click="closeDetail">✕</button>
        </div>
        <div v-if="detailLoading" class="wb-empty" style="padding: 14px;">Loading…</div>
        <div v-else-if="detail" class="ext-detail-body">
          <div class="ext-detail-row">
            <span class="k">Version</span><span class="v mono">{{ detail.version || detail.installed_version || "—" }}</span>
          </div>
          <div class="ext-detail-row">
            <span class="k">API Version</span><span class="v mono">{{ detail.api_version || "—" }}</span>
          </div>
          <div class="ext-detail-row">
            <span class="k">Status</span><span class="v"><span class="status-tag" :class="statusClass(detail.status)">{{ detail.status || "—" }}</span><span v-if="detail.enabled !== undefined" class="mono" style="margin-left:8px; font-size:11px; color:var(--text-faint);">{{ detail.enabled ? "enabled" : "disabled" }}</span></span>
          </div>
          <div v-if="detail.error" class="ext-detail-row">
            <span class="k">Error</span><span class="v" style="color: var(--err); word-break: break-word;">{{ detail.error }}</span>
          </div>
          <div v-if="detail.description" class="ext-detail-desc">{{ detail.description }}</div>

          <div class="ext-detail-section">
            <div class="ext-detail-section-title">Capabilities</div>
            <div class="ext-cap-grid">
              <span v-for="(count, type) in (detail.capabilities || {})" :key="type" class="chip chip-sm">
                {{ type }}: <span class="mono">{{ count }}</span>
              </span>
              <span v-if="!detail.capabilities || !Object.keys(detail.capabilities).length" class="wb-empty">No capabilities.</span>
            </div>
            <div v-if="detail.capabilities" class="ext-cap-summary" style="margin-top:6px;">
              Total: {{ detail.capability_count || 0 }} capabilities
            </div>
          </div>

          <div v-if="detail.ui_contributions && detail.ui_contributions.length" class="ext-detail-section">
            <div class="ext-detail-section-title">UI Contributions</div>
            <div class="ext-ui-list">
              <div v-for="u in detail.ui_contributions" :key="u.id" class="ext-ui-item">
                <span class="mono" style="font-size:11.5px;">{{ u.id }}</span>
                <span class="chip chip-sm">{{ u.type }}</span>
                <span v-if="u.title" style="color: var(--text-dim); font-size:12px;">{{ u.title }}</span>
              </div>
            </div>
          </div>

          <div class="ext-detail-section">
            <div class="ext-detail-section-title">
              Configuration
              <button class="btn-aether btn-ghost-a btn-sm" style="margin-left:8px;" @click="toggleConfig">{{ showConfig ? "Hide" : "Show" }}</button>
            </div>
            <div v-if="showConfig" class="ext-config-wrap">
              <div v-if="configError" class="wb-error" style="margin-bottom:8px;">{{ configError }}</div>
              <ExtensionUI
                v-if="configSchema"
                :schema="configSchema"
                @submit="onConfigSubmit"
                @close="showConfig = false"
              />
              <div v-else class="wb-empty">No configuration.</div>
            </div>
          </div>
        </div>
        <div class="modal-actions" style="border-top:1px solid var(--border-soft); padding-top:12px;">
          <template v-if="detail && String(detail.status).toLowerCase() === 'disabled'">
            <button class="btn-aether btn-primary-a btn-sm" :disabled="!!actionBusy[detailId]" @click="doEnable(detail)">Enable</button>
          </template>
          <template v-else-if="detail && String(detail.status).toLowerCase() !== 'failed'">
            <button class="btn-aether btn-ghost-a btn-sm" :disabled="!!actionBusy[detailId]" @click="doDisable(detail)">Disable</button>
          </template>
          <button class="btn-aether btn-ghost-a btn-sm" :disabled="!!actionBusy[detailId]" @click="doUpdate(detail)">Update</button>
          <button class="btn-aether btn-danger-a btn-sm" :disabled="!!actionBusy[detailId]" @click="askRemove(detail)">Remove</button>
          <button class="btn-aether btn-ghost-a btn-sm" @click="closeDetail">Close</button>
        </div>
      </div>
    </div>

    <!-- Install Modal -->
    <div v-if="installOpen" class="modal-backdrop" @click.self="closeInstall">
      <div class="modal ext-install-m" role="dialog" aria-modal="true">
        <div class="modal-title">Install Extension</div>
        <div class="modal-body">
          <div style="display:grid; gap:12px;">
            <div class="field">
              <label>Git Repository URL</label>
              <input class="input-a" type="text" placeholder="https://github.com/org/repo.git" v-model="installUrl" :disabled="installBusy" />
            </div>
            <div class="field">
              <label>Ref (optional) — branch / tag / commit</label>
              <input class="input-a" type="text" placeholder="main" v-model="installRef" :disabled="installBusy" />
              <div class="hint">Leave empty to use default branch.</div>
            </div>
            <div v-if="installError" class="wb-error">{{ installError }}</div>
            <div v-if="installSuccess" class="wb-notice">{{ installSuccess }}</div>
            <div v-if="installBusy" class="wb-empty">Installing… Validating… Registering… Enabling…</div>
          </div>
        </div>
        <div class="modal-actions">
          <button class="btn-aether btn-ghost-a" :disabled="installBusy" @click="closeInstall">Cancel</button>
          <button class="btn-aether btn-primary-a" :disabled="installBusy" @click="doInstall">
            <span v-if="installBusy">Installing…</span><span v-else>Install</span>
          </button>
        </div>
      </div>
    </div>

    <!-- Remove Confirmation -->
    <div v-if="removeConfirm" class="modal-backdrop" @click.self="cancelRemove">
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-title">Remove "{{ removeConfirm.name }}"?</div>
        <div class="modal-body">
          Extension package will be removed.<br />Extension configuration and runtime data are preserved.
        </div>
        <div class="modal-actions">
          <button class="btn-aether btn-ghost-a" @click="cancelRemove">Cancel</button>
          <button class="btn-aether btn-danger-a" :disabled="!!actionBusy[removeConfirm.id]" @click="confirmRemove">
            <span v-if="actionBusy[removeConfirm.id]">Removing…</span><span v-else>Remove</span>
          </button>
        </div>
      </div>
    </div>

    <!-- Failed Details -->
    <div v-if="failedDetailsOpen" class="modal-backdrop" @click.self="failedDetailsOpen = false">
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-title">Extension Error</div>
        <div class="modal-body" style="white-space: pre-wrap; word-break: break-word; font-family: var(--mono); font-size:12px;">{{ failedDetailText }}</div>
        <div class="modal-actions">
          <button class="btn-aether btn-ghost-a" @click="failedDetailsOpen = false">Close</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ext-mgmt { display: grid; gap: 14px; }
.ext-mgmt-head { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; }
.ext-mgmt-title .title { font-size: 15px; font-weight: 650; }
.ext-mgmt-title .desc { color: var(--text-faint); font-size: 12.5px; margin-top:3px; }
.ext-badge-failed { display:inline-flex; margin-left:8px; padding:2px 8px; border-radius:999px; background: rgba(248,113,113,0.14); color: var(--err); font-size:11px; font-weight:600; }
.ext-toolbar { display:flex; flex-wrap:wrap; gap:12px; align-items:center; justify-content:space-between; }
.ext-filters { display:inline-flex; gap:4px; padding:3px; border:1px solid var(--border-soft); border-radius:10px; background: rgba(0,0,0,0.22); }
.seg-tab { display:inline-flex; align-items:center; gap:7px; padding:6px 12px; border:0; border-radius:8px; background:transparent; color:var(--text-dim); font-size:12px; font-weight:600; cursor:pointer; }
.seg-tab.active { color:var(--text); background: rgba(139,92,246,0.2); box-shadow: inset 0 0 0 1px rgba(139,92,246,0.32); }
.seg-badge { font-family: var(--mono); font-size:10.5px; min-width:18px; text-align:center; padding:1px 6px; border-radius:999px; background: rgba(255,255,255,0.06); color:var(--text-faint); }
.seg-badge-err { background: rgba(248,113,113,0.2); color: #fca5a5; }
.ext-search { display:flex; gap:8px; align-items:center; }
.ext-search-input { min-width: 220px; }
.ext-sort { min-width: 140px; }
.ext-notice, .ext-error { padding: 10px 14px; border-radius: 9px; }
.ext-notice { background: rgba(52,211,153,0.08); border:1px solid rgba(52,211,153,0.2); }
.ext-error-line { display:flex; gap:8px; align-items:center; margin-top:6px; }
.ext-error-text { color: var(--err); font-size:12px; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.ext-list { display:grid; gap:10px; }
.ext-card { border:1px solid var(--border-soft); border-radius:12px; background: linear-gradient(180deg, var(--bg-panel), #100e1a); padding:12px 14px; display:grid; gap:10px; }
.ext-card.failed { border-color: rgba(248,113,113,0.3); }
.ext-card-head { display:flex; gap:12px; cursor:pointer; }
.ext-card-main { flex:1; min-width:0; }
.ext-card-title { font-weight:600; font-size:14px; }
.ext-card-meta { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:4px; }
.ext-id { color: var(--text-faint); font-size:11.5px; }
.ext-ver { color: var(--text-dim); font-size:11.5px; font-family: var(--mono); }
.ext-desc { color: var(--text-dim); font-size:12.5px; margin-top:6px; }
.ext-cap-summary { color: var(--text-faint); font-size:11.5px; margin-top:4px; }
.ext-card-chevron { color: var(--text-faint); font-size:18px; }
.ext-card-actions { display:flex; gap:8px; flex-wrap:wrap; }
.btn-sm { padding:5px 10px; font-size:12px; }
.ext-detail-m { max-width:640px; width:92vw; max-height: calc(100vh - 80px); overflow:auto; display:flex; flex-direction:column; }
.ext-detail-head { display:flex; justify-content:space-between; gap:12px; border-bottom:1px solid var(--border-soft); padding-bottom:10px; }
.ext-detail-title { font-weight:650; font-size:15px; }
.ext-detail-id { font-size:11.5px; color: var(--text-faint); }
.ext-detail-body { display:grid; gap:12px; padding-top:12px; }
.ext-detail-row { display:flex; justify-content:space-between; gap:12px; }
.ext-detail-row .k { color: var(--text-faint); font-size:12px; }
.ext-detail-row .v { color: var(--text); font-size:12px; }
.ext-detail-desc { color: var(--text-dim); font-size:13px; border:1px solid var(--border-soft); border-radius:9px; padding:10px 12px; background: rgba(255,255,255,0.02); }
.ext-detail-section { border-top:1px solid var(--border-soft); padding-top:10px; display:grid; gap:8px; }
.ext-detail-section-title { font-weight:600; font-size:12.5px; }
.ext-cap-grid { display:flex; flex-wrap:wrap; gap:6px; }
.ext-ui-list { display:grid; gap:6px; }
.ext-ui-item { display:flex; gap:8px; align-items:center; padding:6px 8px; border:1px solid var(--border-soft); border-radius:8px; background: rgba(255,255,255,0.02); }
.ext-install-m { max-width:480px; }
</style>
