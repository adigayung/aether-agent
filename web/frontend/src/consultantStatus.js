// AETHER Consultant — pemetaan status LIVE (helper MURNI, tanpa framework).
//
// Status Live Status Consultant (.consultant-thinking) HARUS mengikuti event
// runtime NYATA dari backend, bukan timer/rotasi teks/simulasi:
//
//     ConsultantService (tool_called / tool_completed)
//       -> GatewayService.emit_event            (SessionStore AETHER existing)
//       -> SSE GET /api/events?session_id=...   (EventSubscription + format_sse)
//       -> ConsultantChat.vue::openLiveStream() -> liveStatus
//
// Fungsi di berkas ini sengaja TANPA dependensi (bisa diuji dengan Node biasa)
// dan menjaga SATU invariant penting:
//
//     event yang TIDAK dipetakan (mis. observation_received, provider_response,
//     phase_changed) mengembalikan "" — artinya "tidak ada perubahan status".
//     Caller DILARANG menimpa status dengan string kosong, karena status yang
//     terakhir diketahui masih berlaku sampai event runtime berikutnya tiba.
//
// Tanpa invariant itu, event runtime apa pun yang kebetulan lewat setelah
// tool_completed akan MENGOSONGKAN status, sehingga baris "Analyzing results…"
// tidak pernah benar-benar terlihat (bug: status tampil sebagai fallback
// generik "Consultant is investigating…" lalu langsung hilang).

//: Event runtime Consultant yang bermakna untuk status (dipakai dokumentasi +
//: guard): hanya tool_called / tool_completed yang dipancarkan backend.
export const CONSULTANT_STATUS_EVENTS = ["tool_called", "tool_completed"];

//: Teks fallback saat BELUM ada event runtime (bukan status palsu — ini hanya
//: placeholder diam sampai event nyata pertama tiba).
export const CONSULTANT_STATUS_FALLBACK = "Consultant is investigating…";

//: Pemetaan tool -> status saat tool DIPANGGIL (tool_called).
export const CONSULTANT_TOOL_CALLED_STATUS = {
  read_file: "Reading source code…",
  search_code: "Searching source code…",
  list_files: "Inspecting project files…",
  atlas_query: "Checking project map…",
  rig_query: "Checking project map…",
  project_map_status: "Checking project map…",
  skill_catalog: "Checking available skills…",
  load_skill: "Loading skill…",
  load_skill_reference: "Loading skill reference…",
  consultant_bible: "Reading Project Bible…",
  bible_retrieval: "Reading Project Bible…",
  run_command: "Running command…",
  update_project_bible: "Updating Project Bible…",
};

//: Status saat tool SELESAI (tool_completed) — SATU baris untuk semua tool.
export const CONSULTANT_TOOL_COMPLETED_STATUS = "Analyzing results…";

//: Status bila tool tidak dikenal (tetap informatif, bukan simulasi).
export const CONSULTANT_TOOL_UNKNOWN_STATUS = "Working…";

/**
 * Normalisasi payload SSE menjadi bentuk internal.
 *
 * Format frame SSE AETHER (api/streaming.py::format_sse):
 *     { event_id, sequence, timestamp, session_id, task_id, event_type, payload }
 *
 * `event_type` boleh datang sebagai string ("tool_called") maupun enum
 * dengan `.value`. Payload event sendiri = objek `payload`.
 *
 * @param {any} payload payload hasil JSON.parse frame SSE.
 * @returns {{eventType: string, sessionId: string, taskId: string, body: object}}
 */
export function normalizeConsultantEvent(payload) {
  const raw = payload && typeof payload === "object" ? payload : {};
  let eventType = raw.event_type;
  if (eventType && typeof eventType === "object" && "value" in eventType) {
    eventType = eventType.value;
  }
  eventType = typeof eventType === "string" ? eventType : "";
  const body =
    raw.payload && typeof raw.payload === "object" ? raw.payload : {};
  return {
    eventType,
    sessionId: typeof raw.session_id === "string" ? raw.session_id : "",
    taskId: typeof raw.task_id === "string" ? raw.task_id : "",
    body,
  };
}

/**
 * Petakan SATU event runtime Consultant menjadi SATU baris status.
 *
 * @param {any} payload payload hasil JSON.parse frame SSE.
 * @returns {string} teks status; "" = TIDAK ADA event status yang cocok
 *   (caller harus MEMPERTAHANKAN status sebelumnya, bukan mengosongkannya).
 */
export function statusTextForEvent(payload) {
  const { eventType, body } = normalizeConsultantEvent(payload);
  if (eventType === "tool_called") {
    const tool = typeof body.tool === "string" ? body.tool : "";
    const mapped = CONSULTANT_TOOL_CALLED_STATUS[tool];
    if (mapped) return mapped;
    return tool ? CONSULTANT_TOOL_UNKNOWN_STATUS : "";
  }
  if (eventType === "tool_completed") {
    return CONSULTANT_TOOL_COMPLETED_STATUS;
  }
  return "";
}

/**
 * Terapkan satu event ke status sebelumnya (INTI fix "status tertimpa").
 *
 * Hanya event yang BERHASIL dipetakan yang mengganti status; selain itu
 * status sebelumnya dipertahankan tanpa perubahan.
 *
 * @param {string} current status yang sedang tampil.
 * @param {any} payload payload hasil JSON.parse frame SSE.
 * @returns {string} status berikutnya (=== current bila event tak relevan).
 */
export function applyConsultantEvent(current, payload) {
  const prev = typeof current === "string" ? current : "";
  const next = statusTextForEvent(payload);
  return next || prev;
}

/**
 * Apakah event ini milik session_id yang sedang dipantau?
 *
 * Event TANPA session_id (mis. frame dari stream global) TIDAK dipakai untuk
 * menimpa status sesi ini (defensif; server sudah memfilter lewat query param).
 *
 * @param {any} payload payload SSE.
 * @param {string} sid session_id yang dipantau.
 * @returns {boolean}
 */
export function eventBelongsToSession(payload, sid) {
  const { sessionId } = normalizeConsultantEvent(payload);
  return Boolean(sid) && sessionId === sid;
}
