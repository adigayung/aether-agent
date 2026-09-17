// AETHER Workbench entry point (#52).
import { createApp } from "vue";
// Bootstrap 5 (layout, spacing, form, button, dropdown, responsive).
// Bukan admin template: hanya utility + komponen dasar.
import "bootstrap/dist/css/bootstrap.min.css";
import "bootstrap/dist/js/bootstrap.bundle.min.js";
import App from "./App.vue";
import "./styles.css";

createApp(App).mount("#app");
