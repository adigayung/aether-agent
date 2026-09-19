<script setup>
// Settings page (Configuration).
//
// Mengelola konfigurasi LLM AETHER lewat endpoint backend (`/api/llm/*`) yang
// MEMAKAI LLMConfigService AETHER existing. Frontend HANYA memanggil HTTP:
// tidak ada logic agent, tidak ada model konfigurasi kedua.
//
// Keamanan: backend TIDAK pernah mengirim nilai secret .env — hanya versi
// `masked`. Frontend tidak menyimpan/menampilkan nilai API key.
import { computed, onMounted, reactive, ref } from "vue";
import {
  getLLMConfig,
  createLLMProvider,
  updateLLMProvider,
  deleteLLMProvider,
  createLLMModel,
  updateLLMModel,
  deleteLLMModel,
  createLLMCredential,
  deleteLLMCredential,
} from "../api";

const props = defineProps({
  // Konfigurasi runtime aktif dari AETHER (provider/model/mode + instance).
  config: { type: Object, default: () => ({}) },
});

const loading = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");

const credentials = ref([]);
const providerTypes = ref([]);
const providers = ref([]);

// Form: provider instance baru.
const providerForm = reactive({
  name: "",
  provider_type: "",
  api_key_env: "",
  api_url: "",
});
// Form: credential (.env) baru.
const credentialForm = reactive({ name: "", value: "" });
// Draft nama model per provider instance (key = provider id).
const modelDrafts = reactive({});

const providerTypeSpec = computed(() => {
  const map = {};
  for (const t of providerTypes.value) map[t.key] = t;
  return map;
});
// Provider type terpilih butuh API key? (Ollama lokal: tidak).
// Dipakai untuk menyembunyikan field api_key_env HANYA pada provider lokal.
const providerNeedsApiKey = computed(() => {
  const spec = providerTypeSpec.value[providerForm.provider_type];
  return spec ? spec.requires_api_key !== false : true;
});
const providerTypeLabel = computed(() => {
  const map = {};
  for (const t of providerTypes.value) map[t.key] = t.label;
  return map;
});
const credentialByName = computed(() => {
  const map = {};
  for (const c of credentials.value) map[c.name] = c;
  return map;
});

const activeInstance = computed(() => {
  const id = props.config.provider_instance_id;
  if (!id) return null;
  return (props.config.provider_instances || []).find((p) => p.id === id) || null;
});
const activeModelName = computed(() => {
  const inst = activeInstance.value;
  if (!inst) return "";
  const m = (inst.models || []).find((x) => x.id === props.config.model_id);
  return m ? m.model_name : "";
});

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const data = await getLLMConfig();
    credentials.value = data.credentials || [];
    providerTypes.value = data.provider_types || [];
    providers.value = data.providers || [];
    if (!providerForm.provider_type && providerTypes.value.length) {
      providerForm.provider_type = providerTypes.value[0].key;
      onProviderTypeChange();
    }
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    loading.value = false;
  }
}

// Jalankan aksi tulis, lalu muat ulang config (sumber kebenaran = backend).
async function run(action, okMessage) {
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    await action();
    await load();
    notice.value = okMessage;
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    busy.value = false;
  }
}

// Isi default api_url / api_key_env dari katalog provider type (statis).
function onProviderTypeChange() {
  const spec = providerTypeSpec.value[providerForm.provider_type];
  if (!spec) return;
  providerForm.api_url = spec.default_api_url || "";
  // Provider lokal (Ollama) tidak butuh API key: kosongkan api_key_env.
  // Provider cloud tetap memakai <PREFIX>_API_KEY seperti sebelumnya.
  providerForm.api_key_env = spec.requires_api_key ? `${spec.env_prefix}_API_KEY` : "";
}

function submitProvider() {
  const payload = {
    name: providerForm.name,
    provider_type: providerForm.provider_type,
    // Provider lokal (Ollama) selalu dikirim tanpa api_key_env.
    api_key_env: providerNeedsApiKey.value ? providerForm.api_key_env : "",
    api_url: providerForm.api_url,
  };
  run(async () => {
    await createLLMProvider(payload);
    providerForm.name = "";
    providerForm.api_key_env = "";
    providerForm.api_url = "";
  }, "Provider instance dibuat.");
}

function toggleProvider(p) {
  run(() => updateLLMProvider(p.id, { enabled: !p.enabled }), "Provider diperbarui.");
}

async function removeProvider(p) {
  if (!window.confirm(`Hapus provider instance "${p.name}" beserta model-nya?`)) return;
  run(() => deleteLLMProvider(p.id), "Provider dihapus.");
}

function submitModel(p) {
  const name = (modelDrafts[p.id] || "").trim();
  if (!name) return;
  run(async () => {
    await createLLMModel({ provider_id: p.id, model_name: name });
    modelDrafts[p.id] = "";
  }, "Model ditambahkan.");
}

function toggleModel(m) {
  run(() => updateLLMModel(m.id, { enabled: !m.enabled }), "Model diperbarui.");
}

function removeModel(m) {
  run(() => deleteLLMModel(m.id), "Model dihapus.");
}

function submitCredential() {
  run(async () => {
    await createLLMCredential(credentialForm.name, credentialForm.value);
    credentialForm.name = "";
    credentialForm.value = "";
  }, "Credential disimpan.");
}

function removeCredential(c) {
  const used = (c.used_by || []).length > 0;
  if (used && !window.confirm(`Credential dipakai oleh: ${c.used_by.join(", ")}. Tetap hapus?`)) {
    return;
  }
  run(() => deleteLLMCredential(c.name, used), "Credential dihapus.");
}

onMounted(load);
</script>

<template>
  <!-- Runtime aktif (read-only summary dari AETHER). -->
  <section class="panel">
    <div class="panel-head">
      <div>
        <div class="title">Configuration</div>
        <div class="desc">Provider, model, dan credential yang dipakai AETHER.</div>
      </div>
      <button class="btn-aether btn-ghost-a" :disabled="loading || busy" @click="load">
        Refresh
      </button>
    </div>
    <div class="panel-body">
      <div v-if="error" class="sv-alert err">{{ error }}</div>
      <div v-if="notice" class="sv-alert ok">{{ notice }}</div>
      <div v-if="loading" class="wb-empty">Memuat konfigurasi…</div>
      <template v-else>
        <div class="kv">
          <span class="k">Active Provider</span>
          <span class="v">{{ activeInstance ? activeInstance.name : config.provider || "—" }}</span>
        </div>
        <div class="kv">
          <span class="k">Active Model</span>
          <span class="v">{{ activeModelName || config.model || "—" }}</span>
        </div>
        <div class="kv">
          <span class="k">Mode</span>
          <span class="v">{{ config.mode || "—" }}</span>
        </div>
      </template>
    </div>
  </section>

  <!-- Provider instance + model (relasi Provider -> Model). -->
  <section class="panel">
    <div class="panel-head">
      <div>
        <div class="title">Providers &amp; Models</div>
        <div class="desc">{{ providers.length }} provider instance(s)</div>
      </div>
    </div>
    <div class="panel-body">
      <div v-if="!providers.length" class="wb-empty">Belum ada provider instance.</div>

      <div v-for="p in providers" :key="p.id" class="sv-prov">
        <div class="sv-prov-head">
          <div class="sv-prov-id">
            <span class="avatar">{{ (p.name || "P").slice(0, 1).toUpperCase() }}</span>
            <div>
              <div class="sv-prov-name">
                {{ p.name }}
                <span class="chip chip-sm">{{ providerTypeLabel[p.provider_type] || p.provider_type }}</span>
              </div>
              <div class="sv-prov-meta">
                <span class="mono">{{ p.api_url || "—" }}</span>
                <span class="mono">· {{ p.api_key_env || "no api key" }}</span>
                <span class="mono" :class="p.api_key_present ? 'ok' : 'warn'">
                  · {{ p.api_key_present ? "key set" : "key missing" }}
                </span>
              </div>
            </div>
          </div>
          <div class="sv-actions">
            <button
              class="btn-aether btn-ghost-a"
              :disabled="busy"
              @click="toggleProvider(p)"
            >
              {{ p.enabled ? "Disable" : "Enable" }}
            </button>
            <button class="btn-aether btn-danger-a" :disabled="busy" @click="removeProvider(p)">
              Delete
            </button>
          </div>
        </div>

        <!-- Model milik provider instance ini. -->
        <div class="sv-models">
          <div v-if="!(p.models || []).length" class="sv-models-empty">Belum ada model.</div>
          <div v-for="m in p.models" :key="m.id" class="sv-model-row">
            <span class="mono">{{ m.model_name }}</span>
            <span class="sv-model-actions">
              <span class="status-tag" :class="m.enabled ? 'ok' : 'idle'">
                {{ m.enabled ? "enabled" : "disabled" }}
              </span>
              <button class="sv-mini" :disabled="busy" @click="toggleModel(m)">
                {{ m.enabled ? "Disable" : "Enable" }}
              </button>
              <button class="sv-mini danger" :disabled="busy" @click="removeModel(m)">Delete</button>
            </span>
          </div>

          <div class="sv-add-row">
            <input
              v-model="modelDrafts[p.id]"
              class="sv-input"
              placeholder="Nama model (mis. openai/gpt-4o-mini)"
              @keyup.enter="submitModel(p)"
            />
            <button class="btn-aether btn-primary-a" :disabled="busy" @click="submitModel(p)">
              Add model
            </button>
          </div>
        </div>
      </div>

      <!-- Form: provider instance baru. -->
      <div class="sv-form">
        <div class="sv-form-title">Add provider instance</div>
        <div class="sv-form-grid">
          <input v-model="providerForm.name" class="sv-input" placeholder="Nama instance" />
          <select v-model="providerForm.provider_type" class="sv-input" @change="onProviderTypeChange">
            <option v-for="t in providerTypes" :key="t.key" :value="t.key">{{ t.label }}</option>
          </select>
          <input
            v-if="providerNeedsApiKey"
            v-model="providerForm.api_key_env"
            class="sv-input"
            placeholder="Nama variabel .env API key (mis. OPENROUTER_API_KEY)"
          />
          <input v-model="providerForm.api_url" class="sv-input" placeholder="Base API URL" />
        </div>
        <div class="sv-form-actions">
          <button class="btn-aether btn-primary-a" :disabled="busy" @click="submitProvider">
            Create provider
          </button>
        </div>
      </div>
    </div>
  </section>

  <!-- Credential (.env API key) — hanya versi masked yang ditampilkan. -->
  <section class="panel">
    <div class="panel-head">
      <div>
        <div class="title">API Credentials</div>
        <div class="desc">Disimpan di .env AETHER. Nilai secret tidak pernah ditampilkan.</div>
      </div>
    </div>
    <div class="panel-body">
      <div v-if="!credentials.length" class="wb-empty">Belum ada credential.</div>

      <div v-for="c in credentials" :key="c.name" class="sv-cred">
        <div>
          <div class="sv-cred-name">
            <span class="mono">{{ c.name }}</span>
            <span class="chip chip-sm">{{ c.provider_label }}</span>
            <span class="status-tag" :class="c.is_set ? 'ok' : 'idle'">
              {{ c.is_set ? "set" : "missing" }}
            </span>
          </div>
          <div class="sv-cred-meta">
            <span class="mono">{{ c.masked || "—" }}</span>
            <span v-if="(c.used_by || []).length" class="mono">· used by: {{ c.used_by.join(", ") }}</span>
          </div>
        </div>
        <button class="btn-aether btn-danger-a" :disabled="busy" @click="removeCredential(c)">
          Delete
        </button>
      </div>

      <!-- Form: set/simpan API key (.env). -->
      <div class="sv-form">
        <div class="sv-form-title">Set API key</div>
        <div class="sv-form-grid">
          <input
            v-model="credentialForm.name"
            class="sv-input"
            placeholder="Nama variabel .env (mis. OPENROUTER_API_KEY)"
          />
          <input
            v-model="credentialForm.value"
            class="sv-input"
            type="password"
            placeholder="Nilai API key"
            @keyup.enter="submitCredential"
          />
        </div>
        <div class="sv-form-actions">
          <button class="btn-aether btn-primary-a" :disabled="busy" @click="submitCredential">
            Save credential
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.sv-alert {
  padding: 10px 12px;
  border-radius: 9px;
  font-size: 12.5px;
  border: 1px solid var(--border-soft);
}
.sv-alert.err {
  color: #fecaca;
  background: rgba(248, 113, 113, 0.1);
  border-color: rgba(248, 113, 113, 0.4);
}
.sv-alert.ok {
  color: #bbf7d0;
  background: rgba(34, 197, 94, 0.1);
  border-color: rgba(34, 197, 94, 0.35);
}

.sv-prov {
  border: 1px solid var(--border-soft);
  border-radius: 12px;
  padding: 14px;
  display: grid;
  gap: 12px;
}
.sv-prov-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  flex-wrap: wrap;
}
.sv-prov-id {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.sv-prov-name {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 650;
}
.sv-prov-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 3px;
  font-size: 11.5px;
  color: var(--text-faint);
}
.sv-prov-meta .mono.ok {
  color: #86efac;
}
.sv-prov-meta .mono.warn {
  color: #fcd34d;
}
.sv-actions {
  display: flex;
  gap: 8px;
}

.avatar {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: 9px;
  background: rgba(139, 92, 246, 0.16);
  color: #c4b5fd;
  font-weight: 700;
  flex: 0 0 auto;
}

.sv-models {
  display: grid;
  gap: 8px;
  border-top: 1px solid var(--border-soft);
  padding-top: 12px;
}
.sv-models-empty {
  color: var(--text-faint);
  font-style: italic;
  font-size: 12.5px;
}
.sv-model-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 6px 8px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.015);
}
.sv-model-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.sv-mini {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-dim);
  border-radius: 7px;
  padding: 4px 9px;
  font-size: 11.5px;
  cursor: pointer;
}
.sv-mini:hover {
  color: var(--text);
  border-color: var(--accent);
}
.sv-mini.danger:hover {
  color: var(--err);
  border-color: var(--err);
}

.sv-add-row {
  display: flex;
  gap: 8px;
  margin-top: 4px;
}
.sv-add-row .sv-input {
  flex: 1 1 auto;
}

.sv-form {
  border: 1px dashed var(--border-soft);
  border-radius: 12px;
  padding: 14px;
  display: grid;
  gap: 10px;
}
.sv-form-title {
  font-size: 12.5px;
  font-weight: 650;
  color: var(--text-dim);
}
.sv-form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 10px;
}
.sv-form-actions {
  display: flex;
  justify-content: flex-end;
}

.sv-input {
  width: 100%;
  padding: 9px 12px;
  border-radius: 9px;
  border: 1px solid var(--border);
  background: var(--bg-elev);
  color: var(--text);
  font-size: 12.5px;
  font-family: var(--mono);
}
.sv-input:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.18);
}

.sv-cred {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 12px 14px;
  border: 1px solid var(--border-soft);
  border-radius: 10px;
}
.sv-cred-name {
  display: flex;
  align-items: center;
  gap: 8px;
}
.sv-cred-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 4px;
  font-size: 11.5px;
  color: var(--text-faint);
}

.status-tag.ok {
  color: #86efac;
}
.status-tag.idle {
  opacity: 0.7;
}
</style>
