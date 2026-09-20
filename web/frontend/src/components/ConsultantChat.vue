<script setup>
// AETHER Consultant chat.
//
// Consultant = reasoning layer (Project Intelligence + Investigation +
// Validation + Recommendation + Task Generator) yang TERPISAH dari Agent.
// Frontend ini TIPIS: hanya memanggil endpoint Consultant di Gateway; seluruh
// reasoning/tool/boundary/bible dijalankan backend memakai substem AETHER yang
// sudah ada. Task Proposal yang dihasilkan dapat dikirim ke Agent lewat alur
// task existing (emit "run-task" -> App membuat task).
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { consult } from "../api.js";
import { renderMarkdown } from "../markdown.js";
import QueuePanel from "./QueuePanel.vue";

const props = defineProps({
  providers: { type: Array, default: () => [] },
  providerInstanceId: { type: String, default: "" },
  modelId: { type: String, default: "" },
  // Status task Agent yang sedang berjalan (dari App.vue, sumber tunggal).
  // Dipakai untuk men-disable "Run Task" + label "Running…" selama task
  // berjalan, agar tidak ada double-submit dari Task Proposal yang sama.
  running: { type: Boolean, default: false },
  // Penanda refresh panel TASKS (dinaikkan App.vue setelah Run Task / event
  // terminal task). Panel TASKS membaca SATU queue global yang sama.
  queueRefreshKey: { type: Number, default: 0 },
});

const emit = defineEmits([
  "close",
  "update:providerInstanceId",
  "update:modelId",
  "run-task",
  "stop-task",
  "view-task",
]);

const messages = ref([]);
const input = ref("");
const sending = ref(false);
const error = ref("");
const sessionId = ref("");
const scroller = ref(null);
const composer = ref(null);
const fileInput = ref(null);

// Gambar terlampir (belum dikirim): [{ name, mimeType, dataUrl, base64 }].
// Dimaksimalkan 8 gambar agar konsisten dengan batas backend.
const MAX_ATTACHMENTS = 8;
const attachments = ref([]);

// Tinggi maksimum composer (px). Di atas nilai ini textarea scroll internal
// agar footer tidak memanjang tanpa batas.
const COMPOSER_MAX_HEIGHT = 140;

// Auto-grow: reset ke auto dulu lalu set tinggi = konten sebenarnya,
// dibatasi COMPOSER_MAX_HEIGHT.
function autoGrow() {
  const el = composer.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, COMPOSER_MAX_HEIGHT) + "px";
  el.style.overflowY = el.scrollHeight > COMPOSER_MAX_HEIGHT ? "auto" : "hidden";
}
function resetComposer() {
  const el = composer.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.overflowY = "hidden";
}

// Mode Consultant: "quick" (default) atau "investigate".
// - quick       : Consultant memakai Project Bible + percakapan saja
//                 (backend TIDAK menyediakan tool investigasi project).
// - investigate : Project Bible sebagai konteks awal, lalu boleh memakai tool
//                 project yang tersedia bila perlu verifikasi/investigasi.
// Mengganti mode TIDAK mereset sesi/konteks Consultant.
const mode = ref("quick");
const MODES = [
  { id: "quick", label: "Quick", icon: "⚡" },
  { id: "investigate", label: "Investigate", icon: "🔍" },
];
function setMode(id) {
  if (id === "quick" || id === "investigate") mode.value = id;
}

const inputPlaceholder = computed(() =>
  mode.value === "quick"
    ? "Ask the Consultant (Quick · Project Bible only)…"
    : "Ask the Consultant (Investigate · may inspect project)…"
);

const providerOptions = computed(() =>
  (props.providers || []).filter((p) => p.enabled !== false)
);
const modelOptions = computed(() => {
  const inst = providerOptions.value.find((p) => p.id === props.providerInstanceId);
  if (!inst) return [];
  return (inst.models || []).filter((m) => m.enabled !== false);
});

function providerLabel(p) {
  const type = p.provider_label || p.provider_type || "";
  return type ? `${p.name} (${type})` : p.name;
}
function onProviderChange(e) {
  emit("update:providerInstanceId", String(e.target.value || ""));
  emit("update:modelId", "");
}
function onModelChange(e) {
  emit("update:modelId", String(e.target.value || ""));
}

function scrollToBottom() {
  nextTick(() => {
    const el = scroller.value;
    if (el) el.scrollTop = el.scrollHeight;
  });
}

watch(messages, scrollToBottom, { deep: true });

function summarizeTools(events) {
  if (!events || !events.length) return [];
  const done = events.filter((e) => e.success !== null && e.success !== undefined);
  return done.map((e) => ({
    tool: e.tool || "",
    target: e.target || "",
    success: e.success !== false,
  }));
}

// --- Attach image (multimodal) ---------------------------------------------
const ACCEPTED_TYPES = ["image/jpeg", "image/png", "image/webp"];

function triggerAttach() {
  if (sending.value) return;
  const el = fileInput.value;
  if (el) el.click();
}

function onFilesPicked(e) {
  const files = Array.from(e.target.files || []);
  e.target.value = "";
  for (const file of files) {
    if (attachments.value.length >= MAX_ATTACHMENTS) {
      error.value = `Maksimum ${MAX_ATTACHMENTS} gambar per pesan.`;
      break;
    }
    if (!ACCEPTED_TYPES.includes(file.type)) {
      error.value = `Format tidak didukung: ${file.type || "unknown"} (pakai JPEG/PNG/WebP).`;
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
      error.value = `Gagal membaca gambar: ${file.name}`;
    };
    reader.readAsDataURL(file);
  }
}

function removeAttachment(index) {
  attachments.value.splice(index, 1);
}

async function send() {
  const text = input.value.trim();
  const pending = attachments.value.slice();
  if ((!text && !pending.length) || sending.value) return;
  error.value = "";
  messages.value.push({
    role: "user",
    text,
    images: pending.map((a) => a.dataUrl),
  });
  input.value = "";
  attachments.value = [];
  resetComposer();
  sending.value = true;
  scrollToBottom();
  try {
    const data = await consult(text, {
      sessionId: sessionId.value || null,
      providerInstanceId: props.providerInstanceId || null,
      modelId: props.modelId || null,
      mode: mode.value,
      images: pending.length
        ? pending.map((a) => ({
            data: a.base64,
            mime_type: a.mimeType,
            filename: a.name,
          }))
        : null,
    });
    sessionId.value = data.session_id || sessionId.value;
    messages.value.push({
      role: "assistant",
      text: data.reply || "(no reply)",
      tools: summarizeTools(data.tool_events),
      taskProposal: data.task_proposal || null,
      failed: data.status === "failed",
    });
  } catch (e) {
    error.value = e.message || "Consultant request failed.";
    messages.value.push({
      role: "assistant",
      text: `Consultant error: ${error.value}`,
      tools: [],
      taskProposal: null,
      failed: true,
    });
  } finally {
    sending.value = false;
    scrollToBottom();
  }
}

function onKeydown(e) {
  // Shift+Enter = kirim. Enter biasa = baris baru (default textarea).
  if (e.key === "Enter" && e.shiftKey) {
    e.preventDefault();
    send();
  }
}

// Task Proposal terakhir (dari pesan assistant terakhir yang memilikinya).
const lastProposal = computed(() => {
  for (let i = messages.value.length - 1; i >= 0; i -= 1) {
    if (messages.value[i].taskProposal) return messages.value[i].taskProposal;
  }
  return null;
});

function runTask(proposal) {
  const target = proposal || lastProposal.value;
  if (!target || props.running) return;
  emit("run-task", target);
}

function startNewSession() {
  sessionId.value = "";
  messages.value = [];
  attachments.value = [];
  error.value = "";
}

onMounted(() => {
  messages.value.push({
    role: "assistant",
    text:
      "Halo! Saya **AETHER Consultant**. Saya bisa menganalisa project, " +
      "melakukan investigasi, memvalidasi temuan, dan menyusun Task Proposal " +
      "untuk Agent. Pilih mode **⚡ Quick** (Project Bible saja, cepat) atau " +
      "**🔍 Investigate** (boleh memeriksa project). Apa yang ingin Anda " +
      "ketahui atau kerjakan?",
    tools: [],
    taskProposal: null,
  });
  scrollToBottom();
});
</script>

<template>
  <div class="modal-backdrop" @click.self="$emit('close')">
    <div class="modal consultant-m" role="dialog" aria-modal="true">
      <div class="consultant-head">
        <div class="consultant-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2a4 4 0 0 1 4 4c0 1.95-1.4 3.58-3.25 3.93L12 22l-.75-12.07A4.001 4.001 0 0 1 12 2z"/><circle cx="12" cy="6" r="1.5" fill="currentColor" stroke="none"/><path d="M9 14l-3 3 3 3M15 14l3 3-3 3"/></svg>
          AETHER Consultant
        </div>
        <div class="consultant-selects">
          <label class="consultant-select">
            <span class="cs-label">Provider</span>
            <select class="input-a" :value="providerInstanceId" @change="onProviderChange">
              <option v-if="!providerOptions.length" value="">No provider instance</option>
              <option v-for="p in providerOptions" :key="p.id" :value="p.id">
                {{ providerLabel(p) }}
              </option>
            </select>
          </label>
          <label class="consultant-select">
            <span class="cs-label">Model</span>
            <select class="input-a" :value="modelId" @change="onModelChange">
              <option v-if="!modelOptions.length" value="">No model</option>
              <option v-for="m in modelOptions" :key="m.id" :value="m.id">
                {{ m.model_name }}
              </option>
            </select>
          </label>
        </div>
        <button class="consultant-new" type="button" title="New session" @click="startNewSession">
          New
        </button>
        <button class="close-x" type="button" title="Close" @click="$emit('close')">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
        </button>
      </div>

      <!-- Mode Consultant: Quick (Project Bible saja) vs Investigate (boleh
           inspeksi project). Mengganti mode TIDAK mereset sesi/konteks. -->
      <div class="consultant-modes" role="group" aria-label="Consultant mode">
        <span class="cm-label">Mode</span>
        <button
          v-for="m in MODES"
          :key="m.id"
          type="button"
          class="mode-btn"
          :class="{ active: mode === m.id }"
          :aria-pressed="mode === m.id ? 'true' : 'false'"
          :title="m.id === 'quick' ? 'Quick: Project Bible + conversation only' : 'Investigate: may inspect the project when needed'"
          :disabled="sending"
          @click="setMode(m.id)"
        >
          <span class="mb-icon" aria-hidden="true">{{ m.icon }}</span>{{ m.label }}
        </button>
        <span class="cm-hint">
          {{ mode === "quick" ? "Bible + chat only" : "Bible first, then project tools" }}
        </span>
      </div>

      <div class="consultant-body">
        <div class="consultant-chat">
          <div class="consultant-messages" ref="scroller">
        <div v-for="(msg, i) in messages" :key="i" class="cmsg" :class="msg.role">
          <span class="crole">{{ msg.role === "assistant" ? "AETHER Consultant" : "You" }}</span>
          <div v-if="msg.role === 'user' && msg.images && msg.images.length" class="cmsg-images">
            <img
              v-for="(src, ii) in msg.images"
              :key="ii"
              :src="src"
              class="cmsg-thumb"
              alt="attachment"
            />
          </div>
          <div v-if="msg.role === 'user'" class="ctext">{{ msg.text }}</div>
          <!-- eslint-disable-next-line vue/no-v-html -->
          <div v-else class="consultant-md md" :class="{ failed: msg.failed }" v-html="renderMarkdown(msg.text)"></div>
          <div v-if="msg.role === 'assistant' && msg.tools && msg.tools.length" class="consultant-tools">
            <span v-for="(t, ti) in msg.tools" :key="ti" class="consultant-tool" :class="t.success ? 'ok' : 'err'">
              {{ t.success ? "✓" : "✗" }} {{ t.tool }}<template v-if="t.target"> {{ t.target }}</template>
            </span>
          </div>
          <!-- Task Proposal mengalir sebagai bagian dari message flow: ia
               dirender inline di dalam pesan yang menghasilkannya (bukan selalu
               di akhir container), sehingga balasan Consultant berikutnya
               muncul DI BAWAH card dan card ikut naik seperti bubble lain.
               Card tetap berada di dalam area percakapan yang scrollable. -->
          <div v-if="msg.role === 'assistant' && msg.taskProposal" class="consultant-proposal">
            <div class="cp-head">
              <span class="cp-title">Task Proposal</span>
              <button class="run-task-btn" type="button" :disabled="running" @click="runTask(msg.taskProposal)">
                <svg v-if="!running" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 5l7 7-7 7"/></svg>
                {{ running ? "Running…" : "Run Task" }}
              </button>
            </div>
            <pre class="cp-body">{{ msg.taskProposal }}</pre>
          </div>
        </div>

        <div v-if="sending" class="consultant-thinking">Consultant is investigating…</div>
      </div>

      <div v-if="error" class="wb-error">{{ error }}</div>

      <div class="consultant-foot">
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
        <div class="attach-row">
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
            :disabled="sending"
            @click="triggerAttach"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
          </button>
          <textarea
            ref="composer"
            v-model="input"
            class="input-a"
            rows="1"
            :placeholder="inputPlaceholder"
            :disabled="sending"
            @input="autoGrow"
            @keydown="onKeydown"
          ></textarea>
          <button class="send-btn" type="button" title="Send" :disabled="sending || (!input.trim() && !attachments.length)" @click="send">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 5l7 7-7 7"/></svg>
          </button>
        </div>
      </div>
        </div><!-- /consultant-chat -->

        <!-- TASKS panel: SATU queue global AETHER, ditampilkan di sebelah
             chat Consultant. Bukan queue subsystem kedua. -->
        <QueuePanel
          class="consultant-queue"
          :refresh-key="queueRefreshKey"
          @stop-task="$emit('stop-task', $event)"
          @view-task="$emit('view-task', $event)"
        />
      </div><!-- /consultant-body -->
    </div>
  </div>
</template>
