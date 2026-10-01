// Regression test (Node built-in `assert`, TANPA framework/dependency baru)
// untuk pembacaan versi AETHER dari SINGLE SOURCE OF TRUTH `data/version.json`.
//
// Mengunci perilaku: nilai version diambil dari isi file, dan bila file/isi
// tidak valid maka dipakai fallback aman (UI tidak error).
//
// Jalankan: node web/frontend/src/version.test.mjs

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { pickVersion, VERSION_FALLBACK } from "./version.js";

// Sumber nyata di disk (relatif terhadap src/ -> ../../../data/version.json).
const versionPath = fileURLToPath(new URL("../../../data/version.json", import.meta.url));
const onDisk = JSON.parse(readFileSync(versionPath, "utf-8"));

// --- nilai valid diambil apa adanya ------------------------------------------
assert.equal(pickVersion({ default: { version: onDisk.version } }), onDisk.version);
assert.equal(pickVersion({ version: "1.2.3" }), "1.2.3");
assert.equal(pickVersion({ default: { version: "  2.0.4  " } }), "2.0.4", "trim spasi");

// --- kondisi tidak valid -> fallback aman ------------------------------------
assert.equal(pickVersion(null), VERSION_FALLBACK);
assert.equal(pickVersion(undefined), VERSION_FALLBACK);
assert.equal(pickVersion({}), VERSION_FALLBACK, "tanpa field version");
assert.equal(pickVersion({ default: {} }), VERSION_FALLBACK);
assert.equal(pickVersion({ default: { version: 123 } }), VERSION_FALLBACK, "bukan string");
assert.equal(pickVersion({ default: { version: "   " } }), VERSION_FALLBACK, "string kosong");

// Fallback yang dipakai harus non-kosong agar UI tidak menampilkan "v".
assert.ok(VERSION_FALLBACK && typeof VERSION_FALLBACK === "string");

console.log("[OK] version.js membaca data/version.json + fallback aman.");
