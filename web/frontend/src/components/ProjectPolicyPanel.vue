<script setup>
// Project Permission Matrix MODAL (PROJECT-LOCAL, Sidebar -> Projects ->
// gear "Permission Policy" -> modal).
//
// SATU sumber policy per project: `<root>/.aether/permissions.json` — disimpan
// sebagai Permission Matrix (aksi x inside/outside), dibuat dari Default
// Project Permission Matrix saat project dibuat. Komponen ini HANYA memanggil
// HTTP gateway; TIDAK ada policy engine/konfigurasi permission kedua di
// frontend. Matrix di-enforce oleh PermissionManager AETHER existing saat
// Agent melakukan action.
//
// Perubahan policy di sini HANYA berlaku untuk project ini (isolasi project):
// project lain tidak terpengaruh, dan Default Project Policy tidak berubah.
import { ref, watch } from "vue";
import { getProjectPolicy, saveProjectPolicy } from "../api";

const props = defineProps({
  // Project (dari daftar launcher / active project): { id, name, root|path }.
  project: { type: Object, default: null },
});
// Modal ditutup oleh pemanggil (App.vue) saat Close/Cancel/backdrop diklik.
const emit = defineEmits(["close"]);

const loading = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");

// Nilai AKTUAL project (dibaca dari `<root>/.aether/permissions.json`).
const policy = ref({ matrix: {}, path: "", exists: false, project_id: "" });
// Opsi dari backend (TIDAK di-hardcode di frontend).
const actions = ref([]); // [{value, label, scopes:[{value,label}]}] dari backend
const scopes = ref([]);
const modes = ref([]);
// Pilihan di form (dikirim saat Save): { action: { scope: mode } }.
const form = ref({});

function projectId() {
  return props.project && (props.project.id || props.project.project_id);
}

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
    applyPolicy(await saveProjectPolicy(id, { matrix: form.value }));
    notice.value =
      "Policy disimpan untuk project ini (project lain tidak terpengaruh).";
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    busy.value = false;
  }
}

function close() {
  if (busy.value) return;
  emit("close");
}

watch(() => projectId(), load, { immediate: true });
</script>

<template>
  <!-- Modal AETHER (pola .modal-backdrop/.modal existing). Policy TIDAK lagi
       ditampilkan sebagai panel inline di bawah tombol gear. -->
  <div v-if="projectId()" class="modal-backdrop" @click.self="close">
    <div class="modal pp-modal" role="dialog" aria-modal="true" aria-labelledby="pp-title">
      <div class="pp-modal-head">
        <div>
          <div id="pp-title" class="modal-title">Permission Policy</div>
          <div class="pp-modal-sub">
            Permission matrix for
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

      <div class="pp-modal-body">
        <div v-if="loading" class="pp-empty">Memuat Project Permission Matrix…</div>

        <template v-else>
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
      </div>

      <div class="modal-actions pp-modal-foot">
        <button type="button" class="btn-ghost" :disabled="busy" @click="load">
          Reload
        </button>
        <button type="button" class="btn-ghost" :disabled="busy" @click="close">
          Cancel
        </button>
        <button type="button" class="pp-btn primary" :disabled="busy || loading" @click="save">
          {{ busy ? "Menyimpan…" : "Save Policy" }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pp-modal {
  max-width: 640px;
  width: min(640px, 94vw);
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
  padding: 16px 18px;
  border-bottom: 1px solid var(--border-soft);
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
}
.pp-modal-foot {
  padding: 14px 18px;
  margin-top: 0;
  border-top: 1px solid var(--border-soft);
}
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
