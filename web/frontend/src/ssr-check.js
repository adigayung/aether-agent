// SSR entry untuk verifier #52 (bukan bagian runtime UI).
// Merender App.vue ke string agar verifier dapat memastikan App render tanpa
// browser. Tidak ada logic agent di sini.
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import App from "./App.vue";

export async function renderApp() {
  const app = createSSRApp(App);
  return renderToString(app);
}
