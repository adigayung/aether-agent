import { ref } from "vue";

// Consultant (modal). Reasoning, Project Bible, tool boundary, dan session
// context dijalankan backend (endpoint Consultant). Modul ini HANYA membuka
// modal + mengelola session aktif (persist di localStorage) dan meneruskan Task
// Proposal ke alur task Agent yang sudah ada (submitTask).
//
// ISOLASI PER PROJECT: sesi Consultant backend ter-scope per project. Di UI,
// sesi aktif disimpan PER PROJECT (map {projectId: sessionId}), sehingga
// berpindah project TIDAK me-resume sesi milik project lain (tidak ada
// kebocoran lintas-project di localStorage).
export function useConsultant({ submitTask, bumpQueueRefresh }) {
  const consultantOpen = ref(false);
  // Active consultant session PER PROJECT (localStorage map).
  const activeSessionsByProject = ref({});
  const activeConsultantSessionId = ref("");
  try {
    const stored = window.localStorage.getItem("aether-active-consultant-sessions");
    if (stored) {
      const parsed = JSON.parse(stored);
      if (parsed && typeof parsed === "object") activeSessionsByProject.value = parsed;
    }
  } catch (_) {/* ignore */}

  // Task Proposal dari Consultant -> task Agent (alur task existing submitTask).
  // Modal Consultant SENGAJA tetap terbuka agar user dapat terus melihat
  // percakapan/aktivitas Consultant setelah task dikirim ke Agent.
  // `submittedTaskId` = task yang baru dibuat (dipakai ConsultantChat untuk
  // men-disable tombol Run Task milik Task Proposal itu sampai task terminal).
  const submittedTaskId = ref("");

  function _projectKey(projectId) {
    return projectId || "";
  }

  // Project key aktif saat ini (di-set lewat setActiveProject). Menentukan slot
  // penyimpanan sesi aktif per project.
  const _currentProjectKey = ref(null);

  function persistSessions() {
    try {
      window.localStorage.setItem(
        "aether-active-consultant-sessions",
        JSON.stringify(activeSessionsByProject.value || {})
      );
    } catch (_) {/* ignore */}
  }

  // Dipanggil saat project aktif berubah: muat sesi aktif MILIK project itu
  // (atau kosongkan bila belum ada). Tidak pernah membawa sesi project lain.
  function setActiveProject(projectId) {
    const key = _projectKey(projectId);
    activeConsultantSessionId.value = activeSessionsByProject.value[key] || "";
  }

  function openConsultant() {
    consultantOpen.value = true;
  }

  function closeConsultant() {
    // Persist active session map when closing modal.
    persistSessions();
    consultantOpen.value = false;
  }

  function onConsultantSessionChange(id) {
    // Simpan id sesi pada slot project yang sedang aktif. Pemanggil (App.vue)
    // hanya meneruskan id; slot project diambil dari sesi map yang sudah di-set
    // lewat setActiveProject.
    const key = _currentProjectKey.value;
    activeConsultantSessionId.value = id || "";
    if (key === null || key === undefined) return;
    if (id) {
      activeSessionsByProject.value = {
        ...activeSessionsByProject.value,
        [key]: id,
      };
    } else {
      const next = { ...activeSessionsByProject.value };
      delete next[key];
      activeSessionsByProject.value = next;
    }
    persistSessions();
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

  function _setProjectTracking(projectId) {
    _currentProjectKey.value = _projectKey(projectId);
    setActiveProject(projectId);
  }

  return {
    consultantOpen,
    activeConsultantSessionId,
    submittedTaskId,
    openConsultant,
    closeConsultant,
    onConsultantSessionChange,
    runConsultantTask,
    setActiveProject: _setProjectTracking,
  };
}
