import { ref } from "vue";

// Consultant (modal). Reasoning, Project Bible, tool boundary, dan session
// context dijalankan backend (endpoint Consultant). Modul ini HANYA membuka
// modal + mengelola session aktif (persist di localStorage) dan meneruskan Task
// Proposal ke alur task Agent yang sudah ada (submitTask).
export function useConsultant({ submitTask, bumpQueueRefresh }) {
  const consultantOpen = ref(false);
  // Persist active consultant session ID across reloads (localStorage).
  const activeConsultantSessionId = ref("");
  try {
    const stored = window.localStorage.getItem("aether-active-consultant-session");
    if (stored) activeConsultantSessionId.value = stored;
  } catch (_) {/* ignore */}

  // Task Proposal dari Consultant -> task Agent (alur task existing submitTask).
  // Modal Consultant SENGAJA tetap terbuka agar user dapat terus melihat
  // percakapan/aktivitas Consultant setelah task dikirim ke Agent.
  // `submittedTaskId` = task yang baru dibuat (dipakai ConsultantChat untuk
  // men-disable tombol Run Task milik Task Proposal itu sampai task terminal).
  const submittedTaskId = ref("");

  function persistSession() {
    try {
      window.localStorage.setItem(
        "aether-active-consultant-session",
        activeConsultantSessionId.value
      );
    } catch (_) {/* ignore */}
  }

  function openConsultant() {
    consultantOpen.value = true;
  }

  function closeConsultant() {
    // Persist active session when closing modal.
    persistSession();
    consultantOpen.value = false;
  }

  function onConsultantSessionChange(id) {
    activeConsultantSessionId.value = id || "";
    persistSession();
  }

  // Payload bisa object { text, providerInstanceId, modelId, executionMode } (bentuk
  // baru dari card Task Proposal) ATAU string lama (backward compatible).
  // Provider/model dari card proposal (runner) dipakai untuk task ini; pilihan
  // header (chat) TIDAK diubah. executionMode (queue/parallel) juga diteruskan.
  async function runConsultantTask(payload) {
    const text = typeof payload === "string" ? payload : payload && payload.text;
    if (!text) return;
    const overrideProviderId =
      payload && typeof payload === "object" ? payload.providerInstanceId : null;
    const overrideModelId =
      payload && typeof payload === "object" ? payload.modelId : null;
    const overrideExecutionMode =
      payload && typeof payload === "object" ? payload.executionMode : null;
    const record = await submitTask(text, overrideProviderId, overrideModelId, overrideExecutionMode);
    // Beri tahu ConsultantChat task mana milik tombol Run Task. WAJIB memakai
    // task_id yang BARU dibuat (dari respons createTask), BUKAN task.id: task.id
    // adalah task yang sedang DIPANTAU, yang bisa jadi Task A lain yang masih
    // running saat Task B hanya masuk antrian (pending).
    submittedTaskId.value = (record && record.task_id) || "";
    bumpQueueRefresh();
  }

  return {
    consultantOpen,
    activeConsultantSessionId,
    submittedTaskId,
    openConsultant,
    closeConsultant,
    onConsultantSessionChange,
    runConsultantTask,
  };
}
