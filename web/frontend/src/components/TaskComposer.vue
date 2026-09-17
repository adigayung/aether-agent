<script setup>
// Task Composer (#52 rework). Input utama user untuk memberi pekerjaan.
// Saat idle: composer + Start Task. Saat berjalan: Stop Task.
// Model & mode dibaca dari konfigurasi AETHER (TIDAK hardcode).
// TIDAK ada execution engine di frontend: hanya memanggil API #50.
import { computed, onMounted, ref } from "vue";

const props = defineProps({
  disabled: { type: Boolean, default: false },
  running: { type: Boolean, default: false },
  config: { type: Object, default: () => ({}) },
  provider: { type: String, default: "" },
  model: { type: String, default: "" },
  mode: { type: String, default: "" },
});
const emit = defineEmits([
  "submit",
  "stop",
  "update:provider",
  "update:model",
  "update:mode",
]);

const text = ref("");
const textarea = ref(null);

// Label mode ramah-user dari retrieval profile AETHER (#40).
const MODE_LABELS = { minimal: "Fast", balanced: "Balanced", deep: "Deep" };
const modes = computed(() => props.config.modes || ["minimal", "balanced", "deep"]);

// Daftar provider+model yang benar-benar tersedia dari konfigurasi AETHER.
// `config.models` = [{provider, model}]. Fallback ke `config.providers`
// (nama provider saja) bila `models` belum tersedia.
const modelOptions = computed(() => {
  const models = props.config.models || [];
  if (models.length) return models;
  return (props.config.providers || []).map((p) => ({ provider: p, model: "" }));
});

// Label tombol: tampilkan model (bila ada) + provider.
function optionLabel(opt) {
  return opt.model ? `${opt.model} · ${opt.provider}` : opt.provider;
}

// Nilai model aktif = "provider::model" (untuk <select>).
const currentModelValue = computed(() => {
  if (!props.provider && !props.model) return "";
  return `${props.provider}::${props.model || ""}`;
});

// Ubah pilihan model (native select) -> emit provider+model.
function onModelChange(e) {
  const [prov, mod] = String(e.target.value || "").split("::");
  emit("update:provider", prov || "");
  emit("update:model", mod || "");
}

onMounted(() => {
  if (textarea.value) textarea.value.focus();
});

function submit() {
  const value = text.value.trim();
  if (!value || props.disabled || props.running) return;
  emit("submit", value);
  text.value = "";
}

function onKeydown(e) {
  // Enter mengirim; Shift+Enter baris baru.
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submit();
  }
}
</script>

<template>
  <div class="composer-body">
    <textarea
      ref="textarea"
      v-model="text"
      class="composer-text"
      :disabled="disabled || running"
      placeholder="Describe the task for AETHER…  (Enter to run, Shift+Enter for a new line)"
      @keydown="onKeydown"
    ></textarea>
  </div>

  <div class="composer-foot">
    <div class="composer-selects">
      <!-- Model selector: provider+model dari konfigurasi AETHER (bukan hardcode). -->
      <label class="composer-select">
        <span class="cs-label">Model</span>
        <select class="input-a" :disabled="running" :value="currentModelValue" @change="onModelChange">
          <option v-if="!modelOptions.length" value="">No provider</option>
          <option
            v-for="opt in modelOptions"
            :key="opt.provider + ':' + opt.model"
            :value="opt.provider + '::' + (opt.model || '')"
          >
            {{ optionLabel(opt) }}
          </option>
        </select>
      </label>

      <!-- Mode selector: routing profile AETHER (Fast/Balanced/Deep). -->
      <label class="composer-select">
        <span class="cs-label">Mode</span>
        <select class="input-a" :disabled="running" :value="mode" @change="emit('update:mode', $event.target.value)">
          <option v-for="m in modes" :key="m" :value="m">{{ MODE_LABELS[m] || m }}</option>
        </select>
      </label>
    </div>

    <div class="composer-actions">
      <button v-if="running" class="btn-aether btn-ghost-a" type="button" @click="emit('stop')">
        Stop Task
      </button>
      <button
        v-else
        class="btn-aether btn-primary-a"
        type="button"
        :disabled="disabled || !text.trim()"
        @click="submit"
      >
        Run Task
      </button>
    </div>
  </div>
</template>
