<script setup>
// Agent Settings AETHER (System Prompt Agent) — Sidebar -> Settings -> Agent.
//
// Sumber konfigurasi TETAP `data/settings.json` (satu-satunya sumber
// konfigurasi global AETHER). Komponen ini HANYA memanggil HTTP ke Django
// Gateway (`/api/settings`), yang meneruskan ke loader konfigurasi AETHER yang
// sudah ada (`agent_ai.config.settings`). TIDAK ada sistem prompt /
// konfigurasi kedua di frontend: nilai yang ditampilkan = nilai AKTUAL dari
// backend, dan setiap simpan dikirim PARSIAL (hanya `agent.system_prompt`)
// sehingga key/setting lain di `data/settings.json` tidak hilang.
//
// Prompt ini adalah INSTRUCTION DASAR Agent. Context dinamis yang sudah ada
// (Project Environment, Project Bible, Skill, tool context) tetap disisipkan
// oleh backend runtime AETHER seperti sebelumnya — frontend TIDAK menjalankan
// logic agent apa pun di sini.
import { computed, onMounted, ref } from "vue";
import { getGlobalSettings, updateGlobalSettings } from "../api";

const loading = ref(false);
const busy = ref(false);
const error = ref("");
const notice = ref("");

// Nilai AKTUAL dari backend (sumber kebenaran tampilan; bukan nilai lokal UI).
const actual = ref({ system_prompt: "", default_system_prompt: "" });
// Draft editor (diisi dari `actual` setiap kali load/save).
const draft = ref("");

const maxChars = 200000;

const dirty = computed(() => draft.value !== actual.value.system_prompt);
const charCount = computed(() => draft.value.length);
const canSave = computed(
  () => !busy.value && dirty.value && !!draft.value.trim() && charCount.value <= maxChars
);
const isCustom = computed(
  () => actual.value.system_prompt !== actual.value.default_system_prompt
);
const isDefaultValue = computed(
  () => draft.value === actual.value.default_system_prompt
);

function applyActual(settings) {
  const agent = settings.agent || {};
  actual.value = {
    system_prompt: agent.system_prompt || "",
    default_system_prompt: agent.default_system_prompt || "",
  };
  draft.value = actual.value.system_prompt;
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

// Simpan PARSIAL: hanya `agent.system_prompt` yang dikirim. Backend melakukan
// deep-merge ke `data/settings.json` sehingga key lain dipertahankan.
async function save() {
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const data = await updateGlobalSettings({
      agent: { system_prompt: draft.value },
    });
    applyActual(data.settings || {});
    notice.value = "System Prompt Agent disimpan.";
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    busy.value = false;
  }
}

// Batalkan perubahan yang belum disimpan (kembali ke nilai AKTUAL backend).
function reset() {
  draft.value = actual.value.system_prompt;
  notice.value = "";
  error.value = "";
}

// Isi draft dengan System Prompt bawaan AETHER (belum tersimpan sampai Save).
function restoreDefault() {
  draft.value = actual.value.default_system_prompt || "";
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
          System Prompt / Agent Instructions yang dipakai AETHER Agent saat
          membuat system message.
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
          <span class="mono">agent.system_prompt</span>.
        </span>
      </div>

      <div v-if="loading" class="wb-empty">Memuat System Prompt…</div>
      <template v-else>
        <div class="as-row">
          <div class="as-label">
            <div class="as-name">System Prompt Agent</div>
            <div class="as-help">
              Instruction DASAR Agent. Context dinamis (Project Environment,
              Project Bible, Skill, dan definisi tool) tetap disisipkan otomatis
              oleh AETHER; TIDAK perlu ditulis ulang di sini.
            </div>
          </div>
          <div class="as-control">
            <span class="status-tag" :class="isCustom ? 'ok' : 'idle'">
              {{ isCustom ? "custom" : "default" }}
            </span>
          </div>
        </div>

        <textarea
          v-model="draft"
          class="as-textarea"
          spellcheck="false"
          rows="20"
          placeholder="System Prompt Agent…"
        ></textarea>

        <div class="as-meta">
          <span class="mono">{{ charCount }} / {{ maxChars }} karakter</span>
          <span v-if="dirty" class="as-dirty">perubahan belum disimpan</span>
          <span v-else class="as-clean">tersimpan</span>
        </div>

        <div class="as-actions">
          <button class="btn-aether btn-ghost-a" :disabled="busy" @click="restoreDefault">
            Restore default
          </button>
          <button class="btn-aether btn-ghost-a" :disabled="busy || !dirty" @click="reset">
            Reset
          </button>
          <button class="btn-aether btn-primary-a" :disabled="!canSave" @click="save">
            Save
          </button>
        </div>
        <div v-if="isDefaultValue" class="as-hint">
          Editor sedang memuat System Prompt bawaan AETHER.
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
          Nilai yang benar-benar dipakai AETHER Agent dari
          <span class="mono">data/settings.json</span>.
        </div>
      </div>
    </div>
    <div class="panel-body">
      <div class="kv">
        <span class="k mono">agent.system_prompt</span>
        <span class="v mono">
          {{ actual.system_prompt.length }} karakter ·
          {{ isCustom ? "custom" : "default (bawaan)" }}
        </span>
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

.as-textarea {
  width: 100%;
  min-height: 320px;
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
.as-textarea:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.18);
}

.as-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 8px;
  font-size: 11.5px;
  color: var(--text-faint);
}
.as-dirty {
  color: #fcd34d;
}
.as-clean {
  color: #86efac;
}

.as-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}
.as-hint {
  margin-top: 8px;
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
