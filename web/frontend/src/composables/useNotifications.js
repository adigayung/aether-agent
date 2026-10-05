import { ref } from "vue";

// Notifikasi UI global: banner error (persisten sampai diganti) + notice
// sementara (auto-clear). Satu sumber untuk seluruh modul sehingga pesan tidak
// terpecah menjadi state per-komponen.
export function useNotifications() {
  const error = ref("");
  const notice = ref("");
  let noticeTimer = null;

  function setError(message) {
    error.value = message || "";
  }

  function clearError() {
    error.value = "";
  }

  // Notice sementara (default 5 detik) — mis. hasil hapus project.
  function showNotice(message, duration = 5000) {
    notice.value = message || "";
    if (noticeTimer) clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => {
      notice.value = "";
      noticeTimer = null;
    }, duration);
  }

  function clearNotice() {
    notice.value = "";
    if (noticeTimer) {
      clearTimeout(noticeTimer);
      noticeTimer = null;
    }
  }

  return { error, notice, setError, clearError, showNotice, clearNotice };
}
