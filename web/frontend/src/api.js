// AETHER Gateway API client (#52).
//
// Frontend TIPIS: hanya memanggil HTTP/SSE Django Gateway (#50/#51).
// TIDAK ada logic agent (runtime/loop/planning/tools/validation/recovery) di sini.
// TIDAK ada event system kedua: event SSE berasal dari #51 apa adanya.

const BASE = "/api";

async function request(path, options = {}) {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { raw: text };
  }
  if (!resp.ok) {
    const message = data?.error?.message || `HTTP ${resp.status}`;
    const err = new Error(message);
    err.status = resp.status;
    err.code = data?.error?.code;
    throw err;
  }
  return data;
}

// --- #50 endpoints ---------------------------------------------------------
export function getHealth() {
  return request("/health");
}

// Konfigurasi provider/model/mode dari AETHER (TIDAK hardcode di frontend).
export function getConfig() {
  return request("/config");
}

// --- LLM Config / Settings -------------------------------------------------
// Halaman Settings HANYA memanggil endpoint konfigurasi LLM backend (yang
// memakai LLMConfigService AETHER existing). Nilai secret TIDAK pernah
// dikembalikan oleh backend (hanya versi masked).
export function getLLMConfig() {
  return request("/llm/config");
}

export function createLLMCredential(name, value) {
  return request("/llm/credentials", {
    method: "POST",
    body: JSON.stringify({ name, value }),
  });
}

export function deleteLLMCredential(name, force = false) {
  return request("/llm/credentials/delete", {
    method: "POST",
    body: JSON.stringify({ name, force }),
  });
}

export function createLLMProvider(payload) {
  return request("/llm/providers", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateLLMProvider(providerId, payload) {
  return request(`/llm/providers/${encodeURIComponent(providerId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function deleteLLMProvider(providerId) {
  return request(`/llm/providers/${encodeURIComponent(providerId)}`, {
    method: "DELETE",
  });
}

export function createLLMModel(payload) {
  return request("/llm/models", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateLLMModel(modelId, payload) {
  return request(`/llm/models/${encodeURIComponent(modelId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function deleteLLMModel(modelId) {
  return request(`/llm/models/${encodeURIComponent(modelId)}`, {
    method: "DELETE",
  });
}

export function getProjects() {
  return request("/projects");
}

// File Explorer: daftar file project aktif (read-only, via ListFilesTool AETHER).
export function listFiles(path = ".") {
  return request(`/files?path=${encodeURIComponent(path)}`);
}

// Buka Windows Explorer pada ACTIVE PROJECT (path dari backend, bukan frontend).
export function openInExplorer() {
  return request("/open-in-explorer", { method: "POST" });
}

// --- Project Launcher / Active Project -------------------------------------
// Single-user local app: tidak ada login/session user, hanya active project.
export function createProject(name, path) {
  return request("/projects", {
    method: "POST",
    body: JSON.stringify({ name, path }),
  });
}

export function deleteProject(projectId) {
  return request(`/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
}

export function getActiveProject() {
  return request("/active-project");
}

export function setActiveProject(projectId) {
  return request("/active-project", {
    method: "POST",
    body: JSON.stringify({ project_id: projectId }),
  });
}

export function closeActiveProject() {
  return request("/active-project", { method: "DELETE" });
}

export function createTask(task, projectId = null, metadata = null) {
  const body = { task };
  if (projectId) body.project_id = projectId;
  if (metadata) body.metadata = metadata;
  return request("/tasks", { method: "POST", body: JSON.stringify(body) });
}

export function getTask(taskId) {
  return request(`/tasks/${encodeURIComponent(taskId)}`);
}

// Minta penghentian task (menandai CANCELLED + emit event AETHER existing).
export function cancelTask(taskId) {
  return request(`/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
}

// GET /api/tasks -> daftar task (history). Backend in-memory (#53).
export function listTasks() {
  return request("/tasks");
}

// --- #51 SSE ---------------------------------------------------------------
// Membuka EventSource ke /api/events (opsional filter session_id/task_id).
// Mengembalikan EventSource agar pemanggil dapat menutupnya (disconnect).
export function openEventStream({ sessionId = null, taskId = null, onEvent } = {}) {
  const params = new URLSearchParams();
  if (sessionId) params.set("session_id", sessionId);
  if (taskId) params.set("task_id", taskId);
  const qs = params.toString();
  const url = `${BASE}/events${qs ? `?${qs}` : ""}`;

  const source = new EventSource(url);
  // Event AETHER dikirim dengan `event: <event_type>`. Kita dengarkan tipe
  // yang dikenal (#51) tanpa mengasumsikan semuanya selalu ada.
  const KNOWN_EVENTS = [
    "task_created",
    "task_started",
    "phase_changed",
    "agent_commentary",
    "tool_called",
    "tool_completed",
    "observation_received",
    "provider_request",
    "provider_response",
    "validation_started",
    "validation_completed",
    "recovery_started",
    "recovery_completed",
    "change_detected",
    "task_completed",
    "task_failed",
    "task_cancelled",
  ];

  const handle = (evt) => {
    let payload = null;
    try {
      payload = JSON.parse(evt.data);
    } catch {
      payload = { raw: evt.data };
    }
    if (onEvent) onEvent(payload);
  };

  KNOWN_EVENTS.forEach((name) => source.addEventListener(name, handle));
  // Fallback: event tanpa tipe eksplisit.
  source.onmessage = handle;

  return source;
}
