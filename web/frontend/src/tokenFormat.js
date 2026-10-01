// Token usage formatting (frontend-only, pure).
//
// Sumber angka = token usage AKTUAL yang dilaporkan provider/API (payload
// `usage` pada event lifecycle `provider_response`). Modul ini TIDAK menghitung
// / mengestimasi token: ia hanya membaca angka yang SUDAH ada dan
// memformatnya untuk tampilan.
//
// Dipakai Task Card (App.vue). Dipisah sebagai helper murni agar dapat diuji
// tanpa framework (lihat tokenFormat.test.mjs) — pola yang sama dengan
// timeUtils.js / lifecycle.js / taskView.js.

// Token usage dari payload event -> angka total, atau null bila provider TIDAK
// melaporkan usage (UI menampilkan "—"). Mendukung bentuk yang lazim:
//   - OpenAI-compatible : usage.prompt_tokens / completion_tokens / total_tokens
//   - kanonik AETHER    : usage.prompt / completion / total
//   - Ollama native     : prompt_eval_count / eval_count (nested usage atau top-level)
export function usageTokens(payload) {
  const d = payload || {};
  const u = d.usage && typeof d.usage === "object" ? d.usage : {};
  const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  const pick = (...keys) => {
    for (const k of keys) {
      const v = num(u[k]);
      if (v != null) return v;
    }
    return null;
  };
  // Total dilaporkan provider apa adanya.
  const total = pick("total_tokens", "total");
  if (total != null) return total;
  // prompt + completion.
  const prompt = pick("prompt_tokens", "prompt");
  const completion = pick("completion_tokens", "completion");
  if (prompt != null || completion != null) return (prompt || 0) + (completion || 0);
  // Ollama native (nested usage atau top-level).
  const pe = num(u.prompt_eval_count != null ? u.prompt_eval_count : d.prompt_eval_count);
  const ec = num(u.eval_count != null ? u.eval_count : d.eval_count);
  if (pe != null || ec != null) return (pe || 0) + (ec || 0);
  return null;
}

// Format ringkas angka token (compact untuk baris meta Task Card):
//   825 -> 825 | 1,250 -> 1.25K | 12,500 -> 12.5K | 140,500 -> 140.5K
//   1,250,000 -> 1.25M | 140,500,000 -> 140.5M | 1,250,000,000 -> 1.25B
// Bila provider tidak melaporkan usage (null) -> "—" (bukan nilai dummy).
export function formatTokens(n) {
  if (n == null || !Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs < 1000) return String(n);
  const units = [
    { limit: 1e9, suffix: "B" },
    { limit: 1e6, suffix: "M" },
    { limit: 1e3, suffix: "K" },
  ];
  const unit = units.find((u) => abs >= u.limit) || units[units.length - 1];
  const value = (n / unit.limit).toFixed(2).replace(/\.?0+$/, "");
  return `${value}${unit.suffix}`;
}

// Angka PENUH token (dengan pemisah ribuan) untuk tooltip Task Card.
export function formatTokensFull(n) {
  if (n == null || !Number.isFinite(n)) return "";
  return Math.round(n).toLocaleString("en-US");
}
