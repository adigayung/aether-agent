import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// AETHER Workbench (#52).
// Dev server mem-proxy /api ke Django Gateway (#50/#51) agar frontend tidak
// perlu tahu host backend dan tidak ada logic agent di frontend.
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
