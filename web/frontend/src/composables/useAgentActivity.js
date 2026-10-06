import { computed, reactive, ref } from "vue";
import { eventTimeMs } from "../timeUtils.js";
import {
  VALIDATING_STEP,
  activityPhaseIndex,
  addMilestone,
} from "../lifecycle.js";
import { shouldFollowStartedTask } from "../taskView.js";
import { playStatusSound } from "../audioRegistry.js";
import { buildAgentActivityCopy } from "../activityCopy.js";

// Agent Activity + event state (#51).
//
// Bertanggung jawab atas: feed activity (live SSE atau history persistent log),
// deduplikasi event dari dua stream (global queue + task-scoped), runtime/
// validation/lifecycle activity phase milik task AKTIF, indikator reasoning,
// live filesystem change, dan kopi snapshot activity. Handler `handleEvent`
// adalah satu-satunya tempat event SSE #51 diubah menjadi state UI.
//
// Dependency di-inject dari composition root (tidak ada import composable lain
// -> tidak ada ketergantungan melingkar antar modul):
//   task                = task yang SEDANG dipantau (reactive).
//   timing              = { taskStartedAt, taskEndedAt } (ref).
//   changes             = { upsertChange, isAetherMetadata }.
//   queue               = { runningTaskId, releaseRunningTask, adoptRunningTask,
//                           isViewingRunningTask, deferredTaskIds, terminalTaskId }.
//   approvals           = { handleApprovalEvent, dropApprovalsForTask }.
//   bumpQueueRefresh    = naikkan penanda refresh QueuePanel.
//   refreshTaskHistory  = muat ulang daftar History (setelah task terminal).
//   clearReport         = kosongkan report task sebelumnya saat workspace reset.
//   getCopyMeta         = metadata Agent Card untuk tombol Copy Activity.
export function useAgentActivity({
  task,
  timing,
  changes,
  queue,
  approvals,
  bumpQueueRefresh,
  refreshTaskHistory,
  clearReport,
  getCopyMeta,
}) {
  const { taskStartedAt, taskEndedAt } = timing;
  // Kontrak DI dengan useChanges(): SEMUA helper di bawah WAJIB ada di objek
  // `changes`. Pelanggaran kontrak ini pernah membuat `case "change_detected"`
  // melempar TypeError (`isAetherMetadata is not a function`) sehingga event
  // perubahan dibuang dan panel Changes tetap 0 files. Kontrak dikunci oleh
  // regression test (changesPanelFlow.test.mjs).
  const { upsertChange, isAetherMetadata, parseChangeEvent } = changes;
  let changeHelpersWarned = false;
  function markChangeHelpersBroken() {
    if (changeHelpersWarned) return;
    changeHelpersWarned = true;
    // Best-effort, tanpa data sensitif (hanya nama modul) & tanpa menghentikan
    // handler: event lain tetap diproses.
    // eslint-disable-next-line no-console
    console.error("[aether] useChanges() tidak menyediakan helper perubahan lengkap.");
  }
  const {
    runningTaskId,
    releaseRunningTask,
    adoptRunningTask,
    isViewingRunningTask,
    deferredTaskIds,
    terminalTaskId,
  } = queue;
  const { handleApprovalEvent, dropApprovalsForTask } = approvals;

  const events = ref([]);
  // Activity dari persistent log (Activity API). null = pakai live events (SSE).
  const historyEvents = ref(null);

  // State task/workspace (diisi dari #50 + #51).
  const runtime = reactive({ phase: "", activity: "", provider: "", model: "", tool: "" });
  const validation = reactive({ state: "pending" });

  // Activity Phase (UI) — aktivitas NYATA Agent dari event `phase_changed` (Task 1).
  // Hanya nilai planning/inspecting/editing/running/validating yang masuk ke sini;
  // nilai internal runtime (replan/provider_fallback) TIDAK dipakai untuk lifecycle.
  const activityPhase = ref("");
  // Milestone lifecycle yang SUDAH pernah dicapai (index step 0..5). Tetap `done`
  // walau current phase kembali ke step sebelumnya (Agent boleh mundur aktivitas).
  const lifecycleMilestones = ref([]);

  // Live "Agent reasoning." indicator: true HANYA selama AETHER menunggu respons
  // LLM. Dikendalikan event SSE EXISTING: provider_request (mulai) /
  // provider_response (selesai/error), dengan terminal event sebagai pengaman.
  const isReasoning = ref(false);

  // Penanda refresh File Explorer (dinaikkan setelah agent selesai membuat file).
  const explorerRefresh = ref(0);
  // Live filesystem change terakhir (dari event change_detected) untuk update
  // INCREMENTAL File Explorer tanpa full reload. `seq` memastikan setiap event
  // tetap memicu walau isinya sama.
  const liveFsChange = ref(null);
  let liveFsChangeSeq = 0;

  // Rolling window frontend untuk feed SSE live. Bukan pagination/history tanpa
  // batas: window dibatasi, sedangkan history lengkap dibaca dari .aether/log/
  // via Activity API saat membuka task lama.
  const MAX_ACTIVITY = 500;

  // The queue stream and task stream intentionally overlap for the viewed task.
  // SSE carries a globally stable event_id (and a store sequence fallback), so
  // use that identity rather than event shape/content: identical tool calls are
  // legitimate and must not be collapsed merely because their payload matches.
  // Dedup key includes task_id to avoid cross-task collisions (defensive).
  const handledEventIds = new Set();
  const MAX_HANDLED_EVENT_IDS = 2000;
  function isDuplicateEvent(evt) {
    const id = evt && (evt.event_id || evt.id);
    const sequence = evt && evt.sequence;
    const taskId = evt && evt.task_id;
    if (id == null && sequence == null) return false;
    const key = id != null
      ? `id:${taskId || "global"}:${id}`
      : `seq:${taskId || "global"}:${sequence}`;
    if (handledEventIds.has(key)) return true;
    handledEventIds.add(key);
    if (handledEventIds.size > MAX_HANDLED_EVENT_IDS) {
      handledEventIds.delete(handledEventIds.values().next().value);
    }
    return false;
  }

  function pushRolling(list, item, max) {
    list.push(item);
    if (list.length > max) list.splice(0, list.length - max);
  }

  // Event yang ditampilkan Agent Activity: live SSE atau history dari API.
  const activityEvents = computed(() => historyEvents.value || events.value);

  // Reasoning status hanya relevan untuk alur LIVE (bukan saat menampilkan
  // activity task lama dari persistent log). Sumber tetap satu: isReasoning.
  const showReasoning = computed(() => isReasoning.value && !historyEvents.value);

  // Event hanya boleh mengubah timing task yang SEDANG ditampilkan (stream bisa
  // saja membawa event task lain).
  function isCurrentTaskEvent(evt) {
    return Boolean(evt && evt.task_id && task.id && evt.task_id === task.id);
  }

  // --- Copy Agent Activity (button di header card) ----------------------------
  // Snapshot teks dibangun frontend-only dari data activity yang SUDAH ditampilkan
  // (activityEvents) + metadata Agent Card yang sudah dihitung. Bukan sumber data
  // baru, bukan panggilan backend.
  const activityCopied = ref(false);
  let activityCopyTimer = null;

  async function copyAgentActivity() {
    const text = buildAgentActivityCopy({
      events: activityEvents.value || [],
      meta: getCopyMeta(),
    });
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      return; // clipboard tidak tersedia (http non-secure) -> abaikan diam-diam
    }
    activityCopied.value = true;
    if (activityCopyTimer) clearTimeout(activityCopyTimer);
    activityCopyTimer = setTimeout(() => {
      activityCopied.value = false;
      activityCopyTimer = null;
    }, 1400);
  }

  function clearActivityCopyTimer() {
    if (activityCopyTimer) clearTimeout(activityCopyTimer);
    activityCopyTimer = null;
  }

  function handleEvent(evt) {
    if (!evt || !evt.event_type) return;

    // Approval (ASK) bersifat GLOBAL (satu queue/task lintas project): action
    // ditahan pada task mana pun harus tetap bisa di-Allow/Deny. Ditangani
    // SEBELUM filter task di bawah, dan selalu tertaut ke task_id payload-nya
    // sehingga approval TIDAK tertukar antar task. Approval juga tidak
    // didedup (bisa berulang untuk task yang sama).
    if (evt.event_type === "approval_requested" || evt.event_type === "approval_resolved") {
      handleApprovalEvent(evt);
      return;
    }

    // SSE dapat dikirim dua kali untuk event yang SAMA: stream global queue
    // (tanpa task_id filter) dan stream task-scoped (task_id filter). Keduanya
    // dapat event yang sama -> deduplikasi berdasarkan event_id/sequence
    // (stabil & global) agar tiap event diproses tepat satu kali. Hanya dilakukan
    // SETELAH approval (approval tidak butuh dedup).
    if (isDuplicateEvent(evt)) return;

    // Queue events (global, tidak task-scoped): task_queued, task_started.
    // Dipakai untuk refresh QueuePanel dan badge sidebar secara reactive.
    if (evt.event_type === "task_queued" || evt.event_type === "task_started") {
      bumpQueueRefresh();
    }

    // Stream SSE bersifat GLOBAL (satu queue global AETHER): event untuk task
    // LAIN tidak boleh mengubah Task Card/Agent Activity/runtime task yang sedang
    // dipantau — inilah mekanisme bug "UI ikut pindah ke Task B yang masih
    // pending lalu Agent seolah berhenti". PENGECUALIAN: task yang KITA antrikan
    // BENAR-BENAR mulai running setelah task sebelumnya selesai -> UI mengikuti.
    const viewedId = task.id || "";
    const evtTaskId = evt.task_id || "";
    if (evtTaskId && viewedId && evtTaskId !== viewedId) {
      if (
        evt.event_type === "task_started" &&
        shouldFollowStartedTask({
          startedTaskId: evtTaskId,
          viewedTaskId: viewedId,
          isViewingRunning: isViewingRunningTask(),
          viewingHistory: Boolean(historyEvents.value),
          deferredTaskIds,
        })
      ) {
        { // teks + executionMode dibaca SEBELUM adoptRunningTask menghapus entri deferred.
          const deferred = deferredTaskIds.get(evtTaskId);
          const dText = typeof deferred === "string" ? deferred : (deferred && deferred.task) || "";
          const dMode = typeof deferred === "object" && deferred ? deferred.executionMode : null;
          adoptRunningTask(evtTaskId, dText, dMode);
        }
        // lanjut: proses event task_started untuk task yang baru diadopsi.
      } else if (evt.event_type !== "change_detected") {
        // Change events are task-scoped data even when their task is not viewed;
        // let them reach the handler below so useChanges can update that task's
        // bucket and the filesystem explorer can react incrementally.
        return;
      }
    }

    // Event live untuk task aktif -> tampilkan alur SSE (bukan history lama).
    if (evt.task_id && task.id && evt.task_id === task.id) {
      historyEvents.value = null;
    }
    pushRolling(events.value, evt, MAX_ACTIVITY);
    const p = evt.payload || {};

    switch (evt.event_type) {
      case "task_started":
        task.status = "running";
        // Task baru mulai: pastikan reasoning status task sebelumnya sudah bersih.
        isReasoning.value = false;
        // Task ini BENAR-BENAR mulai running -> jadikan target tombol Stop.
        runningTaskId.value = evt.task_id || runningTaskId.value || "";
        runtime.activity = "Starting task";
        // Timer Task Card mulai dari timestamp START eksekusi (bukan saat card
        // dibuat). Diset SEKALI: reactive update/SSE berikutnya tidak me-reset.
        if (isCurrentTaskEvent(evt) && taskStartedAt.value == null) {
          taskStartedAt.value = eventTimeMs(evt);
        }
        // task_started -> lifecycle step pertama aktif (Planning). Activity phase
        // dari `phase_changed` akan menggantikannya begitu aktivitas nyata terjadi.
        activityPhase.value = "planning";
        lifecycleMilestones.value = addMilestone(lifecycleMilestones.value, 0);
        // Audio feedback HANYA saat task BENAR-BENAR mulai berjalan (transisi
        // status nyata), bukan saat user klik Run Task/Send. Dedup di
        // audioRegistry mencegah dobel-putar bila event running diterima ulang.
        playStatusSound("running");
        break;
      case "phase_changed": {
        // Activity phase (Task 1) menggerakkan lifecycle. Nilai internal runtime
        // (replan/provider_fallback) DIABAIKAN di sini: hanya phase yang dikenal
        // yang mengubah current step + mencatat milestone.
        const phaseIdx = activityPhaseIndex(p.phase);
        if (phaseIdx >= 0) {
          activityPhase.value = String(p.phase).trim().toLowerCase();
          lifecycleMilestones.value = addMilestone(lifecycleMilestones.value, phaseIdx);
        }
        if (p.phase) {
          // Dipertahankan untuk fitur UI lain yang masih memakai runtime.phase.
          runtime.phase = p.phase;
          runtime.activity = p.phase;
        }
        break;
      }
      case "provider_request":
        if (p.provider) runtime.provider = p.provider;
        if (p.model) runtime.model = p.model;
        // AETHER mulai menunggu respons LLM -> tampilkan SATU reasoning status.
        // Dipanggil berulang kali pun tetap satu elemen (state boolean), bukan
        // entri activity/log baru.
        isReasoning.value = true;
        break;
      case "provider_response":
        if (p.provider) runtime.provider = p.provider;
        if (p.model) runtime.model = p.model;
        // Respons LLM diterima (atau error provider) -> hentikan reasoning status.
        isReasoning.value = false;
        break;
      case "tool_called":
        if (p.tool) {
          runtime.tool = p.tool;
          runtime.activity = `Running ${p.tool}`;
        }
        break;
      case "tool_completed":
        // Tool events tampil di Agent Activity (unified timeline).
        break;
      case "observation_received":
        // Observation mentah tidak ditampilkan (hindari dump isi file panjang).
        break;
      case "validation_started":
        validation.state = "running";
        task.status = "validating";
        // Mekanisme validation EXISTING (backend) -> Validating. Bukan tebakan
        // dari terminal command: event ini memang menandakan validasi berjalan.
        activityPhase.value = "validating";
        lifecycleMilestones.value = addMilestone(lifecycleMilestones.value, VALIDATING_STEP);
        break;
      case "validation_completed":
        validation.state = p.success === false ? "err" : "ok";
        break;
      case "recovery_started":
        runtime.activity = "Recovery started";
        break;
      case "recovery_completed":
        runtime.activity = "Recovery completed";
        break;
      case "change_detected": {
        // Normalisasi bentuk event lewat SATU parser canonical (SSE live
        // memakai `payload`, Task Log/Activity API memakai `data`). Path yang
        // kosong/tidak valid -> parseChangeEvent mengembalikan null sehingga
        // TIDAK pernah ada baris "hantu" di Changes.
        if (
          typeof parseChangeEvent !== "function" ||
          typeof upsertChange !== "function" ||
          typeof isAetherMetadata !== "function"
        ) {
          // Kontrak DI rusak: jangan diam-diam membuang perubahan, tapi juga
          // jangan crash-loop. Diberi sinyal sekali (tanpa data sensitif).
          markChangeHelpersBroken();
          break;
        }
        const parsed = parseChangeEvent(evt);
        // Sembunyikan `.aether/**` di daftar Changes (metadata internal AETHER).
        if (parsed && !isAetherMetadata(parsed.change.path)) {
          // Bucket = task_id pada EVENT (isolasi antar-task). Fallback ke task
          // yang sedang dilihat HANYA bila event tidak membawa task_id.
          const bucketId = parsed.task_id || task.id || "";
          if (bucketId) {
            // Upsert (bukan push buta): satu file = satu baris, file yang diedit
            // berkali-kali memperbarui barisnya. Changes panel ikut update live.
            upsertChange(parsed.change, bucketId);
          }
          // Update File Explorer secara INCREMENTAL (refresh direktori terdampak
          // saja; expanded/selected dipertahankan), TANPA menunggu task selesai.
          liveFsChange.value = {
            seq: ++liveFsChangeSeq,
            path: parsed.change.path,
            kind: parsed.change.kind,
            old_path: parsed.change.old_path || "",
          };
        }
        break;
      }
      case "task_completed":
        task.status = "completed";
        // Task selesai -> reasoning status harus benar-benar berhenti.
        isReasoning.value = false;
        runtime.activity = "";
        // Timer Task Card berhenti pada status final (durasi terkunci).
        if (isCurrentTaskEvent(evt) && taskEndedAt.value == null) {
          taskEndedAt.value = eventTimeMs(evt);
        }
        // Refresh File Explorer setelah agent selesai (file baru terlihat).
        explorerRefresh.value += 1;
        // Task terminal: lepas target tombol Stop SECARA SINKRON (tombol langsung
        // hilang), baru refresh antrian untuk memilih task running berikutnya.
        terminalTaskId.value = evt.task_id || task.id || "";
        releaseRunningTask(terminalTaskId.value);
        bumpQueueRefresh();
        playStatusSound("completed");
        refreshTaskHistory();
        dropApprovalsForTask(evt.task_id || task.id || "");
        break;
      case "task_failed":
        task.status = "failed";
        // Task gagal -> hentikan reasoning status.
        isReasoning.value = false;
        // Timer Task Card berhenti pada status final (durasi terkunci).
        if (isCurrentTaskEvent(evt) && taskEndedAt.value == null) {
          taskEndedAt.value = eventTimeMs(evt);
        }
        // Task terminal: tombol Stop langsung hilang (sinkron, tanpa race).
        terminalTaskId.value = evt.task_id || task.id || "";
        releaseRunningTask(terminalTaskId.value);
        bumpQueueRefresh();
        playStatusSound("failed");
        refreshTaskHistory();
        dropApprovalsForTask(evt.task_id || task.id || "");
        break;
      case "task_cancelled":
        task.status = "cancelled";
        // Task dibatalkan -> hentikan reasoning status (tidak ada animasi nyangkut).
        isReasoning.value = false;
        // Execution benar-benar berhenti -> indikator Agent kembali idle.
        runtime.activity = "";
        runtime.tool = "";
        // Timer Task Card berhenti pada status final (durasi terkunci).
        if (isCurrentTaskEvent(evt) && taskEndedAt.value == null) {
          taskEndedAt.value = eventTimeMs(evt);
        }
        // Task terminal: tombol Stop langsung hilang (sinkron, tanpa race).
        terminalTaskId.value = evt.task_id || task.id || "";
        releaseRunningTask(terminalTaskId.value);
        bumpQueueRefresh();
        playStatusSound("cancelled");
        refreshTaskHistory();
        dropApprovalsForTask(evt.task_id || task.id || "");
        break;
      default:
        break;
    }
  }

  function resetWorkspace() {
    events.value = [];
    // changesByTask TIDAK dihapus di sini — data per-task dipertahankan.
    // Hanya historyEvents/currentReport/runtime/lifecycle yang direset.
    historyEvents.value = null;
    clearReport();
    validation.state = "pending";
    runtime.phase = "";
    runtime.activity = "";
    runtime.provider = "";
    runtime.model = "";
    runtime.tool = "";
    // Task baru/workspace kosong -> lifecycle kembali ke awal (belum ada aktivitas).
    activityPhase.value = "";
    lifecycleMilestones.value = [];
    // Workspace direset untuk task baru -> tidak ada reasoning status tersisa.
    isReasoning.value = false;
    liveFsChange.value = null;
    // Task baru/workspace kosong -> reset timing Task Card + hentikan timer.
    timing.resetTiming();
  }

  return {
    events,
    historyEvents,
    runtime,
    validation,
    activityPhase,
    lifecycleMilestones,
    isReasoning,
    explorerRefresh,
    liveFsChange,
    activityEvents,
    showReasoning,
    activityCopied,
    copyAgentActivity,
    clearActivityCopyTimer,
    isDuplicateEvent,
    handleEvent,
    resetWorkspace,
  };
}
