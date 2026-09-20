// AETHER Code Editor: pemetaan extension file -> language id Monaco.
//
// Mapping statis sederhana (bukan parser baru). Untuk menambah bahasa baru,
// cukup tambahkan satu baris di EXT_LANGUAGE. Extension yang tidak dikenal
// jatuh ke "plaintext".
//
// Catatan: Monaco basic-languages tidak punya grammar khusus untuk `vue` dan
// `toml`; keduanya dipetakan ke grammar terdekat yang tersedia
// (`html` untuk SFC Vue, `ini` untuk TOML) agar tetap ada syntax highlighting.

export const DEFAULT_LANGUAGE = "plaintext";

const EXT_LANGUAGE = {
  // JavaScript / TypeScript
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  jsx: "javascript",
  ts: "typescript",
  tsx: "typescript",
  // Markup
  vue: "html",
  html: "html",
  htm: "html",
  xml: "xml",
  svg: "xml",
  // Style
  css: "css",
  scss: "scss",
  sass: "scss",
  less: "less",
  // Data / config
  json: "json",
  yaml: "yaml",
  yml: "yaml",
  toml: "ini",
  ini: "ini",
  conf: "ini",
  env: "ini",
  // Docs
  md: "markdown",
  markdown: "markdown",
  txt: "plaintext",
  // Bahasa program
  py: "python",
  php: "php",
  java: "java",
  c: "c",
  h: "c",
  cc: "cpp",
  cpp: "cpp",
  hpp: "cpp",
  cs: "csharp",
  go: "go",
  rs: "rust",
  rb: "ruby",
  // Shell / script
  sh: "shell",
  bash: "shell",
  bat: "bat",
  cmd: "bat",
  ps1: "powershell",
  // Database
  sql: "sql",
};

/**
 * Tentukan language id Monaco dari nama file.
 *
 * @param {string} name nama file (mis. "src/main.js" atau "main.js")
 * @returns {string} language id Monaco ("plaintext" bila tidak dikenal)
 */
export function languageForFile(name) {
  const raw = String(name || "");
  const base = raw.split(/[\\/]/).pop() || "";
  const dot = base.lastIndexOf(".");
  if (dot <= 0) return DEFAULT_LANGUAGE;
  const ext = base.slice(dot + 1).toLowerCase();
  return EXT_LANGUAGE[ext] || DEFAULT_LANGUAGE;
}

/**
 * Label singkat language untuk status bar (mis. "javascript" -> "JavaScript").
 */
export function languageLabel(languageId) {
  const id = String(languageId || DEFAULT_LANGUAGE);
  if (id === "plaintext") return "Plain Text";
  return id.charAt(0).toUpperCase() + id.slice(1);
}
