<script setup>
// Project Settings MODAL (PROJECT-LOCAL, Sidebar -> Projects -> gear
// "Project Settings" -> modal).
//
// Menyediakan DUA tab:
//   - Security : Project Permission Matrix (`.aether/settings/permissions.json`).
//   - Agents   : System Prompt Agent & Consultant
//                (`.aether/settings/agent.json` / `.aether/settings/consultant.json`).
//
// Komponen ini HANYA memanggil HTTP gateway; TIDAK ada policy engine /
// konfigurasi kedua di frontend. Permission Matrix di-enforce oleh
// PermissionManager AETHER existing saat Agent melakukan action. System Prompt
// per-project dikonsumsi oleh Agent (runtime) dan Consultant; bila project
// tidak mengoverride, sumber existing (Global Settings / prompt bawaan AETHER)
// tetap dipakai.
//
// Perubahan di sini HANYA berlaku untuk project ini (isolasi project):
// project lain tidak terpengaruh, dan default global tidak berubah.
import { computed, ref, watch } from "vue";
import {
  getProjectAgentSettings,
  getProjectPolicy,
  saveProjectAgentSettings,
  saveProjectPolicy,
} from "../api";

const props = defineProps({
  // Project (dari daftar launcher / active project): { id, name, root|path }.
  project: { type: Object, default: null },
});
// Modal ditutup oleh pemanggil (App.vue) saat Close/Cancel/backdrop diklik.
const emit = defineEmits(["close"]);

// Tab aktif: "security" (permission matrix) atau "agents" (system prompt).
const activeTab = ref("security");

const loading = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");

// --- Security tab: Project Permission Matrix -------------------------------
// Nilai AKTUAL project (dibaca dari `.aether/settings/permissions.json`).
const policy = ref({ matrix: {}, path: "", exists: false, project_id: "" });
// Opsi dari backend (TIDAK di-hardcode di frontend).
const actions = ref([]); // [{value, label, scopes:[{value,label}]}] dari backend
const scopes = ref([]);
const modes = ref([]);
// Pilihan di form (dikirim saat Save): { action: { scope: mode } }.
const form = ref({});

// --- Agents tab: System Prompt Agent & Consultant --------------------------
const agentsLoading = ref(false);
const agentDraft = ref("");
const consultantDraft = ref("");
const agentActual = ref({ system_prompt: "", default_system_prompt: "" });
const consultantActual = ref({ system_prompt: "", default_system_prompt: "" });
const agentPaths = ref({ agent: "", consultant: "" });

function projectId() {
  return props.project && (props.project.id || props.project.project_id);
}

// --------------------------------------------------------------------------
// Security tab
// --------------------------------------------------------------------------
function emptyForm() {
  const next = {};
  for (const action of actions.value) {
    next[action.value] = {};
    for (const scope of action.scopes || []) {
      next[action.value][scope.value] = "allow";
    }
  }
  return next;
}

function applyPolicy(data) {
  const d = data || {};
  const opts = d.options || {};
  if (opts.actions && opts.actions.length) actions.value = opts.actions;
  if (opts.scopes) scopes.value = opts.scopes;
  if (opts.modes) modes.value = opts.modes;

  const matrix = d.matrix || {};
  const next = emptyForm();
  for (const action of actions.value) {
    for (const scope of action.scopes || []) {
      const raw = matrix[action.value] && matrix[action.value][scope.value];
      next[action.value][scope.value] = raw || "allow";
    }
  }
  form.value = next;
  policy.value = {
    matrix,
    path: d.path || "",
    exists: Boolean(d.exists),
    project_id: d.project_id || "",
  };
}

async function loadPolicy() {
  const id = projectId();
  error.value = "";
  notice.value = "";
  if (!id) {
    policy.value = { ...policy.value, exists: false, path: "" };
    return;
  }
  loading.value = true;
  try {
    applyPolicy(await getProjectPolicy(id));
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    loading.value = false;
  }
}

async function savePolicy() {
  const id = projectId();
  if (!id) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    applyPolicy(await saveProjectPolicy(id, { matrix: form.value }));
    notice.value =
      "Security policy disimpan untuk project ini (project lain tidak terpengaruh).";
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    busy.value = false;
  }
}

// --------------------------------------------------------------------------
// Agents tab
// --------------------------------------------------------------------------
const agentIsCustom = computed(
  () => agentActual.value.system_prompt !== agentActual.value.default_system_prompt
);
const consultantIsCustom = computed(
  () =>
    consultantActual.value.system_prompt !==
    consultantActual.value.default_system_prompt
);
const agentDirty = computed(() => agentDraft.value !== agentEffective());
const consultantDirty = computed(
  () => consultantDraft.value !== consultantEffective()
);

// Nilai efektif = override project bila ada, selain itu default AETHER.
function agentEffective() {
  return agentActual.value.system_prompt || agentActual.value.default_system_prompt;
}
function consultantEffective() {
  return (
    consultantActual.value.system_prompt || consultantActual.value.default_system_prompt
  );
}

function applyAgents(data) {
  const d = data || {};
  const agent = d.agent || {};
  const consultant = d.consultant || {};
  agentActual.value = {
    system_prompt: agent.system_prompt || "",
    default_system_prompt: agent.default_system_prompt || "",
  };
  consultantActual.value = {
    system_prompt: consultant.system_prompt || "",
    default_system_prompt: consultant.default_system_prompt || "",
  };
  agentPaths.value = d.paths || { agent: "", consultant: "" };
  agentDraft.value = agentEffective();
  consultantDraft.value = consultantEffective();
}

async function loadAgents() {
  const id = projectId();
  error.value = "";
  notice.value = "";
  if (!id) return;
  agentsLoading.value = true;
  try {
    applyAgents(await getProjectAgentSettings(id));
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    agentsLoading.value = false;
  }
}

async function saveAgents() {
  const id = projectId();
  if (!id) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    // Kirim override HANYA bila berbeda dari default AETHER; selain itu kirim
    // string kosong untuk menghapus override (default AETHER berlaku kembali).
    const agentPayload =
      agentDraft.value === agentActual.value.default_system_prompt
        ? ""
        : agentDraft.value;
    const consultantPayload =
      consultantDraft.value === consultantActual.value.default_system_prompt
        ? ""
        : consultantDraft.value;
    applyAgents(
      await saveProjectAgentSettings(id, {
        agent: { system_prompt: agentPayload },
        consultant: { system_prompt: consultantPayload },
      })
    );
    notice.value =
      "Agent & Consultant prompt disimpan untuk project ini (project lain tidak terpengaruh).";
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    busy.value = false;
  }
}

function restoreAgentDefault() {
  agentDraft.value = agentActual.value.default_system_prompt || "";
}
function restoreConsultantDefault() {
  consultantDraft.value = consultantActual.value.default_system_prompt || "";
}

// --------------------------------------------------------------------------
// Lifecycle
// --------------------------------------------------------------------------
async function loadAll() {
  error.value = "";
  notice.value = "";
  await Promise.all([loadPolicy(), loadAgents()]);
}

function close() {
  if (busy.value) return;
  emit("close");
}

watch(
  () => projectId(),
  () => {
    if (projectId()) loadAll();
  },
  { immediate: true }
);
</script>

<template>
  <!-- Modal AETHER (pola .modal-backdrop/.modal existing). -->
  <div v-if="projectId()" class="modal-backdrop" @click.self="close">
    <div class="modal pp-modal" role="dialog" aria-modal="true" aria-labelledby="pp-title">
      <div class="pp-modal-head">
        <div>
          <div id="pp-title" class="modal-title">Project Settings</div>
          <div class="pp-modal-sub">
            Configuration for
            <span class="mono">{{ project ? project.name : "project" }}</span>
            — saved per project.
          </div>
        </div>
        <button
          type="button"
          class="pp-x"
          aria-label="Close"
          title="Close"
          :disabled="busy"
          @click="close"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
      </div>

      <!-- Tab: Security (permission matrix) | Agents (system prompt). -->
      <div class="pp-tabs" role="tablist" aria-label="Project settings sections">
        <button
          type="button"
          class="pp-tab"
          role="tab"
          :class="{ active: activeTab === 'security' }"
          :aria-selected="activeTab === 'security'"
          @click="activeTab = 'security'"
        >
          Security
        </button>
        <button
          type="button"
          class="pp-tab"
          role="tab"
          :class="{ active: activeTab === 'agents' }"
          :aria-selected="activeTab === 'agents'"
          @click="activeTab = 'agents'"
        >
          Agents
        </button>
      </div>

      <div class="pp-modal-body">
        <div v-if="error" class="pp-alert err">{{ error }}</div>
        <div v-if="notice" class="pp-alert ok">{{ notice }}</div>

        <!-- ================= SECURITY ================= -->
        <template v-if="activeTab === 'security'">
          <div v-if="loading" class="pp-empty">Memuat Project Permission Matrix…</div>
          <template v-else>
            <!-- Ringkasan nilai AKTUAL project ini (bukan default global). -->
            <div class="pp-status">
              <span class="pp-dot" :class="policy.exists ? 'ok' : 'off'"></span>
              <span class="pp-status-text">
                <strong :class="policy.exists ? 'ok' : 'off'">
                  {{ policy.exists ? "Configured" : "Not initialized" }}
                </strong>
                — policy milik project ini saja.
              </span>
            </div>
            <div class="pp-meta mono" :title="policy.path">
              {{ policy.path || "—" }}
            </div>

            <div class="pp-form">
              <div class="pp-form-title">Permission Matrix</div>
              <table class="pp-matrix">
                <thead>
                  <tr>
                    <th class="pp-th-action">Action</th>
                    <th v-for="scope in scopes" :key="scope.value" class="pp-th-scope">
                      {{ scope.label }}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="action in actions" :key="action.value">
                    <td class="pp-td-action">{{ action.label }}</td>
                    <td
                      v-for="scope in action.scopes"
                      :key="scope.value"
                      class="pp-td-cell"
                    >
                      <select
                        v-model="form[action.value][scope.value]"
                        class="pp-input"
                        :disabled="busy"
                      >
                        <option v-for="m in modes" :key="m.value" :value="m.value">
                          {{ m.label }}
                        </option>
                      </select>
                    </td>
                  </tr>
                </tbody>
              </table>
              <div class="pp-hint">
                Matrix berlaku untuk operasi project ini (inside vs outside workspace);
                Setting global tidak berubah. DENY menahan action Agent, ASK menahan &
                meminta approval. Perubahan hanya tersimpan untuk project ini.
              </div>
            </div>
          </template>
        </template>

        <!-- ================= AGENTS ================= -->
        <template v-else>
          <div v-if="agentsLoading" class="pp-empty">Memuat Agent &amp; Consultant prompt…</div>
          <template v-else>
            <div class="pp-status">
              <span class="pp-dot" :class="agentIsCustom || consultantIsCustom ? 'ok' : 'off'"></span>
              <span class="pp-status-text">
                <strong :class="agentIsCustom || consultantIsCustom ? 'ok' : 'off'">
                  {{ agentIsCustom || consultantIsCustom ? "Custom" : "Default" }}
                </strong>
                — prompt milik project ini saja.
              </span>
            </div>

            <div class="pp-form">
              <div class="pp-form-title">System Prompt Agent</div>
              <div class="pp-hint">
                Instruction DASAR Agent untuk project ini. Context dinamis
                (Project Environment, Project Bible, Skill, tool) tetap disisipkan
                otomatis oleh AETHER. Bila kosong, default AETHER dipakai.
              </div>
              <textarea
                v-model="agentDraft"
                class="pp-textarea"
                spellcheck="false"
                rows="12"
                placeholder="System Prompt Agent (project ini)…"
              ></textarea>
              <div class="pp-textarea-meta">
                <span class="mono" :title="agentPaths.agent">{{ agentPaths.agent || "—" }}</span>
                <span v-if="agentIsCustom" class="pp-badge">custom</span>
                <span v-else class="pp-badge off">default</span>
              </div>
              <div class="pp-restore-row">
                <button type="button" class="pp-btn" :disabled="busy" @click="restoreAgentDefault">
                  Restore default
                </button>
              </div>
            </div>

            <div class="pp-form">
              <div class="pp-form-title">System Prompt Consultant</div>
              <div class="pp-hint">
                Instruction DASAR Consultant untuk project ini (mode quick /
                investigate tetap diatur AETHER). Bila kosong, prompt bawaan
                AETHER dipakai.
              </div>
              <textarea
                v-model="consultantDraft"
                class="pp-textarea"
                spellcheck="false"
                rows="12"
                placeholder="System Prompt Consultant (project ini)…"
              ></textarea>
              <div class="pp-textarea-meta">
                <span class="mono" :title="agentPaths.consultant">{{ agentPaths.consultant || "—" }}</span>
                <span v-if="consultantIsCustom" class="pp-badge">custom</span>
                <span v-else class="pp-badge off">default</span>
              </div>
              <div class="pp-restore-row">
                <button type="button" class="pp-btn" :disabled="busy" @click="restoreConsultantDefault">
                  Restore default
                </button>
              </div>
            </div>

            <div class="pp-hint">
              Provider, model, base URL, dan API key TETAP GLOBAL (Sidebar ->
              Settings). Hanya System Prompt yang melekat pada project ini.
            </div>
          </template>
        </template>
      </div>

      <div class="modal-actions pp-modal-foot">
        <button
          v-if="activeTab === 'security'"
          type="button"
          class="btn-ghost"
          :disabled="busy"
          @click="loadPolicy"
        >
          Reload
        </button>
        <button v-else type="button" class="btn-ghost" :disabled="busy" @click="loadAgents">
          Reload
        </button>
        <button type="button" class="btn-ghost" :disabled="busy" @click="close">
          Cancel
        </button>
        <button
          v-if="activeTab === 'security'"
          type="button"
          class="pp-btn primary"
          :disabled="busy || loading"
          @click="savePolicy"
        >
          {{ busy ? "Menyimpan…" : "Save Policy" }}
        </button>
        <button
          v-else
          type="button"
          class="pp-btn primary"
          :disabled="busy || agentsLoading || (!agentDirty && !consultantDirty)"
          @click="saveAgents"
        >
          {{ busy ? "Menyimpan…" : "Save Prompts" }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pp-modal {
  max-width: 720px;
  width: min(720px, 94vw);
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
}
.pp-modal-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 18px 12px;
}
.pp-tabs {
  display: inline-flex;
  gap: 4px;
  padding: 3px;
  margin: 0 18px 4px;
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  background: rgba(0, 0, 0, 0.22);
  align-self: flex-start;
}
.pp-tab {
  display: inline-flex;
  align-items: center;
  padding: 6px 14px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--text-dim);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}
.pp-tab:hover {
  color: var(--text);
  background: rgba(255, 255, 255, 0.045);
}
.pp-tab.active {
  color: var(--text);
  background: rgba(139, 92, 246, 0.2);
  box-shadow: inset 0 0 0 1px rgba(139, 92, 246, 0.32);
}
.pp-modal-sub {
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-faint);
}
.pp-x {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-dim);
  border-radius: 8px;
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  flex: 0 0 auto;
}
.pp-x:hover:not(:disabled) {
  color: var(--text);
  border-color: var(--accent);
}
.pp-modal-body {
  padding: 16px 18px;
  overflow: auto;
  display: grid;
  gap: 12px;
  max-height: 62vh;
}
.pp-modal-foot {
  padding: 14px 18px;
  margin-top: 0;
  border-top: 1px solid var(--border-soft);
}
.pp-empty {
  color: var(--text-faint);
  font-size: 12.5px;
}
.pp-alert {
  padding: 10px 12px;
  border-radius: 9px;
  font-size: 12.5px;
  border: 1px solid var(--border-soft);
}
.pp-alert.err {
  color: #fecaca;
  background: rgba(248, 113, 113, 0.1);
  border-color: rgba(248, 113, 113, 0.4);
}
.pp-alert.ok {
  color: #bbf7d0;
  background: rgba(34, 197, 94, 0.1);
  border-color: rgba(34, 197, 94, 0.35);
}
.pp-status {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--text-dim);
}
.pp-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-faint);
  flex: 0 0 auto;
}
.pp-dot.ok {
  background: #4ade80;
}
.pp-status-text strong.ok {
  color: #86efac;
}
.pp-status-text strong.off {
  color: var(--text-faint);
}
.pp-meta {
  font-size: 11.5px;
  color: var(--text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pp-form {
  border: 1px dashed var(--border-soft);
  border-radius: 12px;
  padding: 14px;
  display: grid;
  gap: 12px;
}
.pp-form-title {
  font-size: 12.5px;
  font-weight: 650;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.4px;
}
.pp-matrix {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.pp-th-action,
.pp-th-scope {
  text-align: left;
  font-weight: 600;
  color: var(--text-faint);
  font-size: 11px;
  padding: 4px 8px 6px 0;
}
.pp-td-action {
  color: var(--text-dim);
  padding: 4px 10px 4px 0;
  white-space: nowrap;
}
.pp-td-cell {
  padding: 4px 8px 4px 0;
}
.pp-td-cell .pp-input {
  padding: 6px 10px;
}
.pp-input {
  width: 100%;
  min-width: 0;
  padding: 9px 12px;
  border-radius: 9px;
  border: 1px solid var(--border);
  background: var(--bg-elev);
  color: var(--text);
  font-size: 12.5px;
}
.pp-input:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.18);
}
.pp-textarea {
  width: 100%;
  min-height: 180px;
  resize: vertical;
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--bg-elev);
  color: var(--text);
  font-size: 12.5px;
  line-height: 1.55;
  font-family: var(--mono);
}
.pp-textarea:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.18);
}
.pp-textarea-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11px;
  color: var(--text-faint);
  overflow: hidden;
}
.pp-textarea-meta .mono {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pp-badge {
  flex: 0 0 auto;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 650;
  text-transform: uppercase;
  color: #86efac;
  border: 1px solid rgba(134, 239, 172, 0.35);
}
.pp-badge.off {
  color: var(--text-faint);
  border-color: var(--border-soft);
}
.pp-restore-row {
  display: flex;
  justify-content: flex-end;
}
.pp-hint {
  font-size: 11px;
  color: var(--text-faint);
  line-height: 1.5;
}
.pp-btn {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-dim);
  border-radius: 8px;
  padding: 7px 14px;
  font-size: 12px;
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease;
}
.pp-btn:hover:not(:disabled) {
  color: var(--text);
  border-color: var(--accent);
}
.pp-btn.primary {
  border-color: var(--accent);
  color: var(--text);
  background: rgba(139, 92, 246, 0.16);
}
.pp-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
