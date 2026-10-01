import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Port backend Django = `port` dari `data/settings.json` (satu sumber
// konfigurasi global). `8000` hanya fallback bila file tidak memuat `port`.
function backendPort() {
  const fallback = 8000;
  try {
    const settingsUrl = new URL("../../data/settings.json", import.meta.url);
    const parsed = JSON.parse(readFileSync(fileURLToPath(settingsUrl), "utf-8"));
    const value = Number(parsed?.port);
    return Number.isInteger(value) && value >= 1 && value <= 65535 ? value : fallback;
  } catch {
    return fallback;
  }
}

// AETHER Workbench (#52).
// Dev server mem-proxy /api ke Django Gateway (#50/#51) agar frontend tidak
// perlu tahu host backend dan tidak ada logic agent di frontend.
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    // Izinkan dev server menyajikan asset di luar root frontend (yaitu
    // <repo-root>/assets/**) yang di-import oleh audioRegistry.js, agar
    // sound notification juga berfungsi saat `npm run dev`. Production build
    // tidak butuh ini (Vite menyalin asset ke dist/assets/).
    fs: {
      allow: ["../.."],
    },
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${backendPort()}`,
        changeOrigin: true,
      },
    },
  },
});
