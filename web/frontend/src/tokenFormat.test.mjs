// Regression test (Node built-in `assert`, TANPA framework/dependency baru)
// untuk formatter token usage Task Card (web/frontend/src/tokenFormat.js).
//
// Mengunci perilaku:
//   - Format compact sesuai spesifikasi revisi (825, 1.25K, 12.5K, 140.5K,
//     1.25M, 140.5M, 1.25B).
//   - Provider tanpa usage -> "—" (bukan angka dummy).
//   - Angka penuh untuk tooltip.
//   - Sumber angka = usage provider (OpenAI / kanonik / Ollama), tanpa estimasi.
//
// Jalankan: node web/frontend/src/tokenFormat.test.mjs

import assert from "node:assert/strict";
import { formatTokens, formatTokensFull, usageTokens } from "./tokenFormat.js";

// --- Format compact (contoh dari spesifikasi) -------------------------------
const cases = [
  [825, "825"],
  [1250, "1.25K"],
  [12500, "12.5K"],
  [140500, "140.5K"],
  [1250000, "1.25M"],
  [140500000, "140.5M"],
  [1250000000, "1.25B"],
];
for (const [value, expected] of cases) {
  assert.equal(formatTokens(value), expected, `formatTokens(${value})`);
}

// Batas unit.
assert.equal(formatTokens(999), "999");
assert.equal(formatTokens(1000), "1K");
assert.equal(formatTokens(0), "0");

// Tanpa usage -> "—".
assert.equal(formatTokens(null), "—");
assert.equal(formatTokens(undefined), "—");
assert.equal(formatTokens(NaN), "—");

// --- Angka penuh (tooltip) ---------------------------------------------------
assert.equal(formatTokensFull(140500000), "140,500,000");
assert.equal(formatTokensFull(1250), "1,250");
assert.equal(formatTokensFull(null), "");

// --- usageTokens: bentuk provider -------------------------------------------
// OpenAI-compatible.
assert.equal(usageTokens({ usage: { prompt_tokens: 100, completion_tokens: 50 } }), 150);
assert.equal(usageTokens({ usage: { total_tokens: 999 } }), 999);
// Kanonik AETHER.
assert.equal(usageTokens({ usage: { prompt: 10, completion: 5, total: 15 } }), 15);
// Ollama native (nested + top-level).
assert.equal(usageTokens({ usage: { prompt_eval_count: 20, eval_count: 12 } }), 32);
assert.equal(usageTokens({ prompt_eval_count: 20, eval_count: 12 }), 32);
// Tanpa usage -> null (UI menampilkan "—").
assert.equal(usageTokens({}), null);
assert.equal(usageTokens({ usage: {} }), null);
assert.equal(usageTokens(null), null);

console.log("[OK] tokenFormat: format compact + angka penuh + parsing usage provider benar.");
