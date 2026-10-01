<script setup>
// Project Policy / Permission panel (PROJECT-LOCAL, Sidebar -> Projects ->
// Project Settings / Policy).
//
// SATU sumber policy per project: `<root>/.aether/permissions.json` (dibuat
// dari Default Project Policy saat project dibuat). Panel ini HANYA memanggil
// HTTP gateway; TIDAK ada policy engine kedua di frontend. Mode/scope
// di-enforce oleh PermissionManager AETHER existing.
//
// Perubahan policy di sini HANYA berlaku untuk project ini (isolasi project):
// project lain tidak terpengaruh, dan Default Project Policy tidak berubah.
import { ref, watch } from "vue";
import { getProjectPolicy, saveProjectPolicy } from "../api";

const props = defineProps({
  // Project (dari daftar launcher / active project): { id, name, root|path }.
  project: { type: Object, default: null },
});

const loading = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");

// Nilai AKTUAL project (dibaca dari `<root>/.aether/permissions.json`).
const policy = ref({
  mode: "",
  mode_label: "",
  scope: "",
  scope_label: "",
  path: "",
  exists: false,
  project_id: "",
});
// Opsi mode/scope dari backend (TIDAK di-hardcode di frontend).
const options = ref({ mode: [], scope: [] });
// Pilihan di form (dikirim saat Save).
const form = ref({ mode: "", scope: "" });

function projectId() {
  return props.project && (props.project.id || props.project.project_id);
}

function applyPolicy(data) {
  const d = data || {};
  policy.value = {
    mode: d.mode || "",
    mode_label: d.mode_label || "",
    scope: d.scope || "",
    scope_label: d.scope_label || "",
    path: d.path || "",
    exists: Boolean(d.exists),
    project_id: d.project_id || "",
  };
  if (d.options) options.value = d.options;
  form.value = { mode: policy.value.mode, scope: policy.value.scope };
}

async function load() {
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

async function save() {
  const id = projectId();
  if (!id) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    applyPolicy(
      await saveProjectPolicy(id, { mode: form.value.mode, scope: form.value.scope })
    );
    notice.value = "Policy disimpan untuk project ini (project lain tidak terpengaruh).";
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    busy.value = false;
  }
}

watch(() => projectId(), load, { immediate: true });
</script>

<template>
  <div class="pp">
    <div v-if="loading" class="pp-empty">Memuat Project Policy…</div>

    <template v-else-if="projectId()">
      <div v-if="error" class="pp-alert err">{{ error }}</div>
      <div v-if="notice" class="pp-alert ok">{{ notice }}</div>

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
        <div class="pp-form-title">Project Policy</div>
        <div class="pp-grid">
          <label class="pp-field">
            <span class="pp-label">Permission mode</span>
            <select v-model="form.mode" class="pp-input" :disabled="busy">
              <option v-for="o in options.mode" :key="o.value" :value="o.value">
                {{ o.label }}
              </option>
            </select>
          </label>
          <label class="pp-field">
            <span class="pp-label">Scope</span>
            <select v-model="form.scope" class="pp-input" :disabled="busy">
              <option v-for="o in options.scope" :key="o.value" :value="o.value">
                {{ o.label }}
              </option>
            </select>
          </label>
        </div>
        <div class="pp-hint">
          Mode berlaku untuk operasi project ini; Setting global tidak berubah.
          Perubahan hanya tersimpan untuk project ini (isolasi project).
        </div>
        <div class="pp-actions">
          <button class="pp-btn" type="button" :disabled="busy || loading" @click="load">
            Reload
          </button>
          <button class="pp-btn primary" type="button" :disabled="busy" @click="save">
            {{ busy ? "Menyimpan…" : "Save Policy" }}
          </button>
        </div>
      </div>
    </template>

    <div v-else class="pp-empty">
      Pilih/buka project untuk melihat Project Policy.
    </div>
  </div>
</template>

<style scoped>
.pp {
  display: grid;
  gap: 12px;
  min-width: 0;
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
.pp-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 10px;
}
.pp-field {
  display: grid;
  gap: 5px;
  min-width: 0;
}
.pp-label {
  font-size: 11.5px;
  color: var(--text-faint);
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
.pp-hint {
  font-size: 11px;
  color: var(--text-faint);
  line-height: 1.5;
}
.pp-actions {
  display: flex;
  gap: 9px;
  flex-wrap: wrap;
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
