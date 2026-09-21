// Time utilities untuk Task Card Workbench (frontend-only).
//
// Sumber waktu = timestamp lifecycle AETHER yang SUDAH ADA:
//   - SSE live (#51): `timestamp` epoch detik
//   - history log (.aether/log via Activity API): `timestamp` ISO string
// TIDAK ada polling/timer backend baru. Ticker di sini HANYA me-refresh
// TAMPILAN durasi live; durasi sebenarnya selalu dihitung dari timestamp.

// Event timestamp -> ms epoch (mendukung epoch detik/ms dan ISO string).
// Fallback ke waktu sekarang bila timestamp tidak tersedia / tidak valid.
export function eventTimeMs(raw) {
  const t = raw == null ? null : raw.timestamp;
  if (t == null) return Date.now();
  if (typeof t === "number") return t < 1e12 ? t * 1000 : t;
  const d = new Date(t);
  return Number.isNaN(d.getTime()) ? Date.now() : d.getTime();
}

// Format durasi (ms) -> "MM:SS", atau "HH:MM:SS" bila >= 1 jam.
// Mengembalikan "" bila tidak ada durasi (mis. task lama tanpa timestamp) agar
// bagian durasi cukup tidak ditampilkan.
export function formatDuration(ms) {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "";
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

// Ticker tampilan durasi live (berbasis setInterval).
//
// `start()` idempoten (tidak menumpuk interval) dan `stop()` membersihkan
// interval sepenuhnya -> tidak ada timer nyangkut setelah task terminal/unmount.
// Handler hanya me-refresh nilai tampilan; TIDAK memanggil backend.
export function createDurationTicker(onTick, intervalMs = 1000) {
  let handle = null;
  return {
    start() {
      if (handle != null) return;
      onTick();
      handle = setInterval(onTick, intervalMs);
    },
    stop() {
      if (handle != null) {
        clearInterval(handle);
        handle = null;
      }
    },
  };
}
