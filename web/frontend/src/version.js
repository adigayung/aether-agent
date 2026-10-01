// Versi AETHER untuk tampilan UI — SINGLE SOURCE OF TRUTH `data/version.json`
// (sumber yang sama dengan footer Workbench di App.vue).
//
// Dibaca lewat `import.meta.glob` (Vite), bukan static import, supaya bersifat
// OPSIONAL: bila `data/version.json` hilang / gagal di-resolve, modul ini
// mengembalikan fallback aman sehingga UI tidak error.
//
// TIDAK ada sistem version baru di sini: hanya MEMBACA file version yang sudah
// ada (di-bump oleh scripts/bump_version.py).

export const VERSION_FALLBACK = "0.0.0";

const VERSION_PATH = "../../../data/version.json";

// Ambil string version dari isi data/version.json secara defensif.
// Nilai "tidak valid" (bukan objek / bukan string / kosong) -> fallback.
export function pickVersion(moduleValue) {
  const raw = moduleValue && (moduleValue.default ?? moduleValue);
  const value = raw && typeof raw.version === "string" ? raw.version.trim() : "";
  return value || VERSION_FALLBACK;
}

function resolveVersion() {
  let modules;
  try {
    // Path literal wajib: Vite menganalisis `import.meta.glob` secara statis.
    // eager: true -> nilainya langsung modul (sinkron), dapat dipakai sebagai
    // konstanta tanpa async/await di komponen.
    modules = import.meta.glob("../../../data/version.json", { eager: true });
  } catch {
    // Bukan lingkungan Vite (mis. dijalankan di Node murni) -> fallback aman.
    return VERSION_FALLBACK;
  }
  return pickVersion(modules && modules[VERSION_PATH]);
}

// Nilai version yang dipakai tampilan (mis. "0.1.82").
export const AETHER_VERSION = resolveVersion();
