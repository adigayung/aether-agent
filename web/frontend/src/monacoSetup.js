// AETHER Code Editor: setup Monaco Editor (sekali saja, lazy).
//
// Module ini HANYA di-import secara dinamis saat editor pertama kali dibuka
// (lihat components/CodeEditor.vue). Alasannya:
//   - bundle utama Workbench tidak ikut membawa Monaco (~MB),
//   - render tanpa browser (SSR/verifier) tidak menyentuh Worker/DOM.
//
// Yang diurus di sini:
//   1. Worker Monaco (bundler-aware via `?worker` dari Vite).
//   2. Theme "aether-dark" agar warna editor konsisten dengan visual AETHER.
//
// Tidak ada LSP / language server / subsystem kedua: hanya konfigurasi Monaco.

import * as monaco from "monaco-editor";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import JsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";
import CssWorker from "monaco-editor/esm/vs/language/css/css.worker?worker";
import HtmlWorker from "monaco-editor/esm/vs/language/html/html.worker?worker";
import TsWorker from "monaco-editor/esm/vs/language/typescript/ts.worker?worker";

/** Nama theme Monaco AETHER (dark, konsisten dengan --bg-panel/--accent). */
export const AETHER_THEME = "aether-dark";

/** Opsi editor default. Fitur bawaan Monaco (folding, find/replace, multi
 *  cursor, minimap, shortcut standar, autocomplete language service) aktif
 *  secara default; di sini hanya dipastikan eksplisit. */
export const EDITOR_OPTIONS = {
  theme: AETHER_THEME,
  automaticLayout: false, // layout diurus resize handler sendiri (anti leak)
  minimap: { enabled: true },
  folding: true,
  lineNumbers: "on",
  renderWhitespace: "selection",
  fontSize: 13,
  fontFamily:
    "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace",
  tabSize: 2,
  scrollBeyondLastLine: false,
  smoothScrolling: true,
  cursorBlinking: "smooth",
  multiCursorModifier: "alt",
  find: { addExtraSpaceOnTop: false },
  fixedOverflowWidgets: true,
};

let configured = false;

/**
 * Konfigurasi worker + theme (idempotent) lalu kembalikan namespace Monaco.
 *
 * @returns {typeof import("monaco-editor")} namespace Monaco siap dipakai.
 */
export function getMonaco() {
  if (!configured) {
    // Worker: Monaco memilih worker berdasarkan label bahasa. Label yang tidak
    // punya worker khusus memakai editor.worker (syntax highlighting dsb).
    self.MonacoEnvironment = {
      getWorker(_moduleId, label) {
        switch (label) {
          case "json":
            return new JsonWorker();
          case "css":
          case "scss":
          case "less":
            return new CssWorker();
          case "html":
          case "handlebars":
          case "razor":
            return new HtmlWorker();
          case "typescript":
          case "javascript":
            return new TsWorker();
          default:
            return new EditorWorker();
        }
      },
    };

    monaco.editor.defineTheme(AETHER_THEME, {
      base: "vs-dark",
      inherit: true,
      rules: [],
      colors: {
        "editor.background": "#14121f",
        "editorGutter.background": "#14121f",
        "editorLineNumber.foreground": "#5b5474",
        "editorLineNumber.activeForeground": "#a99fc4",
        "editor.selectionBackground": "#3a2f5c",
        "editorCursor.foreground": "#22d3ee",
        "editorIndentGuide.background1": "#221d36",
        "editorIndentGuide.activeBackground1": "#3a2f5c",
        "minimap.background": "#100e1a",
        "editorWidget.background": "#1a1728",
        "editorWidget.border": "#2a2540",
        "scrollbarSlider.background": "#2a254080",
        "scrollbarSlider.hoverBackground": "#3a2f5c",
      },
    });

    configured = true;
  }
  return monaco;
}
