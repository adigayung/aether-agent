// Audio Registry — centralized sound configuration for AETHER.
//
// Single source of truth that maps terminal task outcomes to sound assets.
// Assets are imported (bundled by Vite) instead of referenced by a raw URL,
// so the SAME registry works in both modes AETHER actually uses:
//   - dev server  : `npm run dev` (Vite)
//   - production  : Django serves web/frontend/dist, and only static-serves
//                   `/assets/**` from that build. Vite emits imported assets
//                   under dist/assets/, so they become reachable at
//                   `/assets/<name>-<hash>.wav` with no extra server route.
//
// Paths are portable module specifiers relative to this file (no absolute
// machine path such as J:\...\assets\audio\succeed.wav).
//
// Extending: to add a new sound later (e.g. warning.wav):
//   1. drop the file under <repo-root>/assets/audio/
//   2. import it below
//   3. add an entry to SOUNDS (and, if needed, STATUS_TO_SOUND)
// No playback logic inside any component has to change.

import succeedSound from "../../../assets/audio/succeed.wav";
import failedSound from "../../../assets/audio/failed.wav";
import stopSound from "../../../assets/audio/stop.wav";

// Sound registry: logical sound name -> resolved asset URL.
export const SOUNDS = Object.freeze({
  succeed: succeedSound,
  failed: failedSound,
  stop: stopSound,
});

// Actual AETHER terminal task status -> logical sound name.
// Statuses verified against the running code (see new_analisa.txt):
//   task_completed  -> runtime._lifecycle_finalize -> task.status "completed"
//   task_failed     -> runtime._lifecycle_finalize -> task.status "failed"
//   task_cancelled  -> gateway cancel_task         -> task.status "cancelled"
const STATUS_TO_SOUND = Object.freeze({
  completed: "succeed",
  failed: "failed",
  cancelled: "stop",
});

// Deduplication — one playback per terminal transition.
// The last terminal status that produced a sound is remembered; repeating the
// same terminal status (e.g. duplicate SSE delivery of "completed") does NOT
// replay. `resetAudioTracker()` is called when a new task starts, so the next
// terminal transition is allowed to play again. This is intentionally NOT a
// state machine: it is a single "last transition" marker.
let lastPlayedStatus = null;

/**
 * Play the sound registered for a terminal AETHER task status.
 *
 * No-op for unknown / non-terminal statuses, and for a terminal status that
 * was already played (deduplication). Triggered only by real terminal task
 * transitions delivered through the SSE event handler — never by a render.
 */
export function playStatusSound(status) {
  const s = (status || "").toLowerCase();
  const soundName = STATUS_TO_SOUND[s];
  if (!soundName) return;
  if (s === lastPlayedStatus) return;

  const src = SOUNDS[soundName];
  if (!src) return;

  // Mark the transition as handled BEFORE playing so overlapping duplicate
  // events cannot slip through.
  lastPlayedStatus = s;

  try {
    const audio = new Audio(src);
    const played = audio.play();
    if (played && typeof played.catch === "function") {
      // Autoplay may be blocked before the first user gesture; ignore silently.
      played.catch(() => {});
    }
  } catch {
    // No DOM/Audio (e.g. SSR) or playback error — never break the event handler.
  }
}

/** Reset the deduplication marker (called when a new task starts). */
export function resetAudioTracker() {
  lastPlayedStatus = null;
}

// Backwards-compatible alias: the frozen registry is exposed for inspection.
export const audioRegistry = SOUNDS;
