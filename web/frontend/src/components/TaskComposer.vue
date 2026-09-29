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
  // Execution mode (Task 01): queue | parallel — hanya parameter task.
  executionMode: { type: String, default: "queue" },
});
const emit = defineEmits([
  "submit",
  "stop",
  "update:providerInstanceId",
  "update:modelId",
  "update:mode",
  "update:executionMode",
]);

const text = ref("");
const textarea = ref(null);
const fileInput = ref(null);

// Attachment gambar (multimodal). Mekanisme IDENTIK dengan Consultant Chat
// (batas & format sama): maks 8 gambar, JPEG/PNG/WebP.
// { name, mimeType, dataUrl, base64 } — dikirim sebagai `images` ke backend.
const MAX_ATTACHMENTS = 8;
const ACCEPTED_TYPES = ["image/jpeg", "image/png", "image/webp"];
const attachments = ref([]);
const attachError = ref("");

function triggerAttach() {
  if (props.disabled) return;
  const el = fileInput.value;
  if (el) el.click();
}

function onFilesPicked(e) {
  const files = Array.from(e.target.files || []);
  e.target.value = "";
  for (const file of files) {
    if (attachments.value.length >= MAX_ATTACHMENTS) {
      attachError.value = `Maksimum ${MAX_ATTACHMENTS} gambar per task.`;
      break;
    }
    if (!ACCEPTED_TYPES.includes(file.type)) {
      attachError.value = `Format tidak didukung: ${file.type || "unknown"} (pakai JPEG/PNG/WebP).`;
      continue;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result || "");
      const comma = dataUrl.indexOf(",");
      const base64 = comma >= 0 ? dataUrl.slice(comma + 1) : "";
      if (!base64) return;
      attachments.value.push({
        name: file.name || "image",
        mimeType: file.type,
        dataUrl,
        base64,
      });
    };
    reader.onerror = () => {
      attachError.value = `Gagal membaca gambar: ${file.name}`;
    };
    reader.readAsDataURL(file);
  }
}

function removeAttachment(index) {
  attachments.value.splice(index, 1);
}

// Label mode ramah-user dari retrieval profile AETHER (#40).
const MODE_LABELS = { minimal: "Fast", balanced: "Balanced", deep: "Deep" };
const modes = computed(() => props.config.modes || ["minimal", "balanced", "deep"]);

// Provider Instance dari konfigurasi LLM tersimpan (SQLite). Hanya instance
// enabled yang ditampilkan (instance disabled tidak bisa dipakai task).
const providerOptions = computed(() =>
  (props.providers || []).filter((p) => p.enabled !== false)
);

// Apakah provider yang dipilih membutuhkan model?
const providerNeedsModel = computed(() => {
  const inst = providerOptions.value.find((p) => p.id === props.providerInstanceId);
  if (!inst) return true; // default: butuh model
  return inst.requires_model !== false;
});

// Model difilter: HANYA model milik Provider Instance yang dipilih.
// Model SELALU ditampilkan bila instance memilikinya — termasuk provider yang
// model-nya OPSIONAL (mis. "custom" untuk routing "auto" atau 9Router), sehingga
// user tetap bisa memilih. Bila dikosongkan, AETHER memakai model enabled
// pertama (atau server menentukan sendiri untuk provider routing).
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
  // Attachment gambar -> format yang dipahami backend (sama dengan Consultant).
  const pending = attachments.value.slice();
  const payload = {
    text: value,
    images: pending.length
      ? pending.map((a) => ({
          data: a.base64,
          mime_type: a.mimeType,
          filename: a.name,
        }))
      : null,
  };
  attachError.value = "";
  emit("submit", payload);
  text.value = "";
  attachments.value = [];
}
</script>

<template>
  <div class="composer-body">
    <!-- Preview gambar terlampir (belum dikirim). -->
    <div v-if="attachments.length" class="attach-strip">
      <div v-for="(a, ai) in attachments" :key="ai" class="attach-item">
        <img :src="a.dataUrl" class="attach-thumb" :alt="a.name" />
        <button
          type="button"
          class="attach-remove"
          title="Remove image"
          @click="removeAttachment(ai)"
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
        </button>
      </div>
    </div>
    <textarea
      ref="textarea"
      v-model="text"
      class="composer-text"
      :disabled="disabled"
      placeholder="Describe the task for AETHER…  (Enter for a new line)"
    ></textarea>
    <div v-if="attachError" class="composer-attach-hint">{{ attachError }}</div>
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
      <label v-if="modelOptions.length > 0 || providerNeedsModel" class="composer-select">
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

      <!-- Execution mode: hanya parameter task (queue/parallel), belum parallel execution. -->
      <label class="composer-select">
        <span class="cs-label">Execution</span>
        <select
          class="input-a"
          :disabled="disabled"
          :value="executionMode"
          @change="emit('update:executionMode', $event.target.value)"
        >
          <option value="queue">Queue</option>
          <option value="parallel">Parallel</option>
        </select>
      </label>
    </div>

    <div class="composer-actions">
      <input
        ref="fileInput"
        type="file"
        accept="image/jpeg,image/png,image/webp"
        multiple
        class="attach-input"
        @change="onFilesPicked"
      />
      <button
        type="button"
        class="attach-btn"
        title="Attach image (JPEG/PNG/WebP)"
        :disabled="disabled"
        @click="triggerAttach"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
      </button>
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
