import { ref } from "vue";
import { getConfig, getLLMProviders } from "../api.js";

// Task Composer + konfigurasi LLM untuk New Task.
//
// Modal composer (dibuka dari agent input) dan state yang dipakai New Task:
// Provider Instance + Model dari konfigurasi LLM TERSIMPAN (SQLite) — bukan
// settings/.env — plus execution mode (queue | parallel). Nilai provider/model
// TIDAK dihardcode di frontend.
export function useComposer({ submitTask }) {
  // Task Composer modal (dibuka dari agent input).
  const composerOpen = ref(false);

  // Konfigurasi AETHER (provider/model/mode) — TIDAK hardcode di frontend.
  const config = ref({});
  const selectedMode = ref("");
  // Provider Instance + Model dari konfigurasi LLM tersimpan (SQLite).
  const llmProviders = ref([]);
  const selectedProviderInstanceId = ref("");
  const selectedModelId = ref("");
  // Execution mode (Task 01): queue | parallel — hanya parameter task.
  const selectedExecutionMode = ref("queue");

  function openComposer() {
    composerOpen.value = true;
  }
  function closeComposer() {
    composerOpen.value = false;
  }

  // Muat konfigurasi AETHER + normalisasi mode legacy ('minimal' -> 'fast').
  async function loadConfig() {
    try {
      config.value = await getConfig();
      const rawMode = config.value.mode || "balanced";
      selectedMode.value = rawMode === "minimal" ? "fast" : rawMode;
    } catch {
      config.value = {};
    }
  }

  // Muat Provider Instance + Model dari konfigurasi LLM tersimpan (SQLite).
  // New Task memakai ini (bukan settings/.env). Default: instance enabled
  // pertama + model enabled pertama (bila belum ada pilihan).
  async function refreshLLMProviders() {
    try {
      const data = await getLLMProviders();
      llmProviders.value = data.providers || [];
    } catch {
      llmProviders.value = [];
    }
    const enabled = llmProviders.value.filter((p) => p.enabled !== false);
    const current = enabled.find((p) => p.id === selectedProviderInstanceId.value);
    if (!current) {
      const first = enabled[0] || null;
      selectedProviderInstanceId.value = first ? first.id : "";
      selectedModelId.value = "";
    }
    const inst = enabled.find((p) => p.id === selectedProviderInstanceId.value);
    const models = inst ? (inst.models || []).filter((m) => m.enabled !== false) : [];
    if (!models.some((m) => m.id === selectedModelId.value)) {
      selectedModelId.value = models[0] ? models[0].id : "";
    }
  }

  // Handler TaskComposer (Agent Input): payload { text, images } (bentuk baru)
  // ATAU string lama (backward compatible). Attachment gambar diteruskan sebagai
  // parameter ke-5 submitTask TANPA mengubah urutan override provider/model/mode
  // (yang dipakai card Task Proposal Consultant).
  async function onComposerSubmit(payload) {
    const text = typeof payload === "string" ? payload : (payload && payload.text) || "";
    if (!text) return null;
    const images =
      payload && typeof payload === "object" ? payload.images || null : null;
    return submitTask(text, null, null, null, images);
  }

  return {
    composerOpen,
    config,
    selectedMode,
    llmProviders,
    selectedProviderInstanceId,
    selectedModelId,
    selectedExecutionMode,
    openComposer,
    closeComposer,
    loadConfig,
    refreshLLMProviders,
    onComposerSubmit,
  };
}
