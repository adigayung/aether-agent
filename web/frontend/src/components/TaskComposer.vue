<script setup>
// Task Composer (#52 rework). Input utama user untuk memberi pekerjaan.
// Send (Run Task) dan Stop adalah DUA aksi TERPISAH (bukan toggle): Run Task
// selalu tersedia — task baru masuk Global Task Queue (pending/queued bila slot
// eksekusi terpakai) — sedangkan Stop hanya tampil bila ADA task yang benar-
// benar RUNNING. `disabled` (= isSubmitting) HANYA mencegah double-submit.
// Model & mode dibaca dari konfigurasi AETHER (TIDAK hardcode).
// TIDAK ada execution engine di frontend: hanya memanggil API #50.
import { computed, onMounted, ref } from "vue";

const props = defineProps({
  disabled: { type: Boolean, default: false },
  running: { type: Boolean, default: false },
  config: { type: Object, default: () => ({}) },
  // Provider Instance + Model dari konfigurasi LLM tersimpan (SQLite).
  // `providers` = [{id, name, provider_type, provider_label, enabled, models:[...]}].
  providers: { type: Array, default: () => [] },
  providerInstanceId: { type: String, default: "" },
  modelId: { type: String, default: "" },
  mode: { type: String, default: "" },
});
const emit = defineEmits([
  "submit",
  "stop",
  "update:providerInstanceId",
  "update:modelId",
  "update:mode",
]);

const text = ref("");
const textarea = ref(null);

// Label mode ramah-user dari retrieval profile AETHER (#40).
const MODE_LABELS = { minimal: "Fast", balanced: "Balanced", deep: "Deep" };
const modes = computed(() => props.config.modes || ["minimal", "balanced", "deep"]);

// Provider Instance dari konfigurasi LLM tersimpan (SQLite). Hanya instance
// enabled yang ditampilkan (instance disabled tidak bisa dipakai task).
const providerOptions = computed(() =>
  (props.providers || []).filter((p) => p.enabled !== false)
);

// Model difilter: HANYA model milik Provider Instance yang dipilih.
const modelOptions = computed(() => {
  const inst = providerOptions.value.find((p) => p.id === props.providerInstanceId);
  if (!inst) return [];
  return (inst.models || []).filter((m) => m.enabled !== false);
});

// Label provider instance: "nama (label type)".
function providerLabel(p) {
  const type = p.provider_label || p.provider_type || "";
  return type ? `${p.name} (${type})` : p.name;
}

// Ubah Provider Instance -> reset model (model lama milik instance lain).
function onProviderChange(e) {
  emit("update:providerInstanceId", String(e.target.value || ""));
  emit("update:modelId", "");
}

// Ubah Model.
function onModelChange(e) {
  emit("update:modelId", String(e.target.value || ""));
}

onMounted(() => {
  if (textarea.value) textarea.value.focus();
});

function submit() {
  // Agent Input TIDAK diblokir oleh task yang sedang running: task baru selalu
  // dapat dikirim dan masuk Global Task Queue (pending/queued). `disabled` hanya
  // mencegah double-submit selama request createTask belum selesai.
  const value = text.value.trim();
  if (!value || props.disabled) return;
  emit("submit", value);
  text.value = "";
}
</script>

<template>
  <div class="composer-body">
    <textarea
      ref="textarea"
      v-model="text"
      class="composer-text"
      :disabled="disabled"
      placeholder="Describe the task for AETHER…  (Enter for a new line)"
    ></textarea>
  </div>

  <div class="composer-foot">
    <div class="composer-selects">
      <!-- Provider Instance: dari konfigurasi LLM tersimpan (SQLite). -->
      <label class="composer-select">
        <span class="cs-label">Provider</span>
        <select
          class="input-a"
          :disabled="disabled"
          :value="providerInstanceId"
          @change="onProviderChange"
        >
          <option v-if="!providerOptions.length" value="">No provider instance</option>
          <option v-for="p in providerOptions" :key="p.id" :value="p.id">
            {{ providerLabel(p) }}
          </option>
        </select>
      </label>

      <!-- Model: HANYA model milik Provider Instance yang dipilih. -->
      <label class="composer-select">
        <span class="cs-label">Model</span>
        <select
          class="input-a"
          :disabled="disabled || !providerInstanceId"
          :value="modelId"
          @change="onModelChange"
        >
          <option v-if="!modelOptions.length" value="">No model</option>
          <option v-for="m in modelOptions" :key="m.id" :value="m.id">
            {{ m.model_name }}
          </option>
        </select>
      </label>

      <!-- Mode selector: routing profile AETHER (Fast/Balanced/Deep). -->
      <label class="composer-select">
        <span class="cs-label">Mode</span>
        <select class="input-a" :disabled="disabled" :value="mode" @change="emit('update:mode', $event.target.value)">
          <option v-for="m in modes" :key="m" :value="m">{{ MODE_LABELS[m] || m }}</option>
        </select>
      </label>
    </div>

    <div class="composer-actions">
      <button
        class="btn-aether btn-primary-a"
        type="button"
        :disabled="disabled || !text.trim()"
        @click="submit"
      >
        {{ disabled ? "Sending…" : "Run Task" }}
      </button>
      <button
        v-if="running"
        class="btn-aether btn-ghost-a"
        type="button"
        title="Stop running task"
        @click="emit('stop')"
      >
        Stop Task
      </button>
    </div>
  </div>
</template>
