<script setup>
// Global Settings AETHER (`data/settings.json`).
//
// Sumber konfigurasi global AETHER = `data/settings.json` (satu-satunya).
// Komponen ini HANYA memanggil HTTP ke Django Gateway (`/api/settings`), yang
// meneruskan ke loader konfigurasi AETHER yang sudah ada
// (`agent_ai.config.settings`). TIDAK ada sumber/skema konfigurasi kedua di
// frontend: nilai yang ditampilkan = nilai AKTUAL dari backend, dan setiap
// simpan dikirim PARSIAL (hanya section terkait) sehingga key/setting lain di
// `data/settings.json` tidak hilang.
//
// Settings dipisah per SECTION (Server / Conversation / Logging / API Retry)
// agar tidak menjadi satu halaman panjang, dengan label + deskripsi jelas.
import { computed, onMounted, reactive, ref } from "vue";
import { getGlobalSettings, updateGlobalSettings } from "../api";

const loading = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");

// Nilai AKTUAL dari backend (sumber kebenaran tampilan; bukan nilai lokal UI).
const actual = ref({
  port: null,
  compression: { enabled: null },
  write_log_response_api: null,
  api_retry: { failed_count: null, failed_sleep: null },
});

// Draft form per section (diisi dari `actual` setiap kali load).
const form = reactive({
  port: 8000,
  compression_enabled: false,
  write_log_response_api: false,
  failed_count: 3,
  failed_sleep: 0,
});

const actualLines = computed(() => [
  { label: "port", value: String(actual.value.port ?? "—") },
  { label: "compression.enabled", value: String(actual.value.compression?.enabled) },
  { label: "write_log_response_api", value: String(actual.value.write_log_response_api) },
  { label: "api_retry.failed_count", value: String(actual.value.api_retry?.failed_count) },
  { label: "api_retry.failed_sleep", value: String(actual.value.api_retry?.failed_sleep) },
]);
// Isi draft dari nilai AKTUAL backend.
function applyActual(settings) {
  actual.value = settings;
  form.port = settings.port;
  form.compression_enabled = !!(settings.compression || {}).enabled;
  form.write_log_response_api = !!settings.write_log_response_api;
  const retry = settings.api_retry || {};
  form.failed_count = retry.failed_count;
  form.failed_sleep = retry.failed_sleep;
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const data = await getGlobalSettings();
    applyActual(data.settings || {});
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    loading.value = false;
  }
}

// Simpan PARSIAL: hanya key milik section yang dikirim. Backend melakukan
// deep-merge ke `data/settings.json` sehingga key lain dipertahankan.
async function save(patch, okMessage) {
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const data = await updateGlobalSettings(patch);
    applyActual(data.settings || {});
    notice.value = okMessage;
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    busy.value = false;
  }
}

function saveServer() {
  save({ port: Number(form.port) }, "Port disimpan.");
}
function saveConversation() {
  save(
    { compression: { enabled: form.compression_enabled } },
    "Compression disimpan."
  );
}
function saveLogging() {
  save({ write_log_response_api: form.write_log_response_api }, "Logging disimpan.");
}
function saveRetry() {
  save(
    {
      api_retry: {
        failed_count: Number(form.failed_count),
        failed_sleep: Number(form.failed_sleep),
      },
    },
    "API retry disimpan."
  );
}

function reset() {
  applyActual(actual.value);
  notice.value = "";
  error.value = "";
}

onMounted(load);
</script>

<template>
  <!-- Global AETHER Settings. Policy/permission TIDAK di sini: konfigurasi
       project (mode/scope permission) dikelola per project di
       Sidebar -> Projects -> Project Settings -> Security
       (`<root>/.aether/settings/permissions.json`). -->
  <section class="panel">
    <div class="panel-body">
      <div class="gs-scope">
        <span class="gs-scope-badge">Global AETHER Settings</span>
        <span class="gs-scope-note">
          Berlaku untuk seluruh AETHER (semua project).
          <span class="mono">data/settings.json</span>
        </span>
        <span class="gs-scope-hint">
          Untuk konfigurasi &amp; policy per project, buka
          <span class="mono">Sidebar -&gt; Projects -&gt; Project Settings / Policy</span>.
        </span>
      </div>
    </div>
  </section>

  <!-- Server -->
  <section class="panel">
    <div class="panel-head">
      <div>
        <div class="title">Server</div>
        <div class="desc">Port lokal yang dipakai AETHER saat dijalankan.</div>
      </div>
      <button class="btn-aether btn-ghost-a" :disabled="loading || busy" @click="load">
        Refresh
      </button>
    </div>
    <div class="panel-body">
      <div v-if="error" class="gs-alert err">{{ error }}</div>
      <div v-if="notice" class="gs-alert ok">{{ notice }}</div>

      <div class="gs-row">
        <div class="gs-label">
          <div class="gs-name">Port</div>
          <div class="gs-help">
            Port HTTP gateway AETHER (1–65535). Perubahan berlaku saat AETHER
            dijalankan ulang.
          </div>
        </div>
        <div class="gs-control">
          <input
            v-model.number="form.port"
            class="gs-input"
            type="number"
            min="1"
            max="65535"
            step="1"
            inputmode="numeric"
          />
        </div>
      </div>

      <div class="gs-actions">
        <button class="btn-aether btn-ghost-a" :disabled="busy" @click="reset">
          Reset
        </button>
        <button class="btn-aether btn-primary-a" :disabled="busy" @click="saveServer">
          Save
        </button>
      </div>
    </div>
  </section>

  <!-- Conversation context -->
  <section class="panel">
    <div class="panel-head">
      <div>
        <div class="title">Conversation</div>
        <div class="desc">Perilaku konteks percakapan sebelum dikirim ke model.</div>
      </div>
    </div>
    <div class="panel-body">
      <div class="gs-row">
        <div class="gs-label">
          <div class="gs-name">Conversation compression</div>
          <div class="gs-help">
            Aktifkan peringkasan/pemadatan riwayat percakapan saat konteks
            mendekati batas — hemat token, tetapi detail lama bisa diringkas.
          </div>
        </div>
        <div class="gs-control">
          <label class="gs-switch">
            <input v-model="form.compression_enabled" type="checkbox" />
            <span class="gs-track"><span class="gs-thumb"></span></span>
            <span class="gs-state">{{ form.compression_enabled ? "Enabled" : "Disabled" }}</span>
          </label>
        </div>
      </div>

      <div class="gs-actions">
        <button class="btn-aether btn-ghost-a" :disabled="busy" @click="reset">
          Reset
        </button>
        <button class="btn-aether btn-primary-a" :disabled="busy" @click="saveConversation">
          Save
        </button>
      </div>
    </div>
  </section>

  <!-- Logging -->
  <section class="panel">
    <div class="panel-head">
      <div>
        <div class="title">Logging</div>
        <div class="desc">Pencatatan response mentah API provider per task.</div>
      </div>
    </div>
    <div class="panel-body">
      <div class="gs-row">
        <div class="gs-label">
          <div class="gs-name">Log API responses</div>
          <div class="gs-help">
            Simpan setiap response provider ke
            <span class="mono">.aether/log/response/&lt;task_id&gt;.json</span>
            pada project target. Berguna untuk diagnosis, tetapi menambah berkas di
            disk.
          </div>
        </div>
        <div class="gs-control">
          <label class="gs-switch">
            <input v-model="form.write_log_response_api" type="checkbox" />
            <span class="gs-track"><span class="gs-thumb"></span></span>
            <span class="gs-state">{{ form.write_log_response_api ? "Enabled" : "Disabled" }}</span>
          </label>
        </div>
      </div>

      <div class="gs-actions">
        <button class="btn-aether btn-ghost-a" :disabled="busy" @click="reset">
          Reset
        </button>
        <button class="btn-aether btn-primary-a" :disabled="busy" @click="saveLogging">
          Save
        </button>
      </div>
    </div>
  </section>

  <!-- API Retry -->
  <section class="panel">
    <div class="panel-head">
      <div>
        <div class="title">API Retry</div>
        <div class="desc">
          Perilaku pengulangan request saat panggilan API provider gagal.
        </div>
      </div>
    </div>
    <div class="panel-body">
      <div class="gs-row">
        <div class="gs-label">
          <div class="gs-name">Retry attempts</div>
          <div class="gs-help">
            Jumlah pengulangan SETELAH request gagal (0 = tanpa retry). Total
            percobaan = 1 percobaan awal + nilai ini.
          </div>
        </div>
        <div class="gs-control">
          <input
            v-model.number="form.failed_count"
            class="gs-input"
            type="number"
            min="0"
            max="1000"
            step="1"
            inputmode="numeric"
          />
        </div>
      </div>

      <div class="gs-row">
        <div class="gs-label">
          <div class="gs-name">Retry delay (seconds)</div>
          <div class="gs-help">
            Jeda tunggu sebelum setiap pengulangan. 0 = langsung mengulang tanpa
            jeda.
          </div>
        </div>
        <div class="gs-control">
          <input
            v-model.number="form.failed_sleep"
            class="gs-input"
            type="number"
            min="0"
            max="3600"
            step="0.1"
            inputmode="decimal"
          />
        </div>
      </div>

      <div class="gs-actions">
        <button class="btn-aether btn-ghost-a" :disabled="busy" @click="reset">
          Reset
        </button>
        <button class="btn-aether btn-primary-a" :disabled="busy" @click="saveRetry">
          Save
        </button>
      </div>
    </div>
  </section>

  <!-- Nilai aktual pada `data/settings.json` (read-only, sumber kebenaran). -->
  <section class="panel">
    <div class="panel-head">
      <div>
        <div class="title">Actual values</div>
        <div class="desc">
          Nilai yang benar-benar dibaca AETHER dari
          <span class="mono">data/settings.json</span>.
        </div>
      </div>
    </div>
    <div class="panel-body">
      <div v-for="line in actualLines" :key="line.label" class="kv">
        <span class="k mono">{{ line.label }}</span>
        <span class="v mono">{{ line.value }}</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.gs-scope {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}
.gs-scope-badge {
  display: inline-flex;
  align-items: center;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 650;
  letter-spacing: 0.3px;
  text-transform: uppercase;
  color: #eae2ff;
  background: rgba(139, 92, 246, 0.18);
  border: 1px solid rgba(139, 92, 246, 0.4);
}
.gs-scope-note {
  font-size: 12px;
  color: var(--text-dim);
}
.gs-scope-note .mono,
.gs-scope-hint .mono {
  font-family: var(--mono);
}
.gs-scope-hint {
  width: 100%;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-faint);
}

.gs-alert {
  padding: 10px 12px;
  border-radius: 9px;
  font-size: 12.5px;
  border: 1px solid var(--border-soft);
  margin-bottom: 12px;
}
.gs-alert.err {
  color: #fecaca;
  background: rgba(248, 113, 113, 0.1);
  border-color: rgba(248, 113, 113, 0.4);
}
.gs-alert.ok {
  color: #bbf7d0;
  background: rgba(34, 197, 94, 0.1);
  border-color: rgba(34, 197, 94, 0.35);
}

.gs-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 12px 0;
  border-bottom: 1px solid var(--border-soft);
}
.gs-row:last-of-type {
  border-bottom: 0;
}
.gs-label {
  min-width: 0;
}
.gs-name {
  font-size: 13px;
  font-weight: 650;
  color: var(--text);
}
.gs-help {
  margin-top: 3px;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-faint);
  max-width: 520px;
}
.gs-help .mono {
  font-family: var(--mono);
}
.gs-control {
  flex: 0 0 auto;
}

.gs-input {
  width: 140px;
  padding: 8px 12px;
  border-radius: 9px;
  border: 1px solid var(--border);
  background: var(--bg-elev);
  color: var(--text);
  font-size: 12.5px;
  font-family: var(--mono);
  text-align: right;
}
.gs-input:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.18);
}

.gs-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}

/* Toggle switch (checkbox asli tetap dipakai -> aksesibel). */
.gs-switch {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  user-select: none;
}
.gs-switch input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}
.gs-track {
  position: relative;
  width: 42px;
  height: 22px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.09);
  border: 1px solid var(--border);
  transition: background 0.15s ease, border-color 0.15s ease;
}
.gs-thumb {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--text-faint);
  transition: transform 0.15s ease, background 0.15s ease;
}
.gs-switch input:checked + .gs-track {
  background: rgba(139, 92, 246, 0.32);
  border-color: rgba(139, 92, 246, 0.5);
}
.gs-switch input:checked + .gs-track .gs-thumb {
  transform: translateX(20px);
  background: #eae2ff;
}
.gs-switch input:focus-visible + .gs-track {
  box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.25);
}
.gs-state {
  font-size: 11.5px;
  color: var(--text-dim);
  min-width: 66px;
}
</style>
