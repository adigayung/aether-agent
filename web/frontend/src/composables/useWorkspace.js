import { ref } from "vue";
import {
  closeActiveProject,
  createProject,
  deleteProject,
  getActiveProject,
  getProjects,
  openInExplorer,
  setActiveProject,
} from "../api.js";

// Workspace / project state.
//
// Menangani daftar project (registry AETHER), active project (single-user local
// app, bukan login/session), Project Launcher, buka/tutup/hapus project, modal
// Policy per project, dan membuka Windows Explorer. SEMUA aksi memakai endpoint
// Gateway yang sudah ada; frontend tidak menyentuh filesystem.
export function useWorkspace({
  task,
  selectedProjectId,
  taskData,
  changes,
  activity,
  stream,
  setActiveNav,
  setError,
  clearNotice,
  showNotice,
}) {
  const { refreshTasks, refreshTaskHistory } = taskData;
  const { resetAllChanges } = changes;
  const { resetWorkspace } = activity;

  // Active Project (single-user local app; bukan login/session user).
  const activeProject = ref(null);
  const projects = ref([]);
  const launcherProjects = ref([]);
  const launcherBusy = ref(false);
  // Project/session terakhir (persisted active project) untuk ditawarkan
  // "buka kembali" di Project Launcher saat AETHER dibuka.
  const lastProject = ref(null);
  // Target konfirmasi hapus project (page Projects, registry-only).
  const projectToDelete = ref(null);
  // Project yang Project Settings / Policy-nya sedang dibuka (page Projects).
  // Policy melekat PER PROJECT (`.aether/permissions.json`) — bukan global.
  const policyProject = ref(null);
  // Konfirmasi Close Project (dialog sebelum benar-benar menutup project).
  const closeProjectConfirm = ref(false);

  async function refreshLauncherProjects() {
    try {
      const data = await getProjects();
      launcherProjects.value = data.projects || [];
      // Page Projects memakai daftar yang sama (satu sumber data: GET /projects).
      projects.value = launcherProjects.value;
    } catch {
      launcherProjects.value = [];
      projects.value = [];
    }
  }

  async function enterWorkbench() {
    setActiveNav("agent");
    try {
      const data = await getProjects();
      projects.value = data.projects || [];
    } catch {
      projects.value = [];
    }
    await refreshTasks();
    await refreshTaskHistory();
    stream.connectStream();
  }

  async function openProject(projectId) {
    launcherBusy.value = true;
    setError("");
    try {
      const data = await setActiveProject(projectId);
      activeProject.value = data.active_project || null;
      selectedProjectId.value = projectId;
      // Refresh task list dan history saat project berubah
      await refreshTasks();
      await refreshTaskHistory();
      await enterWorkbench();
    } catch (e) {
      setError(e.message || "Failed to open project.");
    } finally {
      launcherBusy.value = false;
    }
  }

  async function createNewProject({ name, path }) {
    launcherBusy.value = true;
    setError("");
    try {
      const record = await createProject(name, path);
      await refreshLauncherProjects();
      activeProject.value = record;
      selectedProjectId.value = record.id;
      await enterWorkbench();
      // Tampilkan policy project BARU. Nilainya berasal dari `.aether/permissions.json`
      // yang dibuat backend saat project dibuat (Default Project Permission Matrix)
      // — dibaca lewat endpoint policy existing, BUKAN konfigurasi kedua di frontend.
      openProjectPolicy(record);
    } catch (e) {
      setError(e.message || "Failed to create project.");
    } finally {
      launcherBusy.value = false;
    }
  }

  async function removeProject(projectId) {
    launcherBusy.value = true;
    setError("");
    try {
      await deleteProject(projectId);
      await refreshLauncherProjects();
    } catch (e) {
      setError(e.message || "Failed to delete project.");
    } finally {
      launcherBusy.value = false;
    }
  }

  // --- Projects page: Hapus Project (REGISTRY-ONLY) --------------------------
  // Hapus = hapus RECORD project dari daftar AETHER. TIDAK menghapus folder/file
  // project di disk (backend: DELETE /api/projects/<id>).
  function askProjectDelete(project) {
    if (!project || !project.id) return;
    setError("");
    clearNotice();
    projectToDelete.value = project;
  }

  function cancelProjectDelete() {
    projectToDelete.value = null;
  }

  async function confirmProjectDelete() {
    const project = projectToDelete.value;
    if (!project) return;
    launcherBusy.value = true;
    setError("");
    try {
      await deleteProject(project.id);
      projectToDelete.value = null;
      const wasActive = Boolean(activeProject.value && activeProject.value.id === project.id);
      if (selectedProjectId.value === project.id) selectedProjectId.value = "";
      if (wasActive) {
        // Project aktif dihapus: backend sudah membersihkan active state ->
        // kembalikan UI ke Project Launcher agar tetap konsisten.
        activeProject.value = null;
        if (lastProject.value && lastProject.value.id === project.id) lastProject.value = null;
        task.id = "";
        task.text = "";
        task.status = "idle";
        // Project aktif dihapus = tidak ada task aktif/pending di project ini ->
        // reset changes global.
        resetAllChanges();
      }
      await refreshLauncherProjects();
      showNotice(`"${project.name}" dihapus dari daftar AETHER. File/folder di disk TIDAK dihapus.`);
    } catch (e) {
      // Error: JANGAN hapus entri secara optimistik — entri tetap tampil.
      setError(e.message || "Failed to delete project.");
    } finally {
      launcherBusy.value = false;
    }
  }

  // --- Projects page: Project Settings / Policy (PROJECT-LOCAL) --------------
  // Policy disimpan per project di `<root>/.aether/permissions.json`. Hanya
  // project yang dipilih yang terpengaruh; project lain tidak berubah.
  function openProjectPolicy(project) {
    if (!project || !project.id) return;
    setError("");
    clearNotice();
    policyProject.value = project;
  }

  function closeProjectPolicy() {
    policyProject.value = null;
  }

  // Close Project: minta konfirmasi dulu sebelum benar-benar menutup project.
  function askCloseProject() {
    closeProjectConfirm.value = true;
  }

  function cancelCloseProject() {
    closeProjectConfirm.value = false;
  }

  async function confirmCloseProject() {
    closeProjectConfirm.value = false;
    await closeProject();
  }

  // Close Project: clear active project -> kembali ke Project Launcher.
  // TIDAK menghapus folder filesystem.
  async function closeProject() {
    const previous = activeProject.value;
    try {
      await closeActiveProject();
    } catch {
      // Tetap lanjut ke launcher walau request gagal.
    }
    activeProject.value = null;
    // Tampilkan project terakhir di launcher agar bisa dibuka kembali.
    lastProject.value = previous || null;
    selectedProjectId.value = "";
    task.id = "";
    task.text = "";
    task.status = "idle";
    // Project ditutup = tidak ada task aktif/pending di project ini -> reset changes.
    resetAllChanges();
    resetWorkspace();
    await refreshLauncherProjects();
  }

  // Buka Windows Explorer pada active project (path dari backend, bukan frontend).
  async function openExplorer() {
    try {
      await openInExplorer();
    } catch (e) {
      setError(e.message || "Gagal membuka Explorer.");
    }
  }

  // Muat project/session terakhir untuk ditawarkan "buka kembali" di launcher.
  // AETHER TIDAK auto-masuk Workbench: user harus menentukan workspace dulu.
  async function loadLastProject() {
    try {
      const data = await getActiveProject();
      lastProject.value = data.active_project || null;
    } catch {
      lastProject.value = null;
    }
  }

  return {
    activeProject,
    projects,
    launcherProjects,
    launcherBusy,
    lastProject,
    projectToDelete,
    policyProject,
    closeProjectConfirm,
    refreshLauncherProjects,
    enterWorkbench,
    openProject,
    createNewProject,
    removeProject,
    askProjectDelete,
    cancelProjectDelete,
    confirmProjectDelete,
    openProjectPolicy,
    closeProjectPolicy,
    askCloseProject,
    cancelCloseProject,
    confirmCloseProject,
    closeProject,
    openExplorer,
    loadLastProject,
  };
}
