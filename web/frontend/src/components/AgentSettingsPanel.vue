<script setup>
// Agent Settings AETHER (Default Execution Mode) — Sidebar -> Settings -> Agent.
//
// System Prompt Agent & Consultant TIDAK lagi di sini: konfigurasi prompt
// MELEKAT PER PROJECT dan dikelola di
// Sidebar -> Projects -> Project Settings -> Agents
// (`<root>/.aether/settings/agent.json` / `consultant.json`).
//
// Panel ini HANYA menyisakan preferensi GLOBAL: Default Execution Mode
// (`data/settings.json` -> `agent.default_mode`). Sumber konfigurasi TETAP
// `data/settings.json` (satu-satunya sumber konfigurasi global AETHER).
// Komponen ini HANYA memanggil HTTP ke Django Gateway (`/api/settings`) yang
// meneruskan ke loader konfigurasi AETHER yang sudah ada
// (`agent_ai.config.settings`). TIDAK ada sistem konfigurasi kedua di frontend:
// nilai yang ditampilkan = nilai AKTUAL dari backend, dan setiap simpan dikirim
// PARSIAL (hanya `agent.default_mode`) sehingga key/setting lain tidak hilang.
import { computed, onMounted, ref } from "vue";
import { getGlobalSettings, updateGlobalSettings } from "../api";

const loading = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");

// Nilai AKTUAL dari backend (sumber kebenaran tampilan; bukan nilai lokal UI).
const actual = ref({ default_mode: "balanced" });
// Draft editor (diisi dari `actual` setiap kali load/save).
const draftMode = ref("balanced");

const dirty = computed(() => draftMode.value !== actual.value.default_mode);
const canSave = computed(() => !busy.value && dirty.value);

function applyActual(settings) {
  const agent = settings.agent || {};
  actual.value = { default_mode: agent.default_mode || "balanced" };
  draftMode.value = actual.value.default_mode;
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

// Simpan PARSIAL: hanya `agent.default_mode` yang dikirim. Backend melakukan
// deep-merge ke `data/settings.json` sehingga key lain dipertahankan.
async function save() {
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const data = await updateGlobalSettings({
      agent: { default_mode: draftMode.value },
    });
    applyActual(data.settings || {});
    notice.value = "Pengaturan Agent berhasil disimpan.";
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    busy.value = false;
  }
}

function reset() {
  draftMode.value = actual.value.default_mode;
  notice.value = "";
  error.value = "";
}

onMounted(load);
</script>

<template>
  <section class="panel">
    <div class="panel-head">
      <div>
        <div class="title">Agent</div>
        <div class="desc">
          Preferensi eksekusi GLOBAL AETHER Agent.
        </div>
      </div>
      <button class="btn-aether btn-ghost-a" :disabled="loading || busy" @click="load">
        Refresh
      </button>
    </div>
    <div class="panel-body">
      <div v-if="error" class="as-alert err">{{ error }}</div>
      <div v-if="notice" class="as-alert ok">{{ notice }}</div>

      <div class="as-scope">
        <span class="as-scope-badge">Global AETHER Settings</span>
        <span class="as-scope-note">
          Disimpan di <span class="mono">data/settings.json</span> ->
          <span class="mono">agent.default_mode</span>.
        </span>
      </div>

      <div class="as-moved">
        System Prompt Agent &amp; Consultant sekarang MELEKAT PER PROJECT —
        kelola dari <strong>Sidebar → Projects → Project Settings → Agents</strong>
        (<span class="mono">.aether/settings/agent.json</span> /
        <span class="mono">consultant.json</span>).
      </div>

      <div v-if="loading" class="wb-empty">Memuat pengaturan Agent…</div>
      <template v-else>
        <!-- Mode selector on Settings -> Agent (GLOBAL preference). -->
        <div class="as-row as-mode-row">
          <div class="as-label">
            <div class="as-name">Default Execution Mode</div>
            <div class="as-help">
              Preferensi strategi eksekusi untuk task baru. Task dapat mengubah mode sebelum dijalankan; effective mode tetap ditentukan oleh Agent Policy System.
            </div>
          </div>
          <div class="as-control">
            <select v-model="draftMode" class="input-a" :disabled="busy">
              <option value="fast">Fast</option>
              <option value="balanced">Balanced</option>
              <option value="deep">Deep</option>
            </select>
          </div>
        </div>
        <div class="as-mode-help">
          <div><strong>Fast</strong> — Strategi cepat untuk perubahan kecil</div>
          <div><strong>Balanced</strong> — Strategi default AETHER untuk pekerjaan umum</div>
          <div><strong>Deep</strong> — Strategi analisis mendalam untuk perubahan kompleks</div>
        </div>

        <div class="as-actions">
          <button class="btn-aether btn-ghost-a" :disabled="busy || !dirty" @click="reset">
            Reset
          </button>
          <button class="btn-aether btn-primary-a" :disabled="!canSave" @click="save">
            Save
          </button>
        </div>
      </template>
    </div>
  </section>

  <!-- Nilai aktual pada `data/settings.json` (read-only, sumber kebenaran). -->
  <section class="panel">
    <div class="panel-head">
      <div>
        <div class="title">Actual value</div>
        <div class="desc">
          Nilai yang benar-benar dipakai AETHER dari
          <span class="mono">data/settings.json</span>.
        </div>
      </div>
    </div>
    <div class="panel-body">
      <div class="kv">
        <span class="k mono">agent.default_mode</span>
        <span class="v mono">{{ actual.default_mode }}</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.as-alert {
  padding: 10px 12px;
  border-radius: 9px;
  font-size: 12.5px;
  border: 1px solid var(--border-soft);
  margin-bottom: 12px;
}
.as-alert.err {
  color: #fecaca;
  background: rgba(248, 113, 113, 0.1);
  border-color: rgba(248, 113, 113, 0.4);
}
.as-alert.ok {
  color: #bbf7d0;
  background: rgba(34, 197, 94, 0.1);
  border-color: rgba(34, 197, 94, 0.35);
}

.as-scope {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.as-scope-badge {
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
.as-scope-note {
  font-size: 12px;
  color: var(--text-dim);
}
.as-scope-note .mono {
  font-family: var(--mono);
}

.as-moved {
  margin-bottom: 14px;
  padding: 10px 12px;
  border-radius: 9px;
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-dim);
  background: rgba(139, 92, 246, 0.06);
  border: 1px solid rgba(139, 92, 246, 0.18);
}

.as-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  padding-bottom: 12px;
}
.as-name {
  font-size: 13px;
  font-weight: 650;
  color: var(--text);
}
.as-help {
  margin-top: 3px;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-faint);
  max-width: 640px;
}
.as-control {
  flex: 0 0 auto;
}

.as-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}

.as-mode-row {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--border-soft);
}

.as-mode-help {
  margin-top: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  background: rgba(139, 92, 246, 0.06);
  border: 1px solid rgba(139, 92, 246, 0.15);
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-dim);
}
.as-mode-help div {
  margin: 2px 0;
}
.as-mode-help strong {
  color: var(--text);
}

.status-tag.ok {
  color: #86efac;
}
.status-tag.idle {
  opacity: 0.7;
}
</style>
