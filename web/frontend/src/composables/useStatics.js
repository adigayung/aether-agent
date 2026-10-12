import { computed, ref } from "vue";
import { getStatics, downloadStaticsExport } from "../api.js";

// Statics dashboard data (TASK 4) — SATU project.
//
// Tanggung jawab module ini HANYA pengambilan data statistik + filter +
// loading/error/refresh dari Statistics API existing (`GET /api/statics`,
// TASK 3). TIDAK ada agregasi lintas-project dan TIDAK ada scan filesystem:
// semua angka berasal dari backend untuk SATU project aktif.
//
// Isolasi project ditegakkan dengan GUARD GENERASI:
//   - `selectedProjectId` = project yang sedang dipilih (reactive).
//   - Setiap request menandai `requestSeq`; hanya respons terbaru yang boleh
//     menulis `data`/`error`. Respons lambat milik project/permintaan lama
//     DIBUANG, sehingga tidak pernah menampilkan sisa data project sebelumnya.

export const STATIC_ACTIVITY_VALUES = ["all", "agent", "consultant"];
export const STATIC_GROUP_BY_VALUES = ["day", "provider", "model"];
export const STATIC_EXPORT_FORMATS = ["csv", "json"];

export function useStatics({ selectedProjectId }) {
  // Data kontrak dari backend (null = belum ada / belum dimuat).
  const data = ref(null);
  const loading = ref(false);
  const error = ref("");
  // Status unduhan ekspor (feedback UI; tidak memblokir tampilan data).
  const exporting = ref(false);
  const exportError = ref("");

  // Filter tampilan (default mengikuti kontrak backend).
  const range = ref({ from: "", to: "" });
  const activity = ref("all");
  const groupBy = ref("day");
  const status = ref("");
  const sort = ref("total_tokens");
  const order = ref("desc");
  const limit = ref(50);
  const offset = ref(0);

  // Penanda respons terakhir yang sah (anti race / anti data project lama).
  let requestSeq = 0;
  // Project yang datanya sedang / terakhir dimuat.
  const loadedProjectId = ref("");

  // True bila data yang dimuat saat ini benar-benar milik project terpilih.
  const isCurrent = computed(
    () => Boolean(selectedProjectId.value) && loadedProjectId.value === selectedProjectId.value
  );

  async function refresh() {
    const projectId = selectedProjectId.value || "";
    const seq = ++requestSeq;

    // Project kosong -> tidak ada yang dimuat; bersihkan agar tidak ada sisa
    // data project sebelumnya yang tertinggal di layar.
    if (!projectId) {
      data.value = null;
      loadedProjectId.value = "";
      error.value = "";
      loading.value = false;
      return;
    }

    loading.value = true;
    error.value = "";
    try {
      const result = await getStatics({
        projectId,
        from: range.value.from || null,
        to: range.value.to || null,
        groupBy: groupBy.value || null,
        activity: activity.value || null,
        status: status.value || null,
        sort: sort.value || null,
        order: order.value || null,
        limit: limit.value,
        offset: offset.value,
      });
      // Buang respons basi (project/permintaan berubah saat request berjalan).
      if (seq !== requestSeq) return;
      if (projectId !== (selectedProjectId.value || "")) return;
      data.value = result;
      loadedProjectId.value = projectId;
    } catch (e) {
      if (seq !== requestSeq) return;
      if (projectId !== (selectedProjectId.value || "")) return;
      // Gagal memuat -> jangan tampilkan data project sebelumnya.
      data.value = null;
      loadedProjectId.value = projectId;
      error.value = e.message || String(e);
    } finally {
      if (seq === requestSeq) loading.value = false;
    }
  }

  function setRange({ from, to }) {
    range.value = { from: from || "", to: to || "" };
    return refresh();
  }

  function setActivity(value) {
    activity.value = STATIC_ACTIVITY_VALUES.includes(value) ? value : "all";
    return refresh();
  }

  function setGroupBy(value) {
    groupBy.value = STATIC_GROUP_BY_VALUES.includes(value) ? value : "day";
    return refresh();
  }

  function setStatus(value) {
    status.value = value || "";
    return refresh();
  }

  function setPage(nextOffset) {
    offset.value = Math.max(0, Number(nextOffset) || 0);
    return refresh();
  }

  // Unduh ekspor CSV/JSON mengikuti FILTER AKTIF (project, periode, activity,
  // group_by, status, provider/model, sort/order). Backend mengekspor seluruh
  // task yang cocok filter — bukan hanya halaman pagination yang tampil.
  async function exportData(format = "json") {
    const fmt = STATIC_EXPORT_FORMATS.includes(format) ? format : "json";
    const projectId = selectedProjectId.value || "";
    if (!projectId) return null;
    exporting.value = true;
    exportError.value = "";
    try {
      return await downloadStaticsExport({
        projectId,
        format: fmt,
        from: range.value.from || null,
        to: range.value.to || null,
        groupBy: groupBy.value || null,
        activity: activity.value || null,
        status: status.value || null,
        sort: sort.value || null,
        order: order.value || null,
      });
    } catch (e) {
      exportError.value = e.message || String(e);
      return null;
    } finally {
      exporting.value = false;
    }
  }

  return {
    data,
    loading,
    error,
    exporting,
    exportError,
    range,
    activity,
    groupBy,
    status,
    sort,
    order,
    limit,
    offset,
    isCurrent,
    refresh,
    setRange,
    setActivity,
    setGroupBy,
    setStatus,
    setPage,
    exportData,
  };
}
