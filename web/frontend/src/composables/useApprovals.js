import { ref } from "vue";
import { resolveApproval } from "../api.js";

// Approval (ASK): action Agent ditahan policy -> user Allow/Deny.
//
// Approval terikat ke task/session (dari payload event) agar tidak tertukar
// antar task. List bisa >1 (beberapa task), jadi disimpan sebagai antrian dan
// modal menampilkan SATU approval (paling awal) pada satu waktu agar keputusan
// tidak ambigu. Allow/Deny dikirim ke endpoint resolve existing -> backend
// meneruskan ke gate yang menahan action (terikat task/session yang benar).
export function useApprovals() {
  const approvals = ref([]);
  const approvalBusy = ref(false);
  const approvalError = ref("");

  function upsertApproval(entry) {
    if (!entry || !entry.request_id) return;
    const list = approvals.value;
    const idx = list.findIndex((a) => a.request_id === entry.request_id);
    if (idx >= 0) {
      list[idx] = { ...list[idx], ...entry };
      return;
    }
    list.push(entry);
  }

  function removeApproval(requestId, status = "") {
    if (!requestId) return;
    approvals.value = approvals.value.filter((a) => a.request_id !== requestId);
  }

  function handleApprovalEvent(evt) {
    const p = evt.payload || {};
    if (evt.event_type === "approval_requested") {
      upsertApproval({
        request_id: p.request_id,
        tool: p.tool,
        target: p.target,
        action_class: p.action_class,
        matrix_action: p.matrix_action,
        scope: p.scope,
        reason: p.reason,
        task_id: p.task_id || evt.task_id || "",
        session_id: p.session_id || evt.session_id || "",
        status: "pending",
      });
    } else if (evt.event_type === "approval_resolved") {
      removeApproval(p.request_id, p.status);
    }
  }

  async function decideApproval(allow) {
    const current = approvals.value[0];
    if (!current || approvalBusy.value) return;
    approvalBusy.value = true;
    approvalError.value = "";
    try {
      await resolveApproval(current.request_id, allow);
      removeApproval(current.request_id, allow ? "allowed" : "denied");
    } catch (e) {
      approvalError.value = e.message || "Failed to submit decision.";
    } finally {
      approvalBusy.value = false;
    }
  }

  // Cleanup approval yang menggantung saat task yang memilikinya sudah terminal
  // (task_completed/failed/cancelled) — gate backend akan timeout/DENY sendiri,
  // jadi UI tidak perlu menampilkan modal basi.
  function dropApprovalsForTask(taskId) {
    if (!taskId) return;
    approvals.value = approvals.value.filter((a) => a.task_id !== taskId);
  }

  return {
    approvals,
    approvalBusy,
    approvalError,
    upsertApproval,
    removeApproval,
    handleApprovalEvent,
    decideApproval,
    dropApprovalsForTask,
  };
}
