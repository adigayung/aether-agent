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

const props = defineProps({
  providers: { type: Array, default: () => [] },
  providerInstanceId: { type: String, default: "" },
  modelId: { type: String, default: "" },
});

const emit = defineEmits([
  "close",
  "update:providerInstanceId",
  "update:modelId",
  "run-task",
]);

const messages = ref([]);
const input = ref("");
const sending = ref(false);
const error = ref("");
const sessionId = ref("");
const scroller = ref(null);

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

async function send() {
  const text = input.value.trim();
  if (!text || sending.value) return;
  error.value = "";
  messages.value.push({ role: "user", text });
  input.value = "";
  sending.value = true;
  scrollToBottom();
  try {
    const data = await consult(text, {
      sessionId: sessionId.value || null,
      providerInstanceId: props.providerInstanceId || null,
      modelId: props.modelId || null,
      mode: mode.value,
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
  if (e.key === "Enter" && !e.shiftKey) {
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

function runTask() {
  if (!lastProposal.value) return;
  emit("run-task", lastProposal.value);
}

function startNewSession() {
  sessionId.value = "";
  messages.value = [];
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

      <div class="consultant-messages" ref="scroller">
        <div v-for="(msg, i) in messages" :key="i" class="cmsg" :class="msg.role">
          <span class="crole">{{ msg.role === "assistant" ? "AETHER Consultant" : "You" }}</span>
          <div v-if="msg.role === 'user'" class="ctext">{{ msg.text }}</div>
          <!-- eslint-disable-next-line vue/no-v-html -->
          <div v-else class="consultant-md md" :class="{ failed: msg.failed }" v-html="renderMarkdown(msg.text)"></div>
          <div v-if="msg.role === 'assistant' && msg.tools && msg.tools.length" class="consultant-tools">
            <span v-for="(t, ti) in msg.tools" :key="ti" class="consultant-tool" :class="t.success ? 'ok' : 'err'">
              {{ t.success ? "✓" : "✗" }} {{ t.tool }}<template v-if="t.target"> {{ t.target }}</template>
            </span>
          </div>
        </div>

        <div v-if="sending" class="consultant-thinking">Consultant is investigating…</div>

        <!-- Task Proposal adalah bagian dari message flow: ia hidup di dalam
             area percakapan yang scrollable, bukan panel floating di atas
             composer. Karena itu isinya bisa ikut ter-scroll sampai selesai. -->
        <div v-if="lastProposal" class="consultant-proposal">
          <div class="cp-head">
            <span class="cp-title">Task Proposal</span>
            <button class="run-task-btn" type="button" @click="runTask">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 5l7 7-7 7"/></svg>
              Run Task
            </button>
          </div>
          <pre class="cp-body">{{ lastProposal }}</pre>
        </div>
      </div>

      <div v-if="error" class="wb-error">{{ error }}</div>

      <div class="consultant-foot">
        <input
          v-model="input"
          class="input-a"
          type="text"
          :placeholder="inputPlaceholder"
          :disabled="sending"
          @keydown="onKeydown"
        />
        <button class="send-btn" type="button" title="Send" :disabled="sending || !input.trim()" @click="send">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 5l7 7-7 7"/></svg>
        </button>
      </div>
    </div>
  </div>
</template>
